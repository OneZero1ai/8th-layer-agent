---
name: cq:reflect
description: Mine the current session for knowledge worth sharing — identify learnings, present them for approval, and propose each approved candidate to the cq knowledge store.
---

# /cq:reflect

Retrospectively mine this session for shareable knowledge units and submit approved candidates to cq.

## Instructions

### Step 1 — Summarize the session context

Construct a compact session summary covering:

- External APIs, libraries, or frameworks used.
- Errors encountered and how each was resolved.
- Workarounds applied for known or unexpected issues.
- Configuration decisions that only work under specific conditions.
- Tool calls that failed before the correct approach was found.
- Any behavior observed that differed from documentation or expectation.
- Dead ends abandoned and why.

The summary should be dense prose — enough for a reader with no prior context to reconstruct the session's technical events. Omit routine file edits, standard library calls, and anything already well-documented.

### Step 2 — Identify candidate knowledge units

Reflection is agent-led — there is no MCP tool for this step. Using your own reasoning, scan the session for insights worth sharing.

A candidate is worth sharing if it meets **all** of these criteria:

1. **Generalizable** — applies beyond this specific project or codebase. Strip all organization-specific names, internal service names, and proprietary identifiers.
2. **Non-obvious** — not directly stated in official documentation, or contradicts documentation.
3. **Actionable** — another agent could apply it immediately with a concrete change.
4. **Novel** — unlikely to already exist in the commons (err toward including, not excluding).

Look specifically for:

- **Undocumented API behavior** — an endpoint returned an unexpected status code, response shape, or side effect.
- **Workarounds for known issues** — a library or tool required a non-standard setup to function correctly.
- **Condition-specific configuration** — a setting, flag, or option that behaves differently across versions, environments, or operating systems.
- **Multi-attempt error resolution** — an error that required more than one failed fix, where the solution was not obvious from the error message or documentation.
- **Version incompatibilities** — two libraries, tools, or runtimes that conflict at specific version combinations.
- **Novel patterns** — a non-obvious approach that solved a class of problem elegantly.

Do **not** include:

- Standard usage of a well-documented API.
- Project-specific business logic or implementation details that cannot be generalized.
- Insights already surfaced and confirmed during the session (i.e. knowledge units you retrieved via `query` and subsequently called `confirm` on to record that they proved correct).

For each candidate, assign:

- **summary** — one concise sentence describing what was discovered.
- **detail** — two to four sentences explaining the context and why this behavior exists or matters.
- **action** — a concrete instruction on what to do (start with an imperative verb).
- **domains** — two to five lowercase domain tags (e.g. `["api", "stripe", "rate-limiting"]`).
- **estimated_relevance** — a float between 0.0 and 1.0:
  - 0.8–1.0: broadly applicable across many languages, frameworks, or teams.
  - 0.5–0.8: applicable to a specific ecosystem or toolchain.
  - 0.2–0.5: applicable only under narrow conditions.
- Optionally: **languages**, **frameworks**, **pattern** if relevant.

If the session contained no events meeting the above criteria, skip Steps 3–5 and follow the "no candidates" instruction in Step 6.

### Step 2.5 — Run the VIBE√ safety check on each candidate

Apply the VIBE√ safety check as defined in the cq skill against every candidate from Step 2. Classify each finding as clean, soft-concern, or hard-finding. For hard findings, generate the sanitized rewrite covering every `propose` field that could carry the violating content (`summary`, `detail`, `action`, `domains`, `languages`, `frameworks`, `pattern`). Record the classification per candidate — Steps 3 and 6 use these results for presentation and the final summary.

If a hard finding cannot be coherently sanitized, the candidate fails Step 2's generalizable criterion — drop it from the candidate list and record the exclusion in Step 6's summary. Do not present it. `/cq:reflect` never silently drops *presented* candidates; the user owns the final decision on every candidate that reaches Step 3.

### Step 3 — Auto-store low-risk candidates; present only hard findings

**Default policy (changed 2026-05-06):** clean + soft-concern candidates are auto-proposed without inline review. Only **hard-finding** candidates are presented for human judgment. This minimizes interruption when the session has nothing risky to surface, while preserving operator oversight on the candidates that actually need it.

#### Auto-propose phase (Step 3a — silent)

For each candidate classified as `clean` or `soft` in Step 2.5, call `propose` immediately without presenting. Soft concerns are not lost — they are listed in the Step 6 final summary so the operator can see what flags were raised.

