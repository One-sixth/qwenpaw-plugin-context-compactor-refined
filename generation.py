# -*- coding: utf-8 -*-
"""对话式压缩摘要生成循环（generation.py）。

设计稿 §2.4 定稿流程：

① 入口 guard（复刻原版两行）：new_span None / model 不可调用 → None
   （无需生成，压缩照常继续——这不是失败）。
② previous 校验（retention purge 防御）+ covered span 纯代码合并。
③ 摘要请求与主请求同源构建（prepare_model_input 输出整体复用，
   messages 只追加尾部提问，tools 原样保留）——prefix cache 逐字节对齐；
   禁工具用 ``ToolChoice(mode="none")`` 对象形态（H-A：裸字符串会引爆
   AttributeError）。
④ 对话内重试循环（首次 1 + 重试 max_retries，共 4 次尝试，Q2 定案）：
   流消费复用原版范式（isasyncgen 分支 + _raise_if_summary_interrupted，
   R2-发现3）→ 轻量中文章节校验（允许尾随冒号，R3-发现9）→ 缺章节则
   追加失败稿 + 纠正提问（作者原话模板）。
⑤ provider 400/认证/限流类错误立即包装 raise，不烧重试（H-B 衍生）。
⑥ 全部尝试后仍缺章节 → SummaryGenerationFailed（消息自包含）。

超预算行为（D2 定案，故意不降级）：无预算检查、无降级路径——tokens
逼近上限时摘要请求照发，provider 超限错误自然上浮为压缩失败。

摘要末端固化块（_append_recall_hint）：校验通过后纯代码追加「压缩完成
时间戳（当地时间+UTC 偏移）+ recall_history 即用指引（含 covered 区间）」
——零 tokens、字节级一致、不依赖模型遵守提示词；covered 异常降级为
通用指引，绝不抛。
"""

from __future__ import annotations

import inspect
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentscope.message import AssistantMsg, UserMsg
from agentscope.tool import ToolChoice

from qwenpaw.agents.context import _as_internals as as_internals

from .config import CompactorConfig, CACHE_PRESERVE_TOOLS
from .prompts import build_question, build_retry_correction
from .summary_object import FreeTextSummary

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agentscope.message import Msg

# 提问 UserMsg 的显式 name（R2-发现7：避免 wire name 为 null）
SUMMARY_REQUEST_NAME = "summary_request"

# 章节校验：二级标题整行匹配，允许尾随冒号（R3-发现9），允许标题行尾空白
_SECTION_HEADING_TEMPLATE = r"^##\s*{name}\s*[:：]?\s*$"

# wire-dump 落盘目录（临时调试钩子；COMPACTOR_DUMP_DIR 可覆盖）
_DUMP_ROOT_ENV = "COMPACTOR_DUMP_DIR"
_DUMP_FALLBACK = (
    Path.home() / ".qwenpaw" / "plugin-data" / "context-compactor-refined" / "dumps"
)

# ---------------------------------------------------------------------------
# 摘要末端固化块：压缩完成时间戳 + 查看压缩前上下文的即用指引。
# 纯代码追加（不进提示词）：零 tokens、字节级一致、永不遗漏；
# 在章节校验通过后追加，不参与校验。covered 异常降级为通用指引。
# ---------------------------------------------------------------------------

RECALL_HINT_TEMPLATE = (
    "\n\n---\n"
    "上下文压缩已完成。当前时间：{stamp}\n"
    "\n"
    "📎 查看压缩前原始上下文（本摘要覆盖 seq {lo}–{hi}）：\n"
    '- 读原始轮次全文：recall_history(op="expand", lo={lo}, hi={hi})\n'
    '- 按关键词搜索：recall_history(op="search", query="<关键词>", k=20)\n'
    "- 折叠的工具结果按 [scroll folded] 标注的 tool_call_id 召回：\n"
    '  recall_history(op="recall_tool", tool_call_id="<id>")'
)

RECALL_HINT_GENERIC = (
    "\n\n---\n"
    "上下文压缩已完成。当前时间：{stamp}\n"
    "\n"
    "📎 查看压缩前原始上下文：按压缩地图（[context compressed]）标注的 seq\n"
    '区间召回——recall_history(op="expand", lo=[区间起点], hi=[区间终点])；\n'
    "折叠的工具结果按 [scroll folded] 的 tool_call_id 用\n"
    'recall_history(op="recall_tool", tool_call_id="<id>") 召回。'
)


class SummaryGenerationFailed(RuntimeError):
    """压缩摘要生成失败（校验不过 / 超时 / 模型错误 / 超预算 400）。

    抛出后压缩终止（不驱逐），异常沿三条调用路径各自落点上浮：
    自动路径 = 当前回合失败；压缩工具 = ToolChunk 错误文本；
    /compact 命令 = 「Compact Failed! Reason: ...」（设计稿 §3.2）。
    """


