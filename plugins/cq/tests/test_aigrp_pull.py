"""Tests for the AIGRP ambient-pull hook + its endpoint resolver.

Covers issue #279 Gap 1: the hook must resolve the L2 endpoint the same way
the cq binary's config is sourced (claude-mux profile JSON) instead of dying
silently when ``$CQ_ADDR`` is unset, and must surface an operator-visible
liveness signal independent of ``AIGRP_DEBUG``.

  * cq_endpoint.resolve — env fast path + the three profile-file shapes
  * cq_endpoint.resolve — graceful "unresolved" when nothing carries CQ_ADDR
  * pull mode — best-effort: empty/short prompt, unresolved endpoint → exit 0
  * pull mode — reminder emitted only when the L2 returns hits
  * status mode — LIVE vs INACTIVE lines + exit codes
  * heartbeat — written on every run, read back by status mode
"""

from __future__ import annotations

import json
import sys
from io import StringIO

# ---------------------------------------------------------------------------
# cq_endpoint.resolve
# ---------------------------------------------------------------------------


def test_resolve_env_fast_path(cq_endpoint):
    """CQ_ADDR/CQ_API_KEY in the environment is used verbatim, source=env."""
    ep = cq_endpoint.resolve(env={"CQ_ADDR": "https://l2.example", "CQ_API_KEY": "k"})
    assert ep.addr == "https://l2.example"
    assert ep.api_key == "k"
    assert ep.source == "env"
    assert ep.ok


def test_resolve_from_8lcq_profile_shape(cq_endpoint, tmp_path, monkeypatch):
    """The 8l-cq profile carries CQ_ADDR in mcpServers.cq.env — the #279 case.

    CQ_ADDR is NOT in the shell env (that is the whole bug); the resolver must
    still find it in the profile JSON. The key arrives via the env, as it does
    in a live session where claude-mux merges secrets: into the environment.
    """
    profile = {
        "name": "8l-cq",
        "mcpServers": {"cq": {"env": {"CQ_ADDR": "https://eng.l2.example"}}},
        "secrets": {"CQ_API_KEY": "ssm://8l-cq/8l-cq/api-key"},  # pragma: allowlist secret
    }
    (tmp_path / "8l-cq.json").write_text(json.dumps(profile))
    monkeypatch.setenv("CQ_PROFILES_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PROFILE", "8l-cq")

    ep = cq_endpoint.resolve(
        env={"CQ_API_KEY": "live-key", "CLAUDE_PROFILE": "8l-cq"}  # pragma: allowlist secret
    )
    assert ep.addr == "https://eng.l2.example"
    assert ep.api_key == "live-key"  # pragma: allowlist secret
    assert ep.source == "profile:8l-cq"
    assert ep.ok


def test_resolve_ignores_secret_reference_as_key(cq_endpoint, tmp_path, monkeypatch):
    """An ``ssm://`` placeholder is not a usable bearer token — treated absent."""
    profile = {
        "mcpServers": {
            "cq": {
                "env": {
                    "CQ_ADDR": "https://l2.example",
                    "CQ_API_KEY": "ssm://x/y/z",  # pragma: allowlist secret
                }
            }
        }
    }
    (tmp_path / "cq.json").write_text(json.dumps(profile))
    monkeypatch.setenv("CQ_PROFILES_DIR", str(tmp_path))
    ep = cq_endpoint.resolve(env={"CLAUDE_PROFILE": "cq"})
    assert ep.addr == "https://l2.example"
    assert ep.api_key is None  # ssm:// reference rejected


def test_resolve_from_dwl2_profile_shape(cq_endpoint, tmp_path, monkeypatch):
    """The dw-l2 profile carries CQ_ADDR in top-level env."""
    profile = {"name": "dw-l2", "env": {"CQ_ADDR": "http://team-dw.example"}}
    (tmp_path / "dw-l2.json").write_text(json.dumps(profile))
    monkeypatch.setenv("CQ_PROFILES_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PROFILE", "dw-l2")
    ep = cq_endpoint.resolve(env={"CLAUDE_PROFILE": "dw-l2"})
    assert ep.addr == "http://team-dw.example"
    assert ep.source == "profile:dw-l2"


def test_resolve_from_8ljoin_profile_shape(cq_endpoint, tmp_path, monkeypatch):
    """The `8l join` CLI profile carries flat cq_addr / cq_api_key keys."""
    profile = {
        "persona": "david",
        "cq_addr": "https://joined.l2.example",
        "cq_api_key": "joined-key",  # pragma: allowlist secret
    }
    (tmp_path / "david@ent-eng.json").write_text(json.dumps(profile))
    monkeypatch.setenv("CQ_PROFILES_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PROFILE", "david@ent-eng")
    ep = cq_endpoint.resolve(env={"CLAUDE_PROFILE": "david@ent-eng"})
    assert ep.addr == "https://joined.l2.example"
    assert ep.api_key == "joined-key"  # pragma: allowlist secret


