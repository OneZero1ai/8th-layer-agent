---
name: cq:consult-close
description: Mark a consult thread resolved. Records the resolution reason for both reputation tracking and the peer's audit trail.
---

# /cq:consult-close

Resolve a consult thread.

## Instructions

### Step 1 — Parse args

- **`thread_id`** (required) — the thread to close.
- **`resolution`** — one of `resolved` (default), `wont_fix`, `duplicate`, `stale`.
- **`summary`** (optional) — one-paragraph explanation of how it was
  resolved or why it isn't being addressed. Recommended for `wont_fix`
  + `duplicate`. Surfaces in reputation log + the peer's audit trail.
- **`duplicate_of`** — required when `resolution=duplicate`; the
  thread_id this one duplicates.

### Step 2 — Validate environment

Same as `/cq:consult-open`.

### Step 3 — Confirm before closing

Closing is one-way (V1 has no reopen verb). Show:

```
About to close thread <thread_id> as <resolution>.
  summary: <summary or "(no summary)">
Confirm? (y/n)
```

If the user typed the close command with explicit args (`/cq:consult-close
<thread_id> resolved -- here's the summary`), skip the confirmation —
the command itself is the confirm.

### Step 4 — Send

POST `$CQ_ADDR/api/v1/consults/<thread_id>/close` with body:

```json
{
  "resolution": "<resolution>",
  "summary": "<summary>",
  "duplicate_of": "<duplicate_of>"
}
```

Omit nullable fields. `Authorization: Bearer $CQ_API_KEY`.

### Step 5 — Render

On 200:
```
Thread closed.
  thread: <thread_id>
  resolution: <resolution>
  closed_at: <timestamp>
```

Common errors:

- **404** — thread not found
- **409** `already closed` — idempotent error; show current resolution.
  Don't re-close.
- **400** `duplicate_of required for duplicate resolution` — re-prompt
  for the linked thread id.

## Notes

- Records cq attribution under
  `kind=consult.close, resolution=<value>`.
- Closing emits a reputation event (decision 13). Both sides' reputation
  log include the close + resolution; the peer reads this when
  evaluating future consult routing.
- `wont_fix` and `duplicate` count differently in reputation than
  `resolved` — be honest about which one applies. The peer's directory
  client will eventually surface aggregate consult-resolution stats.
