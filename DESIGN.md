# Context Compactor Refined — 设计稿

> 状态：v3 定稿（2026-11-18 三路对抗复审 + D1-D7 拍板完结）｜宿主：QwenPaw 2.3.0（editable 安装）｜SDK：1.0.0
> 作者：onesixth

## 0. 一句话定位

把 QwenPaw scroll 压缩管线**第 5 步（continuation summary 生成）**从「证据 dump + 严格结构化解析 + 无状态重试」换成「**真实对话续写 + 轻量章节校验 + 对话内重试**」。其余 8 步（persist/trigger/pre-fold/split/index-evict/live-fold/think-fold/active-fold）与基础设施（history.db / recall 工具 / eviction index / 会话持久化）**全部继承，一行不动**。

**四条硬约束（作者拍板，不可变）**：
1. **prefix cache 全量命中至上**：摘要请求复用 `prepare_model_input(agent)` 输出（与主请求同源构造）+ `tool_choice=ToolChoice(mode="none")`——前缀与主请求逐字节对齐；接受工具输出指针形态代价（D1）。
2. **失败即中止压缩并返回错误信息**：摘要生成失败（校验不过/超时/模型错误）→ 异常传播，驱逐不发生；无冷却期、无静默降级、**无超预算降级路径（D2，故意）**。
3. **中文摘要**：5 个必备章节用中文标题；摘要正文中文（所有 language 配置统一中文，有意行为）。
4. **点位精准注入**：`ScrollContextManager`（压缩行为，shadow/replace 双模式分流）+ `CommandHandler._stored_scroll_summary_text`（/compact_str 显示，D4）常驻 + `CommandHandler._process_compact`（/compact new 一次性触发，D5/D7）**仅 shadow 模式安装**（replace 下回归宿主原生行为）；各点位版本自检与卸载恢复。

---

## 1. 背景与动机

### 1.1 现有实现的痛点（scroll 第 5 步）

| 痛点 | 现状 |
|------|------|
| 输入有损 | 驱逐内容被压成扁平文本 dump：user≤8000 字符、assistant≤2000、tool_result≤600 字符预览、整体 80K 上限 + 二分搜索 + 时间采样。模型**看不到真实对话** |
| 校验过严 | 三层机械检查：严格解析（heading 逐字按序、heading 外零文本）+ 质量校验（identifier 幻觉检查、secret 扫描、seq 指针存在性）+ 16000 字符上限。格式漂移即烧重试预算 |
| 重试低效 | 无状态重新生成（1+1 次），模型重读全部证据，看不到自己上一稿 |
| 零 cache 复用 | 发独立的 `[SystemMsg(摘要指令), UserMsg(证据dump)]` + `tools=None`，与主对话前缀**毫无重叠** |

### 1.2 新方案的收益

- **对话文本保真**：模型读真实对话，user/assistant 文本原样（注意：早期 tool results 因 pre-fold 已折成 recall 指针，见 §1.3——工具输出细节由指针召回通道兜底）。
- **prefix cache 全量命中**：摘要请求 = 主对话前缀 + 追加提问，KV cache 逐字节复用，只新算「提问 + 摘要输出」。
- **重试高效**：对话内纠正，模型只补缺失章节，可参照自己上一稿。
- **校验轻量**：只查必备章节 heading 存在性（允许尾随冒号）。
- **旧摘要天然在场**：全量 context 的 memory placeholder 里就有旧摘要与归档索引，模型自然融合，提问无需嵌入。

### 1.3 代价（明确接受，D1/D2/D3 拍板）

- 失去机械 identifier 幻觉检查与输出侧 secret 扫描 → prompt 级规则约束（不编造 / identifier 逐字保留 / 不复制 secret）。
- 失败显式化：自动压缩路径上摘要失败 = **当前回合失败**（见 §3.2）——对话被阻塞直到摘要生成成功，这是「正确性优先」哲学的代价。
- **指针形态（D1 定案）**：自动压缩主路径上，第 3 步 pre-fold 已把 middle 中早期 tool results 折成一行占位符（`[scroll folded] old tool result content cleared; recover with recall_history(op="recall_tool", tool_call_id=...)`，manager.py `_tool_result_pointer_stub`）——摘要模型看到的是指针+完整对话文本，**无法总结被折叠工具输出的内容**（原版从 history.db 回读原文，`_durable_folded_result_contents` manager.py:1484-1487）。接受理由：助手叙述文本通常已提炼工具输出结论，结论对延续任务够用；细节走 recall 召回。**cache 对齐与工具输出保真不可兼得，作者裁定 cache 优先**。
- **超预算死锁风险（D2 定案，故意）**：tokens 逼近模型上限时，摘要请求（全量 context + 提问）可能超限 → provider 400 → SummaryGenerationFailed → 不驱逐 → 下回合重复失败。**作者明确裁定不设计降级路径**：超预算时摘要请求照发，provider 错误自然上浮为压缩失败（错误信息含 provider 原文）。未来若要缓解，可加「预算检查+提前 raise」或「dump 降级」，升级入口保留（§3.6）。
- **工具路径重调放大（D3 定案）**：模型主动调压缩工具失败后可能重调（防护仅 `tool_retries_limit=3` 软提醒 + `max_iters=50` 回合上限，最坏 ~200 次模型调用）。缓解=失败消息加「勿重试」指引，接受剩余放大。

---

## 2. 架构总览

