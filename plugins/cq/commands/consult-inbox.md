---
name: cq:consult-inbox
description: List incoming consult threads addressed to my personas — both intra-Enterprise and cross-Enterprise. Filters out resolved threads by default.
---

# /cq:consult-inbox

Show consults waiting for a reply.

## Instructions

### Step 1 — Parse args (all optional)

- **`limit`** — max threads to show (default 20).
- **`only_open`** — `true` (default) or `false`. When `false`, includes
  resolved/closed threads too.
- **`from`** — filter to threads from a specific peer enterprise or
  persona. Pass through to the API as a query string.
- **`persona`** — filter to consults addressed to a specific local
  persona. Useful when you operate multiple personas.

### Step 2 — Validate environment

Same as `/cq:consult-open` — `CQ_ADDR` + `CQ_API_KEY` required.

### Step 3 — Fetch

GET `$CQ_ADDR/api/v1/consults/inbox?limit=<limit>&only_open=<bool>&from=<from>&persona=<persona>`
with `Authorization: Bearer $CQ_API_KEY`.

### Step 4 — Render

If empty:
```
No consult threads (matching filters).
```

Otherwise, render each thread on two lines:

```
<thread_id>  [<status>]
  from <from_l2>:<from_persona> → <to_persona>  · <created_at> · <message_count> msgs
  subject: <subject>
  latest: <truncated to 80 chars>
```

Sort by `latest_at` descending unless the API already does. Group by
peer enterprise if `from` filter is unset and there are >5 threads.

### Step 5 — Suggest actions

If exactly one open thread, append:
```
Reply: /cq:consult-reply <thread_id> <text>
Close: /cq:consult-close <thread_id>
```

If many, append:
```
Reply to a thread: /cq:consult-reply <thread_id> <text>
```

## Notes

- Inbox reads don't record cq attribution.
- Status values are `open` (waiting on a reply from someone), `resolved`,
  `wont_fix`, `duplicate`, `stale`. The "open" set excludes the
  terminal four.
