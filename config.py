# -*- coding: utf-8 -*-
"""插件配置（config.py）。

默认值 + 宿主 settings 覆盖（``config.plugins.<plugin_id>``；manifest
config 段宿主 2.3.0 不消费——见 plugin.py docstring）。
冷却期机制：已否决（Q4）。超预算降级路径：已否决（D2，故意不设计）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields

from .prompts import DEFAULT_SECTIONS, QUESTION_TEMPLATE, RETRY_CORRECTION_TEMPLATE

# cache_mode 取值
CACHE_PRESERVE_TOOLS = "preserve_tools"  # 定案：保留 tools + ToolChoice(mode="none")
CACHE_NO_TOOLS = "no_tools"              # provider 不支持 tool_choice="none" 时的降级

# mode 取值（D5 拍板：临时调试机制）
MODE_SHADOW = "shadow"    # 调试期：默认透传原版，仅 /compact new 一次性触发新方案
MODE_REPLACE = "replace"  # 验收完成后：全部压缩走新方案；/compact new 拦截不安装（D7），args 原样进宿主 hint 通道

_VALID_MODES = (MODE_SHADOW, MODE_REPLACE)
_VALID_CACHE_MODES = (CACHE_PRESERVE_TOOLS, CACHE_NO_TOOLS)


@dataclass(frozen=True)
class CompactorConfig:
    """Summary-generation configuration (design doc §5)."""

    mode: str = MODE_REPLACE  # D5 验收完成（2026-11-18）：全量替换原版；shadow 分流保留待配
    sections: tuple[str, ...] = DEFAULT_SECTIONS
    max_retries: int = 3  # 首次 1 + 重试 3 = 共 4 次尝试（Q2 定案）
    output_tokens: int = 0  # 0 = 按宿主公式推导 max(256, min(4000, ctx//4))
    disable_thinking: bool = True
    cache_mode: str = CACHE_PRESERVE_TOOLS
    debug_dump: bool = False  # wire-dump 调试钩子（临时；宿主 settings 控制）
    question_template: str = QUESTION_TEMPLATE
    retry_template: str = RETRY_CORRECTION_TEMPLATE

    def __post_init__(self) -> None:
        if self.mode not in _VALID_MODES:
            raise ValueError(
                f"mode must be one of {_VALID_MODES}, got {self.mode!r}",
            )
        if not self.sections:
            raise ValueError("sections must not be empty")
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if self.cache_mode not in _VALID_CACHE_MODES:
            raise ValueError(
                f"cache_mode must be one of {_VALID_CACHE_MODES}, "
                f"got {self.cache_mode!r}",
            )
        if self.output_tokens < 0:
            raise ValueError("output_tokens must be >= 0")


def config_from_mapping(raw: dict | None) -> CompactorConfig:
    """Build a config from the manifest ``config`` mapping (unknown keys
    ignored; values type-checked per field)."""
    if not isinstance(raw, dict):
        return CompactorConfig()
    known = {f.name for f in fields(CompactorConfig)}
    kwargs: dict = {}
    for key, value in raw.items():
        if key not in known:
            continue
        if key == "sections":
            if isinstance(value, (list, tuple)) and value:
                kwargs[key] = tuple(str(item) for item in value)
        elif key == "mode":
            # 值域预检（复审 M-1）：非法值 skip 回落默认，避免穿透
            # __post_init__ 的 ValueError 导致插件激活整体回滚
            if isinstance(value, str) and value in _VALID_MODES:
                kwargs[key] = value
        elif key == "max_retries":
            if isinstance(value, int) and not isinstance(value, bool):
                kwargs[key] = max(0, int(value))
        elif key == "output_tokens":
            if isinstance(value, int) and not isinstance(value, bool):
                kwargs[key] = max(0, int(value))
        elif key == "disable_thinking":
            kwargs[key] = bool(value)
        elif key == "debug_dump":
            kwargs[key] = bool(value)
        elif key == "cache_mode":
            if isinstance(value, str) and value in _VALID_CACHE_MODES:
                kwargs[key] = value
        elif key in ("question_template", "retry_template"):
            if isinstance(value, str) and value.strip():
                kwargs[key] = value
    return CompactorConfig(**kwargs)