```
宿主 scroll 管线（compress() 9 步）
  1 persist  2 trigger  3 pre-fold  4 split
  5 【summarize】←── 本插件替换此步（shadow 模式默认透传原版，`/compact new` 走新方案；replace 模式全量走新方案，`/compact new` 回归原生——D7；失败 → 异常传播 → 6-9 步自然跳过）
  6 index-evict  7 live-fold  8 think-fold  9 active-fold
        │
        ▼  注入点：patch 类（不是 patch 函数）
【patch 点 1】qwenpaw.agents.context.scroll.manager.ScrollContextManager
        = CustomScrollManager(ScrollContextManager)   ← 子类，继承全部；shadow/replace 双模式分流（§2.5）
        │
        ├─ _update_continuation_summary()        分流：legacy 透传 / 新方案（超时 → raise）
        ├─ _update_continuation_summary_inner()  ★核心：对话式生成循环（replace 或 new 触发时）
        └─ load_state()                          兼容加载自由文本摘要
【patch 点 2】qwenpaw.agents.command_handler.CommandHandler._stored_scroll_summary_text
        = 包装函数（free_text 分支走 FreeTextSummary.from_dict 渲染，其余回落原方法）   ← D4
【patch 点 3】qwenpaw.agents.command_handler.CommandHandler._process_compact
        = 包装函数（args=="new" 严格匹配 → 一次性 flag 双路径传递 + args 置空；其余原样）  ← D5
        + FreeTextSummary 适配器                  render/render_background/to_dict/from_dict/covered_seq
```

**为什么不需要 override `compress()`**：失败要「返回错误信息」→ 异常必须传播到调用方。宿主 `compress()` 第 5 步调用处无 try/except（465-719 行 grep 零 try/except，R1 核实），`_update_continuation_summary` 抛出 `SummaryGenerationFailed` 后**第 6-9 步（含驱逐）自然不执行**——中止驱逐与错误上浮一步到位。异常传播路径上无状态残留：persist 已落库（无数据损失）、pre-fold 幂等（folded ids 持久化于 to_dict）、split 纯计算、`last_compress` stats 已如实记录（evicted 仅第 6 步设置，异常时如实为 0）。

### 2.1 注入机制：三 patch 点（调用时 import 全链路生效；点 3 详见 §2.5）

**patch 点 1（压缩行为）**：`ScrollContextManager` 的**全部构造点都是函数体内调用时 import**（R1 核实仅此两处）：
- `qwenpaw/agents/context/__init__.py:187`（`build_scroll_components` 内 lazy import，183-189 行 try 块）
- `qwenpaw/agents/command_handler.py:659`（`_build_standalone_scroll_manager` 内，657-659 行 try 块）

activate 时一行模块属性补丁，宿主自己的构造代码就用子类实例化（kwargs 原样，**零构造逻辑复制**），/compact 路径自动覆盖：

```python
import qwenpaw.agents.context.scroll.manager as mod
mod.ScrollContextManager = CustomScrollManager   # 子类
```

**patch 点 2（/compact_str 显示，D4）**：`CommandHandler._stored_scroll_summary_text`（command_handler.py:561-572，**实例方法**，766 行经 `self.` 调用 → 类属性查找 → 类级 patch 生效）。原方法在 `self._agent is None` 时对会话快照调 `ContinuationSummary.from_dict`——free_text dict 缺 `active_task` 键 → KeyError 捕获 → 返回 None → 显示「No Continuation Summary」。包装函数：

```python
orig = CommandHandler._stored_scroll_summary_text
def patched(self):
    if self._agent is not None:
        return orig(self)                          # 活 agent 走 describe_summary（原样）
    state = self._scroll_state
    raw = state.get("continuation_summary") if isinstance(state, dict) else None
    if isinstance(raw, dict) and raw.get("mode") == "free_text":
        s = FreeTextSummary.from_dict(raw)
        return s.render() if s is not None else ""  # free_text 分支（插件适配）
    return orig(self)                              # 其余回落原方法
CommandHandler._stored_scroll_summary_text = patched
```

**幂等 + 版本自检**（`host_compat.py`，v2 修正查找面——复审 M2）：
- 首次：分别存哨兵 `_ORIGINAL_SCROLL_MANAGER`（manager 模块）与 `_ORIGINAL_STORED_SUMMARY_TEXT`（CommandHandler 类属性）→ 打补丁。
- 重复（热重载）：已 patch（属性 is not 原值）→ 跳过（幂等）。
- 卸载（`resources.defer`）：**两处恢复**（顺序与安装相反）。
- **版本自检（分点位，三层查找面，不可一把查）**：
  - 点 1 类方法（5 个）：`hasattr(mod.ScrollContextManager, name)`——`_update_continuation_summary`(1420)、`_update_continuation_summary_inner`(1444)、`_evicted_span`(846)、`_response_text`(1124)、`_source_backed_previous_summary`(1309)；
  - 点 1 实例属性：`_summary_timeout_seconds`（manager.py:114 构造时赋值）——用 `inspect.signature` 校验 `__init__` 含 `summary_timeout_seconds` 参数，不用 hasattr(类)；
  - 点 1 模块级对象：`ContinuationSummary`（manager.py:41 import）查 manager 模块；`SUMMARY_PREFIX`（continuation_summary.py:16，**manager 未 import**）单独 `import qwenpaw.agents.context.scroll.continuation_summary` 后校验；
  - 点 2：`CommandHandler` 类存在 + `_stored_scroll_summary_text` 属性存在（hasattr）。
- 点 1 任一缺失 → 点 1 不打补丁；点 2 缺失 → 点 2 不打补丁——**点位独立降级**（点 2 失效仅 /compact_str 退化，不阻塞点 1），全部记 warning，绝不阻塞宿主。
- 热重载窗口备注（R3-发现6）：unload 先恢复原类、activate 再 patch；窗口期新构造 manager 一次性 vanilla 降级（功能正确，无数据损坏）；in-flight 实例绑定旧类不受影响。