Do NOT auto-propose hard findings, even with sanitization. Hard findings always require human judgment because the sanitized rewrite may have stripped content the operator cares about, or the operator may want to escalate the finding rather than store either form.

#### Present-hard-only phase (Step 3b)

If there are zero hard findings, skip presentation entirely — go straight to Step 6.

If there are one or more hard findings, open with:

```
cq auto-stored {clean+soft} candidate(s) from this session ({clean} clean, {soft} with soft concerns — see summary below).

{hard} candidate(s) have hard concerns and need your judgment. Each is shown with both the original and a sanitized rewrite — pick which (if either) to store.
```

Omit `{soft}` if zero. Omit the auto-stored line entirely if `clean+soft = 0`.

Present each hard-finding candidate using the template below. Numbering starts at 1; the auto-stored candidates are not numbered (they are reported in the final summary).

**Hard-finding candidate.** The header `summary` and `Domains` use the sanitized values — the header never shows hard-finding content. The Original block shows the full original fields. The Sanitized block shows only fields that differ from the header, i.e. detail and action.

```
{N}. {sanitized summary}

   ⚠️ Hard concern: {one-line concern}
   Domains: {sanitized domain tags}
   Relevance: {estimated_relevance}
   ---
   Original:
     Summary: {original summary}
     Domains: {original domain tags}
     Detail: {original detail}
     Action: {original action}
   Sanitized:
     Detail: {sanitized detail}
     Action: {sanitized action}
```

After listing all candidates, show the command reference:

```
Commands:
  N              approve sanitized version
  N original     approve original instead
  edit N         revise before storing
  skip N         discard
  all            approve every candidate's sanitized default
  none           discard everything

Combine with commas: e.g. "1, 3 original, skip 2" applies each command in order.
```

#### Caveat: hard findings are session-bound (until L2-queued review ships)

Hard findings presented inline are evaluated *only* against this session's running conversation. If the operator closes the session without responding, the candidates are lost. The L2-queued review feature (issue #103) will move hard findings to a server-side `pending_review` tier so they survive session end. Until then, surface a one-line reminder before the command prompt:

```
⚠ Reply to capture these — pending L2-queued review (#103), unanswered hard findings end with the session.
```

### Step 4 — Handle edits (hard findings only)

If the user requests an edit on a hard-finding candidate, show the current field values and ask which field to change. Apply the changes and confirm the updated candidate before proposing.

### Step 5 — Propose human-reviewed candidates

For each hard-finding candidate the operator approved, call `propose` with the chosen variant (sanitized by default; original on explicit `N original`):

```
propose(
  summary=<summary>,
  detail=<detail>,
  action=<action>,
  domains=<domain list>,
  languages=<language list or omit>,
  frameworks=<framework list or omit>,
  pattern=<pattern or omit>
)
```

`domains`, `languages`, and `frameworks` are arrays of strings. `pattern` is a single string. Omit optional arguments entirely when not relevant.

Confirm each inline after the call:

```
Stored: {id} — "{summary}"
```

### Step 6 — Final summary

```
## Session Reflect Complete

{total} candidates identified.
{excluded} dropped by VIBE√ (not generalizable; not presented).
{auto_stored} auto-stored ({clean} clean, {soft} with soft concerns).
{hard_approved} of {hard} hard finding(s) human-reviewed and stored. {hard_skipped} skipped.

VIBE√ findings this session:
- Hard concerns (candidates {numbers}): {one-line concern per candidate}
- Soft concerns (auto-stored): {one-line concern per candidate}
- Excluded (not presented): {one-line reason per excluded candidate}

IDs stored this session:
- {id}: "{summary}" [{clean | soft | sanitized | original}]
- ...
```

Always show the `{total} candidates identified.` line. Omit any line whose count is zero. Omit any VIBE√ findings bullet whose category has no entries.

The bracketed annotation on each stored ID records the VIBE√ provenance of what was stored:

- `clean` — no VIBE√ findings; auto-stored without prompting.
- `soft` — soft concern present; auto-stored with the concern logged in this summary.
- `sanitized` — hard finding; operator picked the sanitized rewrite.
- `original` — hard finding; operator explicitly picked the unmodified version.

If no candidates were identified, display:

```
No shareable learnings identified in this session. Sessions with debugging, workarounds, or undocumented behavior are more likely to produce candidates.
```

## Edge Cases

- **Empty session** — If the session contained only routine tasks, say so and stop after Step 2.
- **All candidates skipped** — Display the summary with 0 proposed.
- **`propose` error** — Report the error inline for that candidate and continue with the next one. Do not abort.
