# CHANGELOG

## v0.2.5（2026-11-23）

- **摘要长度控制（字符域双约束）**：提问模板新增软约束「正文不可超过 15000 字符」（`prompt_max_chars` 可配）；生成后纯代码硬截断到 20000 字符（`hard_max_chars` 可配，截断只作用于模型原稿，固化块在截断后追加）
- **截断与重试闭环**：硬截断后照常跑章节校验——截断把章节标题截掉 → missing 非空 → 走既有对话内重试通道（模型看到截断稿 + 纠正消息重写）；截断点落在章节内容内部（标题全幸存）→ 直接入库（设计如此，不判失败）
- **max_tokens 语义简化（2026-11-23 定案，替代宿主公式）**：`output_tokens=0`（默认）→ 请求不带 max_tokens（端点默认）；`output_tokens>0` → 传入且 ≤32000 时抬高到 32000（思考+正文总预算），>32000 保持用户值。原宿主公式 `max(256, min(4000, ctx//4))` 删除——4000 的 cap 曾把正文挤到撞线服务端截断且截断不可察觉（finished_reason 恒 'completed'）
- **字符统计天然不含思考**：reply 只含正文文本（thinking 走 reasoning_content 通道），20000 上限只数正文；token 预算无法分离思考，故 max_tokens 给总量
- 测试 97 → 107（-1 旧公式断言 +7：默认不传 / 抬到 32000 / 超值保持 / 软约束注入 / 截断章节幸存直接入库 / 默认 20000 截断 / 截断丢章节重试救回；+4 复审修复：模板缺槽位容忍 / 未知占位符显式失败 / chars 两键 mapping 非法值回落 / 直接构造负数拦截）
- 对抗复审（无上下文 subagent，0 HIGH / 1 MEDIUM / 3 LOW）四项修复：M-1 删除 build_question 的 KeyError 回退死代码分支（实测缺 `{max_chars}` 槽位时 str.format 多余 kwargs 天然无害，回退分支永不进入；坏模板显式失败，包装为 SummaryGenerationFailed 压缩中止）；L-1 chars 两键 0/负数防御（mapping 层 skip 回落默认 + `__post_init__` 拦截）；L-2 删测试 helper 失去消费方的 context_size 参数；L-3 README 改良列表补 v0.2.5 条目
- **四路交叉复审第二轮**（代码 / 请求链 / 文档配置 / 仓库卫生，独立取证；请求链首轮因模型空答重跑）：代码路类型缺口实测属实（float 20.5 切片 TypeError / bool=True 静默截 1 字符 / str 裸 TypeError）→ `__post_init__` 改类型+值域双检统一显式 ValueError；文档路 M-1 属实 → README 明确「嵌套段存在时平铺键全部忽略（整体替换非逐键合并）」；M-2 属实 → README 补 settings 入口（工作区 config.json / 插件设置页）与生效方式；LOW 三项：DESIGN §5 旧公式就地标注 v0.2.5 取代、小窝 INDEX 条目刷新、知识库 Scroll 文档沉淀截断不可察觉方法论。仓库卫生路 APPROVE（19 文件 zip 与装机目录 sha256 全同、名称扫描 0 命中、无临时产物）；测试数仍 107（非法值用例就地扩展）
- **请求链复审（wire 实测）发现两项语义偏差 → 文档修正、行为不变**（尊重「其他参数用原来的」「没传就不传」拍板）：① `output_tokens=0` 插件确实不传，但宿主 compat 层会注入模型卡 `generate_kwargs.max_tokens`（实测 qwen3.8-27b=33000 / glm-5.3-flash=36000），故「端点默认」措辞不准——README 改为如实描述并标注「模型卡若改小则长摘要可能被服务端截断」的条件性风险；② `disable_thinking=True` 经 compat 翻译对 qwen/glm 实际无效（thinking kind=unknown/effort 无 off 支持，wire 无思考控制参数）——README 改为「尽力禁思考」。传 32000 时 wire 单一参数、正确覆盖模型卡、无双参冲突；字符统计不含 thinking 均实测通过
- plugin.json 版本号 0.2.4 → 0.2.5

## v0.2.4（2026-11-22）