### 2.2 新 SDK 插件骨架（v2.3.0 体系）

无工具插件 → `type: "general"`（`_infer_type_from_meta`：无 meta.tools → GENERAL）。capabilities 只声明实际用的（`resources` 用于 defer 清理）。

```
plugin.json
├─ id: qwenpaw-plugin-context-compactor-refined
├─ type: "general"
├─ entry: { backend: "plugin.py" }
├─ sdk: { version: "1.0.0", capabilities: ["resources"] }
├─ qwenpaw_version: { min: "2.3.0", max: "2.99.0" }
└─ meta: {}                      # 无工具，无 meta.tools

plugin.py
└─ class ContextCompactorRefinedPlugin:
     ├─ register(context: PluginContext)   # 仅声明：on_activate 注册（不碰 resources）
     ├─ _activate(context)                 # 资源/补丁创建：host_compat.install()（双点位）
     └─ _restore()                         # resources.defer 登记：host_compat.uninstall()（逆序恢复）
   plugin = ContextCompactorRefinedPlugin()   # 模块级导出（probe 检查）
```

**生命周期契约**（SDK 实测）：`register()` 仅声明，**资源创建必须进 `on_activate`**（register 里碰 resources 会 `ResourceError`）。卸载清理用 `context.resources.defer(cb, desc)`（Registration 逆序执行，等价旧 shutdown hook）。

### 2.3 CustomScrollManager 子类（3 override + 1 适配器）

| 组件 | 作用 |
|------|------|
| `_update_continuation_summary()` | 超时包装：`asyncio.timeout` 超时 → `raise SummaryGenerationFailed("压缩摘要生成超时（N 秒）")`（原版静默吞掉继续驱逐） |
| `_update_continuation_summary_inner()` | **核心**：对话式生成循环（§2.4）；4 次尝试全部失败 → `raise SummaryGenerationFailed(错误详情)` |
| `load_state()` | `super().load_state()` 后，若 raw summary `mode=="free_text"` → 用 `FreeTextSummary.from_dict` 重建（原版只认 `ContinuationSummary.from_dict`；free_text dict 走原类 from_dict 会 KeyError→None→摘要丢失，故必须 override，R1 核实兼容） |
| `FreeTextSummary` | 实现下游 4 接口：`render()` / `render_background(stale, include_envelope)` / `to_dict()` / `covered_seq` 属性。**已核实的消费点全景**（R1/R2/R3 三路 grep）：manager 内 12 处（render 2264/1515、render_background 2137-2140、to_dict 2308、covered_seq 1317/1455-1478）+ manager 外 1 处：`command_handler.py:571`（agent-less /compact_str 分支）——**由 patch 点 2 适配（D4）**，不再是退化点 |

### 2.4 生成循环（`_update_continuation_summary_inner` 核心，v3 定稿）

```
① 入口 guard（保留原版两行，复审 R3-发现2）：
   new_span = self._evicted_span(middle)
   if new_span is None or not callable(getattr(agent, "model", None)): return
② previous 校验（继承）：previous = self._continuation_summary
   → _source_backed_previous_summary(existing_seqs)（retention purge 防御）
   covered = previous 存在则 min/max 合并 span，否则 new_span      ← 纯代码计算，不依赖模型
③ 构建摘要请求（与主请求同源，prefix 逐字节对齐）：
   base = await as_internals.prepare_model_input(agent)            # 主请求同源构造！
   messages = base["messages"] + [UserMsg(name="summary_request", content=提问+focus_hint)]
                                                             # base 即主请求 messages，只追加尾部
   tools    = base["tools"]                                  # 与主请求同一份（含 compression tool 时序正确性）
④ for attempt in 1..4:                                       # 首次 1 + 重试 3（拍板）
       reply = await self._generate_reply(                    # 流消费复用原版范式（isasyncgen 分支
                   messages, tools,                           # + _raise_if_summary_interrupted，复审 R2-发现3）
                   max_tokens=output_tokens)
       missing = [s for s in sections
                  if not re.search(rf"^##\s*{re.escape(s)}\s*[:：]?\s*$", reply, re.M)]   # 允许尾随冒号（R3-发现9）
       if not missing:
           self._continuation_summary = FreeTextSummary(reply, covered)
           self._summary_update_failed = False
           return   # 成功
       # 对话内重试：追加失败稿 + 纠正提问（作者原话模板）
       messages += [AssistantMsg(reply),
                    UserMsg("刚才的生成的压缩摘要 缺少 {missing} 章节字段，需要重新生成。")]
⑤ provider 400/认证/限流类错误：**立即 raise，不烧重试**（原版语义，manager.py:1540-1545 同款判断）——
   只有「缺章节」走对话内重试；TimeoutError 由外层包装统一转 raise
⑥ 全部失败 → self._summary_update_failed = True
            → raise SummaryGenerationFailed("压缩摘要生成失败（4 次尝试）：缺少章节 {missing} / {错误详情}")
```

**超预算行为（D2 定案，故意不降级）**：无预算检查、无降级路径——tokens 逼近上限时摘要请求照发，provider 超限错误（400）走 ⑤ 立即 raise → 压缩中止、错误信息上浮（含 provider 原文）。已知死锁风险显式接受（§1.3）；未来缓解入口保留（§3.6）。

