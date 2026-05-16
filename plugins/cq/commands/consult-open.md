---
name: cq:consult-open
description: Open a new cross-Enterprise consult thread. Routes via the cq-server's directory-driven peering when the destination resolves to a peer enterprise.
---

# /cq:consult-open

Open a consult to a persona on this L2 or on a peer Enterprise's L2.

## Instructions

### Step 1 — Parse the user's args

The user invokes this command followed by free-form text. Extract:

- **`to`** (required) — destination, in the form `<enterprise>/<group>:<persona>`.
  Example: `8th-layer/engineering:david`. If the user gave only a persona
  name, ask which enterprise and group. Don't guess.
- **`subject`** (required) — short title (one line). If absent, prompt
  for one before sending.
- **`content`** (required) — the consult body (the question or context).
  This is whatever the user wrote after the args; if it's empty, prompt
  for it.
- **`content_policy`** (optional) — `summary_only` (default) or `full`.
  Override only when the user explicitly says "send full content."

If the destination is `<enterprise>/<group>:<persona>` with `<enterprise>`
matching `$CQ_ENTERPRISE`, this is a local consult; the same endpoint
handles both cases.

### Step 2 — Validate the environment

Read `CQ_ADDR` and `CQ_API_KEY` from environment. If either is missing,
respond:

> `CQ_ADDR` and/or `CQ_API_KEY` not set. See the runbook step 9
> (configure your Claude Code session) at
> https://github.com/OneZero1ai/8th-layer-core/blob/main/docs/runbooks/01-customer-onboarding-zero-to-peered.md

…and stop.

### Step 3 — Send

Construct a JSON body:

```json
{
  "to_l2_id": "<enterprise>/<group>",
  "to_persona": "<persona>",
  "subject": "<subject>",
  "content": "<content>",
  "content_policy": "<summary_only|full>"
}
```

POST to `$CQ_ADDR/api/v1/consults/request` with header
`Authorization: Bearer $CQ_API_KEY`. Use `curl -s -w '\n%{http_code}'` so
status code is on the last line; parse it.

### Step 4 — Render result

On HTTP 201:

```
Consult opened.
  thread: <thread_id>
  to:     <to_l2_id>:<to_persona>
  subject: <subject>
  policy: <content_policy>
```

If the response includes `forwarded_to` (cross-Enterprise hop), show it.

On HTTP 403 with `no active peering`:
```
Cross-Enterprise peering not active for <enterprise>.
- Run `8l-directory peerings` on your workstation. If active there,
  the L2 hasn't refreshed yet (hourly poll). Force a refresh by
  restarting the cq-server task.
```

On HTTP 404 with `persona not found`:
```
Persona <persona> not registered on <to_l2_id>.
- Browse the peer's roster: `8l-directory enterprises --enterprise <enterprise>`
- Personas are registered server-side; check with the peer's operator.
```

On HTTP 413 (`content_policy=full not allowed by peering`):
```
Peer's peering contract restricts to summary_only.
- Re-run with `content_policy=summary_only`, or
- Open a peering renegotiation with the peer.
```

On any other non-2xx, show the status code and detail verbatim.

## Notes

- This command sends immediately — no preview / approval gate. If the
  user said "consider sending …" or "draft a consult …", DON'T call
  this; instead, draft + show + ask.
- The cq-server decides intra- vs cross-Enterprise routing from the
  `to_l2_id`'s enterprise prefix. The plugin is dumb on routing.
- Records cq attribution under `kind=consult.open, scope=outbound`.
