---
name: setup
description: >-
  Guided onboarding wrapper around `8l join` — walks an end-user through
  picking an Enterprise + L2, choosing a persona, pasting an API key, and
  smoke-binding the session. Idempotent; supports `--force` to rewrite
  an existing profile. Falls back to direct HTTP if the `8l` CLI binary
  isn't on PATH.
---

# /cq:setup

Interactive onboarding for a new Claude Code (or compatible) session
that needs to join an 8th-Layer Enterprise / L2 with a persona name +
API key.

This skill is a **thin orchestration layer** over the `8l join` CLI
shipped by [OneZero1ai/8l-cli](https://github.com/OneZero1ai/8l-cli).
The CLI does the actual work — directory lookup, persona registration,
profile file write at `~/.claude-mux/profiles/<profile>.json`. The skill
adds:

1. A friendly six-step walkthrough so the user doesn't need to remember
   flag names.
2. Input validation matched to the L2 server's regexes (persona name
   shape + `cqa.v1.*` API-key shape) so failures surface early, before
   the round-trip.
3. A graceful fallback for users who don't have the Go binary installed
   yet — pure-Python HTTP path that does the same join steps.

## Workflow contract

The walkthrough is **idempotent** — re-running on an already-bound
session is a no-op (matches the CLI's behaviour). To rewrite an
existing profile, the user passes `--force`, which threads through to
`8l join --force`.

Errors are **fail-loud**: every non-zero CLI exit code maps to a
human-readable hint. We do not surface stack traces. The mapping is in
the table at the bottom of this document and in
`scripts/cq_setup.py:EXIT_CODE_HINTS`.

## Six-step flow

Run `python ${CLAUDE_PLUGIN_ROOT}/skills/setup/cq_setup.py` and follow
its prompts. The script implements all six steps below.

### Step 1 — Pick an Enterprise

```
Which Enterprise are you joining?
  1) 8th-layer-corp  (the production tenant)
  2) Custom (free-text)
> _
```

`8th-layer-corp` is the default. Pick `2` if the operator gave you a
different Enterprise slug (e.g. a customer tenant deployed in their own
AWS account).

### Step 2 — Pick an L2

For `8th-layer-corp`:

```
Which L2 inside `8th-layer-corp`?
  1) engineering   (operator default)
  2) sga           (Dirk's L2)
> _
```

For custom Enterprises, the prompt becomes free-text. Whatever you type
must match an L2 ID the Enterprise's directory knows about — the CLI
will reject unknown L2s with exit code 4 (`l2_not_found`).

### Step 3 — Pick a persona name

```
What persona name should this session sign as? (e.g. `david`, `alice-prod`)
> _
```

Validation regex: `^[a-z0-9][a-z0-9-]{1,62}$` — lowercase alphanumeric +
hyphens, must start with letter/digit, length 2–63.

This is the same regex the L2 server enforces; we mirror it locally so
malformed input is rejected before the smoke step rather than after.

### Step 4 — Paste the API key

```
Paste the API key your L2 admin gave you (`cqa.v1.*` format):
> _
```

Validation regex: `^cqa\.v1\.[a-f0-9]{32}\.[a-zA-Z0-9_-]{52}$`.

**Privacy note.** Claude Code's terminal does not yet support
secret-input mode for skill prompts (#175 upstream). The script prints
a one-line warning before reading the key:

> WARNING: pasting the key here means it is recorded in this session's
> transcript. Rotate the key after onboarding if you are concerned.

If your harness hides the input by other means (e.g. you pre-set
`CQ_SETUP_API_KEY` in the environment), the script reads from the env
var instead and skips the prompt entirely.

### Step 5 — Smoke test

The script tries, in order:

1. **`8l join` on PATH** — if found, it's invoked with
   `--enterprise <e> --l2 <l> --persona <p> --api-key <k> --non-interactive`
   plus `--force` if the user passed `--force`. Any pass-through args
   the user added land here.
2. **Python HTTP fallback** — if `8l` is not on PATH, the script does
   the equivalent JSON POST to the L2's `/api/v1/join` endpoint
   directly, then writes the profile JSON itself. The fallback is
   feature-equivalent for the join contract (Decision 29 §3) but does
   not perform directory caching.

Output on success:

```
Successfully joined `8th-layer-corp/engineering` as `david`.
  profile:    ~/.claude-mux/profiles/david@8th-layer-corp-engineering.json
  CQ_ADDR:    https://l2.engineering.8th-layer-corp.example/
  CQ_API_KEY: cqa.v1.<masked>
  bound:      2026-05-09T22:14:33Z
```

Output on failure (example, exit 7 = `peering_inactive`):

```
Smoke failed — exit code 7.
Hint (peering_inactive): The L2 hasn't refreshed its directory cache
yet. Run `8l-directory peerings` on the L2's host, or wait one hour
for the next poll cycle.
```

The complete exit-code table is in **Exit codes & hints** below.

### Step 6 — Confirm and link to runbook

On success the script prints:

```
Your session is bound. To use it:
  - Restart this Claude Code session (the env vars take effect on next launch).
  - Or `source ~/.claude-mux/profiles/<profile>.env` in this shell to bind now.
For the full onboarding runbook, see:
  https://github.com/OneZero1ai/8th-layer-core/blob/main/docs/onboarding/join-runbook.md
```

## Idempotence and `--force`

Re-running the skill on an already-bound profile is a **no-op**:

- The script reads `~/.claude-mux/profiles/<profile>.json` if present.
- If the file's `enterprise`/`l2`/`persona` match what the user picked,
  the script prints `Already bound — no changes.` and exits 0.
- If they don't match (different persona, e.g.), the script refuses to
  overwrite and tells the user to either pick a different profile name
  or pass `--force`.

`--force` is passed through to `8l join --force` (and replicated in
the Python fallback). It rewrites the profile file in-place.

## Exit codes & hints

The CLI's exit code table is sourced from
[Decision 29 §5](https://github.com/OneZero1ai/8th-layer-core/blob/main/docs/decisions/29-l2-join-cli.md).
Mirror it here so the skill can render hints without touching the
network:

| Code | Name                | Hint                                                        |
|------|---------------------|-------------------------------------------------------------|
| 0    | success             | (no hint — success)                                         |
| 1    | unknown             | Generic failure. Re-run with `--debug`.                     |
| 2    | invalid_args        | Persona or API-key format check failed before send.         |
| 3    | enterprise_not_found| Check the Enterprise slug; run `8l-directory enterprises`.  |
| 4    | l2_not_found        | The L2 isn't registered in the Enterprise's directory.      |
| 5    | persona_taken       | Pick a different persona; the L2 admin sees registered ones.|
| 6    | api_key_invalid     | The `cqa.v1.*` key was rejected. Ask your L2 admin to reissue.|
| 7    | peering_inactive    | The Enterprise's directory cache is stale. See hint above.  |
| 8    | network_unreachable | Check the L2 URL / your VPN. The bind never reached the L2. |
| 9    | already_bound       | Rerun with `--force` to rewrite the profile file.           |
| 10   | profile_write_failed| `~/.claude-mux/profiles/` is unwritable; check permissions. |

These map 1:1 to `cq_setup.py:EXIT_CODE_HINTS` — keep the two in sync.

## Constraints / known limitations

- **No secret-input mode.** Claude Code's harness does not yet hide
  prompts the way `read -s` does in a terminal. The skill warns and
  also accepts an env-var hand-off (`CQ_SETUP_API_KEY`) for users who
  want to keep the key out of the transcript.
- **Won't bundle the Go binary.** If `8l` isn't on PATH the script
  prints the install URL once and continues with the Python fallback.
  The Go binary is the supported, faster path.
- **Profile-file shape is owned by the CLI.** Don't read or write the
  JSON shape directly from Python beyond what the fallback explicitly
  needs — keep the canonical shape in the CLI repo. The fallback
  writes the minimum subset documented in Decision 29 §3.

## License

Apache-2.0 — see repository `LICENSE` and `NOTICE`.
