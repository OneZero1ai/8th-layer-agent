"""Tests for plugins/cq/skills/setup/cq_setup.py."""

from __future__ import annotations

import io
import json
import sys
from importlib import util
from pathlib import Path
from types import ModuleType

import pytest

SETUP_PATH = Path(__file__).resolve().parent.parent / "skills" / "setup" / "cq_setup.py"


def _load() -> ModuleType:
    spec = util.spec_from_file_location("cq_setup_under_test", SETUP_PATH)
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    # Register before exec so dataclass field lookup finds the module.
    sys.modules["cq_setup_under_test"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def setup_mod() -> ModuleType:
    return _load()


# ---------------------------------------------------------------------------
# Regex validation — these mirror the L2 server's accept/reject behaviour.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "david",
        "alice-prod",
        "a1",
        "9-letters",
        "x" * 63,  # max length
        "1abc",
    ],
)
def test_persona_regex_accepts_valid(setup_mod, name):
    assert setup_mod.PERSONA_RE.match(name) is not None


@pytest.mark.parametrize(
    "name",
    [
        "",
        "a",  # too short — must be >= 2
        "-leading-hyphen",
        "UPPER",
        "snake_case",
        "with space",
        "x" * 64,  # one over max
        "trailing-",  # this one IS accepted by ^[a-z0-9][a-z0-9-]{1,62}$ — see comment
    ],
)
def test_persona_regex_rejects_invalid(setup_mod, name):
    # Note: "trailing-" matches the regex (hyphens allowed anywhere except
    # the leading char) — drop it from invalid set.
    if name == "trailing-":
        assert setup_mod.PERSONA_RE.match(name) is not None
        return
    assert setup_mod.PERSONA_RE.match(name) is None


@pytest.mark.parametrize(
    "key",
    [
        "cqa.v1." + "a" * 32 + "." + "x" * 52,
        "cqa.v1." + "0" * 32 + "." + ("A" * 26 + "_" * 13 + "-" * 13),
    ],
)
def test_api_key_regex_accepts_valid(setup_mod, key):
    assert setup_mod.API_KEY_RE.match(key) is not None


@pytest.mark.parametrize(
    "key",
    [
        "",
        "cqa.v1.short.short",
        "cqa.v2." + "a" * 32 + "." + "x" * 52,  # wrong version
        "wrong.v1." + "a" * 32 + "." + "x" * 52,  # wrong prefix
        "cqa.v1." + "Z" * 32 + "." + "x" * 52,  # non-hex middle
        "cqa.v1." + "a" * 31 + "." + "x" * 52,  # too short middle
        "cqa.v1." + "a" * 32 + "." + "x" * 51,  # too short tail
        "cqa.v1." + "a" * 32 + "." + "x" * 52 + "x",  # too long tail
        "cqa.v1." + "a" * 32 + "." + "@" * 52,  # disallowed char in tail
    ],
)
def test_api_key_regex_rejects_invalid(setup_mod, key):
    assert setup_mod.API_KEY_RE.match(key) is None


# ---------------------------------------------------------------------------
# Exit-code table
# ---------------------------------------------------------------------------


def test_exit_code_hints_cover_decision_29(setup_mod):
    # Decision 29 §5: codes 0..10 inclusive.
    for code in range(0, 11):
        assert code in setup_mod.EXIT_CODE_HINTS, f"missing hint for code {code}"


# ---------------------------------------------------------------------------
# Prompt helpers — drive with synthetic stdin/stdout.
# ---------------------------------------------------------------------------


def test_prompt_enterprise_accepts_index(setup_mod):
    out = io.StringIO()
    stdin = io.StringIO("1\n")
    assert setup_mod.prompt_enterprise(stdin=stdin, stdout=out) == "8th-layer-corp"


def test_prompt_enterprise_accepts_custom_via_index(setup_mod):
    out = io.StringIO()
    stdin = io.StringIO(f"{len(setup_mod.KNOWN_ENTERPRISES) + 1}\nacme-corp\n")
    assert setup_mod.prompt_enterprise(stdin=stdin, stdout=out) == "acme-corp"


def test_prompt_l2_accepts_index_for_known_enterprise(setup_mod):
    out = io.StringIO()
    stdin = io.StringIO("2\n")
    assert setup_mod.prompt_l2("8th-layer-corp", stdin=stdin, stdout=out) == "sga"


def test_prompt_l2_free_text_for_custom_enterprise(setup_mod):
    out = io.StringIO()
    stdin = io.StringIO("teamA\n")
    assert setup_mod.prompt_l2("acme-corp", stdin=stdin, stdout=out) == "teamA"


def test_prompt_persona_loops_until_valid(setup_mod):
    out = io.StringIO()
    stdin = io.StringIO("BAD!\n_underscore\nalice\n")
    assert setup_mod.prompt_persona(stdin=stdin, stdout=out) == "alice"


def test_prompt_api_key_loops_until_valid(setup_mod):
    out = io.StringIO()
    valid = "cqa.v1." + "a" * 32 + "." + "x" * 52
    stdin = io.StringIO(f"too-short\n{valid}\n")
    assert setup_mod.prompt_api_key(stdin=stdin, stdout=out) == valid


def test_prompt_api_key_uses_env_when_set(setup_mod, monkeypatch):
    valid = "cqa.v1." + "b" * 32 + "." + "y" * 52
    monkeypatch.setenv("CQ_SETUP_API_KEY", valid)
    out = io.StringIO()
    stdin = io.StringIO("")  # should not be read
    assert setup_mod.prompt_api_key(stdin=stdin, stdout=out) == valid


