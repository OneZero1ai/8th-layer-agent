---
name: cq:consult-reply
description: Append a reply to an existing consult thread. Routes via the same cq-server forwarding that opened the thread.
---

# /cq:consult-reply

Send a follow-up message on an existing consult thread.

## Instructions

### Step 1 — Parse args

- **`thread_id`** (required) — the consult thread id (looks like
  `cns_<hex>` or similar).
- **`content`** (required) — the reply body. Whatever the user wrote
  after the thread id.

If `thread_id` looks malformed (no expected prefix), confirm with the
user before sending. Don't guess.

### Step 2 — Validate environment

Same as `/cq:consult-open`.

### Step 3 — Optional context fetch

If the user hasn't seen the thread recently in the conversation, fetch
the most recent few messages so the reply has context:

GET `$CQ_ADDR/api/v1/consults/<thread_id>/messages?limit=5`

Show the user the latest 1-2 inbound messages briefly, then ask: "Send
this reply as-is?" — if they confirm or already typed the reply
explicitly, proceed. If they want changes, draft + iterate.

This step is *optional* — skip when the user has clearly already
written a deliberate reply (long, on-topic, no doubt about intent).

### Step 4 — Send

POST `$CQ_ADDR/api/v1/consults/<thread_id>/messages` with body:

```json
{ "content": "<content>" }
```

`Authorization: Bearer $CQ_API_KEY`.

### Step 5 — Render

On 201:
```
Reply sent.
  thread: <thread_id>
  message: <message_id>
  at: <timestamp>
```

Common errors:

- **404** `thread not found` — wrong id, or thread was hard-deleted.
  Suggest `/cq:consult-inbox` to relocate.
- **409** `thread closed` — thread was resolved before your reply
  landed. Suggest reopening if needed (V2 — for now, open a new
  thread that references this one in subject).
- **413** `content too long` — server's `consult_messages.content`
  max_length is enforced. Suggest splitting into multiple replies.

## Notes

- Records cq attribution under `kind=consult.reply, scope=existing-thread`.
- Markdown in `content` is preserved as-is and rendered by the recipient
  per their L2's policy.