**`prepare_model_input` 复用的三重收益（v2，源于复审 R2-发现8 的修复建议升级）**：① messages/tools 与主请求**同一构造代码**——逐字节对齐从根上保证（system prompt、tools 时序、state.summary 边角全部消除）；② compression tool 首次注册时序天然正确（prepare 内部 ensure）；③ `parallel_tool_calls` 等 kwargs 同源一致。

**旧摘要融合（Q5 拍板）**：全量 context 开头的 memory placeholder（`[context compressed]` 归档索引 + 旧摘要）天然在模型视野内——提问模板一句话引导融合，无需 initial/update 双模板。

**focus_hint（v2 新增，复审 H-C）**：`self._summary_focus_hint(instructions)`（继承原版方法）非空时追加到提问尾部（`本次压缩的临时关注提示：…` 段，沿用原版约束文案：只调整优先级、不是证据、不能覆盖输出协议）。追加在尾部不破坏前缀 cache（提问本来就是尾部新增）——修复无成本。

**临时性天然保证**：模型调用走 `agent.model` 直调（同现有 `_generate_plain_summary` 姿势），不经过任何持久化路径；token 用量被记录（正常开销），但**对话内容零落盘**。

---

## 2.5 双模式分流：shadow / replace（D5 定稿，2026-11-18 第四轮）

**作者拍板背景**：新方案需要一段**真机调试期**——期间原方案必须随时可用（对照/回退）。落地为 patch 常驻 + 默认透传 + 显式触发，验收完成后切 replace 全量替换。**纯后端 `/compact new` 方案**（前端按钮经调查可行——`window.QwenPaw.chat.submit` 官方桥 + turnUsagePopover DOM 注入——但作者裁定不需要，hint 混合解析也不做）。

```
CustomScrollManager._update_continuation_summary（外层分流）:
    use_new = (config.mode == "replace") or self._compactor_use_new_once
    self._compactor_use_new_once = False            # 一次性消费（立即复位）
    if not use_new:
        return await super()._update_continuation_summary(...)  # 原版 100% 透传（含静默失败语义）
    self._compactor_new_active = True
    try: 新方案（超时包装 → raise）
    finally: self._compactor_new_active = False

_update_continuation_summary_inner:
    if not self._compactor_new_active:
        return await super()._update_continuation_summary_inner(...)   # 防御性回落
    新方案生成循环
```

**patch 点 3（D5/D7）**：`CommandHandler._process_compact` 包装，**shadow 模式专属**（D7：replace 下不安装，/compact new 回归宿主原生行为）——
- **安装面按 mode 分流（D7，v0.2.2）**：仅 `shadow` 模式安装本点位；`replace`（默认）不安装（install 状态报 `skipped(mode=replace)`，`/compact new` 的 args 经宿主 `_compact_instructions` 进 hint 通道）；若 shadow 残留 wrapper 在位，install(replace) 就地恢复并报 `residual-restored`；
- `args.strip() == "new"`（**严格匹配**，hint 语义零破坏：`/compact new xxx` 不触发、args 原样走 hint 通道）→ 触发 + **args 置空传原方法**（防 "new" 漏成 hint 噪音）；
- flag 传递双路径：agent 分支 `self._agent._context_manager`（marker 校验）设实例 flag；agent-less 分支模块级 `_pending_new_once`（standalone manager 在同调用栈内构造时读取并立即清除）；
- finally 兜底复位——compress 在第 1-4 步失败时 flag 不泄漏到自动压缩；
- 自检/幂等/逆序恢复与点位独立降级同其余点位（哨兵 `_SENTINEL_COMPACT_ATTR`）。

**与硬约束的关系**：四条硬约束在 replace 模式下原样成立；shadow 模式下仅 new 触发的那一次压缩适用（默认态完全 vanilla）。验收完成后的切换动作 = 改 config.py 默认值（`MODE_REPLACE`，已实施）——manifest config 段在宿主 2.3.0 不被消费（见 §5 配置说明），无二次改码。

---

## 3. 关键设计决策

### 3.1 prefix cache 全量命中（Q6/Q7 + D1，不可变）

主对话 wire 前缀 = `[system prompt][tools 数组][messages]`——**tools 序列化在对话消息之前，是 KV cache 前缀的一部分**（证据：agentscope `_prepare_model_input` 注释「schema is stable for prompt caching」逐字存在（`_agent.py:3261-3262`）；agentscope `_openai_chat/_model.py:576-579` `_format_tools` docstring「…without needing to filter the list, preserving prompt-cache efficiency」（QwenPaw compat 的 `_format_tools` 经 914 行 `super()._format_tools` 继承执行该逻辑）；QwenPaw `cache_usage()` 追踪 `cache_input_tokens`/`cache_creation_input_tokens`/`x-ds-cache-status`）。

| 禁工具实现 | 摘要请求前缀 | cache 复用 |
|-----------|------------|----------|
| `tools=None` | `[system][msgs][question]` | ❌ system 后即分叉 |
| **保留 tools + `ToolChoice(mode="none")`（定案）** | `[system][tools][msgs][question]` | ✅ 逐字节对齐，对话全量复用 |

**⚠️ `tool_choice` 必须用对象形态（复审 H-A，双路独立实锤 + 作者侧亲读复核）**：agentscope 期待 `ToolChoice` pydantic 对象（`agentscope/tool/_types.py:178-203`），**裸字符串 `"none"` 会在两处引爆 AttributeError**——`_base.py:316-336` `_validate_tool_choice`（333 行只拦 None，336 行 `.mode`）与 `_model.py:595→607`（604 行只拦 falsy，607 行 `.mode`）。正确形态：`tool_choice=ToolChoice(mode="none")` → `_format_tools` 609-612 行对 literal mode 返回 `(tools, "none")` → wire `tool_choice: "none"` 字符串——**前缀不受影响，cache 论点不变**。

