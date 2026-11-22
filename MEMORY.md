# MEMORY.md — 项目记忆索引

> 项目：QwenPaw 压缩摘要插件（context-compactor-refined）。设计稿 `DESIGN.md`（v3 定稿）。

## 实现状态

- **v0.2.4 已热装：marker 改 HintBlock（UI 隐藏）**（2026-11-22）。marker 从 `UserMsg(content=文案)` 改成 `AssistantMsg(name="compaction_boundary", content=[HintBlock(hint=文案, source="compaction_boundary")])`——借 QwenPaw 的 HintBlock 机制：agentscope formatter 把 HintBlock 无条件转成独立 `role="user"` 消息（LLM 照常看到边界提示），console/PawApp 的 history converter（`chats/utils.py:775`）直接跳过 hint block（UI 彻底不显示，4 条 context 转 3 条）。**关键约束**：UserMsg/SystemMsg 拒 hint block（agentscope Msg 校验："User message can only contain text blocks or data blocks"），必须 AssistantMsg 承载；AssistantMsg/UserMsg/SystemMsg 是工厂函数非类（isinstance 用 role 判断）。端到端实测：formatter 输出 marker 为 user 消息、history converter 跳过（marker 文案不在 UI）。连续 user 消息行为不变（formatter 转出的还是 user 消息）。4 个 marker 测试更新（断言 assistant 角色 + HintBlock + hint 文案），97 全绿。zip 热装（18 文件）。发现已入知识库《QwenPaw消息可见性与HintBlock机制》。**真机验证发现 LLM 没注意到 marker**：fork 会话里 qwen3.8-27b 看到了 placeholder 的 `[context compressed]`/`[archived task state]`/recall 指引，却说"没看到 `[压缩边界]` 中文标记"——marker 是 user 消息夹在保留轮和压缩后新回合之间，被 placeholder 的"压缩元信息"淹没、被误当普通 user 消息。**修复**：文案前加 `[系统提示：压缩边界]` 标识（原 `[压缩边界]`），让 LLM 一眼认出它是系统注入的边界标记而非普通 user 消息。机制不变（HintBlock/UI 隐藏/位置正确），只改文案。
- **v0.2.3 已热装：压缩边界 marker**（2026-11-22）。压缩驱逐后往 context 追加一条普通 user 消息 `[压缩边界] 上方为压缩前保留的近期回合——它们运行时拥有全量上下文（含下方摘要所归档的更早内容），并非基于摘要跑出的；下方为压缩后新回合，需经摘要/召回才能看到上方更早内容。`，插在保留轮之后（tail 末尾），修掉模型误以为上一轮基于摘要跑的误区。普通 UserMsg，不进 `_synthetic_ids`、不打 tag，随正常驱逐消失只标最近一次边界。**flag 分流**：`_compactor_marker_pending` 由 `_update_continuation_summary`（仅驱逐路径调用）置 True、`load_state`（restore 时）清 False、`_rebuild_context` 覆写消费——精确区分 compress() 驱逐路径（插）与 reconcile_loaded_context 恢复路径（不插），abort 残留由 load_state 兜底清零。**单轮陷阱（已修）**：初版用 `_active_turn_tail` 把 marker 插在 active turn 之前，单轮会话里唯一那轮既是保留轮又是 active turn，non_active 为空 → marker 被挤到 idx 0 最前、语义反掉；改为一律追加 tail 末尾。文案去掉「seq 连续不重置」——seq 跳跃且跨会话，诱导模型续号会召回到别的会话的被驱逐上下文（v0.2.1 固化块的 covered 具体区间不在此雷区，直接 expand 不做算术）。4 个新测试（驱逐末尾插/恢复不插/普通 UserMsg 非 synthetic/无 active turn 边界），97 全绿。zip 热装（18 文件工作区内容）。
- **v0.2.1 已热装：摘要末端固化块生效**（2026-11-18 晚）。成功出口（generation.py `_generation_attempts` 内）纯代码追加 `_append_recall_hint(reply, covered)`——「上下文压缩已完成。当前时间：YYYY-MM-DD HH:MM:SS +08:00（当地时间+UTC 偏移）」+ 📎 recall_history 三件套即用指令（expand 含 covered 累积区间 / search / recall_tool）；covered 异常（如倒挂 lo>hi）降级 RECALL_HINT_GENERIC 通用指引（指向压缩地图），绝不抛。校验针对模型原稿，固化块在 missing_sections 通过后追加，重试对话里的失败稿保持原样。零 tokens、字节级一致、不依赖模型遵守提示词。
- **v0.2.2 已热装：patch 3 改为 shadow 专属**（2026-11-18）。install 按 mode 分流——replace（默认）不安装 `/compact new` 拦截（回归宿主原生行为），shadow 安装（一次性对照）；shadow 残留 wrapper 遇 install(replace) 就地恢复（`residual-restored`）；uninstall 哨兵天然幂等 + sentinel-missing 分支 hasattr 守卫。D7 拍板（DESIGN §9 第六轮）。README 新增开头「如何使用」小节。93 测试全绿。
- **真机验收通过**（2026-11-18 21:45:29 压缩）：注入形态 `[archived task state]` 末尾固化块完整落地，时间戳=压缩时刻，covered 167415–170642 与上段（至 170616）无缝累积衔接；本次压缩未生成 dump——**归因勘误（B 路复审 H-1）**：manifest config 段宿主 2.3.0 不消费，debug_dump=False 从未被读取，运行态 False 是 config.py 默认值的效果（非「配置实例完整刷新」）；调试开 dump 须走宿主 settings（平铺键 debug_dump 已在 _resolve_config 键列表内）。
- **v0.2.0 基线：mode=replace 全量替换生效**（2026-11-18）。调试期（shadow + /compact new）结束，全部压缩（自动 / /compact / /compact new）走新方案（v0.2.0 时点；v0.2.2 起 /compact new 除外，见首行）；debug_dump 随切换自然关闭（默认 False）。
- **验收证据链**（切换依据）：中文摘要真机三会话成功（2026-11-18 三次真机压缩验收）+ /compact_str 显示 + 卸载恢复（三点位哨兵实测）+ **wire 字节对齐证死**（formatter 层 dump：common_prefix=732,678=main 全量，tail=1,095 提问段，数学闭合）+ keep-context 插件叠加兼容。87 测试全绿（v0.2.1 +3：固化块 seq 指令 / 倒挂降级 / 重试对话无固化块）。
- **cache 0 命中定性完成**（2026-11-18 21:11 压缩）：attempt2 cache_input_tokens=84,032（input 85,340，99.98% 命中，46.9s）——框架未丢数实锤；attempt1 cache=0 = standalone 首发 prefix 首次出现（方舟隐式缓存按请求上下文粘性，摘要请求独立管线首发必 0）；**重试机制首战成功**——attempt1 撞 max_tokens 4000 截断（output 整数 4000.0=截断指纹）→ 章节校验不过 → 对话内重试 → attempt2 吃缓存红利 99.98% 命中，失败重试成本从「重头 eval」降到「增量生成」；VolcengineFormatter 字节对齐补验通过（315,926=main 全量，双 formatter 证死）。GLM 隐式缓存机制实证（上下文粘性 / standalone 首发必 0 / 重试缓存红利）详见 `DESIGN.md` §9 复审拍板记录与本文件 cache 条目。
- 对抗复审 PASS（0 HIGH / 4M / 2L，M-1/M-2 已修复，M-3 已声明，M-4 由真机验收闭环）。
- **宿主进程内三点位验证**（internal_debug_tools 实测）：patch1=CustomScrollManager / patch2=stored_summary_text wrapper / patch3=_process_compact wrapper，三个哨兵正确。
- **与 keep-context-after-compact-failure 插件叠加兼容**：`_process_compact` 调用链 = 我的 wrapper → keep-context wrapper → 真原版。语义互补：新方案失败 raise → keep-context 拦截替换为「压缩失败，上下文保持不变」友好消息（fail-fast 的显示兜底层）。两插件哨兵各指各层，恢复链独立正确。
- 待办：replace 模式下观察自动压缩稳定性（固化块已随 21:45 真机压缩验收）；manifest config 段传递机制未走通（api.config 不读 manifest config——调试期用宿主内存注入，replace 切换用默认值切换规避；如需运行时可配需调研 plugin settings 路径）。

