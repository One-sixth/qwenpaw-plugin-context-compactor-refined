# -*- coding: utf-8 -*-
"""三 patch 点注入与恢复（host_compat.py）。

**patch 点 1（压缩行为）**：``ScrollContextManager`` 的全部构造点都是
函数体内调用时 import（context/__init__.py:187 + command_handler.py:659，
R1 核实仅此两处）→ patch 模块属性后宿主构造代码自然用子类实例化，
/compact / 压缩工具 / 自动压缩全链路生效。

**patch 点 2（/compact_str 显示，D4）**：``CommandHandler._stored_
scroll_summary_text`` 是实例方法、经 ``self.`` 调用 → 类属性查找 →
类级 patch 生效；free_text 分支渲染，其余回落原方法。

**幂等 + 版本自检**（三层查找面，复审 M2）：
* 哨兵挂在宿主侧（manager 模块属性 / CommandHandler 类属性）——插件
  模块热重载后哨兵不丢，恢复方向永远正确；
* 已 patch 检测用 ``_compactor_marker`` 标记（跨热重载的新旧类对象）；
* 点位独立降级：点 2 缺失仅 /compact_str 退化，不阻塞点 1；全部记
  warning，绝不阻塞宿主。

**热重载窗口**（R3-发现6）：unload 先恢复原类、activate 再 patch；窗口期
新构造 manager 一次性 vanilla 降级（功能正确，无数据损坏）；in-flight
实例绑定旧类不受影响。
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

from . import summary_manager
from .config import MODE_REPLACE, CompactorConfig
from .summary_object import FREE_TEXT_MODE, FreeTextSummary

logger = logging.getLogger(__name__)

COMPACTOR_MARKER = summary_manager.COMPACTOR_MARKER

# 宿主侧哨兵属性名（恢复方向锚点；卸载时删除）
_SENTINEL_MANAGER_ATTR = "_compactor_original_manager_cls"
_SENTINEL_CMD_ATTR = "_compactor_original_stored_summary_text"
_SENTINEL_COMPACT_ATTR = "_compactor_original_process_compact"

# patch 点 1 自检清单：生成循环直接依赖的 manager 接口 + 被 override 的入口
REQUIRED_MANAGER_METHODS: tuple[str, ...] = (
    "_update_continuation_summary",
    "_update_continuation_summary_inner",
    "_evicted_span",
    "_response_text",
    "_raise_if_summary_interrupted",
    "_summary_focus_hint",
    "_source_backed_previous_summary",
)

REQUIRED_CONTINUATION_SUMMARY_ATTRS: tuple[str, ...] = ("SUMMARY_PREFIX",)


def is_patched_manager_cls(cls: Any) -> bool:
    """Whether ``cls`` is a manager class installed by this plugin."""
    return inspect.isclass(cls) and getattr(cls, "_compactor_marker", None) == COMPACTOR_MARKER


def is_patched_command_func(func: Any) -> bool:
    """Whether ``func`` is a wrapper installed by this plugin."""
    return callable(func) and getattr(func, "_compactor_marker", None) == COMPACTOR_MARKER


# ---------------------------------------------------------------------------
# 版本自检（三层查找面，不可一把查——复审 M2）
# ---------------------------------------------------------------------------

def check_manager_compat() -> tuple[bool, str]:
    """Patch point 1 compatibility: class methods + constructor signature +
    module-level objects (SUMMARY_PREFIX lives in the continuation_summary
    module, NOT the manager namespace)."""
    try:
        from qwenpaw.agents.context.scroll import (
            continuation_summary as cs_mod,
        )
        from qwenpaw.agents.context.scroll import manager as manager_mod
    except Exception as exc:  # pragma: no cover - host broken anyway
        return False, f"scroll modules import failed: {exc!r}"

    cls = getattr(manager_mod, "ScrollContextManager", None)
    if not inspect.isclass(cls):
        return False, "manager module has no ScrollContextManager class"
    if is_patched_manager_cls(cls):
        # 已是本插件子类（热重载残留）——结构自检无意义，直接放行
        return True, "already patched by this plugin"

    missing = [name for name in REQUIRED_MANAGER_METHODS if not hasattr(cls, name)]
    if missing:
        return False, f"missing ScrollContextManager methods: {missing}"

    try:
        params = inspect.signature(cls.__init__).parameters
    except (TypeError, ValueError) as exc:
        return False, f"cannot inspect __init__ signature: {exc!r}"
    if "summary_timeout_seconds" not in params:
        return False, (
            "ScrollContextManager.__init__ has no summary_timeout_seconds "
            "parameter (instance attribute _summary_timeout_seconds is "
            "assigned from it)"
        )

    if not hasattr(manager_mod, "ContinuationSummary"):
        return False, "manager module has no ContinuationSummary"
    for name in REQUIRED_CONTINUATION_SUMMARY_ATTRS:
        if not hasattr(cs_mod, name):
            return False, f"continuation_summary module has no {name}"
    return True, "ok"


def check_command_compat() -> tuple[bool, str]:
    """Patch point 2 compatibility (independent degrade from point 1)."""
    try:
        from qwenpaw.agents.command_handler import CommandHandler
    except Exception as exc:
        return False, f"command_handler import failed: {exc!r}"
    if not hasattr(CommandHandler, "_stored_scroll_summary_text"):
        return False, "CommandHandler has no _stored_scroll_summary_text"
    return True, "ok"


def check_compact_command_compat() -> tuple[bool, str]:
    """Patch point 3 compatibility: the ``/compact new`` dispatcher (D5)."""
    try:
        from qwenpaw.agents.command_handler import CommandHandler
    except Exception as exc:
        return False, f"command_handler import failed: {exc!r}"
    if not hasattr(CommandHandler, "_process_compact"):
        return False, "CommandHandler has no _process_compact"
    return True, "ok"


# ---------------------------------------------------------------------------
# patch 点 2 的包装函数
# ---------------------------------------------------------------------------

def make_stored_summary_wrapper(
    original: Callable[[Any], str],
) -> Callable[[Any], str]:
    """Wrap ``CommandHandler._stored_scroll_summary_text`` (D4).

    Three branches (design doc §2.1): live agent → original verbatim;
    agent-less + free_text snapshot dict → FreeTextSummary rendering;
    anything else → original (structured summaries keep upstream behavior).
    """

    def patched(self: Any) -> str:
        if self._agent is not None:
            return original(self)
        state = getattr(self, "_scroll_state", None)
        raw = (
            state.get("continuation_summary")
            if isinstance(state, dict)
            else None
        )
        if isinstance(raw, dict) and raw.get("mode") == FREE_TEXT_MODE:
            summary = FreeTextSummary.from_dict(raw)
            return summary.render() if summary is not None else ""
        return original(self)

    patched._compactor_marker = COMPACTOR_MARKER  # type: ignore[attr-defined]
    patched.__name__ = getattr(original, "__name__", "_stored_scroll_summary_text")
    patched.__qualname__ = getattr(original, "__qualname__", patched.__name__)
    patched.__doc__ = getattr(original, "__doc__", None)
    return patched


# ---------------------------------------------------------------------------
# patch 点 3 的包装函数（D5：/compact new 一次性触发）
# ---------------------------------------------------------------------------

NEW_KEYWORD = "new"


def make_compact_command_wrapper(
    original: Callable[..., Any],
) -> Callable[..., Any]:
    """Wrap ``CommandHandler._process_compact`` (D5 shadow mode).

    ``args.strip() == "new"`` → route this one compression through the
    refined pipeline: set the one-shot flag on the live agent's context
    manager (marker-checked) or, for the agent-less standalone manager
    (constructed inside the same call stack), on the module-level pending
    slot. ``args`` is then blanked so "new" does not leak into the hint
    path. Any other args keep the upstream hint semantics verbatim.

    The flag is one-shot: the manager consumes it at the first summary
    update; the ``finally`` below only backstops the case where compression
    fails before reaching that step (never leaks into auto-compaction).
    """

    async def patched(self: Any, messages: Any, args: str = "") -> Any:
        if isinstance(args, str) and args.strip() == NEW_KEYWORD:
            cm = (
                getattr(self._agent, "_context_manager", None)
                if self._agent is not None
                else None
            )
            marker_ok = (
                cm is not None
                and getattr(cm, "_compactor_marker", None) == COMPACTOR_MARKER
            )
            if marker_ok:
                cm._compactor_use_new_once = True
            else:
                summary_manager.set_pending_new_once(True)
            try:
                return await original(self, messages, "")
            finally:
                summary_manager.set_pending_new_once(False)
                if marker_ok:
                    cm._compactor_use_new_once = False
        return await original(self, messages, args)

    patched._compactor_marker = COMPACTOR_MARKER  # type: ignore[attr-defined]
    patched.__name__ = getattr(original, "__name__", "_process_compact")
    patched.__qualname__ = getattr(original, "__qualname__", patched.__name__)
    patched.__doc__ = getattr(original, "__doc__", None)
    return patched


# ---------------------------------------------------------------------------
# install / uninstall（幂等 + 逆序恢复 + 点位独立降级）
# ---------------------------------------------------------------------------

def install(config: CompactorConfig | None = None) -> dict[str, str]:
    """Install patch points per config.mode (point 3 only in shadow mode,
    D7). Never raises into the host: per-point failures degrade to
    warnings. Returns a per-point status dict."""
    if config is None:
        config = CompactorConfig()
    status = {
        "patch1": "skipped",
        "patch2": "skipped",
        "patch3": "skipped",
    }

    # ---- patch point 1: ScrollContextManager class (module attribute) ----
    try:
        from qwenpaw.agents.context.scroll import manager as manager_mod
    except Exception as exc:
        logger.warning(
            "context-compactor: patch point 1 unavailable (%r)", exc,
        )
    else:
        current = getattr(manager_mod, "ScrollContextManager", None)
        if is_patched_manager_cls(current):
            summary_manager.set_module_config(config)
            status["patch1"] = "already"
        else:
            ok, detail = check_manager_compat()
            if ok:
                sentinel = getattr(manager_mod, _SENTINEL_MANAGER_ATTR, None)
                if sentinel is None:
                    setattr(manager_mod, _SENTINEL_MANAGER_ATTR, current)
                summary_manager.set_module_config(config)
                manager_mod.ScrollContextManager = summary_manager.CustomScrollManager
                status["patch1"] = "installed"
                logger.info(
                    "context-compactor: patch point 1 installed "
                    "(mode=%s, cache_mode=%s)",
                    config.mode,
                    config.cache_mode,
                )
            else:
                logger.warning(
                    "context-compactor: patch point 1 skipped: %s", detail,
                )

    # ---- patch point 2: CommandHandler._stored_scroll_summary_text (D4) ----
    try:
        from qwenpaw.agents.command_handler import CommandHandler
    except Exception as exc:
        logger.warning(
            "context-compactor: patch point 2 unavailable (%r)", exc,
        )
    else:
        current = getattr(CommandHandler, "_stored_scroll_summary_text", None)
        if is_patched_command_func(current):
            status["patch2"] = "already"
        else:
            ok, detail = check_command_compat()
            if ok:
                sentinel = getattr(CommandHandler, _SENTINEL_CMD_ATTR, None)
                if sentinel is None:
                    setattr(CommandHandler, _SENTINEL_CMD_ATTR, current)
                wrapper = make_stored_summary_wrapper(current)
                CommandHandler._stored_scroll_summary_text = wrapper
                status["patch2"] = "installed"
                logger.info(
                    "context-compactor: patch point 2 installed "
                    "(/compact_str free-text rendering)",
                )
            else:
                logger.warning(
                    "context-compactor: patch point 2 skipped: %s", detail,
                )

    # ---- patch point 3: CommandHandler._process_compact (D5/D7) ----
    # shadow 专属：/compact new 一次性触发新管线。replace（默认）不安装
    # ——/compact new 回归宿主原生行为；若此前 shadow 残留 wrapper，就地
    # 恢复（诊断面与实际行为保持一致）。
    try:
        from qwenpaw.agents.command_handler import CommandHandler
    except Exception as exc:
        logger.warning(
            "context-compactor: patch point 3 unavailable (%r)", exc,
        )
    else:
        current = getattr(CommandHandler, "_process_compact", None)
        if is_patched_command_func(current):
            if config.mode == MODE_REPLACE:
                # shadow 残留：就地恢复原方法，不留静默拦截
                original = getattr(
                    CommandHandler, _SENTINEL_COMPACT_ATTR, None,
                )
                if original is not None:
                    CommandHandler._process_compact = original
                    status["patch3"] = "residual-restored"
                    if hasattr(CommandHandler, _SENTINEL_COMPACT_ATTR):
                        delattr(CommandHandler, _SENTINEL_COMPACT_ATTR)
                    logger.warning(
                        "context-compactor: patch point 3 residual wrapper "
                        "restored (mode=replace)",
                    )
                else:
                    status["patch3"] = "residual-active"
                    logger.warning(
                        "context-compactor: patch point 3 residual wrapper "
                        "active but sentinel missing (mode=replace)",
                    )
            else:
                status["patch3"] = "already"
        elif config.mode == MODE_REPLACE:
            status["patch3"] = "skipped(mode=replace)"
            logger.info(
                "context-compactor: patch point 3 not installed "
                "(mode=replace; /compact new stays with upstream behavior)",
            )
        else:
            ok, detail = check_compact_command_compat()
            if ok:
                sentinel = getattr(CommandHandler, _SENTINEL_COMPACT_ATTR, None)
                if sentinel is None:
                    setattr(CommandHandler, _SENTINEL_COMPACT_ATTR, current)
                wrapper = make_compact_command_wrapper(current)
                CommandHandler._process_compact = wrapper
                status["patch3"] = "installed"
                logger.info(
                    "context-compactor: patch point 3 installed "
                    "(/compact new one-shot trigger, mode=%s)",
                    config.mode,
                )
            else:
                logger.warning(
                    "context-compactor: patch point 3 skipped: %s", detail,
                )

    return status


def uninstall() -> dict[str, str]:
    """Restore all patch points (reverse order: 3, 2, 1). Never raises
    into the host."""
    status = {
        "patch1": "untouched",
        "patch2": "untouched",
        "patch3": "untouched",
    }

    # ---- patch point 3 first (reverse installation order) ----
    try:
        from qwenpaw.agents.command_handler import CommandHandler
    except Exception as exc:  # pragma: no cover
        logger.warning("context-compactor: uninstall point 3 error (%r)", exc)
    else:
        current = getattr(CommandHandler, "_process_compact", None)
        original = getattr(CommandHandler, _SENTINEL_COMPACT_ATTR, None)
        if is_patched_command_func(current):
            if original is not None:
                CommandHandler._process_compact = original
                status["patch3"] = "restored"
            else:
                status["patch3"] = "sentinel-missing"
            if hasattr(CommandHandler, _SENTINEL_COMPACT_ATTR):
                delattr(CommandHandler, _SENTINEL_COMPACT_ATTR)
        elif hasattr(CommandHandler, _SENTINEL_COMPACT_ATTR):
            delattr(CommandHandler, _SENTINEL_COMPACT_ATTR)

    # ---- patch point 2 ----
    try:
        from qwenpaw.agents.command_handler import CommandHandler
    except Exception as exc:  # pragma: no cover
        logger.warning("context-compactor: uninstall point 2 error (%r)", exc)
    else:
        current = getattr(CommandHandler, "_stored_scroll_summary_text", None)
        original = getattr(CommandHandler, _SENTINEL_CMD_ATTR, None)
        if is_patched_command_func(current):
            if original is not None:
                CommandHandler._stored_scroll_summary_text = original
                status["patch2"] = "restored"
            else:
                # 哨兵丢失（理论不可能）：宁可留着包装也不置 None
                status["patch2"] = "sentinel-missing"
            if hasattr(CommandHandler, _SENTINEL_CMD_ATTR):
                delattr(CommandHandler, _SENTINEL_CMD_ATTR)
        elif hasattr(CommandHandler, _SENTINEL_CMD_ATTR):
            delattr(CommandHandler, _SENTINEL_CMD_ATTR)

    # ---- patch point 1 ----
    try:
        from qwenpaw.agents.context.scroll import manager as manager_mod
    except Exception as exc:  # pragma: no cover
        logger.warning("context-compactor: uninstall point 1 error (%r)", exc)
    else:
        current = getattr(manager_mod, "ScrollContextManager", None)
        original = getattr(manager_mod, _SENTINEL_MANAGER_ATTR, None)
        if is_patched_manager_cls(current):
            if original is not None:
                manager_mod.ScrollContextManager = original
                status["patch1"] = "restored"
            else:
                status["patch1"] = "sentinel-missing"
            if hasattr(manager_mod, _SENTINEL_MANAGER_ATTR):
                delattr(manager_mod, _SENTINEL_MANAGER_ATTR)
        elif hasattr(manager_mod, _SENTINEL_MANAGER_ATTR):
            delattr(manager_mod, _SENTINEL_MANAGER_ATTR)

    logger.info("context-compactor: uninstall complete (%s)", status)
    return status
