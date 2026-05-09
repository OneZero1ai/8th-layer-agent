#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 OneZero1.ai
"""Interactive onboarding wrapper around `8l join`.

Six-step walkthrough invoked by the `/cq:setup` slash command (or directly
via `python skills/setup/cq_setup.py`). Drives the user through picking an
Enterprise + L2, choosing a persona, pasting an API key, and smoke-binding
the session.

The script is **thin orchestration**:

* If `8l` is on PATH, we shell out to `8l join --non-interactive`. The CLI
  owns the canonical join contract (Decision 29 §3): directory lookup,
  persona registration, profile file write at
  ``~/.claude-mux/profiles/<profile>.json``.
* If `8l` is NOT on PATH, the script falls back to a stdlib-only HTTP
  path that posts to the L2's ``/api/v1/join`` endpoint and writes the
  same profile JSON locally. The fallback is feature-equivalent for the
  join contract but does not perform directory caching.

Idempotent: re-running on an already-bound profile is a no-op. ``--force``
threads through to ``8l join --force`` (and overwrites in the fallback).

Exit codes mirror Decision 29 §5; see ``EXIT_CODE_HINTS`` and the SKILL.md
table.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Validation regexes — kept in sync with the L2 server (cli/internal/join).
# ---------------------------------------------------------------------------

PERSONA_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
API_KEY_RE = re.compile(r"^cqa\.v1\.[a-f0-9]{32}\.[a-zA-Z0-9_-]{52}$")

# ---------------------------------------------------------------------------
# Decision-29 exit codes. Keep in sync with skills/setup/SKILL.md.
# ---------------------------------------------------------------------------

EXIT_CODE_HINTS: dict[int, tuple[str, str]] = {
    0: ("success", ""),
    1: ("unknown", "Generic failure. Re-run with `--debug`."),
    2: ("invalid_args", "Persona or API-key format check failed before send."),
    3: ("enterprise_not_found", "Check the Enterprise slug; run `8l-directory enterprises`."),
    4: ("l2_not_found", "The L2 isn't registered in the Enterprise's directory."),
    5: ("persona_taken", "Pick a different persona; ask the L2 admin which are taken."),
    6: ("api_key_invalid", "The `cqa.v1.*` key was rejected. Ask your L2 admin to reissue."),
    7: ("peering_inactive", "Directory cache is stale. Run `8l-directory peerings` on the L2 host or wait an hour."),
    8: ("network_unreachable", "Check the L2 URL / your VPN — the bind never reached the L2."),
    9: ("already_bound", "Rerun with `--force` to rewrite the profile file."),
    10: ("profile_write_failed", "`~/.claude-mux/profiles/` is unwritable; check permissions."),
}

KNOWN_ENTERPRISES: list[tuple[str, str]] = [
    ("8th-layer-corp", "the production tenant"),
]
KNOWN_L2S: dict[str, list[tuple[str, str]]] = {
    "8th-layer-corp": [
        ("engineering", "operator default"),
        ("sga", "Dirk's L2"),
    ],
}

INSTALL_HINT = (
    "`8l` CLI not found on PATH. The faster path is to install the Go binary "
    "from https://github.com/OneZero1ai/8l-cli/releases — falling back to a "
    "Python HTTP path for now."
)
RUNBOOK_URL = "https://github.com/OneZero1ai/8th-layer-core/blob/main/docs/onboarding/join-runbook.md"
DEFAULT_PROFILES_DIR = Path.home() / ".claude-mux" / "profiles"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class JoinChoice:
    """User-supplied choices captured by the six-step walkthrough."""

    enterprise: str
    l2: str
    persona: str
    api_key: str

    @property
    def profile_name(self) -> str:
        """Return the canonical profile name shared with the `8l` CLI."""
        return f"{self.persona}@{self.enterprise}-{self.l2}"


# ---------------------------------------------------------------------------
# Step 1-4 — interactive prompts
# ---------------------------------------------------------------------------


def prompt_enterprise(stdin=sys.stdin, stdout=sys.stdout) -> str:
    """Step 1 — ask the user which Enterprise they're joining."""
    print("Which Enterprise are you joining?", file=stdout)
    for i, (slug, blurb) in enumerate(KNOWN_ENTERPRISES, start=1):
        print(f"  {i}) {slug}  ({blurb})", file=stdout)
    print(f"  {len(KNOWN_ENTERPRISES) + 1}) Custom (free-text)", file=stdout)
    while True:
        choice = stdin.readline().strip()
        if not choice:
            print("(input required) > ", end="", file=stdout, flush=True)
            continue
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(KNOWN_ENTERPRISES):
                return KNOWN_ENTERPRISES[idx - 1][0]
            if idx == len(KNOWN_ENTERPRISES) + 1:
                print("Enterprise slug: ", end="", file=stdout, flush=True)
                slug = stdin.readline().strip()
                if slug:
                    return slug
                continue
        # Allow direct typing of a known slug as a shortcut.
        if any(choice == slug for slug, _ in KNOWN_ENTERPRISES):
            return choice
        # Or just accept any non-empty string as a custom slug.
        return choice