三件套：① messages/tools 整体复用 `prepare_model_input(agent)` 输出（同源构造）；② `ToolChoice(mode="none")`（控制参数不 tokenize 进前缀）；③ 全量 context（`prepare_model_input` 天然全量）。

**逐字节不变还需**：随 tools 出现的 kwargs 一致（`parallel_tool_calls` 等，同源构造天然一致）；formatter 确定性（`OpenAIChatFormatter.format` 无连续 user 合并/无报错，R2 核实原样透传）。
**真机验证点**：`ToolChoice(mode="none")` 在火山方舟/GLM 的支持性与前缀无关性（读 `usage.cache_input_tokens` 实测）；连续 user 消息 provider 兼容性（formatter 层已核，wire 层实测）。
**降级**：provider 不支持 `tool_choice="none"` → `cache_mode="no_tools"` 配置降级（牺牲 cache，功能正确）。

### 3.2 失败语义：异常传播，无冷却（Q3/Q4 + D3，定案）

**任何失败（4 次尝试后仍缺章节 / 超时 / 模型错误 / 超预算 provider 400）→ `raise SummaryGenerationFailed`，压缩终止（不驱逐），错误信息上浮到调用方。** 无冷却期、无静默降级、无超预算降级——重复失败就重复返回错误信息（作者拍板）。provider 400/认证/限流类错误**不参与重试**（立即 raise，复审 H-B 衍生）。

三条调用路径的错误落点（R1 逐层核实 + R2 九层传播链验证无一吞异常，含 plugin_middleware `is_downstream_error` 身份比对重抛）：

| 路径 | 调用点 | 异常处理 | 错误信息落点 |
|------|--------|---------|------------|
| 自动压缩（每个推理步前） | agentscope `_agent.py:1185` | **无 try/except**（`_reply_impl` 仅捕 CancelledError） | **当前回合失败**，错误信息在回合错误里 |
| 压缩工具（模型主动调） | `_agent.py:468-475` | catch → ToolChunk | `Context compression failed: {e}` 返回给模型 |
| /compact 命令 | `command_handler.py:474-479` | catch → 系统消息 | 「❌ **Compact Failed!** Reason: {e}」 |

**作者需知情的后果（均已拍板接受）**：
1. 自动路径（Q3）：摘要持续失败 = 对话被阻塞（每条消息 → 4 次摘要尝试 → 回合失败），直到生成成功。上下文始终无损（persist 已落库，驱逐未发生）。
2. 超预算死锁风险（D2）：tokens 逼近上限时压缩必失败且重复（见 §1.3）。**故意接受**。
3. 工具路径放大（D3=(a)）：模型重调压缩工具的防护=失败消息追加「**压缩已中止，请勿立即重试本工具**」指引 + 宿主既有 `tool_retries_limit=3` 软提醒与 `max_iters=50` 上限；接受剩余放大（最坏 ~200 次调用），真机验收实测一次，若真发生再升级处置。

**/compact 异常状态自洽（R3-发现3，显式化）**：异常时 `to_dict` 不执行 → `_updated_scroll_state` 保持 None → 会话保留旧 scroll block；驱逐未发生故旧 index/摘要与 live context 一致；本轮 persist 落库行由 `(session_id, dedup_key)` 唯一约束兜底（history.py `ON CONFLICT DO NOTHING`）→ 无重复行、无状态覆盖冲突。

异常定义：`class SummaryGenerationFailed(RuntimeError)`，消息自包含（含尝试次数、缺失章节或失败原因），保证 /compact 的 `Reason: {e}` 可读。

### 3.3 FreeTextSummary 适配器

摘要对象从结构化 `ContinuationSummary`（dataclass，每条带 source）换成**自由文本**（模型正文原样）。下游 4 接口适配，管线零改动（17 个使用点三路核实）。`to_dict` 用 `{"mode":"free_text","covered_seq":[lo,hi],"text":...,"version":2}`，与结构化 v1 共存（`load_state` 双格式兼容，旧会话平滑迁移）。`render_background` 复用宿主 `SUMMARY_PREFIX` + 归档范围提示，与原版注入形态一致；`stale` 分支在新失败语义下成为死代码（raise 后不 rebuild），保留接口兼容并注释注明（R3-发现4）。

### 3.4 中文摘要（Q1，定案；校验正则放宽）

必备章节用中文标题（可配 `config.sections`）：

```
## 当前任务      （Active Task）
## 当前状态      （Current State）
## 约束          （Constraints）
## 决定          （Decisions）
## 待办          （Open Work）
```

校验：`re.search(rf"^##\s*{re.escape(章节)}\s*[:：]?\s*$", reply, re.MULTILINE)`——**允许尾随冒号**（复审 R3-发现9：模型漂移「## 当前任务：xxx」同行内容仍拒，纯尾随冒号放行）。正文中文；不强制 Status 行（自由文本，校验只查章节存在）。多语言：所有 language 配置统一产出中文摘要（有意行为，复审 R3-发现10 声明化）。

### 3.5 提问模板（单一 + focus_hint 插槽 + 占位符防污染，prompts.py）