- 压缩边界 marker：压缩驱逐后在保留轮之后注入一条边界提示，让 LLM 区分「上方回合拥有全量上下文、下方回合需经摘要/召回」，修掉模型误以为上一轮基于摘要跑的误区
- marker 借 QwenPaw HintBlock 机制实现「LLM 可见、UI 不显示」：`AssistantMsg(name="compaction_boundary", content=[HintBlock(hint=文案, source="compaction_boundary")])`——agentscope formatter 把 HintBlock 转独立 user 消息（LLM 看到），console/PawApp 的 history converter 跳过 hint block（UI 不显示）。UserMsg/SystemMsg 拒 hint block，必须 AssistantMsg 承载
- 文案前加 `[系统提示：压缩边界]` 标识（原 `[压缩边界]`），让 LLM 一眼认出是系统注入的边界标记而非普通 user 消息（真机验证发现 LLM 被 placeholder 的压缩元信息淹没、没注意到原 marker）
- marker 位置在保留轮之后（tail 末尾），不用 `_active_turn_tail` 切分（单轮陷阱：唯一那轮既是保留轮又是 active turn，按 active turn 前插会把 marker 挤到最前、语义反掉）
- 不进 `_synthetic_ids`、不打 tag，随正常驱逐消失只标最近一次边界；flag 分流精确区分驱逐路径（插）与恢复路径（不插），abort 残留由 load_state 兜底清零
- 文案去掉「seq 连续不重置」——seq 跳跃且跨会话，诱导模型续号会召回到别的会话的被驱逐上下文
- 测试 93 → 97（+4：驱逐末尾插/恢复不插/AssistantMsg+HintBlock 非 synthetic/无 active turn 边界）
- plugin.json 版本号 0.2.2 → 0.2.4；README 补 marker 功能说明与测试数


## v0.2.2（2026-11-18）

- patch 点 3（`/compact new` 一次性触发）改为 **shadow 模式专属**：`replace` 模式（默认）下不再安装该拦截，`/compact new` 回归宿主原生行为；`shadow` 模式行为不变（安装 + 一次性对照）
- install 状态新增 `skipped(mode=replace)` 取值；uninstall 对未打点位幂等跳过（原有哨兵机制天然支持）
- README 开头新增「如何使用」小节（生效场景 + 查看摘要方式）；「三处补强」章节改为按模式精准描述
- 测试 91 → 93（+1：replace 模式跳过 patch 3；+1：shadow 残留就地恢复）

## v0.2.1（2026-11-18）

- 摘要末端固化 recall 指引块：成功出口纯代码追加「压缩完成时间戳（当地时间 + UTC 偏移）+ recall_history 三件套即用指令（expand 含 covered 累积区间 / search / recall_tool）」
- covered 异常（倒挂 / 非法值）降级通用指引模板，绝不抛——固化块是增强层，不影响压缩成败
- 追加在章节校验通过之后：校验针对模型原稿，重试对话中的失败稿不含固化块
- 测试 84 → 87（+3：固化块 seq 指令 / 倒挂降级 / 重试对话无固化块）；终审修复后 87 → 91（+4：stamp 失败降级 / `_resolve_config` 三分支）
- 真机验收通过：压缩注入形态末尾固化块完整落地，时间戳 = 压缩时刻同秒，covered 区间与上段摘要无缝累积衔接

## v0.2.0（2026-11-18）

- mode 默认值切换为 `replace`：全部压缩（自动 / `/compact` / `/compact new`）走新方案
- debug_dump 随切换自动关闭（默认 false）
- 验收依据：中文摘要真机三会话 + `/compact_str` 显示 + 卸载恢复 + wire 字节对齐证死（OpenAI 兼容 formatter 732,678 字节 / VolcengineFormatter 315,926 字节，均数学闭合）+ keep-context 插件叠加兼容

## v0.1.4（2026-11-18）

- formatter async 调用修复（wire-dump 路径 coroutine 未 await）

## v0.1.3（2026-11-18）

- wire-dump 调试钩子（debug_dump）：formatter 层 wire 字节比对 + usage 全量 dump，用于前缀缓存对齐取证

## v0.1.2（2026-11-18）

- 实现稿对抗复审修复（M-1 / M-2），热装上线

## v0.1.1（2026-11-18）

- D5 双模式（shadow / replace）+ patch 点 3（`/compact new` 一次性触发：严格匹配 + args 置空 + flag 双路径 + finally 兜底）

## v0.1.0（2026-11-18）

- Phase 1 骨架：三点位 patch（manager 类 / `/compact_str` / `_process_compact`）+ 真实对话续写生成 + 中文章节校验 + 对话内重试 + 失败中止驱逐