def test_resolve_scan_fallback_no_env_hint(cq_endpoint, tmp_path, monkeypatch):
    """With no env hint at all, the resolver scans for a cq/8l profile."""
    (tmp_path / "unrelated.json").write_text(json.dumps({"env": {"FOO": "bar"}}))
    (tmp_path / "8l-cq.json").write_text(
        json.dumps({"mcpServers": {"cq": {"env": {"CQ_ADDR": "https://scanned.example"}}}})
    )
    monkeypatch.setenv("CQ_PROFILES_DIR", str(tmp_path))
    ep = cq_endpoint.resolve(env={})
    assert ep.addr == "https://scanned.example"
    assert ep.source == "profile:8l-cq"


def test_resolve_unresolved_when_nothing_carries_addr(cq_endpoint, tmp_path, monkeypatch):
    """No env var, no matching profile → unresolved (not a crash)."""
    monkeypatch.setenv("CQ_PROFILES_DIR", str(tmp_path))  # empty dir
    ep = cq_endpoint.resolve(env={})
    assert ep.addr is None
    assert ep.source == "unresolved"
    assert not ep.ok


def test_resolve_unresolved_when_profiles_dir_missing(cq_endpoint, monkeypatch):
    """A non-existent profiles dir resolves to unresolved, no exception."""
    monkeypatch.setenv("CQ_PROFILES_DIR", "/no/such/dir/279")
    ep = cq_endpoint.resolve(env={})
    assert ep.source == "unresolved"


# ---------------------------------------------------------------------------
# pull mode — best-effort behaviour
# ---------------------------------------------------------------------------


def _run(aigrp_hook, monkeypatch, argv, stdin_payload, capsys):
    monkeypatch.setattr(sys, "argv", ["cq_aigrp_pull.py", *argv])
    monkeypatch.setattr("sys.stdin", StringIO(json.dumps(stdin_payload)))
    rc = aigrp_hook.main()
    return rc, capsys.readouterr()


def test_pull_empty_prompt_is_noop(aigrp_hook, monkeypatch, capsys):
    """An empty prompt exits 0 with no stdout."""
    rc, captured = _run(aigrp_hook, monkeypatch, ["--mode", "pull"], {"prompt": ""}, capsys)
    assert rc == 0
    assert captured.out == ""


def test_pull_short_prompt_is_noop(aigrp_hook, monkeypatch, capsys):
    """A sub-threshold-length prompt exits 0 with no stdout."""
    rc, captured = _run(aigrp_hook, monkeypatch, ["--mode", "pull"], {"prompt": "hi"}, capsys)
    assert rc == 0
    assert captured.out == ""


def test_pull_unresolved_endpoint_exits_zero_with_stderr(aigrp_hook, monkeypatch, capsys, tmp_path):
    """The #279 fix: an unresolved endpoint no longer dies SILENTLY.

    The hook still exits 0 (best-effort, never block the prompt) and writes
    nothing to stdout, but it DOES emit a one-line stderr breadcrumb so the
    failure is detectable in `claude --debug` instead of being invisible.
    """
    monkeypatch.setenv("CQ_PROFILES_DIR", str(tmp_path))  # empty → unresolved
    monkeypatch.delenv("CQ_ADDR", raising=False)
    monkeypatch.setenv("AIGRP_HEARTBEAT_PATH", str(tmp_path / "hb.json"))
    rc, captured = _run(
        aigrp_hook,
        monkeypatch,
        ["--mode", "pull"],
        {"prompt": "a reasonably long prompt about terraform state"},
        capsys,
    )
    assert rc == 0
    assert captured.out == ""  # no injected context
    assert "inactive" in captured.err.lower()
    assert "cq_addr" in captured.err.lower() or "endpoint" in captured.err.lower()
    # Heartbeat records the unresolved outcome for /cq:status to surface.
    record = json.loads((tmp_path / "hb.json").read_text())
    assert record["outcome"] == "unresolved"


def test_pull_emits_reminder_on_hits(aigrp_hook, monkeypatch, capsys, tmp_path):
    """When the L2 returns hits, pull injects a <system-reminder> block."""
    monkeypatch.setenv("CQ_ADDR", "https://l2.example")
    monkeypatch.setenv("CQ_API_KEY", "cqa.v1.test")
    monkeypatch.setenv("AIGRP_HEARTBEAT_PATH", str(tmp_path / "hb.json"))

    fake_resp = {
        "elapsed_ms": 42,
        "results": [
            {
                "summary": "Use workload identity federation, not key files.",
                "action": "Set CQ_GCP_WIF=1.",
                "ku_id": "ku-123",
                "created_by": "probe-agent",
                "confidence": 0.9,
                "similarity": 0.812,
            }
        ],
    }
    monkeypatch.setattr(aigrp_hook, "_post_lookup", lambda *a, **k: fake_resp)

    rc, captured = _run(
        aigrp_hook,
        monkeypatch,
        ["--mode", "pull"],
        {"prompt": "how do I authenticate to gcp without a service account key"},
        capsys,
    )
    assert rc == 0
    assert "<system-reminder>" in captured.out
    assert "[8L AIGRP]" in captured.out
    assert "ku-123" in captured.out
    assert "workload identity federation" in captured.out
    record = json.loads((tmp_path / "hb.json").read_text())
    assert record["outcome"] == "hits"
    assert record["hits"] == 1