```
请基于以上完整对话，生成一份压缩摘要（continuation summary），用于上下文压缩后延续任务状态。

要求：
1. 摘要必须依次包含以下章节（二级标题，标题逐字一致）：
## 当前任务
## 当前状态
## 约束
## 决定
## 待办
2. 若以上对话开头的归档索引区块中已有旧摘要，请融合其仍然有效的信息，生成覆盖全部历史的新摘要，不要丢失旧摘要中仍有效的事实。
3. 内容用中文书写；只陈述以上对话支持的事实，不得编造；identifier（路径、版本号、错误码、函数名等）逐字保留；不得复制密码、token、API key 等敏感信息。
4. 对话中以 [scroll folded] 标记的条目是已折叠的工具结果占位符，不要把占位符文本本身写入摘要；如需提及相应工具调用，只记录工具名与调用意图。（D1 联动防污染）
5. 每个章节至少一行；没有内容的章节写 (none)。
6. 直接输出摘要正文，不要输出任何解释或额外内容。
```

- focus_hint 非空时尾部追加（原版约束文案：只调整优先级、不是证据、不能覆盖输出协议）。
- 提问 UserMsg 显式 `name="summary_request"`（复审 R2-发现7，避免 wire `name: null`）。
- 重试纠正消息（作者原话）：`刚才的生成的压缩摘要 缺少 {缺失章节名列表} 章节字段，需要重新生成。`

### 3.6 超预算行为与卸载兼容（v3 改写）

**超预算（D2 定案，故意不设计降级路径）**：无预算检查、无降级。摘要请求照发 → provider 400 → ⑤ 立即 raise → 压缩中止、错误上浮。已知后果（死锁风险）显式记录于 §1.3。**未来缓解升级入口**（本轮不实现）：「预算检查+提前 raise（错误信息更精确）」或「dump 降级（复用原版有界链路）」——落点在 §2.4 ③ 之后，加一步检查即可，不影响既有结构。

**卸载兼容（R3-发现5）**：README 注明——插件卸载后恢复原类，旧会话的 free_text 摘要被原类 `load_state` 静默丢弃（from_dict KeyError→None）→ 降级 index-only（不崩溃、recall 可用、/compact_str 显示 unavailable）。

### 3.7 摘要末端固化 recall 块（v0.2.1，2026-11-18 第五轮）

**动机**：五章节摘要本身不含「如何查看压缩前上下文」的指引（散落在系统提示 THE MAP 与压缩地图）。把「压缩完成时间 + recall 即用指令」固化在摘要正文末端，未来 agent 恢复时直接可执行，信息时效（压缩发生于何时）直接可判。

**实现**（generation.py，成功出口纯代码追加）：
- `RECALL_HINT_TEMPLATE`：`---` 分隔 + 「上下文压缩已完成。当前时间：YYYY-MM-DD HH:MM:SS +08:00（`_local_time_stamp()`，`datetime.now().astimezone()` 当地时间 + UTC 偏移）」+ 📎 三件套（expand 含 covered 累积区间数字 / search / recall_tool）；
- covered 异常（倒挂 lo>hi、非法值）降级 `RECALL_HINT_GENERIC` 通用指引（指向压缩地图、无具体数字），**绝不抛**——固化块是增强层，任何异常都不该让已成功的压缩失败；
- 追加点在 `missing_sections` 校验通过**之后**：校验针对模型原稿，重试对话里的失败稿保持原样，零 tokens、字节级一致、不依赖模型遵守提示词（与 §3.5 提问模板防污染句同一哲学：**能代码固化的不进提示词**）；
- 追加在构造时（`FreeTextSummary(_append_recall_hint(reply, covered), covered)`）——text 是全部下游消费的源（render / render_background / to_dict），一处追加全覆盖。

**验收**（21:45:29 真机压缩）：注入形态 `[archived task state]` 末尾固化块完整落地，时间戳=压缩时刻同秒，covered 167415–170642 与上段无缝累积。87 测试全绿（+3：seq 指令 / 倒挂降级 / 重试对话无固化块）。

---

## 4. 模块布局

```
qwenpaw-plugin-context-compactor-refined/
├── plugin.json           # manifest（SDK 1.0.0 / type=general / capabilities=[resources]）
├── plugin.py             # 入口：register + on_activate(install 按模式打点) + defer(uninstall 逆序恢复)
├── host_compat.py        # patch 点：ScrollContextManager 类 + CommandHandler._stored_scroll_summary_text
│                         #   + CommandHandler._process_compact（/compact new，D5/D7——仅 shadow 安装）
│                         #   三层查找面版本自检（点位独立降级）+ 幂等哨兵 + uninstall 恢复
├── summary_manager.py    # CustomScrollManager(ScrollContextManager)：3 override + SummaryGenerationFailed
├── summary_object.py     # FreeTextSummary 适配器（含 patch 点 2 使用的 from_dict/render）
├── generation.py         # 摘要请求构建（prepare_model_input 同源）+ 生成循环 + 流消费 + 章节校验
├── prompts.py            # 提问模板 + focus_hint 插槽 + 占位符防污染句 + 重试纠正消息 + 章节文案
├── config.py             # 配置 dataclass（默认值 + context.config 覆盖）
├── README.md             # 用户向说明（含卸载退化、多语言声明、D2/D3 已知后果）
├── AGENTS.md             # 项目约定（作者，非真实称呼）
├── MEMORY.md             # 项目记忆索引
└── tests/
    ├── conftest.py       # sys.path 快照恢复（TestHost 副作用）
    ├── test_generation.py
    ├── test_summary_object.py
    ├── test_host_compat.py   # 三点位（patch 3 按 mode 分流）：自检/幂等/独立降级/恢复
    └── test_sdk_host.py      # （未实现——原计划 PluginTestHost 集成；当前由真机验收覆盖）
```

---

## 5. 配置项（config.py）