def missing_sections(reply: str, sections: tuple[str, ...]) -> list[str]:
    """Return required sections whose heading is absent from ``reply``.

    Light validation only: heading existence, trailing colon tolerated,
    same-line content still rejected (design doc §3.4).
    """
    missing: list[str] = []
    for name in sections:
        pattern = _SECTION_HEADING_TEMPLATE.format(name=re.escape(name))
        if not re.search(pattern, reply, re.MULTILINE):
            missing.append(name)
    return missing


async def run_conversation_summary(
    manager: Any,
    agent: Any,
    middle: list["Msg"],
    *,
    focus_hint: str,
    config: CompactorConfig,
) -> FreeTextSummary | None:
    """Generate a free-text continuation summary by conversing with the
    model over the full real context.

    Returns ``None`` when generation is unnecessary (nothing evicted or no
    callable model — mirrors the upstream early-return so compression keeps
    its previous summary). Raises :class:`SummaryGenerationFailed` when
    generation fails after all attempts (aborts compression; no eviction).
    """
    # ① 入口 guard（复刻原版，复审 R3-发现2）
    new_span = manager._evicted_span(middle)
    if new_span is None or not callable(getattr(agent, "model", None)):
        return None

    # ② previous 校验（继承原版 retention purge 防御）+ covered 合并
    previous = manager._continuation_summary
    if previous is not None:
        endpoints = set(previous.covered_seq)
        existing = await _to_thread(manager._history.existing_seqs, endpoints)
        previous = manager._source_backed_previous_summary(existing)
    covered = (
        (
            min(previous.covered_seq[0], new_span[0]),
            max(previous.covered_seq[1], new_span[1]),
        )
        if previous is not None
        else new_span
    )

    # 输出预算：0 = 不传 max_tokens（端点默认）；>0 = 传入，≤32000 时
    # 抬高到 32000（思考+正文总预算，2026-11-23 定案，替代宿主公式）
    output_tokens: int | None = None
    if config.output_tokens > 0:
        output_tokens = (
            32000 if config.output_tokens <= 32000 else config.output_tokens
        )

    try:
        return await _generation_attempts(
            manager,
            agent,
            focus_hint=focus_hint,
            config=config,
            covered=covered,
            output_tokens=output_tokens,
        )
    except SummaryGenerationFailed:
        raise
    except Exception as exc:  # provider/auth/transport/limit errors
        # ⑤ 立即失败，不烧重试（H-B 衍生；原版语义 manager.py:1540-1545）。
        # CancelledError（BaseException 家族）不被本子句捕获，自然上传播。
        raise SummaryGenerationFailed(
            f"压缩摘要生成失败：{type(exc).__name__}: {exc}",
        ) from exc


async def _to_thread(func: Any, *args: Any) -> Any:
    import asyncio

    return await asyncio.to_thread(func, *args)


def _local_time_stamp() -> str:
    """当地时间 + UTC 偏移（如 ``2026-11-18 21:30:45 +08:00``）。"""
    now = datetime.now().astimezone()
    offset = now.strftime("%z")  # e.g. "+0800"
    # 防秒级偏移畸形（+130015 → +13:0015）：仅对标准 +HHMM 形态插冒号
    if len(offset) == 5 and offset[0] in "+-":
        offset = f"{offset[:3]}:{offset[3:]}"  # +08:00
    return f"{now.strftime('%Y-%m-%d %H:%M:%S')} {offset}"


def _append_recall_hint(reply: str, covered: tuple[int, int]) -> str:
    """成功稿末端追加固化块（压缩完成时间 + recall 即用指引）。

    纯代码追加：零 tokens、字节级一致、不依赖模型遵守提示词。
    任何异常（stamp 计算失败、covered 非法）都降级——固化块是
    增强层，绝不让已成功的压缩失败。"""
    try:
        stamp = _local_time_stamp()
    except Exception:
        stamp = "未知时间"
    try:
        lo, hi = int(covered[0]), int(covered[1])
        if lo <= hi:
            tail = RECALL_HINT_TEMPLATE.format(stamp=stamp, lo=lo, hi=hi)
        else:
            tail = RECALL_HINT_GENERIC.format(stamp=stamp)
    except Exception:
        tail = RECALL_HINT_GENERIC.format(stamp=stamp)
    return reply.rstrip() + tail


