#!/usr/bin/env python3
"""Claude Code ``UserPromptSubmit`` hook for 8l-cq AIGRP ambient pull.

On every prompt this hook asks the tenant L2 for knowledge units relevant to
what the operator just typed and injects the hits as a ``<system-reminder>``
block — the "seamless ambient query" half of the cq experience (the other
half being the model proposing KUs back).

Modes (``--mode``):

  pull     The ``UserPromptSubmit`` path. Read Claude Code's hook JSON on
           stdin, resolve the L2 endpoint, POST the prompt to
           ``/api/v1/aigrp/lookup``, and emit any hits as injected context.

  status   Operator-facing liveness check. Resolve the endpoint and print a
           one-line human-readable summary of whether the ambient hook can
           reach the L2 — independent of ``AIGRP_DEBUG``. Invoked by the
           ``/cq:status`` command (or directly).

Design contract (matches the cursor / cc hooks in this directory):

  * **stdlib-only** — runs on every prompt, so import + startup must be cheap.
  * **best-effort** — in ``pull`` mode *any* failure exits 0 with empty
    stdout. A broken hook must never block prompt submission.
  * **quiet on the happy path** — ``pull`` emits a reminder block only when
    the L2 returns hits; zero hits / errors produce no output.

The #279 fix
------------
The original ``8l-cq-aigrp-pull.sh`` bailed at its first guard whenever
``$CQ_ADDR`` was unset and gave the operator *no signal*. In a claude-mux
session ``CQ_ADDR`` lives in the MCP server's config block, not the shell —
so the hook was permanently dead while the cq MCP server worked fine.

Two changes close the gap:

  1. Endpoint resolution is delegated to :mod:`cq_endpoint`, which falls back
     to the claude-mux profile JSON (the same config the MCP server block is
     generated from) when ``$CQ_ADDR`` is absent. The hook now finds the L2
     whenever the MCP server can.
  2. The ``status`` mode + a one-line stderr breadcrumb make "the ambient
     hook is off / can't reach the L2" detectable without enabling
     ``AIGRP_DEBUG``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

# Resolve the sibling endpoint module whether the hook is launched as a script
# (``python3 .../cq_aigrp_pull.py``) or imported by the test suite.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cq_endpoint  # noqa: E402

# --- Tunables (env-overridable, mirroring the original shell hook) ----------
#
# Read at call time, not import time: the hook module is loaded once and
# reused, so a frozen import-time snapshot would ignore per-invocation env.

# Default values; the env var name is checked live by the accessors below.
_DEFAULTS = {
    "AIGRP_TIMEOUT_S": "1.5",
    "AIGRP_MIN_PROMPT": "10",
    "AIGRP_MAX_HITS": "5",
    "AIGRP_MIN_CONF": "0.5",
    "AIGRP_MIN_SIM": "0.3",
}


def _tunable(name: str, cast):
    """Read an env-overridable tunable, falling back to its default."""
    return cast(os.environ.get(name, _DEFAULTS[name]))


def _heartbeat_path() -> Path:
    """Path of the liveness breadcrumb the hook stamps on every run.

    ``--mode status`` reads it back so the operator can see when the hook last
    fired and what happened — the AIGRP_DEBUG-independent signal.
    """
    return Path(
        os.environ.get(
            "AIGRP_HEARTBEAT_PATH",
            str(Path.home() / ".cache" / "cq" / "aigrp-heartbeat.json"),
        )
    )


def _log(message: str) -> None:
    """Append a debug line to ``~/.claude-mux/aigrp.log`` when AIGRP_DEBUG is set."""
    if not os.environ.get("AIGRP_DEBUG"):
        return
    try:
        log_path = Path.home() / ".claude-mux" / "aigrp.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%H:%M:%S")
        with log_path.open("a") as fh:
            fh.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


def _write_heartbeat(outcome: str, detail: dict | None = None) -> None:
    """Record the most recent hook run so ``--mode status`` can report liveness.

    Best-effort: a heartbeat write must never break the hook. ``outcome`` is a
    short tag (``hits`` / ``no_hits`` / ``unresolved`` / ``error`` / ...);
    ``detail`` carries extra fields surfaced by the status line.
    """
    record = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "outcome": outcome,
    }
    if detail:
        record.update(detail)
    path = _heartbeat_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# pull mode
# ---------------------------------------------------------------------------


def run_pull(payload: dict) -> int:
    """UserPromptSubmit handler. Always returns 0 — the hook is best-effort."""
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        _log("skip: empty prompt")
        return 0
    if len(prompt) < _tunable("AIGRP_MIN_PROMPT", int):
        _log(f"skip: prompt too short ({len(prompt)})")
        return 0

    endpoint = cq_endpoint.resolve()
    if not endpoint.addr:
        # This is the #279 failure mode. It is no longer SILENT: a stderr
        # breadcrumb surfaces in `claude --debug` and the heartbeat lets
        # `/cq:status` report it. Still exits 0 so prompt submission proceeds.
        _write_heartbeat("unresolved", {"source": endpoint.source})
        msg = (
            "[8l-cq] ambient AIGRP pull inactive: could not resolve the L2 "
            "endpoint (CQ_ADDR unset and no claude-mux profile carried it). "
            "Run the /cq:status command for details."
        )
        print(msg, file=sys.stderr)
        _log("skip: endpoint unresolved")
        return 0
    if not endpoint.api_key:
        _write_heartbeat("no_api_key", {"source": endpoint.source})
        print(
            "[8l-cq] ambient AIGRP pull inactive: L2 endpoint resolved but "
            "CQ_API_KEY is missing. Run the /cq:status command for details.",
            file=sys.stderr,
        )
        _log("skip: api key missing")
        return 0

    persona = os.environ.get("CLAUDE_PERSONA") or os.environ.get("CLAUDE_PROFILE") or os.environ.get("CQ_SESSION") or ""
    session_id = payload.get("session_id") or payload.get("sessionId") or "unknown"

    body = json.dumps(
        {
            "context": prompt,
            "trigger": "user_prompt",
            "session_id": session_id,
            "persona": persona,
            "max_results": _tunable("AIGRP_MAX_HITS", int),
            "min_confidence": _tunable("AIGRP_MIN_CONF", float),
            "min_similarity": _tunable("AIGRP_MIN_SIM", float),
            "exclude_self": persona != "",
        }
    ).encode("utf-8")

    started = time.monotonic()
    try:
        resp = _post_lookup(endpoint.addr, endpoint.api_key, body)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _write_heartbeat("error", {"source": endpoint.source, "error": str(exc)[:120]})
        _log(f"skip: lookup failed ({exc})")
        return 0
    except json.JSONDecodeError as exc:
        _write_heartbeat("error", {"source": endpoint.source, "error": f"bad JSON: {exc}"})
        _log(f"skip: lookup returned non-JSON ({exc})")
        return 0

    elapsed_ms = int((time.monotonic() - started) * 1000)
    results = resp.get("results") or []
    if not results:
        _write_heartbeat("no_hits", {"source": endpoint.source, "hits": 0, "elapsed_ms": elapsed_ms})
        _log(f"skip: zero hits ({elapsed_ms}ms)")
        return 0

    server_ms = resp.get("elapsed_ms", elapsed_ms)
    _write_heartbeat(
        "hits",
        {"source": endpoint.source, "hits": len(results), "elapsed_ms": elapsed_ms},
    )
    _emit_reminder(results, server_ms)
    _log(f"fired {len(results)} hits, {server_ms}ms, persona={persona}")
    return 0


def _post_lookup(addr: str, api_key: str, body: bytes) -> dict:
    """POST the AIGRP lookup and return the parsed JSON body."""
    req = urllib.request.Request(  # noqa: S310 — addr comes from operator config
        url=addr.rstrip("/") + "/api/v1/aigrp/lookup",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    timeout = _tunable("AIGRP_TIMEOUT_S", float)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def _emit_reminder(results: list, server_ms: object) -> None:
    """Write the ``<system-reminder>`` block Claude Code injects into the prompt."""
    lines = [
        "<system-reminder>",
        f"[8L AIGRP] Relevant prior knowledge for this prompt (server {server_ms}ms, {len(results)} hits):",
        "",
    ]
    for i, hit in enumerate(results, start=1):
        sim = hit.get("similarity")
        sim_str = f"{round(sim, 2)}" if isinstance(sim, (int, float)) else "?"
        lines.append(f"{i}. (sim {sim_str}) {hit.get('summary', '')}")
        lines.append(f"   Action: {hit.get('action', '')}")
        lines.append(
            f"   {hit.get('ku_id', '?')} (created_by {hit.get('created_by', '?')}, "
            f"confidence {hit.get('confidence', '?')})"
        )
    lines.append("</system-reminder>")
    sys.stdout.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# status mode
# ---------------------------------------------------------------------------


def run_status() -> int:
    """Print a one-line operator-facing liveness summary. Returns 0 if live."""
    endpoint = cq_endpoint.resolve()

    if not endpoint.addr:
        print(
            "[8l-cq] ambient AIGRP pull: INACTIVE — no L2 endpoint. "
            "CQ_ADDR is unset and no claude-mux profile under "
            f"{cq_endpoint.profiles_dir()} carried a cq address. "
            "The UserPromptSubmit hook cannot reach the L2."
        )
        return 1
    if not endpoint.api_key:
        print(
            f"[8l-cq] ambient AIGRP pull: INACTIVE — endpoint {endpoint.addr} "
            f"resolved (via {endpoint.source}) but CQ_API_KEY is missing."
        )
        return 1

    masked = _mask(endpoint.api_key)
    last = _format_heartbeat()
    print(
        f"[8l-cq] ambient AIGRP pull: LIVE — endpoint {endpoint.addr} "
        f"(resolved via {endpoint.source}, key {masked}). {last}"
    )
    return 0


def _format_heartbeat() -> str:
    """Render the last-run breadcrumb for the status line, if one exists."""
    path = _heartbeat_path()
    if not path.is_file():
        return "Hook has not fired yet this install (no heartbeat recorded)."
    try:
        record = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return "Heartbeat file present but unreadable."
    ts = record.get("ts", "?")
    outcome = record.get("outcome", "?")
    detail = ""
    if "hits" in record:
        detail = f", {record['hits']} hits, {record.get('elapsed_ms', '?')}ms"
    return f"Last fired {ts} ({outcome}{detail})."


def _mask(api_key: str) -> str:
    """Mask an API key for display — keep the recognisable prefix + suffix."""
    if len(api_key) <= 12:
        return "****"
    return f"{api_key[:8]}...{api_key[-4:]}"


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------


def _read_payload() -> dict:
    """Parse the hook JSON from stdin; return {} on empty/garbage input."""
    try:
        raw = sys.stdin.read().strip()
    except OSError:
        return {}
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def main(argv: list[str] | None = None) -> int:
    """Dispatch to the requested mode. ``pull`` always returns 0."""
    parser = argparse.ArgumentParser(prog="cq_aigrp_pull")
    parser.add_argument("--mode", required=True, choices=["pull", "status"])
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if args.mode == "status":
        return run_status()

    # pull mode — wrap everything so an unexpected error still exits 0.
    try:
        payload = _read_payload()
        return run_pull(payload)
    except Exception as exc:  # noqa: BLE001 — best-effort hook, never block the prompt
        _log(f"unexpected error: {exc}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
