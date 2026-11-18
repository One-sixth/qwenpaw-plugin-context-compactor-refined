# -*- coding: utf-8 -*-
"""pytest fixtures：以宿主同款姿势加载插件包（import_definition 等价物），
并做 sys.path 快照恢复（防宿主 import 副作用外溢）。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
MODULE_NAME = "plugin_qwenpaw_plugin_context_compactor_refined"


def _load_plugin_module():
    spec = importlib.util.spec_from_file_location(
        MODULE_NAME,
        PLUGIN_DIR / "plugin.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    module.__package__ = MODULE_NAME
    module.__path__ = [str(PLUGIN_DIR)]
    spec.loader.exec_module(module)
    return module


_plugin_module = None


def plugin_module():
    global _plugin_module
    if _plugin_module is None:
        _plugin_module = _load_plugin_module()
    return _plugin_module


@pytest.fixture(autouse=True)
def _restore_sys_path():
    """Snapshot sys.path around every test (host imports may mutate it)."""
    snapshot = list(sys.path)
    yield
    sys.path[:] = snapshot


@pytest.fixture()
def pkg():
    """The loaded plugin package (shared across the test session)."""
    return plugin_module()
