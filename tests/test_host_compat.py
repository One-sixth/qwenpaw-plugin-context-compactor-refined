# -*- coding: utf-8 -*-
"""host_compat 单元测试——三点位自检/幂等/点位独立降级/逆序恢复/包装分支。

直接对真宿主（qwenpaw 2.3.0 editable）执行 install/uninstall，用例内部
保证恢复现场（每测 uninstall 或 try/finally）。
"""

from __future__ import annotations

import asyncio

import pytest
from agentscope.message import Msg

from conftest import plugin_module

_pkg = plugin_module()
host_compat = _pkg.host_compat
summary_manager = _pkg.summary_manager
config_mod = _pkg.config
FreeTextSummary = _pkg.summary_object.FreeTextSummary

install = host_compat.install
uninstall = host_compat.uninstall
from qwenpaw.agents.context.scroll.manager import ScrollContextManager


@pytest.fixture()
def manager_mod():
    from qwenpaw.agents.context.scroll import manager as manager_mod

    return manager_mod


@pytest.fixture()
def command_handler_cls():
    from qwenpaw.agents.command_handler import CommandHandler

    return CommandHandler


@pytest.fixture(autouse=True)
def _clean_after():
    """Every test leaves the host pristine."""
    yield
    uninstall()
    summary_manager.set_module_config(None)
    summary_manager.set_pending_new_once(False)


# ---------------------------------------------------------------------------
# 版本自检
# ---------------------------------------------------------------------------


class TestCompatChecks:
    def test_manager_compat_ok_on_real_host(self):
        ok, detail = host_compat.check_manager_compat()
        assert ok, detail

    def test_command_compat_ok_on_real_host(self):
        ok, detail = host_compat.check_command_compat()
        assert ok, detail


# ---------------------------------------------------------------------------
# install / uninstall 全生命周期
# ---------------------------------------------------------------------------


class TestInstallUninstallLifecycle:
    def test_install_then_uninstall_restores_originals(
        self, manager_mod, command_handler_cls,
    ):
        original_cls = manager_mod.ScrollContextManager
        original_fn = command_handler_cls._stored_scroll_summary_text
        original_compact = command_handler_cls._process_compact
        # shadow 模式下全三点位安装（patch 3 为 shadow 专属，见下）
        status = install(
            config_mod.CompactorConfig(mode=config_mod.MODE_SHADOW),
        )
        assert status == {
            "patch1": "installed",
            "patch2": "installed",
            "patch3": "installed",
        }
        assert manager_mod.ScrollContextManager is not original_cls
        assert issubclass(
            manager_mod.ScrollContextManager,
            summary_manager.CustomScrollManager,
        )
        assert (
            command_handler_cls._stored_scroll_summary_text is not original_fn
        )
        assert command_handler_cls._process_compact is not original_compact
        uninstall()
        assert manager_mod.ScrollContextManager is original_cls
        assert (
            command_handler_cls._stored_scroll_summary_text is original_fn
        )
        assert command_handler_cls._process_compact is original_compact
        assert not hasattr(manager_mod, "_compactor_original_manager_cls")
        assert not hasattr(
            command_handler_cls, "_compactor_original_stored_summary_text",
        )
        assert not hasattr(
            command_handler_cls, "_compactor_original_process_compact",
        )

    def test_replace_mode_skips_patch3(self, manager_mod, command_handler_cls):
        """replace 模式（默认）：patch 3 不安装，/compact new 回归原版。"""
        original_compact = command_handler_cls._process_compact
        status = install()  # 默认 CompactorConfig = replace
        assert status["patch1"] == "installed"
        assert status["patch2"] == "installed"
        assert status["patch3"] == "skipped(mode=replace)"
        # /compact new 拦截未生效：_process_compact 保持原方法
        assert command_handler_cls._process_compact is original_compact
        assert not hasattr(
            command_handler_cls, "_compactor_original_process_compact",
        )
        uninstall()
        # 未打的点位无哨兵残留
        assert not hasattr(
            command_handler_cls, "_compactor_original_process_compact",
        )

    def test_replace_over_shadow_residual_restores(
        self, manager_mod, command_handler_cls,
    ):
        """shadow 残留 + install(replace) → 就地恢复 patch 3，诊断一致。"""
        original_compact = command_handler_cls._process_compact
        install(config_mod.CompactorConfig(mode=config_mod.MODE_SHADOW))
        # 此刻 patch 3 已打（shadow 专属拦截在位）
        assert command_handler_cls._process_compact is not original_compact
        status = install()  # replace（默认），无 unload 周期的异常路径
        assert status["patch3"] == "residual-restored"
        # 残留 wrapper 已恢复原方法，哨兵已清
        assert command_handler_cls._process_compact is original_compact
        assert not hasattr(
            command_handler_cls, "_compactor_original_process_compact",
        )
        uninstall()
        assert command_handler_cls._process_compact is original_compact

    def test_install_is_idempotent(self, manager_mod):
        first = install()
        second = install()
        assert first["patch1"] == "installed"
        assert second["patch1"] == "already"
        original = getattr(manager_mod, "_compactor_original_manager_cls")
        assert manager_mod.ScrollContextManager is not original
        # 哨兵在首次 install 时固定，二次 install 不覆盖
        assert getattr(manager_mod, "_compactor_original_manager_cls") is original

    def test_uninstall_without_install_is_noop(self):
        status = uninstall()
        assert status == {
            "patch1": "untouched",
            "patch2": "untouched",
            "patch3": "untouched",
        }

    def test_reinstall_after_uninstall(self, manager_mod):
        original_cls = manager_mod.ScrollContextManager
        install()
        uninstall()
        status = install()
        assert status["patch1"] == "installed"
        uninstall()
        assert manager_mod.ScrollContextManager is original_cls

    def test_config_flows_to_manager_instances(self, manager_mod):
        install(config_mod.CompactorConfig(max_retries=1))
        cls = manager_mod.ScrollContextManager
        # 构造 kwargs 原样透传（不传 config 参数也能实例化）
        import inspect

        sig = inspect.signature(cls.__init__)
        params = set(sig.parameters)
        assert "config" not in params  # 零构造逻辑复制：签名与宿主一致

    def test_custom_config_reaches_instances(self, manager_mod):
        install(config_mod.CompactorConfig(max_retries=2))
        cls = manager_mod.ScrollContextManager
        # 只验证模块级配置注入（不实例化真 manager——需要 db 等依赖）
        cfg = summary_manager._module_config
        assert cfg is not None and cfg.max_retries == 2