| 键 | 默认 | 说明 |
|----|------|------|
| `mode` | `replace` | 默认全量替换（D5）；`shadow`=调试用透传模式（默认透传原版，仅 `/compact new` 触发新管线并注册其拦截；D7 起 replace 下不注册） |
| `sections` | `["当前任务","当前状态","约束","决定","待办"]` | 必备章节（中文，Q1 定案） |
| `max_retries` | `3` | 重新生成上限（首次 1 + 重试 3 = 共 4 次尝试，Q2 定案） |
| `output_tokens` | `0` | ~~摘要输出预算：0 = 按宿主公式 `max(256, min(4000, ctx//4))` 推导~~ **【v0.2.5 已取代】**：0 = 不传 max_tokens（端点默认）；>0 且 ≤32000 抬到 32000；>32000 保持。旧公式 4000 cap 曾导致正文撞线截断且不可察觉（见 CHANGELOG v0.2.5） |
| `disable_thinking` | `True` | 禁思考（GLM 思考 ~12 token/s 会烧预算） |
| `cache_mode` | `"preserve_tools"` | `preserve_tools`(ToolChoice(mode="none")，定案) / `no_tools`(provider 不支持时降级) |
| `question` | 见 prompts.py | 提问模板（单一 + focus_hint 插槽 + 防污染句，Q5/D1 定案） |
| `summary_timeout_seconds` | 继承 scroll 配置 | 超时包装沿用宿主 `self._summary_timeout_seconds`，不新增 |

> 冷却期机制：**已否决**（Q4）。超预算降级路径：**已否决**（D2，故意不设计）。

---

## 6. 测试策略

| 层 | 覆盖 |
|----|------|
| 单元 | 生成循环（mock `agent.model` 流式/非流式：成功/缺章节对话内重试/4 次全败 raise/provider 400 立即 raise/超时 raise）、FreeTextSummary 四接口、章节校验（中文 heading 正则含尾随冒号变体）、提问模板（focus_hint 插槽 + 防污染句）、covered_seq 合并、入口 guard（new_span None） |
| host_compat | 三点位自检（patch 3 按 mode 分流；SUMMARY_PREFIX 模块级/实例属性签名/类方法/CommandHandler 属性）、**点位独立降级**（点 2 缺失不阻塞点 1）、幂等 install、uninstall 逆序恢复 |
| patch 点 2 | 包装函数三分支（活 agent 回落/free_text 渲染/其余回落原方法）、/compact_str 显示摘要文本 |
| SDK 集成 | （未实现）原计划 PluginTestHost：load→register→on_activate→unload；当前 patch 生效由真机验收覆盖（三点位哨兵 + 宿主内验证），另有 patch 点自检函数在激活时运行 |
| 真机验收 | ① 长会话自然触发压缩 → 中文摘要落位 + `/compact` 报告 + recall 可用 + 会话重启摘要恢复；② **cache 命中**：读 `usage.cache_input_tokens` 确认摘要请求命中主对话前缀（核心验收项）；③ 失败路径三落点（/compact 报错、工具路径 ToolChunk 含「勿重试」指引、自动路径回合失败）+ 上下文原样 + 4 次尝试计费正确；④ `ToolChoice(mode="none")` wire 形态与 provider 支持性 + 连续 user 消息兼容性；⑤ **/compact_str**（agent-less 场景）显示摘要文本（D4 验收）；⑥ 超预算场景观察 provider 400 → 压缩失败错误信息可读（D2 行为确认，不做降级） |

**对抗复审**（按惯例）：实现完成后 commit 前再派无上下文 subagent 对**实现**做对抗复审（本轮是设计稿复审，实现稿另审）。

---

## 7. 分阶段计划

| 阶段 | 内容 | 验收 |
|------|------|------|
| **Phase 1 骨架** | manifest + plugin.py + host_compat（双点位/三层自检/幂等/逆序恢复）+ FreeTextSummary + 3 override 最小实现（含流消费）+ patch 点 2 | 真机 `/compact` 走自定义路径：中文摘要落位、/compact 报告、recall 可用、重启恢复、unload 恢复原类；/compact_str 显示摘要 |
| **Phase 2 生成循环** | prepare_model_input 同源构建 + ToolChoice 对象 + 章节校验 + 对话内重试 + 异常传播 + 配置 | 长会话自然触发成功；**cache 命中实测**；失败路径三落点验证 |
| **Phase 3 复审验收** | 无上下文对抗复审（实现稿）+ 真机全链路验收 | 复审零 HIGH；验收全绿 |

---

## 8. 拍板记录（2026-11-18 第一轮，7/7 定稿）

| # | 问题 | 决定 |
|---|------|------|
| Q1 | 章节列表 | 沿用现有 5 个概念，**标题用中文**：当前任务/当前状态/约束/决定/待办 |
| Q2 | 重试口径 | **共 4 次尝试**（首次 1 + 重试 3） |
| Q3 | 失败语义 | 生成失败/不符合要求 → **终止压缩并返回错误信息**（异常传播，三路径落点见 §3.2） |
| Q4 | 冷却期 | **不需要**。重复失败直接返回错误信息，无额外机制 |
| Q5 | 旧摘要处理 | **不嵌入提问**——旧摘要自然在全量上下文里（memory placeholder），模型天然可见可融合 |
| Q6 | 输入形态 | **全量 context**——缓存命中是最重要的，必须保留 |
| Q7 | system prompt | **保持缓存命中，不能变**——用 agent 真实 system prompt |

> 其他已定：插件名 = `qwenpaw-plugin-context-compactor-refined`；tools 保留；新 SDK 体系（v2.3.0）。

## 9. 复审拍板记录（2026-11-18 第二轮 + 第三轮定案）

