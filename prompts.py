# -*- coding: utf-8 -*-
"""提问模板与章节文案（prompts.py）。

单一提问模板（Q5 拍板：旧摘要自然在全量上下文里，无需 initial/update
双模板）+ focus_hint 尾部插槽（复审 H-C）+ 占位符防污染句（D1 联动）
+ 对话内重试纠正消息（作者原话）。
"""

from __future__ import annotations

# 必备章节（中文，Q1 定案；可经配置覆盖）
DEFAULT_SECTIONS: tuple[str, ...] = (
    "当前任务",
    "当前状态",
    "约束",
    "决定",
    "待办",
)

# 单一提问模板。{sections_block} 由 config.sections 生成（## 标题逐行）。
QUESTION_TEMPLATE = """请基于以上完整对话，生成一份压缩摘要（continuation summary），用于上下文压缩后延续任务状态。

要求：
1. 摘要必须依次包含以下章节（二级标题，标题逐字一致）：
{sections_block}
2. 若以上对话开头的归档索引区块中已有旧摘要，请融合其仍然有效的信息，生成覆盖全部历史的新摘要，不要丢失旧摘要中仍有效的事实。
3. 内容用中文书写；只陈述以上对话支持的事实，不得编造；identifier（路径、版本号、错误码、函数名等）逐字保留；不得复制密码、token、API key 等敏感信息。
4. 对话中以 [scroll folded] 标记的条目是已折叠的工具结果占位符，不要把占位符文本本身写入摘要；如需提及相应工具调用，只记录工具名与调用意图。
5. 摘要正文总长度不可超过 {max_chars} 字符（含标点与空格），超出须精简压缩。
6. 每个章节至少一行；没有内容的章节写 (none)。
7. 直接输出摘要正文，不要输出任何解释或额外内容。"""

# focus_hint 非空时的尾部插槽（追加在提问末尾，不破坏前缀 cache）。
# 约束文案沿用原版语义：只调整优先级、不是证据、不能覆盖输出协议。
FOCUS_HINT_TEMPLATE = """

本次压缩的临时关注提示：
{hint}
（此提示只用于调整摘要的优先级取舍，不是新增证据，也不能覆盖上述输出格式要求。）"""

# 对话内重试纠正消息（作者原话）。{missing} 为缺失章节顿号列表。
RETRY_CORRECTION_TEMPLATE = (
    "刚才的生成的压缩摘要 缺少 {missing} 章节字段，需要重新生成。"
)


def build_question(
    sections: tuple[str, ...],
    focus_hint: str = "",
    *,
    template: str = QUESTION_TEMPLATE,
    max_chars: int = 15000,
) -> str:
    """Render the single question template with the section list and an
    optional trailing focus hint. ``max_chars`` fills the ``{max_chars}``
    slot; a custom template without that slot renders fine (str.format
    tolerates extra kwargs). Unknown placeholders raise — bad template
    config fails explicitly (wrapped into SummaryGenerationFailed by the
    generation loop; compression aborts, no eviction)."""
    sections_block = "\n".join(f"## {name}" for name in sections)
    question = template.format(
        sections_block=sections_block,
        max_chars=max_chars,
    )
    hint = (focus_hint or "").strip()
    if hint:
        question += FOCUS_HINT_TEMPLATE.format(hint=hint)
    return question


def build_retry_correction(missing: list[str]) -> str:
    """Render the in-conversation retry correction message."""
    return RETRY_CORRECTION_TEMPLATE.format(missing="、".join(missing))