def test_pull_quiet_on_zero_hits(aigrp_hook, monkeypatch, capsys, tmp_path):
    """Zero hits → no stdout (the hook stays quiet on the happy path)."""
    monkeypatch.setenv("CQ_ADDR", "https://l2.example")
    monkeypatch.setenv("CQ_API_KEY", "cqa.v1.test")
    monkeypatch.setenv("AIGRP_HEARTBEAT_PATH", str(tmp_path / "hb.json"))
    monkeypatch.setattr(aigrp_hook, "_post_lookup", lambda *a, **k: {"results": []})
    rc, captured = _run(
        aigrp_hook,
        monkeypatch,
        ["--mode", "pull"],
        {"prompt": "a prompt the commons knows nothing about"},
        capsys,
    )
    assert rc == 0
    assert captured.out == ""
    assert json.loads((tmp_path / "hb.json").read_text())["outcome"] == "no_hits"


def test_pull_network_error_exits_zero(aigrp_hook, monkeypatch, capsys, tmp_path):
    """A network failure during lookup must not block the prompt — exit 0."""
    monkeypatch.setenv("CQ_ADDR", "https://l2.example")
    monkeypatch.setenv("CQ_API_KEY", "cqa.v1.test")
    monkeypatch.setenv("AIGRP_HEARTBEAT_PATH", str(tmp_path / "hb.json"))

    def _boom(*_a, **_k):
        raise OSError("connection refused")

    monkeypatch.setattr(aigrp_hook, "_post_lookup", _boom)
    rc, captured = _run(
        aigrp_hook,
        monkeypatch,
        ["--mode", "pull"],
        {"prompt": "a long enough prompt to pass the length guard"},
        capsys,
    )
    assert rc == 0
    assert captured.out == ""
    assert json.loads((tmp_path / "hb.json").read_text())["outcome"] == "error"


# ---------------------------------------------------------------------------
# status mode
# ---------------------------------------------------------------------------


def test_status_live_when_endpoint_resolves(aigrp_hook, monkeypatch, capsys, tmp_path):
    """status prints a LIVE line and exits 0 when the endpoint resolves."""
    monkeypatch.setenv("CQ_ADDR", "https://l2.example")
    monkeypatch.setenv("CQ_API_KEY", "cqa.v1.0123456789abcdef")
    monkeypatch.setenv("AIGRP_HEARTBEAT_PATH", str(tmp_path / "hb.json"))
    rc, captured = _run(aigrp_hook, monkeypatch, ["--mode", "status"], {}, capsys)
    assert rc == 0
    assert "LIVE" in captured.out
    assert "https://l2.example" in captured.out
    # The key is masked, never printed in full.
    assert "cqa.v1.0123456789abcdef" not in captured.out


def test_status_inactive_when_unresolved(aigrp_hook, monkeypatch, capsys, tmp_path):
    """status prints an INACTIVE line and exits 1 when nothing resolves."""
    monkeypatch.delenv("CQ_ADDR", raising=False)
    monkeypatch.setenv("CQ_PROFILES_DIR", str(tmp_path))  # empty dir
    rc, captured = _run(aigrp_hook, monkeypatch, ["--mode", "status"], {}, capsys)
    assert rc == 1
    assert "INACTIVE" in captured.out


def test_status_reports_last_heartbeat(aigrp_hook, monkeypatch, capsys, tmp_path):
    """status surfaces the most recent hook run from the heartbeat file."""
    hb = tmp_path / "hb.json"
    hb.write_text(json.dumps({"ts": "2026-05-19T10:00:00Z", "outcome": "hits", "hits": 3}))
    monkeypatch.setenv("CQ_ADDR", "https://l2.example")
    monkeypatch.setenv("CQ_API_KEY", "cqa.v1.0123456789abcdef")
    monkeypatch.setenv("AIGRP_HEARTBEAT_PATH", str(hb))
    rc, captured = _run(aigrp_hook, monkeypatch, ["--mode", "status"], {}, capsys)
    assert rc == 0
    assert "2026-05-19T10:00:00Z" in captured.out
    assert "3 hits" in captured.out
