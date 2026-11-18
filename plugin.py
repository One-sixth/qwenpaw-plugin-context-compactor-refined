# -*- coding: utf-8 -*-
"""Context Compactor Refined 插件入口（SDK 形态，qwenpaw 2.3.0+）。

无工具 general 插件：激活时打 patch 点（ScrollContextManager 类 +
CommandHandler._stored_scroll_summary_text 常驻；CommandHandler._process_
compact 仅 shadow 模式安装——D7），卸载时逆序恢复。

生命周期契约（SDK 1.0.0）：``register()`` 仅声明（on_activate 注册），
资源创建 / 补丁安装必须进 ``on_activate``；卸载清理用
``context.resources.defer``（Registration 逆序执行）。

配置来源：宿主 settings（``config.plugins.<plugin_id>``，平铺键或
``compactor`` 嵌套段）→ PluginConfig → CompactorConfig；未提供时全部
用默认值（设计稿 §5）。⚠️ manifest ``config`` 段在宿主 2.3.0 不被消费
（MEMORY.md「传递机制未走通」实锤），plugin.json 中的 config 声明仅为
占位——调试开关（如 debug_dump）经宿主 settings 配置。
"""

from __future__ import annotations

import logging

from qwenpaw.plugins import PluginContext

from .config import CompactorConfig, config_from_mapping
from .host_compat import install, uninstall

logger = logging.getLogger(__name__)


class ContextCompactorRefinedPlugin:
    """Plugin main class (SDK Plugin Protocol)."""

    def register(self, context: PluginContext) -> None:
        logger.info("注册 Context Compactor Refined 插件（按模式打点）...")
        context.on_activate(self._activate)
        logger.info("✓ Context Compactor Refined 插件注册完成")

    def _activate(self, context: PluginContext) -> None:
        """Activate: resolve config, install patch points per mode, register
        the reverse-order uninstall via resources.defer."""
        config = self._resolve_config(context)
        if config is None:
            # 点位自检失败等场景不阻塞宿主：install 内部已按点位独立降级
            config = CompactorConfig()
        status = install(config)
        context.resources.defer(
            self._restore, "context_compactor_restore",
        )
        logger.info("Context Compactor Refined 激活完成：%s", status)

    def _restore(self) -> None:
        """Uninstall: restore both patch points (reverse order)."""
        uninstall()

    @staticmethod
    def _resolve_config(context: PluginContext) -> CompactorConfig:
        """Build config from host settings (``config.plugins.<id>``, flat
        keys or ``compactor`` nested section); unknown or malformed values
        fall back to defaults per key. Note: the manifest ``config`` section
        is NOT consumed by host 2.3.0 — settings are the only live source."""
        try:
            raw = context.config.get("compactor", None)
        except Exception as exc:  # config 面故障不阻塞宿主
            logger.warning(
                "context-compactor: config read failed, using defaults (%r)",
                exc,
            )
            return CompactorConfig()
        if raw is None:
            # 也接受平铺键（sections / max_retries / ... 直接放 settings）
            flat = {}
            for key in (
                "mode", "sections", "max_retries", "output_tokens",
                "disable_thinking", "cache_mode", "debug_dump",
                "question_template", "retry_template",
            ):
                try:
                    value = context.config.get(key, None)
                except Exception:
                    value = None
                if value is not None:
                    flat[key] = value
            return config_from_mapping(flat or None)
        return config_from_mapping(raw)


plugin = ContextCompactorRefinedPlugin()
