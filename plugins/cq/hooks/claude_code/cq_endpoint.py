#!/usr/bin/env python3
"""Resolve the cq L2 endpoint (CQ_ADDR + CQ_API_KEY) for shell-launched hooks.

Why this module exists
----------------------
The cq **MCP server** and the cq **lifecycle hooks** reach the L2 by two
different paths, and only the hook path was fragile:

* The MCP server (``cq mcp``) is launched *by Claude Code* from the plugin's
  ``mcpServers.cq`` config block. Claude Code injects that block's ``env``
  into the subprocess, so ``CQ_ADDR`` lands in the server's environment even
  though it is never exported to the operator's shell. ``CQ_API_KEY`` is
  merged into the same block from the profile's ``secrets:`` map.

* A ``UserPromptSubmit`` / ``PostToolUse`` hook is launched as a plain shell
  command. It sees the *shell* environment, not the MCP block's ``env``. In a
  claude-mux session ``CQ_ADDR`` lives only in ``mcpServers.cq.env`` — so the
  hook observed ``CQ_ADDR`` unset and silently no-op'd (issue #279, Gap 1).
  The flagship ambient-retrieval feature was dead with zero operator signal.

The fix: hooks must resolve the endpoint the *same way the binary's config is
sourced* — from the claude-mux profile JSON that the MCP block was generated
from — instead of trusting a bare ``$CQ_ADDR`` env var.

Resolution order
----------------
1. ``$CQ_ADDR`` / ``$CQ_API_KEY`` from the environment (fast path; honoured
   when a session *does* export them, e.g. the ``dw-l2`` profile shape).
2. The active claude-mux profile JSON under ``~/.claude-mux/profiles/``.
   Three on-disk shapes are supported, checked in order:
     a. ``mcpServers.cq.env.CQ_ADDR``         — the 8l-cq plugin profile shape
        (``secrets.CQ_API_KEY`` for the key — already resolved into the env,
        so step 1 normally covers the key).
     b. ``env.CQ_ADDR``                       — the dw-l2 profile shape.
     c. top-level ``cq_addr`` / ``cq_api_key`` — the ``8l join`` CLI profile
        shape written to ``~/.claude-mux/profiles/<persona>@<ent>-<l2>.json``.

The active profile is chosen by ``$CLAUDE_PROFILE`` (claude-mux exports it),
falling back to ``$CQ_SESSION`` then ``$CLAUDE_SESSION_DISPLAY_NAME``. If none
is set, every profile file is scanned and the first that yields a CQ_ADDR
*and* whose name matches a cq/8l profile is used; this keeps single-profile
installs working without any env wiring.

Stdlib-only by design — this module is imported by hooks that run on every
prompt, so it must stay dependency-free and cheap to import.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import NamedTuple

# Default location claude-mux writes session profiles to. Overridable via
# CQ_PROFILES_DIR so tests (and non-standard installs) can redirect it.
DEFAULT_PROFILES_DIR = Path.home() / ".claude-mux" / "profiles"


class Endpoint(NamedTuple):
    """A resolved L2 endpoint plus where it came from (for the liveness line)."""

    addr: str | None
    api_key: str | None
    source: str  # "env", "profile:<name>", or "unresolved"

    @property
    def ok(self) -> bool:
        """True when both an address and a key are present."""
        return bool(self.addr) and bool(self.api_key)


def profiles_dir() -> Path:
    """Return the claude-mux profiles directory, honouring ``$CQ_PROFILES_DIR``."""
    override = os.environ.get("CQ_PROFILES_DIR")
    if override:
        return Path(override)
    return DEFAULT_PROFILES_DIR


def _addr_key_from_profile(data: dict) -> tuple[str | None, str | None]:
    """Pull (CQ_ADDR, CQ_API_KEY) out of one parsed profile JSON.

    Tries the three known on-disk shapes (see module docstring) in order.
    A secret reference such as ``ssm://...`` is *not* a usable key; only a
    literal value is returned. Returning ``None`` for the key is fine — the
    key almost always arrives via the environment (claude-mux merges
    ``secrets:`` into the live env), so address resolution is the load-bearing
    part here.
    """
    addr: str | None = None
    api_key: str | None = None

    # Shape (a): 8l-cq plugin profile — CQ_ADDR lives in the MCP server block.
    mcp_env = (data.get("mcpServers") or {}).get("cq", {}).get("env") or {}
    if isinstance(mcp_env, dict):
        addr = addr or mcp_env.get("CQ_ADDR")
        api_key = api_key or _literal(mcp_env.get("CQ_API_KEY"))

    # Shape (b): dw-l2 profile — CQ_ADDR in top-level env.
    top_env = data.get("env") or {}
    if isinstance(top_env, dict):
        addr = addr or top_env.get("CQ_ADDR")
        api_key = api_key or _literal(top_env.get("CQ_API_KEY"))

    # Shape (c): `8l join` CLI profile — flat cq_addr / cq_api_key.
    addr = addr or data.get("cq_addr")
    api_key = api_key or _literal(data.get("cq_api_key"))

    return addr, api_key


def _literal(value: object) -> str | None:
    """Return ``value`` only if it is a usable literal, not a secret reference.

    Profile ``secrets:`` entries hold URIs like ``ssm://8l-cq/foo/api-key`` or
    ``keychain://...`` — placeholders claude-mux resolves at launch. Those are
    not bearer tokens; treat them as absent so the caller falls back to the
    environment (where the resolved key actually lives).
    """
    if not isinstance(value, str) or not value:
        return None
    if "://" in value or value.startswith("${"):
        return None
    return value


def _load_profile(path: Path) -> dict | None:
    """Parse one profile JSON file, returning ``None`` on any read/parse error."""
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _active_profile_names() -> list[str]:
    """Return candidate profile names from the environment, most-specific first."""
    names: list[str] = []
    for var in ("CLAUDE_PROFILE", "CQ_SESSION", "CLAUDE_SESSION_DISPLAY_NAME"):
        value = os.environ.get(var)
        if value and value not in names:
            names.append(value)
    return names


def resolve(env: dict | None = None) -> Endpoint:
    """Resolve the cq L2 endpoint for a shell-launched hook.

    See the module docstring for the full resolution order. ``env`` defaults
    to ``os.environ`` and is injectable for tests.
    """
    env = os.environ if env is None else env

    # Step 1 — environment fast path.
    env_addr = env.get("CQ_ADDR")
    env_key = env.get("CQ_API_KEY")
    if env_addr:
        return Endpoint(addr=env_addr, api_key=env_key, source="env")

    # Step 2 — claude-mux profile JSON.
    pdir = profiles_dir()
    if not pdir.is_dir():
        return Endpoint(addr=None, api_key=env_key, source="unresolved")

    # 2a — try the explicitly-named active profile(s) first.
    for name in _active_profile_names():
        path = pdir / f"{name}.json"
        if not path.is_file():
            continue
        data = _load_profile(path)
        if data is None:
            continue
        addr, api_key = _addr_key_from_profile(data)
        if addr:
            return Endpoint(addr=addr, api_key=api_key or env_key, source=f"profile:{name}")

    # 2b — no env hint (or it didn't carry CQ_ADDR): scan for the first
    # cq/8l profile that yields an address. Sorted for deterministic pick.
    for path in sorted(pdir.glob("*.json")):
        name = path.stem
        if "cq" not in name and not name.startswith("8l") and "l2" not in name:
            continue
        data = _load_profile(path)
        if data is None:
            continue
        addr, api_key = _addr_key_from_profile(data)
        if addr:
            return Endpoint(addr=addr, api_key=api_key or env_key, source=f"profile:{name}")

    return Endpoint(addr=None, api_key=env_key, source="unresolved")