复审规模：R1 源码事实核对 / R2 技术正确性 / R3 边界遗漏，三路并行无上下文；4 HIGH（2 个双路独立命中）+ 10 MEDIUM + 8 LOW；HIGH 全部经作者侧亲读源码抽验实锤。修复已全部落入本稿。

**技术修复（无需拍板，直接采纳）**：

| # | 复审发现 | 处置 |
|---|---------|------|
| H-A | `tool_choice="none"` 裸字符串 AttributeError（R1+R2 双路；`_base.py:336`、`_model.py:607` 两处引爆点） | ✅ `ToolChoice(mode="none")` 对象形态，wire 仍输出 "none"，cache 论点不变 |
| H-B 衍生 | provider 400/认证/限流类错误不应参与重试（烧 3 次纯浪费且逐次膨胀） | ✅ 立即 raise（原版语义，manager.py:1540-1545 同款） |
| H-C | focus_hint（/compact 一次性指令）静默丢弃 | ✅ 提问尾部插槽（追加不破坏前缀，修复无成本） |
| M1 | docstring 引用归属写错（agentscope 基类非 QwenPaw compat） | ✅ §3.1 引用已修正 |
| M2 | 版本自检三层查找面（SUMMARY_PREFIX 不在 manager 命名空间、_summary_timeout_seconds 是实例属性） | ✅ §2.1 三层自检方案 + 点位独立降级 |
| M4 | 流式 AsyncGenerator 未消费（stream=True 全 provider 默认） | ✅ §2.4 ④ 复用原版流消费范式 |
| M5 | 生成循环入口 guard 缺失（new_span None） | ✅ §2.4 ① 保留原版两行 |
| M8 | 章节校验假阴性（尾随冒号等漂移） | ✅ §3.4 正则放宽 `[:：]?` |
| M9 | 多语言冲突（language=en 强制中文） | ✅ §3.4 声明化（有意行为） |
| 升级 | compression tool 首次注册时序（R2-发现8）→ 复用 prepare_model_input 的修复建议升级 | ✅ messages/tools 整体同源复用（§2.4 ③） |
| L1-L8 | cache 读数属性名（usage.cache_input_tokens）、UserMsg name、/compact 自洽补句、热重载窗口备注、类型注解、stale 死代码备注等 | ✅ 全部落入对应章节 |

**作者拍板（第三轮，2026-11-18）**：

| # | 问题 | 决定 |
|---|------|------|
| **D1** | 自动压缩主路径上工具输出是指针形态（cache 对齐与保真不可兼得） | **选择全量命中（cache 优先）**：接受指针形态；提问加占位符防污染句（§3.5 第 4 条）；细节走 recall 召回 |
| **D2** | tokens 超预算时：自动降级 dump / 预算检查 / 还是照发？ | **不降级，故意不设计降级路径**：摘要请求照发，provider 超限错误自然 raise；已知死锁风险显式接受；未来缓解入口保留（§3.6） |
| **D3** | 压缩工具路径重调放大（最坏 ~200 调用） | **选 (a)**：接受 + 失败消息加「压缩已中止，请勿立即重试本工具」指引；真机实测观察，若放大真发生再升级 |
| **D4** | agent-less /compact_str 对 free_text 的显示 | **选 patch**：第二 patch 点位 `CommandHandler._stored_scroll_summary_text`（实例方法，类级 patch 可行），/compact_str 完整正常；点位独立降级 + 自检 + 卸载恢复（§2.1） |

**作者拍板（第四轮，2026-11-18，双方案调查后）**：

| # | 问题 | 决定 |
|---|------|------|
| **D5** | 新方案如何与原方案并存（调试期） | **shadow/replace 双模式 + 纯后端 `/compact new`**：shadow（调试期默认）= patch 常驻 + 原版 100% 透传，仅 `/compact new` 一次性触发（严格匹配，args 置空防 hint 噪音，flag 双路径传递 + finally 兜底复位）；replace（验收完成后）= 全部压缩走新方案，切换 = config `mode: "replace"` 无二次改码。**前端按钮不做**（调查确认可行：`window.QwenPaw.chat.submit` 官方桥 + turnUsagePopover DOM 注入，作者裁定不需要）；hint 混合解析不做。详见 §2.5。**→ 2026-11-18 验收完成后已切 replace（默认值切换）**：验收证据 = 中文摘要真机三会话成功 + /compact_str 显示 + 卸载恢复 + wire 字节对齐证死（732,678 字节前缀逐字节覆盖，tail=1095 提问段）+ keep-context 插件叠加兼容；llama-server slot 轮换致 0 命中属 provider 时序（非 wire 缺陷），GLM 正样本可选 |

**作者拍板（第五轮，2026-11-18，固化块）**：

| # | 问题 | 决定 |
|---|------|------|
| **D6** | 摘要末端是否固化 recall 指引、如何实现 | **代码级固化追加**（v0.2.1）：成功出口纯代码追加时间戳（当地时间+UTC 偏移格式，作者指定）+ recall 三件套即用指令（含 covered 区间）；不做 config 开关（YAGNI 写死）、不进提示词（防措辞漂移）、追加在章节校验之后（模型产出保持纯净）；covered 异常降级通用指引绝不抛 |

**作者拍板（第六轮，2026-11-18，v0.2.2）**：

| # | 问题 | 决定 |
|---|------|------|
| **D7** | replace 模式下 /compact new 的注册 | **解除注册（patch 3 改为 shadow 专属）**：install 按 mode 分流——replace（默认）不安装 patch 3，/compact new 回归宿主原生行为；shadow 安装（一次性对照）。uninstall 哨兵机制天然幂等，无需改恢复逻辑 |
