# -*- coding: utf-8 -*-
"""FreeTextSummary 适配器单元测试（设计稿 §6 单元层）。"""

from __future__ import annotations

import pytest

from conftest import plugin_module

_pkg = plugin_module()
summary_object = _pkg.summary_object
FreeTextSummary = summary_object.FreeTextSummary
SUMMARY_MODE = summary_object.SUMMARY_MODE


def _summary():
    return FreeTextSummary("## 当前任务\nx", (10, 20))


class TestRoundTrip:
    def test_render_returns_text_as_is(self):
        assert _summary().render() == "## 当前任务\nx"

    def test_to_dict_shape(self):
        data = _summary().to_dict()
        assert data["mode"] == SUMMARY_MODE == "free_text"
        assert data["version"] == 2
        assert data["covered_seq"] == [10, 20]
        assert data["text"] == "## 当前任务\nx"

    def test_from_dict_round_trip(self):
        data = _summary().to_dict()
        rebuilt = FreeTextSummary.from_dict(data)
        assert isinstance(rebuilt, FreeTextSummary)
        assert rebuilt.render() == "## 当前任务\nx"
        assert rebuilt.covered_seq == (10, 20)


class TestFromDictTolerance:
    @pytest.mark.parametrize(
        "data",
        [
            None,
            {},
            {"mode": "structured_v1", "covered_seq": [1, 2], "text": "x"},
            {"mode": "free_text", "text": "x"},                      # no covered_seq
            {"mode": "free_text", "covered_seq": [5], "text": "x"},  # too short
            {"mode": "free_text", "covered_seq": "ab", "text": "x"}, # wrong type
            {"mode": "free_text", "covered_seq": [20, 10], "text": "x"},  # lo > hi
            {"mode": "free_text", "covered_seq": [1, 2], "text": "   "},  # empty
            {"mode": "free_text", "covered_seq": [1, 2]},            # no text
        ],
    )
    def test_invalid_dicts_return_none(self, data):
        assert FreeTextSummary.from_dict(data) is None


class TestCoveredSeqNormalization:
    def test_coerces_to_int_tuple(self):
        s = FreeTextSummary("t", ("3", "9"))
        assert s.covered_seq == (3, 9)
        assert isinstance(s.covered_seq, tuple)


class TestRenderBackground:
    def test_without_envelope_contains_prefix_and_history_pointer(self):
        text = _summary().render_background(include_envelope=False)
        assert "[archived task state]" in text          # SUMMARY_PREFIX
        assert "sequence range 10–20" in text            # durable pointer
        assert "## 当前任务" in text                      # body
        assert "<system-info>" not in text

    def test_with_envelope_wraps_system_info(self):
        text = _summary().render_background(include_envelope=True)
        assert text.startswith("<system-info>\n")
        assert text.endswith("</system-info>")

    def test_stale_marker(self):
        fresh = _summary().render_background(include_envelope=False)
        stale = _summary().render_background(stale=True, include_envelope=False)
        assert "Summary status: stale" not in fresh
        assert "Summary status: stale" in stale

    def test_matches_upstream_shape(self):
        """与宿主 ContinuationSummary.render_background 同形（除正文）。"""
        from qwenpaw.agents.context.scroll.continuation_summary import (
            SUMMARY_PREFIX,
        )

        text = _summary().render_background(include_envelope=False)
        assert text.startswith(SUMMARY_PREFIX)