async def _generation_attempts(
    manager: Any,
    agent: Any,
    *,
    focus_hint: str,
    config: CompactorConfig,
    covered: tuple[int, int],
    output_tokens: int | None,
) -> FreeTextSummary:
    """The retry loop itself. Only missing-section outcomes retry here;
    any model exception propagates to the caller untouched."""
    # ③ 与主请求同源构建（prefix cache 逐字节对齐，设计稿 §3.1 三件套）
    base = await as_internals.prepare_model_input(agent)
    messages: list["Msg"] = list(base.get("messages") or [])
    tools = base.get("tools") if config.cache_mode == CACHE_PRESERVE_TOOLS else None
    tool_choice = (
        ToolChoice(mode="none") if tools else None  # H-A：对象形态定案
    )

    question = build_question(
        config.sections,
        focus_hint,
        template=config.question_template,
        max_chars=config.prompt_max_chars,
    )
    messages.append(
        UserMsg(name=SUMMARY_REQUEST_NAME, content=question),
    )

    # wire-dump：主请求 vs 摘要请求的 formatter 字节比对（临时调试钩子）
    if config.debug_dump:
        await _dump_prefix_compare(agent, base.get("messages") or [], messages)

    total_attempts = 1 + config.max_retries
    missing: list[str] = []
    for attempt in range(total_attempts):
        reply, usage = await _invoke_model(
            manager,
            agent,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=output_tokens,
            disable_thinking=config.disable_thinking,
        )
        if config.debug_dump:
            _dump_usage(agent, attempt + 1, usage, reply)
        # 硬上限截断（纯代码，2026-11-23 定案）：超长原稿截到 hard_max_chars，
        # 截掉章节 → 下方 missing 检查自然非空 → 走既有对话内重试通道。
        if len(reply) > config.hard_max_chars:
            reply = reply[: config.hard_max_chars]
        missing = missing_sections(reply, config.sections)
        if not missing:
            # 成功：对话文本零落盘（token 用量正常记录）。
            # 末端追加固化块（压缩完成时间 + recall 即用指引）——
            # 校验针对模型原稿，固化块是基础设施层，追加在校验之后。
            return FreeTextSummary(_append_recall_hint(reply, covered), covered)
        # ④ 对话内重试：追加失败稿 + 纠正提问（作者原话模板）
        if attempt < total_attempts - 1:
            messages.append(AssistantMsg(name="assistant", content=reply))
            messages.append(
                UserMsg(
                    name=SUMMARY_REQUEST_NAME,
                    content=config.retry_template.format(
                        missing="、".join(missing),
                    ),
                ),
            )
    # ⑥ 全部尝试后仍缺章节
    raise SummaryGenerationFailed(
        f"压缩摘要生成失败（{total_attempts} 次尝试）："
        f"缺少章节 {'、'.join(missing) or '(unknown)'}",
    )