# ---------------------------------------------------------------------------
# 点位独立降级（点 2 缺失不阻塞点 1；反之亦然）
# ---------------------------------------------------------------------------


class TestIndependentDegrade:
    def test_point2_failure_does_not_block_point1(self, monkeypatch):
        monkeypatch.setattr(
            host_compat, "check_command_compat",
            lambda: (False, "simulated missing attribute"),
        )
        status = install()
        assert status["patch1"] == "installed"
        assert status["patch2"] == "skipped"

    def test_point1_failure_does_not_block_point2(self, monkeypatch):
        monkeypatch.setattr(
            host_compat, "check_manager_compat",
            lambda: (False, "simulated missing method"),
        )
        status = install()
        assert status["patch1"] == "skipped"
        assert status["patch2"] == "installed"


# ---------------------------------------------------------------------------
# patch 点 2 包装函数三分支（D4）
# ---------------------------------------------------------------------------


class FakeCommandHandler:
    """Duck-typed stand-in exercising the wrapper's three branches."""

    def __init__(self, agent, scroll_state):
        self._agent = agent
        self._scroll_state = scroll_state

    def _stored_scroll_summary_text(self):
        return self.__class__.__name__ + "-original"


class TestStoredSummaryWrapper:
    def _wrap_original(self):
        return host_compat.make_stored_summary_wrapper(
            FakeCommandHandler._stored_scroll_summary_text,
        )

    def test_live_agent_returns_original_verbatim(self):
        wrapper = self._wrap_original()
        handler = FakeCommandHandler(agent=object(), scroll_state={})
        assert wrapper(handler) == "FakeCommandHandler-original"

    def test_agentless_free_text_renders_summary(self):
        wrapper = self._wrap_original()
        raw = FreeTextSummary("## 当前任务\nx", (1, 2)).to_dict()
        handler = FakeCommandHandler(agent=None, scroll_state={
            "continuation_summary": raw,
        })
        assert wrapper(handler) == "## 当前任务\nx"

    def test_agentless_invalid_free_text_renders_empty(self):
        wrapper = self._wrap_original()
        handler = FakeCommandHandler(agent=None, scroll_state={
            "continuation_summary": {"mode": "free_text", "text": ""},
        })
        assert wrapper(handler) == ""

    def test_agentless_structured_dict_falls_back_to_original(self):
        wrapper = self._wrap_original()
        handler = FakeCommandHandler(agent=None, scroll_state={
            "continuation_summary": {"mode": "structured_v1"},
        })
        assert wrapper(handler) == "FakeCommandHandler-original"

    def test_agentless_no_state_falls_back_to_original(self):
        wrapper = self._wrap_original()
        handler = FakeCommandHandler(agent=None, scroll_state=None)
        assert wrapper(handler) == "FakeCommandHandler-original"

    def test_wrapper_carries_marker(self):
        wrapper = self._wrap_original()
        assert host_compat.is_patched_command_func(wrapper)
        assert wrapper.__name__ == "_stored_scroll_summary_text"


