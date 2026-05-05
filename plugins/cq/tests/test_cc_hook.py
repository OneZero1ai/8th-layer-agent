"""Tests for plugins/cq/hooks/claude_code/cq_cc_hook.py.

Covers:
  * Error-recovery pattern detector (failed-then-succeeded on same surface)
  * Concentrated-work pattern detector (≥3 same-surface successes)
  * Idempotency cache (re-fires suppressed within a session)
  * Stop-hook one-shot semantics + ``stop_hook_active`` re-fire suppression
  * Rate-limit-aware suppression (cached 429 → no fire within Retry-After)
  * Output schema (``hookSpecificOutput.additionalContext``)
"""

from __future__ import annotations

import json
import sys
import time
from io import StringIO


def _stdin_with(payload: dict) -> StringIO:
    return StringIO(json.dumps(payload))


def _run(cc_hook, monkeypatch, mode: str, state_dir, payload: dict, capsys) -> tuple[int, str]:
    monkeypatch.setattr(
        sys,
        "argv",
        ["cq_cc_hook.py", "--mode", mode, "--state-dir", str(state_dir)],
    )
    monkeypatch.setattr("sys.stdin", _stdin_with(payload))
    rc = cc_hook.main()
    captured = capsys.readouterr()
    return rc, captured.out


# ---------------------------------------------------------------------------
# Error-recovery pattern detector
# ---------------------------------------------------------------------------


def test_recovery_fires_when_same_surface_edit_succeeds_after_error(cc_hook, tmp_path, monkeypatch, capsys):
    state_dir = tmp_path / "state"
    sid = "s-recover"

    # 1. failed Edit on /x/y.py
    rc, out = _run(
        cc_hook,
        monkeypatch,
        "post-tool-use",
        state_dir,
        {
            "session_id": sid,
            "tool_name": "Edit",
            "tool_input": {"file_path": "/x/y.py"},
            "tool_response": {"is_error": True, "error": "old_string not unique"},
        },
        capsys,
    )
    assert rc == 0
    assert out == ""  # no fire on a fresh failure

    # 2. successful Edit on the same path
    rc, out = _run(
        cc_hook,
        monkeypatch,
        "post-tool-use",
        state_dir,
        {
            "session_id": sid,
            "tool_name": "Edit",
            "tool_input": {"file_path": "/x/y.py"},
            "tool_response": {"is_error": False},
        },
        capsys,
    )
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "recovered from a tool error" in parsed["hookSpecificOutput"]["additionalContext"]


def test_no_recovery_fire_for_different_surface(cc_hook, tmp_path, monkeypatch, capsys):
    state_dir = tmp_path / "state"
    sid = "s-different"

    _run(
        cc_hook,
        monkeypatch,
        "post-tool-use",
        state_dir,
        {
            "session_id": sid,
            "tool_name": "Edit",
            "tool_input": {"file_path": "/x/a.py"},
            "tool_response": {"is_error": True, "error": "boom"},
        },
        capsys,
    )
    _, out = _run(
        cc_hook,
        monkeypatch,
        "post-tool-use",
        state_dir,
        {
            "session_id": sid,
            "tool_name": "Edit",
            "tool_input": {"file_path": "/x/b.py"},
            "tool_response": {"is_error": False},
        },
        capsys,
    )
    assert out == ""


def test_recovery_fires_for_bash_after_failed_bash_same_binary(cc_hook, tmp_path, monkeypatch, capsys):
    state_dir = tmp_path / "state"
    sid = "s-bash"

    _run(
        cc_hook,
        monkeypatch,
        "post-tool-use",
        state_dir,
        {
            "session_id": sid,
            "tool_name": "Bash",
            "tool_input": {"command": "git push origin main"},
            "tool_response": "command failed: rejected non-fast-forward",
        },
        capsys,
    )
    _, out = _run(
        cc_hook,
        monkeypatch,
        "post-tool-use",
        state_dir,
        {
            "session_id": sid,
            "tool_name": "Bash",
            "tool_input": {"command": "git push --force-with-lease"},
            "tool_response": "ok",
        },
        capsys,
    )
    parsed = json.loads(out)
    assert "recovered from a tool error" in parsed["hookSpecificOutput"]["additionalContext"]


