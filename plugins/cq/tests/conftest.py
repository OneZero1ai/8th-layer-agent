"""Shared pytest fixtures for the plugin tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

HOOK_PATH = Path(__file__).resolve().parent.parent / "hooks" / "cursor" / "cq_cursor_hook.py"
CC_HOOK_PATH = Path(__file__).resolve().parent.parent / "hooks" / "claude_code" / "cq_cc_hook.py"
CLAUDE_CODE_HOOK_DIR = Path(__file__).resolve().parent.parent / "hooks" / "claude_code"
AIGRP_HOOK_PATH = CLAUDE_CODE_HOOK_DIR / "cq_aigrp_pull.py"
ENDPOINT_PATH = CLAUDE_CODE_HOOK_DIR / "cq_endpoint.py"


def _load_module(name: str, path: Path) -> ModuleType:
    """Load a hook script as a module by path (hooks aren't a package)."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def hook() -> ModuleType:
    """Load cq_cursor_hook.py as a module once per test session.

    The hook script is not a package member so can't be imported directly; we
    use importlib.util to load it by path. The module has no mutable state
    between tests (each test uses a fresh tmp_path for state files), so a
    session-scoped fixture is safe and cheap.
    """
    spec = importlib.util.spec_from_file_location("cq_cursor_hook", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["cq_cursor_hook"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def cc_hook() -> ModuleType:
    """Load cq_cc_hook.py (Claude Code L2 ambient hook) as a module."""
    spec = importlib.util.spec_from_file_location("cq_cc_hook", CC_HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["cq_cc_hook"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def cq_endpoint() -> ModuleType:
    """Load cq_endpoint.py (shared L2 endpoint resolver) as a module."""
    return _load_module("cq_endpoint", ENDPOINT_PATH)


@pytest.fixture(scope="session")
def aigrp_hook() -> ModuleType:
    """Load cq_aigrp_pull.py (AIGRP ambient-pull hook) as a module.

    The hook inserts its own directory onto ``sys.path`` so its
    ``import cq_endpoint`` resolves; that runs at import time here too.
    """
    return _load_module("cq_aigrp_pull", AIGRP_HOOK_PATH)
