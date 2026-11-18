# -*- coding: utf-8 -*-
"""FreeTextSummary — 自由文本压缩摘要适配器（summary_object.py）。

替代宿主结构化 ``ContinuationSummary``（dataclass，每条带 source）：
模型正文原样保存，实现 scroll 管线与 agent-less ``/compact_str`` 所消费
的 4 个下游接口（设计稿 §3.3，17 个使用点三路核实）：

* ``render()``               —— describe_summary / patch 点 2
* ``render_background(...)`` —— _rebuild_context（memory placeholder 注入）
* ``to_dict()``              —— manager.to_dict（会话快照）
* ``covered_seq`` 属性        —— 压缩 span 合并 / retention purge 防御

``from_dict`` 与结构化 v1 共存：快照 dict 带 ``mode == "free_text"`` 标记
（version 2），原版 ``load_state`` 对 free_text dict 走
``ContinuationSummary.from_dict`` 会 KeyError → None（摘要静默丢失），
因此 CustomScrollManager.load_state 覆写负责重建本类型（设计稿 §2.3）。

卸载兼容（R3-发现5）：插件卸载恢复原类后，旧会话的 free_text 摘要被原类
``load_state`` 静默丢弃 → 降级 index-only（不崩溃、recall 可用、
/compact_str 显示 unavailable）。
"""

from __future__ import annotations

from typing import Any

SUMMARY_MODE = "free_text"
FREE_TEXT_MODE = SUMMARY_MODE  # alias for host_compat readability
SUMMARY_VERSION = 2


class FreeTextSummary:
    """A free-text continuation summary produced by conversation continuation."""

    __slots__ = ("text", "covered_seq")

    def __init__(self, text: str, covered_seq: tuple[int, int]) -> None:
        self.text = text
        self.covered_seq = (int(covered_seq[0]), int(covered_seq[1]))

    def render(self) -> str:
        """The summary body as-is (deterministic Markdown)."""
        return self.text

    def render_background(
        self,
        *,
        stale: bool = False,
        include_envelope: bool = True,
    ) -> str:
        """Render model background, optionally without ``system-info`` tags.

        Mirrors the upstream ``ContinuationSummary.render_background`` shape
        (SUMMARY_PREFIX + stale marker + durable-history pointer + body).
        The ``stale`` branch is dead code under the new fail-fast semantics
        (a failed update raises instead of rebuilding), kept for interface
        compatibility (design doc §3.3 / R3-发现4).
        """
        from qwenpaw.agents.context.scroll.continuation_summary import (
            SUMMARY_PREFIX,
        )

        state = (
            "\nSummary status: stale because the latest update failed."
            if stale
            else ""
        )
        lo, hi = self.covered_seq
        history = (
            "Exact archived content remains in conversation_history for\n"
            f"sequence range {lo}–{hi}. Recall that range only when exact\n"
            "wording or evidence is needed."
        )
        body = f"{SUMMARY_PREFIX}{state}\n{history}\n\n{self.render()}"
        return (
            f"<system-info>\n{body}\n</system-info>"
            if include_envelope
            else body
        )

    def to_dict(self) -> dict[str, Any]:
        """Snapshot form; coexists with structured v1 via the mode tag."""
        return {
            "mode": SUMMARY_MODE,
            "version": SUMMARY_VERSION,
            "covered_seq": [self.covered_seq[0], self.covered_seq[1]],
            "text": self.text,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FreeTextSummary | None":
        """Rebuild from :meth:`to_dict` output; ``None`` for anything that is
        not a valid free-text summary dict (mirrors upstream tolerance)."""
        if not isinstance(data, dict) or data.get("mode") != SUMMARY_MODE:
            return None
        covered = data.get("covered_seq")
        if not isinstance(covered, (list, tuple)) or len(covered) != 2:
            return None
        try:
            lo, hi = int(covered[0]), int(covered[1])
        except (TypeError, ValueError):
            return None
        if lo > hi:
            return None
        text = str(data.get("text") or "").strip()
        if not text:
            return None
        return cls(text, (lo, hi))