def prompt_l2(enterprise: str, stdin=sys.stdin, stdout=sys.stdout) -> str:
    """Step 2 — pick an L2 inside the chosen Enterprise."""
    options = KNOWN_L2S.get(enterprise)
    if options:
        print(f"Which L2 inside `{enterprise}`?", file=stdout)
        for i, (slug, blurb) in enumerate(options, start=1):
            print(f"  {i}) {slug}  ({blurb})", file=stdout)
        while True:
            choice = stdin.readline().strip()
            if choice.isdigit():
                idx = int(choice)
                if 1 <= idx <= len(options):
                    return options[idx - 1][0]
            if any(choice == slug for slug, _ in options):
                return choice
            # Free-text fallback even for known Enterprises.
            if choice:
                return choice
            print("(input required) > ", end="", file=stdout, flush=True)
    # Custom Enterprise — pure free-text.
    print(f"L2 / group name inside `{enterprise}`: ", end="", file=stdout, flush=True)
    while True:
        slug = stdin.readline().strip()
        if slug:
            return slug
        print("(input required) > ", end="", file=stdout, flush=True)


def prompt_persona(stdin=sys.stdin, stdout=sys.stdout) -> str:
    """Step 3 — read a persona name and validate against the L2 regex."""
    print(
        "What persona name should this session sign as? (e.g. `david`, `alice-prod`)",
        file=stdout,
    )
    while True:
        name = stdin.readline().strip()
        if PERSONA_RE.match(name):
            return name
        print(
            "  Invalid persona name. Must match ^[a-z0-9][a-z0-9-]{1,62}$ "
            "(lowercase letters/digits/hyphens, 2–63 chars, no leading hyphen). "
            "Try again: ",
            end="",
            file=stdout,
            flush=True,
        )


def prompt_api_key(stdin=sys.stdin, stdout=sys.stdout) -> str:
    """Step 4 — read the `cqa.v1.*` API key from env or stdin."""
    env_key = os.environ.get("CQ_SETUP_API_KEY")
    if env_key:
        if not API_KEY_RE.match(env_key):
            print(
                "CQ_SETUP_API_KEY is set but does not match cqa.v1.* shape; ignoring.",
                file=stdout,
            )
        else:
            print("Using API key from $CQ_SETUP_API_KEY (not echoed).", file=stdout)
            return env_key
    print(
        "WARNING: pasting the key here means it is recorded in this session's "
        "transcript. Rotate the key after onboarding if that's a concern.",
        file=stdout,
    )
    print("Paste the API key your L2 admin gave you (`cqa.v1.*` format):", file=stdout)
    while True:
        key = stdin.readline().strip()
        if API_KEY_RE.match(key):
            return key
        print(
            "  Invalid API key shape. Expected `cqa.v1.<32 hex>.<52 url-safe>`. Try again: ",
            end="",
            file=stdout,
            flush=True,
        )


# ---------------------------------------------------------------------------
# Step 5 — smoke (CLI then Python fallback)
# ---------------------------------------------------------------------------


def cli_available() -> bool:
    """Return True if the `8l` Go CLI is on PATH."""
    return shutil.which("8l") is not None


def run_cli_join(choice: JoinChoice, force: bool, debug: bool) -> int:
    """Shell out to `8l join --non-interactive` and return its exit code."""
    cmd = [
        "8l",
        "join",
        "--enterprise",
        choice.enterprise,
        "--l2",
        choice.l2,
        "--persona",
        choice.persona,
        "--api-key",
        choice.api_key,
        "--non-interactive",
    ]
    if force:
        cmd.append("--force")
    if debug:
        cmd.append("--debug")
    proc = subprocess.run(cmd, check=False)  # noqa: S603
    return proc.returncode


