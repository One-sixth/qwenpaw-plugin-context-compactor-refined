# -*- coding: utf-8 -*-
"""生成循环单元测试（generation.py）——mock agent.model 流式/非流式。"""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace

import pytest
from agentscope.message import Msg, UserMsg
from agentscope.model import FinishedReason

from conftest import plugin_module

_pkg = plugin_module()
generation = _pkg.generation
config_mod = _pkg.config
prompts_mod = _pkg.prompts

SummaryGenerationFailed = generation.SummaryGenerationFailed
missing_sections = generation.missing_sections
run_conversation_summary = generation.run_conversation_summary

SECTIONS = config_mod.CompactorConfig().sections

FULL_REPLY = (
    "## 当前任务\n收尾压缩摘要插件\n\n"
    "## 当前状态\nPhase 1 代码落盘\n\n"
    "## 约束\ncache 对齐优先\n\n"
    "## 决定\nD2 不降级\n\n"
    "## 待办\n真机验收"
)


# ---------------------------------------------------------------------------
# fakes（精确复刻被消费的 manager 接口）
# ---------------------------------------------------------------------------


class FakeHistory:
    def __init__(self, existing):
        self._existing = set(existing)

    def existing_seqs(self, endpoints):
        return {seq for seq in endpoints if seq in self._existing}


class FakeManager:
    """Duck-typed manager: mirrors the upstream interface the loop uses."""

    def __init__(self, seq_by_id=None, previous=None, durable_seqs=None):
        self._seq_by_id = dict(seq_by_id or {})
        self._continuation_summary = previous
        self._history = FakeHistory(durable_seqs or [])
        self.model_calls: list[dict] = []

    def _evicted_span(self, messages):
        spans = [
            self._seq_by_id.get(getattr(m, "id", None))
            for m in messages
        ]
        known = [s for s in spans if s is not None]
        if not known:
            return None
        return min(s[0] for s in known), max(s[1] for s in known)

    def _source_backed_previous_summary(self, existing):
        previous = self._continuation_summary
        if previous is None:
            return None
        endpoints = set(previous.covered_seq)
        return previous if existing == endpoints else None

    @staticmethod
    def _raise_if_summary_interrupted(response):
        if getattr(response, "finished_reason", None) == FinishedReason.INTERRUPTED:
            raise asyncio.CancelledError()

    @staticmethod
    def _response_text(response):
        parts = []
        for block in getattr(response, "content", None) or []:
            btype = (
                block.get("type")
                if isinstance(block, dict)
                else getattr(block, "type", None)
            )
            if btype == "text":
                parts.append(
                    str(
                        block.get("text", "")
                        if isinstance(block, dict)
                        else getattr(block, "text", "") or "",
                    ),
                )
        return "".join(parts).strip()


def make_agent(responder, base_tools=None):
    """agent.model = async callable; responder(chunks_so_far) → str|exception.

    Also provides ``_prepare_model_input`` (the same-source construction
    entry the summary request reuses): base messages + tool schemas.
    """
    calls: list[dict] = []
    base_messages = [
        UserMsg(name="user", content="历史对话", id="base1"),
    ]

    async def prepare_model_input():
        return {"messages": list(base_messages), "tools": list(base_tools or [])}

    async def model(**kwargs):
        calls.append(kwargs)
        return responder(len(calls), kwargs)

    agent = SimpleNamespace(model=model, _prepare_model_input=prepare_model_input)
    agent.model_calls = calls
    return agent


def plain_response(text):
    return SimpleNamespace(
        content=[{"type": "text", "text": text}],
        finished_reason=None,
    )


def stream_response(chunks):
    """chunks: list[(text, is_last)] — mirrors the agentscope wire protocol:
    incremental deltas (is_last=False) followed by one accumulated final
    chunk (is_last=True, full content)."""

    async def gen():
        for text, is_last in chunks:
            yield SimpleNamespace(
                content=[{"type": "text", "text": text}],
                is_last=is_last,
                finished_reason=None,
            )

    return gen()


def make_middle(*seqs):
    return [
        UserMsg(name="user", content=f"m{seq}", id=f"id{seq}")
        for seq in seqs
    ]


