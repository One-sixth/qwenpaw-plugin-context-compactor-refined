# -*- coding: utf-8 -*-
"""CustomScrollManager — 只替换续写摘要生成步骤（summary_manager.py）。

继承 ``ScrollContextManager`` 全部行为（persist/trigger/pre-fold/split/
index-evict/live-fold/think-fold/active-fold + 基础设施零改动），仅覆盖：

* ``_update_continuation_summary``        —— 超时包装：超时 → raise
  （原版静默吞掉继续驱逐，设计稿 §2.3）；
* ``_update_continuation_summary_inner``  —— 核心生成循环（generation.py），
  任何失败 raise ``SummaryGenerationFailed`` → 驱逐不发生、错误上浮；
* ``load_state``                          —— free_text 摘要兼容重建
  （原类 from_dict 对 free_text dict KeyError→None，摘要会静默丢失）。

模块级配置（``set_module_config``）由插件激活时注入——宿主构造点 kwargs
原样透传（零构造逻辑复制），实例化时读取当前配置快照。

失败语义（Q3/Q4/D2 定案）：无冷却期、无静默降级、无超预算降级——重复失败
就重复返回错误信息。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from qwenpaw.agents.context.scroll.manager import ScrollContextManager

from .config import MODE_REPLACE, CompactorConfig
from .generation import (
    SummaryGenerationFailed,  # noqa: F401  re-export for host_compat/tests
    run_conversation_summary,
)
from .summary_object import FreeTextSummary

logger = logging.getLogger(__name__)

# 类标记：host_compat 用它区分「本插件已 patch 的类」与宿主原类（热重载安全）
COMPACTOR_MARKER = "qwenpaw-plugin-context-compactor-refined"

# 模块级配置快照（插件 on_activate 时注入；None = 默认值）
_module_config: CompactorConfig | None = None

# 模块级一次性触发标志（D5 shadow 模式：agent-less 分支的 standalone manager
# 在 _process_compact 调用栈内构造时读取；patch 点 3 finally 兜底复位）
_pending_new_once: bool = False


def set_module_config(config: CompactorConfig | None) -> None:
    """Set the config snapshot used by subsequently constructed managers."""
    global _module_config
    _module_config = config


def set_pending_new_once(value: bool) -> None:
    """Set/clear the one-shot ``/compact new`` flag for agent-less mode
    (the standalone manager is constructed inside the same call stack).

    Known boundary (review M-3, declared): the module-level slot is NOT
    concurrency-safe. A concurrent agent construction in another session
    during the second-level window between set() and clear() would read
    ``True`` once (wrongly entering the refined pipeline for that session),
    and a concurrent clear() would cancel the pending trigger. Accepted for
    the debugging phase: /compact new is a rare manual operation, the worst
    case is one misplaced (visible) summary run, and replace mode ignores
    the flag entirely. If it ever bites, upgrade to a generation-token slot.
    """
    global _pending_new_once
    _pending_new_once = value


class CustomScrollManager(ScrollContextManager):
    """Scroll manager; summary generation runs in shadow or replace mode.

    * ``shadow``（D5 调试期，默认）：原版逻辑 100% 透传（含原版静默失败
      语义），仅 ``/compact new`` 通过一次性 flag 触发新方案；
    * ``replace``（验收完成后）：全部压缩走新方案（v3 原语义），flag 无关。
    """

    _compactor_marker = COMPACTOR_MARKER

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._compactor_config = _module_config or CompactorConfig()
        global _pending_new_once
        self._compactor_use_new_once = _pending_new_once
        _pending_new_once = False  # 一次性：读取即清除（防泄漏到后续构造）
        self._compactor_new_active = False

    def _should_use_new_summary(self) -> bool:
        if self._compactor_config.mode == MODE_REPLACE:
            return True
        return bool(getattr(self, "_compactor_use_new_once", False))

    async def _update_continuation_summary(
        self,
        agent: Any,
        middle: list[Any],
        *,
        focus_hint: str = "",
    ) -> None:
        """Dispatch between the legacy (transparent super() call) and the
        refined summary pipeline (design doc §2.5, D5).

        Legacy mode keeps the upstream behavior verbatim, including its
        silent-failure semantics. Refined mode wraps the pipeline in a
        timeout that raises instead of swallowing."""
        use_new = self._should_use_new_summary()
        self._compactor_use_new_once = False  # 一次性消费（立即复位）
        if not use_new:
            await super()._update_continuation_summary(
                agent,
                middle,
                focus_hint=focus_hint,
            )
            return
        self._compactor_new_active = True
        try:
            async with asyncio.timeout(self._summary_timeout_seconds):
                await self._update_continuation_summary_inner(
                    agent,
                    middle,
                    focus_hint=focus_hint,
                )
        except TimeoutError as exc:
            self._summary_update_failed = True
            logger.warning(
                "context-compactor: summary generation timed out after %g "
                "seconds; aborting compression (no eviction)",
                self._summary_timeout_seconds,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            raise SummaryGenerationFailed(
                f"压缩摘要生成超时（{self._summary_timeout_seconds:g} 秒），"
                "压缩已中止（未驱逐）",
            ) from exc
        finally:
            self._compactor_new_active = False

    async def _update_continuation_summary_inner(
        self,
        agent: Any,
        middle: list[Any],
        *,
        focus_hint: str = "",
    ) -> None:
        """Refined generation loop when active; legacy fallback otherwise."""
        if not getattr(self, "_compactor_new_active", False):
            await super()._update_continuation_summary_inner(
                agent,
                middle,
                focus_hint=focus_hint,
            )
            return
        summary = await run_conversation_summary(
            self,
            agent,
            middle,
            focus_hint=focus_hint,
            config=self._compactor_config,
        )
        if summary is not None:
            self._continuation_summary = summary
            self._summary_update_failed = False

    def load_state(self, data: Any) -> None:
        """Rehydrate, then rebuild a free-text summary the upstream loader
        silently drops (its ``ContinuationSummary.from_dict`` returns None
        for free_text dicts on KeyError)."""
        super().load_state(data)
        if not isinstance(data, dict):
            return
        raw = data.get("continuation_summary")
        if isinstance(raw, dict) and raw.get("mode") == "free_text":
            self._continuation_summary = FreeTextSummary.from_dict(raw)
            if self._continuation_summary is None:
                # 畸形 free_text dict（text 空 / covered_seq 非法）：与宿主
                # 对无效 dict 的行为一致（置 None → index-only），留排查线索
                logger.debug(
                    "context-compactor: free_text summary in checkpoint "
                    "failed validation; dropping to index-only",
                )