def python_fallback_join(
    choice: JoinChoice,
    force: bool,
    profiles_dir: Path = DEFAULT_PROFILES_DIR,
    l2_url: str | None = None,
    *,
    _opener=urllib.request.urlopen,
) -> int:
    """Stdlib-only HTTP join. Returns a Decision-29 exit code.

    `l2_url` defaults to `https://l2.<l2>.<enterprise>.example/` — the
    canonical pattern documented in Decision 29 §3. Real deployments
    typically override via the `CQ_L2_URL_TEMPLATE` env var so customer
    URLs (e.g. behind their own ALB) work without code changes.
    """
    profile_path = profiles_dir / f"{choice.profile_name}.json"
    if profile_path.exists() and not force:
        try:
            existing = json.loads(profile_path.read_text())
            if (
                existing.get("enterprise") == choice.enterprise
                and existing.get("l2") == choice.l2
                and existing.get("persona") == choice.persona
            ):
                # Already bound, idempotent no-op.
                return 9  # already_bound — caller renders this as "no-op" for matching profiles
        except (OSError, json.JSONDecodeError):
            pass

    template = os.environ.get(
        "CQ_L2_URL_TEMPLATE",
        "https://l2.{l2}.{enterprise}.example/",
    )
    base = l2_url or template.format(l2=choice.l2, enterprise=choice.enterprise)
    body = json.dumps(
        {
            "enterprise": choice.enterprise,
            "l2": choice.l2,
            "persona": choice.persona,
            "force": force,
        }
    ).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 — fixed scheme via template
        url=base.rstrip("/") + "/api/v1/join",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {choice.api_key}",
        },
    )
    try:
        with _opener(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return _http_status_to_exit(exc.code)
    except urllib.error.URLError:
        return 8  # network_unreachable
    except (TimeoutError, OSError):
        return 8

    try:
        profiles_dir.mkdir(parents=True, exist_ok=True)
        merged = {
            "enterprise": choice.enterprise,
            "l2": choice.l2,
            "persona": choice.persona,
            "cq_addr": payload.get("cq_addr") or base,
            "cq_api_key": choice.api_key,
            "bound_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        profile_path.write_text(json.dumps(merged, indent=2) + "\n")
        profile_path.chmod(0o600)
    except OSError:
        return 10  # profile_write_failed
    return 0


def _http_status_to_exit(code: int) -> int:
    """Map an HTTP status code from the L2 join endpoint to a Decision-29 exit code."""
    if code == 401 or code == 403:
        return 6  # api_key_invalid
    if code == 404:
        return 4  # l2_not_found (best guess; CLI distinguishes 3 vs 4)
    if code == 409:
        return 5  # persona_taken
    if code == 412:
        return 7  # peering_inactive
    return 1


# ---------------------------------------------------------------------------
# Idempotence pre-check
# ---------------------------------------------------------------------------


def existing_profile_matches(choice: JoinChoice, profiles_dir: Path = DEFAULT_PROFILES_DIR) -> bool:
    """Return True if a profile file already binds this exact (enterprise, l2, persona) tuple."""
    path = profiles_dir / f"{choice.profile_name}.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return (
        data.get("enterprise") == choice.enterprise
        and data.get("l2") == choice.l2
        and data.get("persona") == choice.persona
    )


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def render_success(choice: JoinChoice, profiles_dir: Path, stdout) -> None:
    """Print the post-join confirmation block including the masked API key."""
    masked = choice.api_key[:10] + "..." + choice.api_key[-4:]
    profile_path = profiles_dir / f"{choice.profile_name}.json"
    print(
        f"Successfully joined `{choice.enterprise}/{choice.l2}` as `{choice.persona}`.",
        file=stdout,
    )
    print(f"  profile:    {profile_path}", file=stdout)
    print(f"  CQ_API_KEY: {masked}", file=stdout)
    print("", file=stdout)
    print("Your session is bound. To use it:", file=stdout)
    print(
        "  - Restart this Claude Code session (env vars take effect on next launch).",
        file=stdout,
    )
    print(
        "  - Or `source ~/.claude-mux/profiles/<profile>.env` to bind in this shell now.",
        file=stdout,
    )
    print("", file=stdout)
    print("For the full onboarding runbook, see:", file=stdout)
    print(f"  {RUNBOOK_URL}", file=stdout)


def render_failure(exit_code: int, stdout) -> None:
    """Print a `Hint (<name>): …` line corresponding to the Decision-29 exit code."""
    name, hint = EXIT_CODE_HINTS.get(exit_code, ("unknown", "Re-run with `--debug`."))
    print(f"Smoke failed — exit code {exit_code}.", file=stdout)
    print(f"Hint ({name}): {hint}", file=stdout)


# ---------------------------------------------------------------------------
# Top-level entrypoint
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse `--force` / `--debug` / `--profiles-dir` (test-only) flags."""
    parser = argparse.ArgumentParser(
        prog="cq:setup",
        description="Guided onboarding wrapper around `8l join`.",
    )
    parser.add_argument("--force", action="store_true", help="Rewrite an existing profile.")
    parser.add_argument("--debug", action="store_true", help="Pass --debug through to `8l join`.")
    parser.add_argument(
        "--profiles-dir",
        type=Path,
        default=DEFAULT_PROFILES_DIR,
        help=argparse.SUPPRESS,  # mainly for tests
    )
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    stdin=sys.stdin,
    stdout=sys.stdout,
) -> int:
    """Top-level walkthrough — runs steps 1–6 and returns a Decision-29 exit code."""
    args = parse_args(argv if argv is not None else sys.argv[1:])

    enterprise = prompt_enterprise(stdin=stdin, stdout=stdout)
    l2 = prompt_l2(enterprise, stdin=stdin, stdout=stdout)
    persona = prompt_persona(stdin=stdin, stdout=stdout)
    api_key = prompt_api_key(stdin=stdin, stdout=stdout)
    choice = JoinChoice(enterprise=enterprise, l2=l2, persona=persona, api_key=api_key)

    # Idempotence pre-check — short-circuit before bothering the L2.
    if not args.force and existing_profile_matches(choice, args.profiles_dir):
        print("Already bound — no changes.", file=stdout)
        return 0

    if cli_available():
        rc = run_cli_join(choice, force=args.force, debug=args.debug)
    else:
        print(INSTALL_HINT, file=stdout)
        rc = python_fallback_join(choice, force=args.force, profiles_dir=args.profiles_dir)

    if rc == 0:
        render_success(choice, args.profiles_dir, stdout)
    else:
        render_failure(rc, stdout)
    return rc


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