## 关键实现事实（真机验证过）

- 三点位自检在宿主 2.3.0 全绿：`check_manager_compat()` / `check_command_compat()` / `check_compact_command_compat()` 均 `(True, 'ok')`。
- **手动 /compact 的 agent 分支链路**：`_process_compact` → `agent.compress_context` → middleware 链 → `QwenPawAgent._compress_context_impl`（react_agent.py:405-415）→ `_context_manager.compress`（scroll 实例 = patch 点 1 的子类实例）。
- **前端桥调查结论**（D5 调查，未采用）：`window.QwenPaw.chat.submit(pluginId, {query, agentId, sessionId})`（hostSdk/chatSubmit.ts，宿主 Chat 页 bindChatSubmit 绑定）；计量器弹出窗 = `turnUsagePopover`（Compact 按钮发 `/compact`，handleCompactCommand）；modules 注册表 = `src/pages/**`（React 组件 patch 不影响已 import 绑定）；slot 无 turnUsage 槽位 → DOM 注入是唯一注入姿势。
- agentscope 流式协议（`model/_base.py` `_stream()` + `_StreamAccumulator.build()`）：**增量 chunks（is_last=False）+ 流末尾累计 chunk（is_last=True，content 全量）**；中断时累计 chunk 的 finished_reason=INTERRUPTED。原版 `final or deltas` 消费逻辑与之匹配。
- `_response_text` 逐 chunk strip → deltas 兜底跨 chunk 粘连（原版固有；真实协议几乎不触发，触发时校验失败走重试自愈）。
- agentscope.message 的 `UserMsg`/`AssistantMsg` 是**工厂函数**不是类（isinstance 判断用 role）。
- `prepare_model_input(agent)` 返回 `{"messages", "tools"}`（agentscope `_agent.py:3274`）。
- 章节校验正则 `^##\s*{name}\s*[:：]?\s*$`：行首缩进标题不算命中（4+ 空格缩进 ## 在 Markdown 渲染退化为代码块，拒绝+重试修正）——测试用例曾写错已修正。
- 插件加载机制：`python_backend.import_definition` → 模块名 `plugin_<id 下划线化>` + `submodule_search_locations` → 相对 import 可用；模块级 `plugin` 对象导出。

## 测试基建

- `tests/conftest.py`：宿主同款加载姿势（spec_from_file_location + __package__/__path__），sys.path 快照恢复。
- fake manager（FakeManager）复刻 5 个被消费接口；fake agent 提供 `_prepare_model_input` + 可记录调用的 model。
- `tests/test_boundary_marker.py`：压缩边界 marker 4 用例（驱逐末尾插/恢复不插/AssistantMsg+HintBlock 非 synthetic/无 active turn 边界），`object.__new__` 绕过宿主 `__init__` 只造 `_rebuild_context` 消费的状态（`_compactor_marker_pending`/`_synthetic_ids`/`_index`/`_continuation_summary`）；`msg_text` 兼容 str/text block/hint block 三形态（hint 取 `block.hint`）。