def test_prompt_api_key_ignores_malformed_env(setup_mod, monkeypatch):
    monkeypatch.setenv("CQ_SETUP_API_KEY", "not-a-key")
    valid = "cqa.v1." + "c" * 32 + "." + "z" * 52
    out = io.StringIO()
    stdin = io.StringIO(f"{valid}\n")
    assert setup_mod.prompt_api_key(stdin=stdin, stdout=out) == valid


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


def test_existing_profile_matches_true_when_identical(setup_mod, tmp_path):
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    choice = setup_mod.JoinChoice(
        enterprise="8th-layer-corp",
        l2="engineering",
        persona="alice",
        api_key="cqa.v1." + "a" * 32 + "." + "x" * 52,  # pragma: allowlist secret
    )
    (profiles_dir / f"{choice.profile_name}.json").write_text(
        json.dumps(
            {
                "enterprise": "8th-layer-corp",
                "l2": "engineering",
                "persona": "alice",
            }
        )
    )
    assert setup_mod.existing_profile_matches(choice, profiles_dir) is True


def test_existing_profile_matches_false_when_persona_differs(setup_mod, tmp_path):
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    choice = setup_mod.JoinChoice(
        enterprise="8th-layer-corp",
        l2="engineering",
        persona="alice",
        api_key="cqa.v1." + "a" * 32 + "." + "x" * 52,  # pragma: allowlist secret
    )
    (profiles_dir / f"{choice.profile_name}.json").write_text(
        json.dumps(
            {
                "enterprise": "8th-layer-corp",
                "l2": "engineering",
                "persona": "bob",
            }
        )
    )
    assert setup_mod.existing_profile_matches(choice, profiles_dir) is False


def test_existing_profile_matches_false_when_missing(setup_mod, tmp_path):
    choice = setup_mod.JoinChoice(
        enterprise="x",
        l2="y",
        persona="z",
        api_key="cqa.v1." + "a" * 32 + "." + "x" * 52,  # pragma: allowlist secret
    )
    assert setup_mod.existing_profile_matches(choice, tmp_path) is False


# ---------------------------------------------------------------------------
# HTTP fallback exit-code mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, 6),  # api_key_invalid
        (403, 6),  # api_key_invalid
        (404, 4),  # l2_not_found
        (409, 5),  # persona_taken
        (412, 7),  # peering_inactive
        (500, 1),  # unknown
    ],
)
def test_http_status_to_exit(setup_mod, status, expected):
    assert setup_mod._http_status_to_exit(status) == expected


def test_python_fallback_writes_profile(setup_mod, tmp_path, monkeypatch):
    monkeypatch.setenv("CQ_L2_URL_TEMPLATE", "https://example.invalid/")

    class FakeResp:
        def __init__(self, body):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._body

    def fake_opener(req, timeout=10):
        # Sanity: header + body shape.
        assert req.headers["Authorization"].startswith("Bearer cqa.v1.")
        return FakeResp(json.dumps({"cq_addr": "https://example.invalid/"}).encode())

    choice = setup_mod.JoinChoice(
        enterprise="acme-corp",
        l2="teamA",
        persona="alice",
        api_key="cqa.v1." + "a" * 32 + "." + "x" * 52,  # pragma: allowlist secret
    )
    profiles_dir = tmp_path / "profiles"
    rc = setup_mod.python_fallback_join(choice, force=False, profiles_dir=profiles_dir, _opener=fake_opener)
    assert rc == 0
    written = json.loads((profiles_dir / f"{choice.profile_name}.json").read_text())
    assert written["enterprise"] == "acme-corp"
    assert written["l2"] == "teamA"
    assert written["persona"] == "alice"
    assert written["cq_api_key"].startswith("cqa.v1.")


def test_python_fallback_idempotent_when_profile_matches(setup_mod, tmp_path, monkeypatch):
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    choice = setup_mod.JoinChoice(
        enterprise="acme-corp",
        l2="teamA",
        persona="alice",
        api_key="cqa.v1." + "a" * 32 + "." + "x" * 52,  # pragma: allowlist secret
    )
    (profiles_dir / f"{choice.profile_name}.json").write_text(
        json.dumps(
            {
                "enterprise": "acme-corp",
                "l2": "teamA",
                "persona": "alice",
            }
        )
    )

    # Opener should never be called.
    def boom(*a, **kw):
        raise AssertionError("network must not be touched on idempotent path")

    rc = setup_mod.python_fallback_join(choice, force=False, profiles_dir=profiles_dir, _opener=boom)
    # 9 = already_bound (caller renders this as no-op for matching profiles).
    assert rc == 9


def test_python_fallback_force_rewrites(setup_mod, tmp_path):
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    choice = setup_mod.JoinChoice(
        enterprise="acme-corp",
        l2="teamA",
        persona="alice",
        api_key="cqa.v1." + "a" * 32 + "." + "x" * 52,  # pragma: allowlist secret
    )
    (profiles_dir / f"{choice.profile_name}.json").write_text(
        json.dumps({"enterprise": "stale", "l2": "stale", "persona": "stale"})
    )

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    rc = setup_mod.python_fallback_join(
        choice,
        force=True,
        profiles_dir=profiles_dir,
        l2_url="https://example.invalid/",
        _opener=lambda req, timeout=10: FakeResp(),
    )
    assert rc == 0
    data = json.loads((profiles_dir / f"{choice.profile_name}.json").read_text())
    assert data["enterprise"] == "acme-corp"