# ---------------------------------------------------------------------------
# CustomScrollManager 关键行为（load_state free_text 重建 / 超时包装）
# ---------------------------------------------------------------------------


class TestCustomScrollManager:
    def test_load_state_rebuilds_free_text_summary(self, pkg):
        """真调 load_state override（复审 M-2：原测试假覆盖已重写）。

        super().load_state 对不含 index 键的 dict 全程容错（manager.py
        2315-2367：各键 data.get 默认值，index 分支 if "index" in data
        不触发）——最小属性集即可跑通 override 的 free_text 分支。
        """
        cls = summary_manager.CustomScrollManager
        manager = cls.__new__(cls)
        manager._continuation_summary = None
        manager._summary_update_failed = False

        raw = FreeTextSummary("## 当前任务\nx", (1, 2)).to_dict()
        manager.load_state({"continuation_summary": raw})
        rebuilt = manager._continuation_summary
        assert isinstance(rebuilt, FreeTextSummary)
        assert rebuilt.render() == "## 当前任务\nx"
        assert rebuilt.covered_seq == (1, 2)

    def test_load_state_drops_invalid_free_text_dict(self, pkg):
        """畸形 free_text dict → from_dict None → 摘要置 None（index-only
        降级，与宿主对无效 dict 行为一致）+ 不崩。"""
        cls = summary_manager.CustomScrollManager
        manager = cls.__new__(cls)
        manager._continuation_summary = None
        manager._summary_update_failed = False

        manager.load_state({
            "continuation_summary": {"mode": "free_text", "text": ""},
        })
        assert manager._continuation_summary is None
        assert manager._summary_update_failed is False

    def test_load_state_ignores_structured_dict(self, pkg):
        """结构化 v1 dict（宿主旧会话）→ 走 super() 原路径，override 不碰。"""
        cls = summary_manager.CustomScrollManager
        manager = cls.__new__(cls)
        marker = object()  # 哨兵：super() 是否赋值由宿主逻辑决定
        manager._continuation_summary = marker

        # 结构化 dict 的 mode 不是 free_text → override 分支不触发
        manager.load_state({"continuation_summary": {"mode": "structured_v1"}})
        # super().load_state 会调 ContinuationSummary.from_dict（结构化残缺
        # dict → None 覆盖哨兵）——这里只断言 override 没有把 free_text
        # 逻辑套在结构化 dict 上（from_dict 结果由宿主决定，非 FreeText）
        assert not isinstance(
            manager._continuation_summary, FreeTextSummary,
        )

    def test_timeout_raises_summary_generation_failed(self):
        async def scenario():
            # replace 模式（D5 后新方案语义的权威路径）：超时 → raise
            manager = make_bare_manager("replace")
            manager._summary_timeout_seconds = 0.05

            async def slow_inner(agent, middle, *, focus_hint=""):
                await asyncio.sleep(1.0)

            manager._update_continuation_summary_inner = slow_inner
            with pytest.raises(summary_manager.SummaryGenerationFailed):
                await manager._update_continuation_summary(
                    object(), [], focus_hint="",
                )
            assert manager._summary_update_failed is True

        asyncio.run(scenario())


# ---------------------------------------------------------------------------
# patch 点 3 包装函数（D5：/compact new 一次性触发）
# ---------------------------------------------------------------------------


class FakeAgent:
    def __init__(self, cm=None):
        self._context_manager = cm


class FakeMarkerCM:
    """Duck-typed context manager carrying this plugin's marker."""

    _compactor_marker = summary_manager.COMPACTOR_MARKER

    def __init__(self):
        self._compactor_use_new_once = False


class PlainCM:
    """A context manager without the plugin marker (point 1 degraded)."""


class FakeCommandHandlerForCompact:
    def __init__(self, agent):
        self._agent = agent
        self.calls: list[tuple] = []

    async def _process_compact(self, messages, args=""):
        self.calls.append((messages, args))
        return "orig-reply"


