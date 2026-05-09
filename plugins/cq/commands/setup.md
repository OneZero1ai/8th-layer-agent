---
name: cq:setup
description: Guided onboarding — pick an Enterprise + L2, choose a persona, paste an API key, and smoke-bind this session. Wraps `8l join` (or a Python fallback if the CLI isn't installed).
---

# /cq:setup

Walk an end-user through joining an 8th-Layer Enterprise / L2 with a
persona name + API key. Idempotent on re-run; pass `--force` to rewrite
an existing profile.

This command delegates to the `setup` skill at
`${CLAUDE_PLUGIN_ROOT}/skills/setup/SKILL.md`. The skill describes the
six-step contract, the validation regexes, and the Decision-29 exit-code
table.

## Instructions

1. Parse user args. Recognised flags:
   - `--force` — pass through to the underlying `8l join --force`.
   - `--debug` — pass through to `8l join --debug` (extra logging).
2. Run the script:
   `python3 "${CLAUDE_PLUGIN_ROOT}/skills/setup/cq_setup.py" [flags]`.
3. Stream the script's stdout/stderr to the user verbatim. The script
   handles all prompting, validation, smoke, and result rendering — do
   NOT layer additional prompts on top of it.
4. When the script exits, surface its exit code as-is. Non-zero exits
   already include a `Hint (<name>): …` line; do not add a second
   stack-trace summary.

## When to read SKILL.md instead of running the command

If the user wants to understand the flow before running it, send them
to `${CLAUDE_PLUGIN_ROOT}/skills/setup/SKILL.md` — that document has the
full step-by-step walkthrough, the exit-code table, and the
known-limitations section.

## Notes

- Never echo the API key back in the chat. The script masks it in the
  success summary; preserve that masking when relaying the output.
- If the harness blocks interactive stdin (some CI/agent contexts do),
  the script's `prompt_*` helpers will hang. In that case advise the
  user to set `CQ_SETUP_API_KEY` in their environment and run the
  underlying `8l join` directly with explicit flags.
- Do NOT modify the cq plugin's auth logic, MCP server config, or hook
  code from this skill. The skill is orchestration-only.