def run(manager, agent, middle, focus_hint="", config=None):
    return asyncio.run(
        run_conversation_summary(
            manager,
            agent,
            middle,
            focus_hint=focus_hint,
            config=config or config_mod.CompactorConfig(),
        ),
    )


# ---------------------------------------------------------------------------
# build_question（M-复审 M-1：缺 {max_chars} 槽位天然容忍；坏模板显式失败）
# ---------------------------------------------------------------------------


class TestBuildQuestion:
    def test_custom_template_without_max_chars_slot(self):
        """自定义模板缺 {max_chars} 槽位 → 多余 kwargs 无害，正常渲染。"""
        q = prompts_mod.build_question(
            ("当前任务",),
            template="章节：{sections_block}",
        )
        assert q == "章节：## 当前任务"

    def test_custom_template_unknown_placeholder_raises(self):
        """未知占位符 → KeyError 显式失败（坏模板配置不被静默吞）。"""
        with pytest.raises(KeyError):
            prompts_mod.build_question(
                ("当前任务",),
                template="章节：{sections_block} 未知：{foo}",
            )


# ---------------------------------------------------------------------------
# missing_sections（章节校验正则，R3-发现9）
# ---------------------------------------------------------------------------


class TestMissingSections:
    def test_full_reply_has_no_missing(self):
        assert missing_sections(FULL_REPLY, SECTIONS) == []

    def test_all_missing_on_plain_text(self):
        assert missing_sections("没有标题的文本", SECTIONS) == list(SECTIONS)

    def test_trailing_colon_tolerated(self):
        reply = "\n".join(f"## {name}：" for name in SECTIONS)
        assert missing_sections(reply, SECTIONS) == []

    def test_inline_content_rejected(self):
        reply = "## 当前任务：不能同行写内容\n" + "\n".join(
            f"## {name}" for name in SECTIONS[1:]
        )
        assert missing_sections(reply, SECTIONS) == ["当前任务"]

    def test_wrong_level_rejected(self):
        reply = "# 当前任务\n" + "\n".join(f"## {n}" for n in SECTIONS[1:])
        assert missing_sections(reply, SECTIONS) == ["当前任务"]

    def test_trailing_whitespace_tolerated(self):
        reply = "\n".join(f"## {name}  " for name in SECTIONS)
        assert missing_sections(reply, SECTIONS) == []

    def test_leading_indent_rejected(self):
        """行首缩进的标题不算命中（4+ 空格缩进的 ## 在 Markdown 渲染中
        退化为代码块；严格拒绝让对话内重试修正，设计稿 §3.4 正则定稿）。"""
        reply = "\n".join(f"  ## {name}" for name in SECTIONS)
        assert missing_sections(reply, SECTIONS) == list(SECTIONS)


# ---------------------------------------------------------------------------
# 入口 guard（复刻原版语义：无需生成 → None，不是失败）
# ---------------------------------------------------------------------------


class TestEntryGuard:
    def test_no_evicted_span_returns_none_without_model_call(self):
        manager = FakeManager(seq_by_id={})
        agent = make_agent(lambda n, k: plain_response(FULL_REPLY))
        middle = make_middle(1, 2)  # ids unknown → span None
        assert run(manager, agent, middle) is None
        assert agent.model_calls == []

    def test_uncallable_model_returns_none(self):
        manager = FakeManager(seq_by_id={"id1": (1, 1)})
        agent = SimpleNamespace(model=None)
        assert run(manager, agent, make_middle(1)) is None


# ---------------------------------------------------------------------------
# 成功路径（非流式 / 流式 / covered 合并 / 输出预算）
# ---------------------------------------------------------------------------


