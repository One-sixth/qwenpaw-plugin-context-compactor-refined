# CHANGELOG

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