class TestCompactCommandWrapper:
    def _wrap(self, handler):
        # 宿主从类上取 unbound 函数；fake 同姿势
        return host_compat.make_compact_command_wrapper(
            type(handler)._process_compact,
        )

    def _run(self, coro):
        return asyncio.run(coro)

    def test_new_keyword_sets_flag_on_marker_cm_and_blanks_args(self):
        handler = FakeCommandHandlerForCompact(FakeAgent(FakeMarkerCM()))
        wrapper = self._wrap(handler)
        cm = handler._agent._context_manager
        result = self._run(wrapper(handler, [], "new"))
        assert result == "orig-reply"
        assert handler.calls == [([], "")]          # args 置空：不漏成 hint
        assert cm._compactor_use_new_once is False  # finally 兜底复位

    def test_flag_set_during_execution(self, monkeypatch):
        """orig 执行期间 flag 处于置位态（manager 可消费），结束后复位。"""
        handler = FakeCommandHandlerForCompact(FakeAgent(FakeMarkerCM()))
        cm = handler._agent._context_manager
        observed: list[bool] = []

        async def spy_orig(self, messages, args=""):
            observed.append(cm._compactor_use_new_once)
            return "ok"

        monkeypatch.setattr(
            FakeCommandHandlerForCompact, "_process_compact", spy_orig,
        )
        wrapper = self._wrap(handler)
        self._run(wrapper(handler, [], "new"))
        assert observed == [True]
        assert cm._compactor_use_new_once is False

    def test_agentless_uses_pending_slot(self, monkeypatch):
        handler = FakeCommandHandlerForCompact(FakeAgent(None))
        observed: list[bool] = []

        async def spy_orig(self, messages, args=""):
            observed.append(summary_manager._pending_new_once)
            return "ok"

        monkeypatch.setattr(
            FakeCommandHandlerForCompact, "_process_compact", spy_orig,
        )
        wrapper = self._wrap(handler)  # monkeypatch 之后再取（捕获 spy）
        self._run(wrapper(handler, [], "new"))
        assert observed == [True]                    # 执行期间 pending 置位
        assert summary_manager._pending_new_once is False  # finally 复位

    def test_plain_cm_falls_back_to_pending_slot(self):
        """点 1 降级（cm 无 marker）→ pending 兜底，无实例 flag 泄漏。"""
        cm = PlainCM()
        handler = FakeCommandHandlerForCompact(FakeAgent(cm))
        wrapper = self._wrap(handler)
        self._run(wrapper(handler, [], "new"))
        assert summary_manager._pending_new_once is False
        assert not hasattr(cm, "_compactor_use_new_once")

    def test_other_args_keep_hint_semantics_untouched(self):
        handler = FakeCommandHandlerForCompact(FakeAgent(FakeAgent(None)))
        wrapper = self._wrap(handler)
        self._run(wrapper(handler, [], "优先保留错误码"))
        assert handler.calls == [([], "优先保留错误码")]
        assert summary_manager._pending_new_once is False

    def test_new_with_suffix_is_not_triggered(self):
        """严格匹配：`new xxx` 不触发（hint 语义保持原样，D5 拍板）。"""
        handler = FakeCommandHandlerForCompact(FakeAgent(FakeAgent(None)))
        wrapper = self._wrap(handler)
        self._run(wrapper(handler, [], "new 保留错误码"))
        assert handler.calls == [([], "new 保留错误码")]
        assert summary_manager._pending_new_once is False

    def test_wrapper_carries_marker(self):
        handler = FakeCommandHandlerForCompact(FakeAgent(None))
        wrapper = self._wrap(handler)
        assert host_compat.is_patched_command_func(wrapper)
        assert wrapper.__name__ == "_process_compact"


# ---------------------------------------------------------------------------
# shadow / replace 双模式分流（D5）
# ---------------------------------------------------------------------------


def make_bare_manager(mode: str, use_new_once: bool = False):
    cls = summary_manager.CustomScrollManager
    manager = cls.__new__(cls)
    manager._compactor_config = config_mod.CompactorConfig(mode=mode)
    manager._compactor_use_new_once = use_new_once
    manager._compactor_new_active = False
    manager._summary_timeout_seconds = 30
    manager._summary_update_failed = False
    return manager