class TestSuccessPaths:
    def test_plain_success(self):
        manager = FakeManager(seq_by_id={"id1": (5, 5), "id2": (9, 9)})
        agent = make_agent(lambda n, k: plain_response(FULL_REPLY))
        summary = run(manager, agent, make_middle(1, 2))
        assert isinstance(summary, generation.FreeTextSummary)
        assert summary.covered_seq == (5, 9)
        assert summary.render().startswith(FULL_REPLY)

    def test_stream_success_uses_final_accumulated_chunk(self):
        """协议：增量 chunks (is_last=False) + 末尾累计 chunk (is_last=True,
        content 全量)——原版/复刻消费逻辑取 final = 累计文本。"""
        manager = FakeManager(seq_by_id={"id1": (3, 3)})
        chunks = [
            (FULL_REPLY[:20], False),
            (FULL_REPLY[20:], False),
            (FULL_REPLY, True),  # accumulated final chunk
        ]
        agent = make_agent(lambda n, k: stream_response(chunks))
        summary = run(manager, agent, make_middle(1))
        assert summary.render().startswith(FULL_REPLY)

    def test_stream_deltas_fallback_when_final_chunk_has_no_text(self):
        """收尾累计 chunk 无 text 时 deltas 兜底（原版 `final or deltas`）。

        注：_response_text 逐 chunk strip，跨 chunk 的换行边界会粘连
        （原版固有行为；真实协议下正常流必有累计收尾 chunk，此路径几乎
        不触发——触发时粘连导致校验失败 → 对话内重试自愈）。故本用例
        用单个 chunk 携带全量内容验证兜底可达性与内容保真。"""
        manager = FakeManager(seq_by_id={"id1": (3, 3)})
        chunks = [
            (FULL_REPLY, False),
            ("", True),  # accumulated chunk without visible text
        ]
        agent = make_agent(lambda n, k: stream_response(chunks))
        summary = run(manager, agent, make_middle(1))
        assert summary.render().startswith(FULL_REPLY)

    def test_question_appended_after_full_context(self):
        manager = FakeManager(seq_by_id={"id1": (3, 3)})
        seen_kwargs = {}

        def responder(n, kwargs):
            seen_kwargs.update(kwargs)
            return plain_response(FULL_REPLY)

        base_tools = [{"type": "function", "function": {"name": "demo_tool"}}]
        agent = make_agent(responder, base_tools=base_tools)
        run(manager, agent, make_middle(1))
        messages = seen_kwargs["messages"]
        # 提问是最后一条消息：工厂函数产出的 Msg（role=user）带显式 name
        last = messages[-1]
        assert getattr(last, "role", None) == "user"
        assert last.name == "summary_request"
        assert "## 当前任务" in str(last.content)   # 章节清单在提问里
        assert "[scroll folded]" in str(last.content)  # D1 防污染句
        # tools 原样保留 + ToolChoice 对象形态（H-A）
        assert seen_kwargs["tools"] == base_tools
        from agentscope.tool import ToolChoice

        assert seen_kwargs["tool_choice"] == ToolChoice(mode="none")
        assert seen_kwargs["disable_thinking"] is True

    def test_previous_span_merged_min_max(self):
        from conftest import plugin_module as _pm

        summary_obj = _pm().summary_object
        previous = summary_obj.FreeTextSummary("旧摘要", (1, 4))
        manager = FakeManager(
            seq_by_id={"id1": (7, 7)},
            previous=previous,
            durable_seqs=[1, 4],  # retention purge 防御：endpoints 可追溯
        )
        agent = make_agent(lambda n, k: plain_response(FULL_REPLY))
        summary = run(manager, agent, make_middle(1))
        assert summary.covered_seq == (1, 7)

    def test_purged_previous_is_dropped(self):
        from conftest import plugin_module as _pm

        summary_obj = _pm().summary_object
        previous = summary_obj.FreeTextSummary("旧摘要", (1, 4))
        manager = FakeManager(
            seq_by_id={"id1": (7, 7)},
            previous=previous,
            durable_seqs=[],  # endpoints 丢失 → previous 视为不可信
        )
        agent = make_agent(lambda n, k: plain_response(FULL_REPLY))
        summary = run(manager, agent, make_middle(1))
        assert summary.covered_seq == (7, 7)

    def test_output_tokens_not_passed_by_default(self):
        """output_tokens=0（默认）→ 请求 kwargs 不含 max_tokens（端点默认）。"""
        manager = FakeManager(seq_by_id={"id1": (3, 3)})
        seen = {}

        def responder(n, kwargs):
            seen.update(kwargs)
            return plain_response(FULL_REPLY)

        agent = make_agent(responder)
        run(manager, agent, make_middle(1))
        assert "max_tokens" not in seen

    def test_output_tokens_raised_to_32000(self):
        """output_tokens>0 且 ≤32000 → 抬高到 32000。"""
        manager = FakeManager(seq_by_id={"id1": (3, 3)})
        seen = {}

        def responder(n, kwargs):
            seen.update(kwargs)
            return plain_response(FULL_REPLY)

        agent = make_agent(responder)
        cfg = config_mod.CompactorConfig(output_tokens=8000)
        run(manager, agent, make_middle(1), config=cfg)
        assert seen["max_tokens"] == 32000

    def test_output_tokens_above_32000_kept(self):
        """output_tokens>32000 → 保持用户显式值。"""
        manager = FakeManager(seq_by_id={"id1": (3, 3)})
        seen = {}

        def responder(n, kwargs):
            seen.update(kwargs)
            return plain_response(FULL_REPLY)

        agent = make_agent(responder)
        cfg = config_mod.CompactorConfig(output_tokens=40000)
        run(manager, agent, make_middle(1), config=cfg)
        assert seen["max_tokens"] == 40000

    def test_prompt_max_chars_soft_cap_injected(self):
        """提问模板注入软约束（15000 字符）。"""
        manager = FakeManager(seq_by_id={"id1": (3, 3)})

        def responder(n, kwargs):
            return plain_response(FULL_REPLY)

        agent = make_agent(responder)
        run(manager, agent, make_middle(1))
        question = str(agent.model_calls[0]["messages"][-1].content)
        assert "15000" in question

    def test_hard_truncate_sections_survive(self):
        """超长回复截断到 hard_max_chars；截断后章节标题幸存 → 直接入库
        （截断稿 + 固化块，单次调用零重试）。"""
        long_reply = FULL_REPLY + "x" * 50
        manager = FakeManager(seq_by_id={"id1": (3, 3)})
        agent = make_agent(lambda n, k: plain_response(long_reply))
        cfg = config_mod.CompactorConfig(hard_max_chars=len(FULL_REPLY) + 10)
        summary = run(manager, agent, make_middle(1), config=cfg)
        assert len(agent.model_calls) == 1
        # 入库 = 截断稿（hard 上限）+ 末端固化块
        assert summary.text.startswith(FULL_REPLY)
        assert "## 待办" in summary.text
        assert cfg.hard_max_chars < len(summary.text) < cfg.hard_max_chars + 2000

    def test_hard_truncate_default_20000(self):
        """默认 config（hard_max_chars=20000）→ 3 万字符回复被截到 2 万。"""
        long_reply = FULL_REPLY + "x" * 30000
        manager = FakeManager(seq_by_id={"id1": (3, 3)})
        agent = make_agent(lambda n, k: plain_response(long_reply))
        summary = run(manager, agent, make_middle(1))
        assert len(summary.text) >= 20000
        assert len(summary.text) < 20000 + 2000

    def test_hard_truncate_lost_section_retries(self):
        """截断把最后一章标题截掉 → missing 非空 → 对话内重试救回。"""
        # 前四章 + 填充把 "## 待办" 推到截断点之后
        padded = (
            "## 当前任务\n收尾压缩摘要插件\n\n"
            "## 当前状态\nPhase 1 代码落盘\n\n"
            "## 约束\ncache 对齐优先\n\n"
            "## 决定\nD2 不降级\n\n" + "y" * 50
        )
        long_reply = padded + "## 待办\n真机验收"
        calls: list[list] = []

        def responder(n, kwargs):
            calls.append(kwargs["messages"])
            return plain_response(FULL_REPLY if n > 1 else long_reply)

        manager = FakeManager(seq_by_id={"id1": (3, 3)})
        agent = make_agent(responder)
        cfg = config_mod.CompactorConfig(hard_max_chars=len(padded) + 5)
        summary = run(manager, agent, make_middle(1), config=cfg)
        assert len(agent.model_calls) == 2
        # 重试对话里带截断稿 + 缺章节纠正（"待办"）
        retry_messages = calls[1]
        assert "y" * 10 in str(retry_messages[-2].content)  # 失败稿已追加
        assert "待办" in str(retry_messages[-1].content)
        assert summary.text.startswith(FULL_REPLY)

    def test_focus_hint_appended(self):
        manager = FakeManager(seq_by_id={"id1": (3, 3)})
        seen = {}

        def responder(n, kwargs):
            seen.update(kwargs)
            return plain_response(FULL_REPLY)

        agent = make_agent(responder)
        run(manager, agent, make_middle(1), focus_hint="优先保留错误码")
        assert "优先保留错误码" in str(seen["messages"][-1].content)


