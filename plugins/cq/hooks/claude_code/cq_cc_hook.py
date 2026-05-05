#!/usr/bin/env python3
"""Claude Code lifecycle hook for cq ambient capture (L2).

Fires at deterministic moments so the cq capture flow doesn't depend on the
model remembering the CLAUDE.md nudge to reflect.

Modes (passed via --mode):
  post-tool-use   Append to a per-session tool history. If the recent window
                  shows an error-recovery pattern (a failed tool followed by a
                  same-surface tool that succeeded) OR three consecutive
                  same-surface successful tool calls, inject a system reminder
                  prompting the model to call ``mcp__cq__propose`` if the
                  workaround is share-worthy.
  stop            Inject a one-shot reminder to run ``/cq:reflect`` so the
                  session-end scan happens deterministically rather than
                  relying on operator/model memory.

Both modes:
  * stdlib-only — no third-party deps; the hook runs on every tool call so
    startup must be cheap.
  * idempotent — each fire-event is keyed and recorded; re-fires within the
    same session are suppressed.
  * rate-limit aware — respects a cached ``Retry-After`` from the
    server-side ``/api/v1/reflect/submit`` 429 response.

Output: when injecting, the hook writes a JSON object to stdout matching
Claude Code's hook-output schema:

    {"hookSpecificOutput": {"hookEventName": "<event>",
                            "additionalContext": "<reminder>"}}

Otherwise the hook exits 0 with empty stdout (Claude Code treats this as
"no opinion").

Hook payload reference (Claude Code 2026-04+):
  PostToolUse: {session_id, tool_name, tool_input, tool_response, cwd, ...}
  Stop:        {session_id, stop_hook_active, ...}

`tool_response` for failed tools commonly contains an "is_error": true field
or has a non-empty "error" key; we treat those as failures, plus the
out-of-band `is_error` top-level field if present.

Reference: docs/specs/batch-reflect-contract.md (the 4-hour-per-key 429 the
hook backs off on lives there).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# How many recent tool calls to keep in the rolling window. ≥3 consecutive
# same-surface successes triggers a "concentrated-work" nudge; pairs of
# (fail, same-surface success) trigger the recovery nudge.
HISTORY_WINDOW = 5

# Fields beyond `summary` of a tool call we keep for fingerprinting; chosen
# to be small + safe to write to disk.
MAX_INPUT_SNIPPET = 200

# Suppress nudges when a server-side rate-limit response (HTTP 429 with
# Retry-After) is fresher than this. Default Retry-After per
# batch-reflect-contract.md is 14400s (4h); we honour whatever the server
# stamped in the cache file.
DEFAULT_RETRY_AFTER_SECONDS = 4 * 60 * 60

# Sweep-old-state cutoff. Mirrors the cursor hook so abandoned sessions don't
# pile up on disk.
STATE_TTL_SECONDS = 24 * 60 * 60

# The two reminder strings the hook injects. Kept as module constants so
# tests can assert exact strings.
RECOVERY_REMINDER = (
    "[8l-cq] You just recovered from a tool error. If the workaround would "
    "help another agent in the same situation, propose a KU now via "
    "mcp__cq__propose."
)
CONCENTRATED_REMINDER = (
    "[8l-cq] Several recent tool calls hit the same surface. If you've "
    "discovered a non-obvious pattern, propose a KU now via mcp__cq__propose."
)
STOP_REMINDER = (
    "[8l-cq] Session ending. Run /cq:reflect to scan this session for KU candidates and submit any worth keeping."
)


def main() -> int:
    """Parse args, dispatch to the per-mode handler."""
    args = _parse_args()
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = _read_payload()

    if args.mode == "post-tool-use":
        return run_post_tool_use(state_dir, payload)
    if args.mode == "stop":
        return run_stop(state_dir, payload)
    print(f"unknown mode: {args.mode}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# PostToolUse
# ---------------------------------------------------------------------------


def run_post_tool_use(state_dir: Path, payload: dict) -> int:
    """Update tool history and inject a reminder if a pattern is detected."""
    session_id = payload.get("session_id") or payload.get("sessionId") or "unknown"
    tool_name = payload.get("tool_name") or payload.get("toolName") or ""
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    tool_response = payload.get("tool_response") or payload.get("toolResponse")

    if not tool_name:
        return 0

    is_error = _detect_error(payload, tool_response)
    surface = _surface_fingerprint(tool_name, tool_input)
    history = _load_history(state_dir, session_id)

    entry = {
        "tool": tool_name,
        "surface": surface,
        "error": is_error,
        "ts": int(time.time()),
    }
    history.append(entry)
    history = history[-HISTORY_WINDOW:]
    _save_history(state_dir, session_id, history)

    if _rate_limited(state_dir):
        return 0

    fired = _load_fired(state_dir, session_id)
    decision = _decide(history, fired)
    if decision is None:
        return 0

    event_key, reminder = decision
    fired.add(event_key)
    _save_fired(state_dir, session_id, fired)
    _emit_additional_context("PostToolUse", reminder)
    return 0


def _detect_error(payload: dict, tool_response) -> bool:
    """True if the tool reported an error.

    Claude Code's PostToolUse payload conventions vary by tool; we accept any
    of the known shapes:

    * Top-level ``is_error: true`` on the payload (some MCP tools).
    * ``tool_response`` is a dict with ``is_error: true`` or a non-empty
      ``error`` field.
    * ``tool_response`` is a string containing a recognisable error marker.
    * ``exit_code`` non-zero (Bash).
    """
    if payload.get("is_error") is True:
        return True
    exit_code = payload.get("tool_response_metadata", {}).get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        return True
    if isinstance(tool_response, dict):
        if tool_response.get("is_error") is True:
            return True
        err = tool_response.get("error")
        if isinstance(err, str) and err:
            return True
    if isinstance(tool_response, str):
        # Very loose; we'd rather over-detect recovery than under-detect.
        lowered = tool_response.lower()
        if "error:" in lowered or "traceback" in lowered or "command failed" in lowered:
            return True
    return False


def _surface_fingerprint(tool_name: str, tool_input: dict) -> str:
    """A short, stable identifier for "the surface this tool call touched".

    Used to detect "the next call hit the same surface" patterns. We keep the
    fingerprint coarse on purpose — exact-match would miss recovery edits to
    the same file but with different content.
    """
    if tool_name in {"Edit", "Write", "Read", "NotebookEdit"}:
        return _truncate(str(tool_input.get("file_path") or tool_input.get("path") or ""), MAX_INPUT_SNIPPET)
    if tool_name in {"Bash", "Shell"}:
        # First word of the command — same binary == same surface.
        cmd = str(tool_input.get("command") or "")
        first = cmd.strip().split(None, 1)[0] if cmd.strip() else ""
        return _truncate(first, MAX_INPUT_SNIPPET)
    if tool_name == "Grep":
        return _truncate(str(tool_input.get("path") or tool_input.get("pattern") or ""), MAX_INPUT_SNIPPET)
    return tool_name


def _decide(history: list, fired: set) -> tuple | None:
    """Return ``(event_key, reminder)`` or None.

    Detection rules (checked in priority order):

    1. **Error→recovery**: the most recent entry succeeded, AND there exists
       an earlier entry on the same surface that errored. The event_key is
       ``recovery:<surface>:<latest_ts>`` so each distinct recovery fires
       at most once.
    2. **Concentrated work**: the most recent ``HISTORY_WINDOW`` entries
       (need at least 3) all hit the same surface AND none errored. The
       event_key is ``concentrated:<surface>`` — fires at most once per
       (session, surface).
    """
    if not history:
        return None
    latest = history[-1]
    if latest["error"]:
        return None  # Don't fire on a fresh error; wait for recovery.

    # Rule 1: error-recovery
    for prior in history[:-1]:
        if prior["error"] and prior["surface"] == latest["surface"]:
            key = f"recovery:{latest['surface']}:{latest['ts']}"
            if key not in fired:
                return key, RECOVERY_REMINDER
            break  # already fired for this exact event; fall through to rule 2

    # Rule 2: concentrated work
    if len(history) >= 3:
        last_three = history[-3:]
        if all(not e["error"] for e in last_three) and len({e["surface"] for e in last_three}) == 1:
            key = f"concentrated:{latest['surface']}"
            if key not in fired:
                return key, CONCENTRATED_REMINDER

    return None


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------


def run_stop(state_dir: Path, payload: dict) -> int:
    """Inject the run-/cq:reflect reminder once per session-end."""
    session_id = payload.get("session_id") or payload.get("sessionId") or "unknown"

    # Claude Code re-fires Stop on compact/resume. Claude Code marks those with
    # ``stop_hook_active = true``; we also stamp our own marker so we never
    # fire twice for the same logical session-end.
    if payload.get("stop_hook_active") is True:
        return 0

    if _rate_limited(state_dir):
        return 0

    marker = state_dir / f"{session_id}-stopped.json"
    if marker.exists():
        return 0
    marker.write_text(json.dumps({"ts": int(time.time())}))
    _emit_additional_context("Stop", STOP_REMINDER)
    return 0


# ---------------------------------------------------------------------------
# Rate-limit awareness
# ---------------------------------------------------------------------------


def _rate_limited(state_dir: Path) -> bool:
    """True if the most recent batch-reflect 429 hasn't expired yet.

    The 429 cache file is global (one per machine) because the rate limit is
    per-session-key, not per-CC-session. Whoever wrote it last wins.
    """
    cache = state_dir / "rate429.json"
    if not cache.exists():
        return False
    try:
        record = json.loads(cache.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    until = record.get("retry_after_until")
    if not isinstance(until, (int, float)):
        return False
    return time.time() < until


def record_rate_limit(state_dir: Path, retry_after_seconds: int | None = None) -> None:
    """Public helper for the cq client to call after a 429 response.

    Kept here so the hook + the client share one cache schema. Not invoked
    from the hook itself; tests exercise it directly.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(retry_after_seconds, int) and retry_after_seconds > 0:
        seconds = retry_after_seconds
    else:
        seconds = DEFAULT_RETRY_AFTER_SECONDS
    cache = state_dir / "rate429.json"
    cache.write_text(json.dumps({"retry_after_until": int(time.time()) + seconds}))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_history(state_dir: Path, session_id: str) -> list:
    path = state_dir / f"{session_id}-history.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        return data
    return []