def test_error_via_exit_code(cc_hook, tmp_path, monkeypatch, capsys):
    state_dir = tmp_path / "state"
    sid = "s-exit"

    _run(
        cc_hook,
        monkeypatch,
        "post-tool-use",
        state_dir,
        {
            "session_id": sid,
            "tool_name": "Bash",
            "tool_input": {"command": "ls /nonexistent"},
            "tool_response_metadata": {"exit_code": 2},
        },
        capsys,
    )
    _, out = _run(
        cc_hook,
        monkeypatch,
        "post-tool-use",
        state_dir,
        {
            "session_id": sid,
            "tool_name": "Bash",
            "tool_input": {"command": "ls /tmp"},
            "tool_response": "files",
        },
        capsys,
    )
    parsed = json.loads(out)
    assert parsed["hookSpecificOutput"]["hookEventName"] == "PostToolUse"


# ---------------------------------------------------------------------------
# Concentrated-work pattern
# ---------------------------------------------------------------------------


def test_concentrated_fires_after_three_same_surface_successes(cc_hook, tmp_path, monkeypatch, capsys):
    state_dir = tmp_path / "state"
    sid = "s-concentrated"

    for _ in range(2):
        _, out = _run(
            cc_hook,
            monkeypatch,
            "post-tool-use",
            state_dir,
            {
                "session_id": sid,
                "tool_name": "Edit",
                "tool_input": {"file_path": "/repo/file.py"},
                "tool_response": {"is_error": False},
            },
            capsys,
        )
        assert out == ""  # no fire yet

    _, out = _run(
        cc_hook,
        monkeypatch,
        "post-tool-use",
        state_dir,
        {
            "session_id": sid,
            "tool_name": "Edit",
            "tool_input": {"file_path": "/repo/file.py"},
            "tool_response": {"is_error": False},
        },
        capsys,
    )
    parsed = json.loads(out)
    assert "non-obvious pattern" in parsed["hookSpecificOutput"]["additionalContext"]


def test_concentrated_does_not_fire_when_surfaces_differ(cc_hook, tmp_path, monkeypatch, capsys):
    state_dir = tmp_path / "state"
    sid = "s-mixed"

    for path in ("/a.py", "/b.py", "/c.py"):
        _, out = _run(
            cc_hook,
            monkeypatch,
            "post-tool-use",
            state_dir,
            {
                "session_id": sid,
                "tool_name": "Edit",
                "tool_input": {"file_path": path},
                "tool_response": {"is_error": False},
            },
            capsys,
        )
    assert out == ""


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_recovery_fires_only_once_per_event(cc_hook, tmp_path, monkeypatch, capsys):
    state_dir = tmp_path / "state"
    sid = "s-once"

    _run(
        cc_hook,
        monkeypatch,
        "post-tool-use",
        state_dir,
        {
            "session_id": sid,
            "tool_name": "Edit",
            "tool_input": {"file_path": "/p.py"},
            "tool_response": {"is_error": True, "error": "boom"},
        },
        capsys,
    )
    _, out_first = _run(
        cc_hook,
        monkeypatch,
        "post-tool-use",
        state_dir,
        {
            "session_id": sid,
            "tool_name": "Edit",
            "tool_input": {"file_path": "/p.py"},
            "tool_response": {"is_error": False},
        },
        capsys,
    )
    assert out_first  # fired

    # Re-fire path: another success on the same surface should NOT trigger
    # the recovery reminder again because the recovery event_key is keyed
    # to the recovery timestamp; subsequent successes ride on rule 2 only
    # after we hit 3-in-a-row.
    _, out_second = _run(
        cc_hook,
        monkeypatch,
        "post-tool-use",
        state_dir,
        {
            "session_id": sid,
            "tool_name": "Edit",
            "tool_input": {"file_path": "/p.py"},
            "tool_response": {"is_error": False},
        },
        capsys,
    )
    # Second success: not a recovery (no fresh error), and we only have 2
    # consecutive same-surface successes after the recovery. No fire.
    assert out_second == ""


def test_concentrated_fires_only_once_per_surface(cc_hook, tmp_path, monkeypatch, capsys):
    state_dir = tmp_path / "state"
    sid = "s-once-surface"

    for _ in range(3):
        _run(
            cc_hook,
            monkeypatch,
            "post-tool-use",
            state_dir,
            {
                "session_id": sid,
                "tool_name": "Edit",
                "tool_input": {"file_path": "/x.py"},
                "tool_response": {"is_error": False},
            },
            capsys,
        )
    # 4th + 5th successful edit on the same file should NOT re-fire.
    for _ in range(2):
        _, out = _run(
            cc_hook,
            monkeypatch,
            "post-tool-use",
            state_dir,
            {
                "session_id": sid,
                "tool_name": "Edit",
                "tool_input": {"file_path": "/x.py"},
                "tool_response": {"is_error": False},
            },
            capsys,
        )
        assert out == ""