# ---------------------------------------------------------------------------
# 重试语义（Q2：共 4 次；缺章节对话内纠正；provider 错误立即失败）
# ---------------------------------------------------------------------------


class TestRetrySemantics:
    def test_in_conversation_retry_then_success(self):
        manager = FakeManager(seq_by_id={"id1": (3, 3)})
        replies = [
            "## 当前任务\nx",                # 缺 4 章
            FULL_REPLY,                       # 第 2 次全
        ]

        def responder(n, kwargs):
            return plain_response(replies[n - 1])

        agent = make_agent(responder)
        summary = run(manager, agent, make_middle(1))
        assert summary.render().startswith(FULL_REPLY)
        assert len(agent.model_calls) == 2
        # 第 2 次请求：base + 提问 + 失败稿 + 纠正消息
        msgs = agent.model_calls[1]["messages"]
        assert "缺少" in str(msgs[-1].content)       # 纠正消息
        assert "当前状态" in str(msgs[-1].content)    # 纠正消息列出缺失章节
        assert "当前任务" in str(msgs[-2].content)    # 失败稿被追加在前面

    # ------------------------------------------------------------------
    # 摘要末端固化块（_append_recall_hint）：时间戳 + recall 即用指引
    # ------------------------------------------------------------------

    TIME_RE = r"当前时间：\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{2}:\d{2}"

    def test_hint_appended_with_covered_seq(self):
        """成功稿末尾含固化块：时间戳格式正确 + covered 区间即用指令。"""
        manager = FakeManager(seq_by_id={"id1": (5, 5), "id2": (9, 9)})
        agent = make_agent(lambda n, k: plain_response(FULL_REPLY))
        summary = run(manager, agent, make_middle(1, 2))
        text = summary.render()
        assert text.startswith(FULL_REPLY)
        assert re.search(self.TIME_RE, text)
        assert "查看压缩前原始上下文（本摘要覆盖 seq 5–9）" in text
        assert 'recall_history(op="expand", lo=5, hi=9)' in text
        assert 'recall_history(op="search"' in text
        assert 'recall_history(op="recall_tool"' in text

    def test_hint_generic_fallback_on_inverted_covered(self):
        """covered 倒挂（lo > hi）→ 通用指引，无具体 seq 数字，不抛。"""
        manager = FakeManager(seq_by_id={"id1": (9, 5)})
        agent = make_agent(lambda n, k: plain_response(FULL_REPLY))
        summary = run(manager, agent, make_middle(1))
        text = summary.render()
        assert text.startswith(FULL_REPLY)
        assert re.search(self.TIME_RE, text)
        assert "压缩地图" in text  # generic 版措辞
        assert 'lo=[区间起点]' in text
        assert "lo=9" not in text and "hi=5" not in text

    def test_stamp_failure_still_succeeds(self):
        """stamp 计算异常 → 占位「未知时间」，压缩照常成功（绝不抛）。"""
        class Boom:
            @staticmethod
            def now(*args, **kwargs):
                raise RuntimeError("stamp boom")

        manager = FakeManager(seq_by_id={"id1": (3, 3)})
        agent = make_agent(lambda n, k: plain_response(FULL_REPLY))
        orig = generation.datetime
        generation.datetime = Boom
        try:
            summary = run(manager, agent, make_middle(1))
        finally:
            generation.datetime = orig
        text = summary.render()
        assert text.startswith(FULL_REPLY)
        assert "未知时间" in text
        assert 'recall_history(op="expand"' in text  # 指引仍在

    def test_hint_absent_from_retry_conversation(self):
        """对话内重试的失败稿是模型原稿：无固化块、校验针对原稿。"""
        manager = FakeManager(seq_by_id={"id1": (3, 3)})
        replies = ["## 当前任务\nx", FULL_REPLY]  # 第 1 次缺 4 章节
        agent = make_agent(lambda n, k: plain_response(replies[n - 1]))
        summary = run(manager, agent, make_middle(1))
        msgs = agent.model_calls[1]["messages"]
        assert "上下文压缩已完成" not in str(msgs[-2].content)  # 失败稿原样
        assert summary.render().startswith(FULL_REPLY)

    def test_all_attempts_exhausted_raises_with_details(self):
        manager = FakeManager(seq_by_id={"id1": (3, 3)})
        agent = make_agent(lambda n, k: plain_response("## 当前任务\nx"))
        with pytest.raises(SummaryGenerationFailed) as excinfo:
            run(manager, agent, make_middle(1))
        message = str(excinfo.value)
        assert "4 次尝试" in message
        assert "当前状态" in message      # 缺失章节名在错误信息里
        assert len(agent.model_calls) == 4

    def test_provider_error_fails_immediately_without_retry(self):
        manager = FakeManager(seq_by_id={"id1": (3, 3)})

        def responder(n, kwargs):
            raise RuntimeError("HTTP 429: rate limited")

        agent = make_agent(responder)
        with pytest.raises(SummaryGenerationFailed) as excinfo:
            run(manager, agent, make_middle(1))
        assert "RuntimeError" in str(excinfo.value)
        assert "429" in str(excinfo.value)
        assert len(agent.model_calls) == 1  # 不烧重试

    def test_provider_error_message_wraps_original(self):
        manager = FakeManager(seq_by_id={"id1": (3, 3)})

        def responder(n, kwargs):
            raise ValueError("400 Bad Request: context too long")

        agent = make_agent(responder)
        with pytest.raises(SummaryGenerationFailed) as excinfo:
            run(manager, agent, make_middle(1))
        assert "context too long" in str(excinfo.value)
        assert excinfo.value.__cause__ is not None

    def test_interrupted_stream_propagates_cancellation(self):
        manager = FakeManager(seq_by_id={"id1": (3, 3)})

        def responder(n, kwargs):
            async def gen():
                yield SimpleNamespace(
                    content=[{"type": "text", "text": "partial"}],
                    is_last=False,
                    finished_reason=FinishedReason.INTERRUPTED,
                )

            return gen()

        agent = make_agent(responder)
        with pytest.raises(asyncio.CancelledError):
            run(manager, agent, make_middle(1))

    def test_no_tools_mode_omits_tool_choice(self):
        manager = FakeManager(seq_by_id={"id1": (3, 3)})
        seen = {}

        def responder(n, kwargs):
            seen.update(kwargs)
            return plain_response(FULL_REPLY)

        agent = make_agent(responder)
        asyncio.run(
            run_conversation_summary(
                manager,
                agent,
                make_middle(1),
                focus_hint="",
                config=config_mod.CompactorConfig(cache_mode="no_tools"),
            ),
        )
        assert "tools" not in seen
        assert "tool_choice" not in seen


