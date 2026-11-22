# -*- coding: utf-8 -*-
"""压缩边界 marker 单元测试（summary_manager._rebuild_context）。

覆盖三条路：
* 驱逐路径（_compactor_marker_pending=True）→ 在 tail 末尾（保留轮之后）插 marker；
* 恢复路径（flag=False）→ 不插；
* marker 是 AssistantMsg+HintBlock（formatter 转 user 消息 / history converter 跳过、不进 _synthetic_ids）。
另覆盖 tail 全 assistant（无 user 轮）的边界场景（marker 同样追加到末尾）。
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

from agentscope.message import UserMsg

from conftest import MODULE_NAME, plugin_module

plugin_module()  # 确保包已加载（conftest 的 sys.path 快照恢复 autouse）
summary_manager = importlib.import_module(f"{MODULE_NAME}.summary_manager")
CustomScrollManager = summary_manager.CustomScrollManager
BOUNDARY_MARKER_TEXT = summary_manager.BOUNDARY_MARKER_TEXT


# ---------------------------------------------------------------------------
# fakes（只造 _rebuild_context 消费的状态，绕过宿主 __init__）
# ---------------------------------------------------------------------------


class FakeIndex:
    def render(self, **kwargs):
        return "<eviction-index>"


def make_manager(marker_pending: bool) -> CustomScrollManager:
    mgr = object.__new__(CustomScrollManager)
    mgr._compactor_marker_pending = marker_pending
    mgr._synthetic_ids = set()
    mgr._index = FakeIndex()
    mgr._continuation_summary = None
    return mgr


def make_agent(messages) -> SimpleNamespace:
    state = SimpleNamespace(context=list(messages))
    model = SimpleNamespace(context_size=128000)
    return SimpleNamespace(state=state, model=model)


def user(mid: str, text: str = "u") -> UserMsg:
    return UserMsg(name="user", content=text, id=mid)


def assistant(mid: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=mid,
        role="assistant",
        content=[{"type": "text", "text": "a"}],
    )


def msg_text(m):
    """提取消息文本，兼容 str / text block / hint block 三种 content 形态。"""
    content = getattr(m, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                t = b.get("text") or b.get("hint")
            else:
                t = getattr(b, "text", None) or getattr(b, "hint", None)
            if t:
                parts.append(t)
        return "".join(parts)
    return None


def find_marker(context):
    """返回 context 里 marker 消息的索引，没有则 None。"""
    for i, m in enumerate(context):
        if msg_text(m) == BOUNDARY_MARKER_TEXT:
            return i
    return None


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


def test_eviction_path_appends_marker_at_end():
    tail = [user("u1"), assistant("a1"), user("u2"), assistant("a2")]
    agent = make_agent(tail)
    mgr = make_manager(marker_pending=True)
    mgr._rebuild_context(agent, tail)
    ctx = agent.state.context
    # 期望：[placeholder, u1, a1, u2, a2, MARKER]——marker 在保留轮之后（末尾）
    marker_idx = find_marker(ctx)
    assert marker_idx is not None, "驱逐路径未插 marker"
    assert marker_idx == len(ctx) - 1, "marker 必须在 tail 末尾（保留轮之后）"
    # marker 之后没有消息（下一轮 user 消息将落在此后）
    assert all(getattr(m, "id", None) != "u2" for m in ctx[marker_idx:]), (
        "active turn（u2）应在 marker 之前，不是之后"
    )
    assert mgr._compactor_marker_pending is False, "flag 未被消费"


def test_restore_path_does_not_insert_marker():
    tail = [user("u1"), assistant("a1"), user("u2"), assistant("a2")]
    agent = make_agent(tail)
    mgr = make_manager(marker_pending=False)
    mgr._rebuild_context(agent, tail)
    ctx = agent.state.context
    assert find_marker(ctx) is None, "恢复路径不应插 marker"
    # 占位符仍在（super() 正常执行）
    assert any(getattr(m, "name", None) == "memory" for m in ctx)


def test_marker_is_hintblock_assistantmsg_not_synthetic():
    from agentscope.message import HintBlock

    tail = [user("u1"), assistant("a1")]
    agent = make_agent(tail)
    mgr = make_manager(marker_pending=True)
    mgr._rebuild_context(agent, tail)
    ctx = agent.state.context
    marker_idx = find_marker(ctx)
    assert marker_idx is not None
    marker = ctx[marker_idx]
    # marker 是 assistant 角色（AssistantMsg 工厂；UserMsg/SystemMsg 拒绝 hint block）
    assert getattr(marker, "role", None) == "assistant", "marker 应是 assistant 角色"
    # content 是 HintBlock（formatter 转 user 消息 / history converter 跳过）
    assert len(marker.content) == 1 and isinstance(marker.content[0], HintBlock)
    assert marker.content[0].hint == BOUNDARY_MARKER_TEXT
    # 不进 _synthetic_ids（无特殊对待）
    assert (
        getattr(marker, "id", None) not in mgr._synthetic_ids
    ), "marker 不应进 _synthetic_ids（无特殊对待）"


def test_eviction_no_active_turn_appends_marker_at_end():
    # tail 全 assistant、无 user 轮，marker 同样追加到末尾
    tail = [assistant("a1")]
    agent = make_agent(tail)
    mgr = make_manager(marker_pending=True)
    mgr._rebuild_context(agent, tail)
    ctx = agent.state.context
    marker_idx = find_marker(ctx)
    assert marker_idx is not None, "无 active turn 时也应插 marker"
    assert marker_idx == len(ctx) - 1, "无 active turn 时 marker 应在末尾"