async def _invoke_model(
    manager: Any,
    agent: Any,
    *,
    messages: list["Msg"],
    tools: Any,
    tool_choice: Any,
    max_tokens: int | None,
    disable_thinking: bool,
) -> tuple[str, dict | None]:
    """Call ``agent.model`` directly and consume the response.

    Returns ``(reply_text, usage_dict)`` — usage is best-effort extracted
    for the wire-dump debug hook (cache-hit evidence). Stream consumption
    mirrors the upstream ``_generate_plain_summary`` (isasyncgen branch +
    interruption guard, R2-发现3). Any exception the model raises
    propagates untouched so provider errors fail immediately.
    ``max_tokens`` is omitted from the call when ``None`` (endpoint
    default output budget).
    """
    kwargs: dict[str, Any] = {
        "messages": messages,
        "disable_thinking": disable_thinking,
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if tools:
        kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
    response = await agent.model(**kwargs)
    if not inspect.isasyncgen(response):
        manager._raise_if_summary_interrupted(response)
        usage = _usage_dict(getattr(response, "usage", None))
        return manager._response_text(response), usage
    deltas: list[str] = []
    final = ""
    usage: dict | None = None
    async for chunk in response:
        manager._raise_if_summary_interrupted(chunk)
        chunk_usage = getattr(chunk, "usage", None)
        if chunk_usage is not None:
            usage = _usage_dict(chunk_usage)  # 收尾累计 chunk 携带全量 usage
        text = manager._response_text(chunk)
        if getattr(chunk, "is_last", False):
            final = text
        elif text:
            deltas.append(text)
    return final or "".join(deltas).strip(), usage

# ---------------------------------------------------------------------------
# wire-dump 调试钩子（临时；config.debug_dump 开关，异常全吞绝不影响压缩）
# ---------------------------------------------------------------------------


def _usage_dict(usage: Any) -> dict | None:
    """Best-effort normalize an agentscope/pydantic usage object to dict."""
    if usage is None:
        return None
    for meth in ("model_dump", "dict"):
        try:
            value = getattr(usage, meth)()
            if isinstance(value, dict):
                return value
        except Exception:
            continue
    try:
        return dict(usage)
    except Exception:
        return {"repr": repr(usage)}


def _find_formatter(agent: Any) -> Any:
    """Unwrap recording-wrapper chains to locate the model's formatter."""
    model = getattr(agent, "model", None)
    seen = 0
    while model is not None and seen < 6:
        formatter = getattr(model, "formatter", None)
        if formatter is not None and hasattr(formatter, "format"):
            return formatter
        model = getattr(model, "model", None)
        seen += 1
    return None


async def _wire_lines(agent: Any, messages: list["Msg"]) -> list[str] | None:
    """Serialize a message list through the real formatter (wire layer),
    one JSON document per message. ``formatter.format`` is async in the
    host's compat formatters (复审跟进：同步调用返回 coroutine 曾被吞)."""
    formatter = _find_formatter(agent)
    if formatter is None:
        return None
    try:
        out = formatter.format(list(messages))
        if inspect.isawaitable(out):
            out = await out
        return [
            json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            for item in out
        ]
    except Exception:
        return None


def _common_prefix_len(a: bytes, b: bytes) -> int:
    """Byte-level common prefix length of the two payloads (chunked scan)."""
    min_len = min(len(a), len(b))
    step = 4096
    pos = 0
    while pos < min_len:
        chunk = min(step, min_len - pos)
        if a[pos:pos + chunk] == b[pos:pos + chunk]:
            pos += chunk
        else:
            for i in range(pos, pos + chunk):
                if a[i] != b[i]:
                    return i
            return pos + chunk
    return pos


def _dump_root() -> Path:
    root = os.environ.get(_DUMP_ROOT_ENV)
    return Path(root) if root else _DUMP_FALLBACK


# 当前 run 目录（compare 先建，usage 落同一目录；调试钩子容忍并发简陋性）
_current_run_dir: Path | None = None


def _get_run_dir() -> Path:
    global _current_run_dir
    if _current_run_dir is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        _current_run_dir = _dump_root() / stamp
    return _current_run_dir


def _dump_files(run_dir: Path, files: dict[str, str]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (run_dir / name).write_text(text, encoding="utf-8")


async def _dump_prefix_compare(agent: Any, base: list["Msg"], summary: list["Msg"]) -> None:
    """Byte-compare the main-request wire bytes against the summary-request
    wire bytes. The summary request is ``base + [question]`` — a prefix hit
    means ``common_prefix_bytes == len(main_bytes)`` exactly."""
    global _current_run_dir
    try:
        _current_run_dir = None
        run_dir = _get_run_dir()
        main_lines = await _wire_lines(agent, base)
        summary_lines = await _wire_lines(agent, summary)
        payload: dict[str, Any] = {
            "captured_at": datetime.now().isoformat(),
            "main_msg_count": len(base),
            "summary_msg_count": len(summary),
            "formatter_layer": main_lines is not None,
        }
        if main_lines is not None and summary_lines is not None:
            main_bytes = "\n".join(main_lines).encode("utf-8")
            summary_bytes = "\n".join(summary_lines).encode("utf-8")
            prefix = _common_prefix_len(main_bytes, summary_bytes)
            payload.update(
                {
                    "main_wire_bytes": len(main_bytes),
                    "summary_wire_bytes": len(summary_bytes),
                    "common_prefix_bytes": prefix,
                    "main_fully_covered_by_summary": prefix == len(main_bytes),
                    "tail_added_bytes": len(summary_bytes) - prefix,
                },
            )
            _dump_files(
                run_dir,
                {
                    "main_wire.jsonl": "\n".join(main_lines),
                    "summary_wire.jsonl": "\n".join(summary_lines),
                    "compare.json": json.dumps(payload, ensure_ascii=False, indent=2),
                },
            )
        else:
            # formatter unavailable: object-layer fallback (still proves the
            # same-source construction; wire layer stays for provider usage)
            payload["object_layer"] = {
                "main": [json.dumps({"role": getattr(m, "role", None), "id": getattr(m, "id", None)}, default=str) for m in base],
                "summary_tail": json.dumps({"role": getattr(summary[-1], "role", None), "id": getattr(summary[-1], "id", None)}, default=str) if summary else None,
            }
            _dump_files(run_dir, {"compare.json": json.dumps(payload, ensure_ascii=False, indent=2)})
    except Exception:
        return  # debug dump must never break compression


def _dump_usage(agent: Any, attempt: int, usage: dict | None, reply: str) -> None:
    """Persist the summary-request response usage (provider-authoritative
    cache-hit evidence: cache_input_tokens / cached_tokens 等)."""
    try:
        run_dir = _get_run_dir()
        payload = {
            "attempt": attempt,
            "usage": usage,
            "reply_bytes": len(reply.encode("utf-8")),
            "reply_head": reply[:200],
        }
        _dump_files(run_dir, {f"usage_attempt{attempt}.json": json.dumps(payload, ensure_ascii=False, indent=2)})
    except Exception:
        return