# ---------------------------------------------------------------------------
# 配置解析
# ---------------------------------------------------------------------------


class TestConfigParsing:
    def test_defaults(self):
        cfg = config_mod.CompactorConfig()
        assert cfg.max_retries == 3
        # D5 验收完成（2026-11-18）：默认 replace 全量替换；shadow 转为可选配
        assert cfg.mode == "replace"
        assert cfg.sections == SECTIONS

    def test_from_mapping_overrides(self):
        cfg = config_mod.config_from_mapping(
            {"max_retries": 1, "cache_mode": "no_tools", "output_tokens": 1024},
        )
        assert cfg.max_retries == 1
        assert cfg.cache_mode == "no_tools"
        assert cfg.output_tokens == 1024

    def test_unknown_keys_ignored(self):
        cfg = config_mod.config_from_mapping({"bogus": 1, "max_retries": 2})
        assert cfg.max_retries == 2
        assert not hasattr(cfg, "bogus")

    def test_invalid_cache_mode_rejected(self):
        with pytest.raises(ValueError):
            config_mod.CompactorConfig(cache_mode="wat")

    def test_negative_retries_rejected(self):
        with pytest.raises(ValueError):
            config_mod.CompactorConfig(max_retries=-1)

    def test_chars_keys_mapping_and_defaults(self):
        """chars 两键正常值生效；0/负数非法值 skip 回落默认（M-复审 L-1）。"""
        cfg = config_mod.config_from_mapping(
            {"prompt_max_chars": 12000, "hard_max_chars": 18000},
        )
        assert cfg.prompt_max_chars == 12000
        assert cfg.hard_max_chars == 18000
        for bad in (0, -5):
            cfg_bad = config_mod.config_from_mapping(
                {"hard_max_chars": bad, "prompt_max_chars": bad},
            )
            assert cfg_bad.hard_max_chars == 20000  # 默认
            assert cfg_bad.prompt_max_chars == 15000  # 默认

    def test_chars_direct_invalid_rejected(self):
        """直接构造 dataclass 传非法值 → __post_init__ 显式 ValueError
        （类型/值域双检：float/bool/str/0/负数）。"""
        for bad in (-5, 0, 20.5, True, "20"):
            with pytest.raises(ValueError):
                config_mod.CompactorConfig(hard_max_chars=bad)
            with pytest.raises(ValueError):
                config_mod.CompactorConfig(prompt_max_chars=bad)