def _save_history(state_dir: Path, session_id: str, history: list) -> None:
    path = state_dir / f"{session_id}-history.json"
    path.write_text(json.dumps(history))


def _load_fired(state_dir: Path, session_id: str) -> set:
    path = state_dir / f"{session_id}-fired.json"
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    if isinstance(data, list):
        return set(data)
    return set()


def _save_fired(state_dir: Path, session_id: str, fired: set) -> None:
    path = state_dir / f"{session_id}-fired.json"
    path.write_text(json.dumps(sorted(fired)))


def _emit_additional_context(event_name: str, reminder: str) -> None:
    """Write a Claude-Code hook-output JSON object that injects ``reminder``.

    Schema reference (Claude Code hook output, current as of 2026-04):
        {"hookSpecificOutput": {"hookEventName": "<event>",
                                "additionalContext": "<text>"}}
    """
    payload = {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": reminder,
        }
    }
    sys.stdout.write(json.dumps(payload))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="cq_cc_hook")
    parser.add_argument("--mode", required=True, choices=["post-tool-use", "stop"])
    parser.add_argument(
        "--state-dir",
        default=os.environ.get(
            "CQ_CC_HOOK_STATE_DIR",
            str(Path.home() / ".cache" / "cq" / "cc-hooks"),
        ),
    )
    return parser.parse_args()


def _read_payload() -> dict:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "…"


if __name__ == "__main__":
    raise SystemExit(main())