# ---------------------------------------------------------------------------
# Stop hook
# ---------------------------------------------------------------------------


def test_stop_fires_reminder_once(cc_hook, tmp_path, monkeypatch, capsys):
    state_dir = tmp_path / "state"
    sid = "s-stop"

    _, out = _run(
        cc_hook,
        monkeypatch,
        "stop",
        state_dir,
        {"session_id": sid},
        capsys,
    )
    parsed = json.loads(out)
    assert parsed["hookSpecificOutput"]["hookEventName"] == "Stop"
    assert "/cq:reflect" in parsed["hookSpecificOutput"]["additionalContext"]

    # Second stop in the same session is a no-op (compact/resume re-fire).
    _, out2 = _run(
        cc_hook,
        monkeypatch,
        "stop",
        state_dir,
        {"session_id": sid},
        capsys,
    )
    assert out2 == ""


def test_stop_skipped_when_stop_hook_active(cc_hook, tmp_path, monkeypatch, capsys):
    state_dir = tmp_path / "state"
    _, out = _run(
        cc_hook,
        monkeypatch,
        "stop",
        state_dir,
        {"session_id": "s-active", "stop_hook_active": True},
        capsys,
    )
    assert out == ""


# ---------------------------------------------------------------------------
# Rate-limit-aware suppression
# ---------------------------------------------------------------------------


def test_post_tool_use_suppressed_when_rate_limit_cached(cc_hook, tmp_path, monkeypatch, capsys):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    cc_hook.record_rate_limit(state_dir, retry_after_seconds=600)
    sid = "s-rl"

    _run(
        cc_hook,
        monkeypatch,
        "post-tool-use",
        state_dir,
        {
            "session_id": sid,
            "tool_name": "Edit",
            "tool_input": {"file_path": "/p.py"},
            "tool_response": {"is_error": True, "error": "boom"},
        },
        capsys,
    )
    _, out = _run(
        cc_hook,
        monkeypatch,
        "post-tool-use",
        state_dir,
        {
            "session_id": sid,
            "tool_name": "Edit",
            "tool_input": {"file_path": "/p.py"},
            "tool_response": {"is_error": False},
        },
        capsys,
    )
    assert out == ""


def test_stop_suppressed_when_rate_limit_cached(cc_hook, tmp_path, monkeypatch, capsys):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    cc_hook.record_rate_limit(state_dir, retry_after_seconds=600)
    _, out = _run(cc_hook, monkeypatch, "stop", state_dir, {"session_id": "s-rl-stop"}, capsys)
    assert out == ""


def test_rate_limit_expired_does_not_suppress(cc_hook, tmp_path, monkeypatch, capsys):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    # Write an already-expired record.
    (state_dir / "rate429.json").write_text(json.dumps({"retry_after_until": int(time.time()) - 1}))
    _, out = _run(cc_hook, monkeypatch, "stop", state_dir, {"session_id": "s-rl-expired"}, capsys)
    parsed = json.loads(out)
    assert parsed["hookSpecificOutput"]["hookEventName"] == "Stop"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_surface_fingerprint_bash_keeps_only_first_word(cc_hook):
    assert cc_hook._surface_fingerprint("Bash", {"command": "git push origin main"}) == "git"
    assert cc_hook._surface_fingerprint("Bash", {"command": ""}) == ""


def test_surface_fingerprint_edit_uses_file_path(cc_hook):
    assert cc_hook._surface_fingerprint("Edit", {"file_path": "/x/y.py"}) == "/x/y.py"


def test_detect_error_handles_top_level_is_error(cc_hook):
    assert cc_hook._detect_error({"is_error": True}, None) is True


def test_detect_error_handles_string_response(cc_hook):
    assert cc_hook._detect_error({}, "Error: file not found") is True
    assert cc_hook._detect_error({}, "ok") is False


def test_unknown_mode_returns_2(cc_hook, tmp_path, monkeypatch, capsys):
    state_dir = tmp_path / "state"
    monkeypatch.setattr(
        sys,
        "argv",
        ["cq_cc_hook.py", "--mode", "unknown", "--state-dir", str(state_dir)],
    )
    monkeypatch.setattr("sys.stdin", _stdin_with({}))
    # argparse rejects invalid choices with SystemExit(2); accept either.
    try:
        rc = cc_hook.main()
        assert rc == 2
    except SystemExit as exc:
        assert exc.code == 2