# ---------------------------------------------------------------------------
# wire-dump 调试钩子（debug_dump=True；临时，验收后随 replace 切换关闭）
# ---------------------------------------------------------------------------


class TestWireDump:
    def _make_agent(self, formatter=None):
        model = SimpleNamespace()
        if formatter is not None:
            model.formatter = formatter
        return SimpleNamespace(model=model)

    def test_dump_disabled_config_skips_hook(self, tmp_path, monkeypatch):
        """config.debug_dump=False → 生成循环不调用 dump 钩子（开关在调用方）。"""
        monkeypatch.setenv("COMPACTOR_DUMP_DIR", str(tmp_path))
        calls: list[str] = []

        async def fake_compare(*a, **k):
            calls.append("compare")

        async def fake_usage(*a, **k):
            calls.append("usage")

        monkeypatch.setattr(_pkg.generation, "_dump_prefix_compare", fake_compare)
        monkeypatch.setattr(_pkg.generation, "_dump_usage", fake_usage)
        manager = FakeManager(seq_by_id={"id1": (3, 3)})
        agent = make_agent(lambda n, k: plain_response(FULL_REPLY))
        asyncio.run(
            run_conversation_summary(
                manager, agent, make_middle(1), focus_hint="",
                config=config_mod.CompactorConfig(debug_dump=False),
            ),
        )
        assert calls == []

    def test_dump_enabled_config_invokes_hook(self, tmp_path, monkeypatch):
        """config.debug_dump=True → compare(async) + usage(sync) 均被调用。"""
        monkeypatch.setenv("COMPACTOR_DUMP_DIR", str(tmp_path))
        calls: list[str] = []

        async def fake_compare(*a, **k):
            calls.append("compare")

        def fake_usage(*a, **k):  # 真函数是同步的（调用处无 await）
            calls.append("usage")

        monkeypatch.setattr(_pkg.generation, "_dump_prefix_compare", fake_compare)
        monkeypatch.setattr(_pkg.generation, "_dump_usage", fake_usage)
        manager = FakeManager(seq_by_id={"id1": (3, 3)})
        agent = make_agent(lambda n, k: plain_response(FULL_REPLY))
        summary = asyncio.run(
            run_conversation_summary(
                manager, agent, make_middle(1), focus_hint="",
                config=config_mod.CompactorConfig(debug_dump=True),
            ),
        )
        assert summary is not None
        assert calls == ["compare", "usage"]

    def test_dump_prefix_compare_writes_files(self, tmp_path, monkeypatch):
        """formatter 可用：dump 主请求/摘要请求 wire + 前缀比对结果。"""
        monkeypatch.setenv("COMPACTOR_DUMP_DIR", str(tmp_path))
        _pkg.generation._current_run_dir = None

        class FakeFormatter:
            def format(self, messages):
                # wire 形态：每条消息一个 dict
                return [
                    {"role": getattr(m, "role", "user"),
                     "content": getattr(m, "content", "")} for m in messages
                ]

        agent = self._make_agent(FakeFormatter())
        base = [UserMsg(name="u", content="历史", id="a1")]
        summary = list(base) + [UserMsg(name="summary_request", content="提问", id="q1")]
        asyncio.run(
            _pkg.generation._dump_prefix_compare(agent, base, summary),
        )

        run_dirs = [d for d in tmp_path.iterdir() if d.is_dir()]
        assert len(run_dirs) == 1
        names = {f.name for f in run_dirs[0].iterdir()}
        assert {"main_wire.jsonl", "summary_wire.jsonl", "compare.json"} <= names
        compare = json.loads((run_dirs[0] / "compare.json").read_text(encoding="utf-8"))
        assert compare["formatter_layer"] is True
        # 摘要请求 = 主请求 + 提问 → 主请求字节被完整覆盖（前缀命中形态）
        assert compare["main_fully_covered_by_summary"] is True
        assert compare["common_prefix_bytes"] == compare["main_wire_bytes"]

    def test_dump_prefix_compare_without_formatter(self, tmp_path, monkeypatch):
        """无 formatter：object-layer 兜底，不炸。"""
        monkeypatch.setenv("COMPACTOR_DUMP_DIR", str(tmp_path))
        _pkg.generation._current_run_dir = None
        agent = self._make_agent()
        base = [UserMsg(name="u", content="x", id="a1")]
        asyncio.run(_pkg.generation._dump_prefix_compare(agent, base, base + []))
        run_dirs = [d for d in tmp_path.iterdir() if d.is_dir()]
        assert len(run_dirs) == 1
        compare = json.loads((run_dirs[0] / "compare.json").read_text(encoding="utf-8"))
        assert compare["formatter_layer"] is False
        assert "object_layer" in compare

    def test_dump_usage_same_run_dir(self, tmp_path, monkeypatch):
        """usage 落在与 compare 相同的 run 目录（attempt 序号区分）。"""
        monkeypatch.setenv("COMPACTOR_DUMP_DIR", str(tmp_path))
        _pkg.generation._current_run_dir = None
        agent = self._make_agent()
        asyncio.run(_pkg.generation._dump_prefix_compare(agent, [], []))
        _pkg.generation._dump_usage(agent, 1, {"input_tokens": 10, "cache_input_tokens": 8}, "回复")
        run_dirs = [d for d in tmp_path.iterdir() if d.is_dir()]
        assert len(run_dirs) == 1  # 同目录
        usage_file = run_dirs[0] / "usage_attempt1.json"
        data = json.loads(usage_file.read_text(encoding="utf-8"))
        assert data["usage"]["cache_input_tokens"] == 8

    def test_common_prefix_len_cases(self):
        fn = _pkg.generation._common_prefix_len
        assert fn(b"abc", b"abc") == 3
        assert fn(b"abc", b"abd") == 2
        assert fn(b"", b"xyz") == 0
        assert fn(b"a" * 5000, b"a" * 4999 + b"b") == 4999  # 跨 4KB 块边界
        assert fn(b"x" * 10, b"x" * 10 + b"tail") == 10