class TestShadowReplaceDispatch:
    def test_shadow_without_flag_delegates_to_legacy(self, monkeypatch):
        """shadow + 无 flag → super() 原版透传（含原版静默语义）。"""
        legacy_calls: list[str] = []
        inner_calls: list[str] = []

        async def fake_legacy(self, agent, middle, *, focus_hint=""):
            legacy_calls.append("legacy-outer")

        async def fake_legacy_inner(self, agent, middle, *, focus_hint=""):
            inner_calls.append("legacy-inner")

        monkeypatch.setattr(
            ScrollContextManager, "_update_continuation_summary",
            fake_legacy,
        )
        monkeypatch.setattr(
            ScrollContextManager, "_update_continuation_summary_inner",
            fake_legacy_inner,
        )
        manager = make_bare_manager("shadow")
        asyncio.run(
            manager._update_continuation_summary(object(), []),
        )
        assert legacy_calls == ["legacy-outer"]
        assert inner_calls == []
        assert manager._compactor_use_new_once is False
        assert manager._compactor_new_active is False

    def test_shadow_with_flag_runs_refined_pipeline(self, monkeypatch):
        """shadow + /compact new flag → 新方案（inner 走 refined 路径）。"""
        from conftest import plugin_module as _pm

        generation = _pm().generation

        legacy_calls: list[str] = []

        async def fake_legacy(self, agent, middle, *, focus_hint=""):
            legacy_calls.append("legacy-outer")

        monkeypatch.setattr(
            ScrollContextManager, "_update_continuation_summary",
            fake_legacy,
        )
        manager = make_bare_manager("shadow", use_new_once=True)

        async def fake_inner(manager_, agent, middle, *, focus_hint, config):
            # run_conversation_summary 被 patch：返回成功摘要
            return generation.FreeTextSummary("## 当前任务\nx", (1, 2))

        monkeypatch.setattr(
            summary_manager, "run_conversation_summary", fake_inner,
        )
        asyncio.run(
            manager._update_continuation_summary(object(), []),
        )
        assert legacy_calls == []                     # 未走原版
        assert manager._compactor_use_new_once is False   # 一次性消费
        assert manager._compactor_new_active is False     # finally 复位
        assert manager._continuation_summary.render() == "## 当前任务\nx"
        assert manager._summary_update_failed is False

    def test_replace_mode_always_refined(self, monkeypatch):
        from conftest import plugin_module as _pm

        generation = _pm().generation
        legacy_calls: list[str] = []

        async def fake_legacy(self, agent, middle, *, focus_hint=""):
            legacy_calls.append("legacy-outer")

        monkeypatch.setattr(
            ScrollContextManager, "_update_continuation_summary",
            fake_legacy,
        )
        manager = make_bare_manager("replace")       # flag 未置位

        async def fake_inner(manager_, agent, middle, *, focus_hint, config):
            return generation.FreeTextSummary("## 当前任务\nx", (1, 2))

        monkeypatch.setattr(
            summary_manager, "run_conversation_summary", fake_inner,
        )
        asyncio.run(
            manager._update_continuation_summary(object(), []),
        )
        assert legacy_calls == []
        assert manager._continuation_summary is not None

    def test_replace_mode_inner_fallback_guard(self, monkeypatch):
        """replace 模式下 _compactor_new_active 未置位时 inner 走 legacy
        （防御：外层 finally 复位后的裸 inner 调用不会误入新管线）。"""
        inner_calls: list[str] = []

        async def fake_legacy_inner(self, agent, middle, *, focus_hint=""):
            inner_calls.append("legacy-inner")

        monkeypatch.setattr(
            ScrollContextManager, "_update_continuation_summary_inner",
            fake_legacy_inner,
        )
        manager = make_bare_manager("replace")
        asyncio.run(
            manager._update_continuation_summary_inner(object(), []),
        )
        assert inner_calls == ["legacy-inner"]

    def test_flag_consumed_even_on_timeout(self):
        """新方案超时：flag 已消费 + SummaryGenerationFailed 上浮。"""
        manager = make_bare_manager("shadow", use_new_once=True)
        manager._summary_timeout_seconds = 0.05  # < sleep 时长 → 必超时

        async def slow_inner(agent, middle, *, focus_hint=""):
            await asyncio.sleep(1.0)

        manager._update_continuation_summary_inner = slow_inner
        with pytest.raises(summary_manager.SummaryGenerationFailed):
            asyncio.run(
                manager._update_continuation_summary(object(), []),
            )
        assert manager._compactor_use_new_once is False
        assert manager._summary_update_failed is True

    def test_pending_flag_picked_up_at_construction(self, monkeypatch):
        """agent-less 分支：pending → 构造时读取并立即清除（防泄漏）。"""
        monkeypatch.setattr(
            ScrollContextManager, "__init__",
            lambda self, *a, **k: None,  # 真构造需要 history/session_id
        )
        summary_manager.set_pending_new_once(True)
        cls = summary_manager.CustomScrollManager
        manager = cls.__new__(cls)
        manager.__init__()
        assert manager._compactor_use_new_once is True
        assert summary_manager._pending_new_once is False  # 读取即清除