class TestResolveConfig:
    """plugin._resolve_config 三分支回归（含 H-1 补键 debug_dump）。"""

    class _FakeConfig:
        def __init__(self, table):
            self._table = table

        def get(self, key, default=None):
            return self._table.get(key, default)

    class _FakeContext:
        def __init__(self, table):
            self.config = TestResolveConfig._FakeConfig(table)

    def test_nested_section_wins(self):
        """compactor 嵌套段存在 → 直接走 config_from_mapping(raw)。"""
        plugin_cls = _pkg.ContextCompactorRefinedPlugin
        ctx = self._FakeContext({"compactor": {"max_retries": 1}})
        cfg = plugin_cls._resolve_config(ctx)
        assert cfg.max_retries == 1
        assert cfg.mode == _pkg.config.MODE_REPLACE  # 未指定键用默认

    def test_flat_keys_include_debug_dump(self):
        """平铺键收集：debug_dump 在键列表内（H-1 回归）。"""
        plugin_cls = _pkg.ContextCompactorRefinedPlugin
        ctx = self._FakeContext({"debug_dump": True, "max_retries": 2})
        cfg = plugin_cls._resolve_config(ctx)
        assert cfg.debug_dump is True
        assert cfg.max_retries == 2

    def test_config_read_failure_falls_back_to_defaults(self):
        """config.get 抛异常 → 全默认，不阻塞宿主。"""
        class Boom:
            def get(self, key, default=None):
                raise RuntimeError("config boom")

        class Ctx:
            config = Boom()

        plugin_cls = _pkg.ContextCompactorRefinedPlugin
        cfg = plugin_cls._resolve_config(Ctx())
        assert cfg.mode == _pkg.config.MODE_REPLACE
        assert cfg.debug_dump is False
        assert cfg.max_retries == 3
