"""SES → SNS bounce/complaint event → ``transactional_suppression`` writer.

Subscribes to the two SNS topics in account ``124074140789``:

* ``ses-bounces``
* ``ses-complaints``

SES emits a JSON envelope per event. We extract the recipient
addresses and write one suppression row per address. Only **hard**
bounces drive suppression — soft bounces are transient (mailbox full,
greylisting, etc.) and shouldn't permanently block.

# Where this runs

Two deployment shapes work; pick at ops time:

1. **Lambda** subscribed directly to the two SNS topics (cheapest;
   the recommended path). Lambda calls ``handle_sns_record(record,
   store)`` per record.
2. **In-process worker** inside cq-server (polls SQS subscribed to
   the SNS topics). Useful in dev where standing up a Lambda is
   friction.

This module is the *handler* — both deployment shapes import it.

# Why hard-only

Soft bounces (SES type ``Transient``) are recoverable: the receiver
queue was full, the user was temporarily over quota, etc. Suppressing
on soft would lock out users who recovered an hour later. Hard
bounces (``Permanent``) and complaints are the right signal.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .suppression import record_suppression

log = logging.getLogger(__name__)


def handle_sns_message(message_body: str, store: Any, *, event_id: str | None = None) -> int:
    """Process one SNS-wrapped SES event. Returns the number of rows recorded.

    ``message_body`` is the raw SNS ``Message`` field — itself a JSON
    string containing the SES bounce/complaint envelope.

    SNS envelope shape (from SES):

    * Bounces: ``notificationType: "Bounce"`` with ``bounce.bounceType``
      ∈ ``{"Permanent", "Transient", "Undetermined"}``. Recipients are
      under ``bounce.bouncedRecipients[].emailAddress``.
    * Complaints: ``notificationType: "Complaint"``. Recipients under
      ``complaint.complainedRecipients[].emailAddress``.

    A single SES message can hit multiple recipients, hence the per-
    recipient loop. We swallow per-recipient failures (log + continue)
    so one malformed entry doesn't stall the queue.
    """
    try:
        envelope = json.loads(message_body)
    except json.JSONDecodeError:
        log.warning("sns_writer: non-JSON message body, skipping")
        return 0

    event_type = envelope.get("notificationType")
    if event_type == "Bounce":
        bounce = envelope.get("bounce", {})
        bounce_type = bounce.get("bounceType", "Unknown")
        if bounce_type != "Permanent":
            log.info("sns_writer: skipping non-permanent bounce type=%s", bounce_type)
            return 0
        date = (bounce.get("timestamp", "") or "")[:10] or "unknown"
        reason = f"hard_bounce_{date}"
        recipients = bounce.get("bouncedRecipients", [])
    elif event_type == "Complaint":
        complaint = envelope.get("complaint", {})
        date = (complaint.get("timestamp", "") or "")[:10] or "unknown"
        reason = f"complaint_{date}"
        recipients = complaint.get("complainedRecipients", [])
    else:
        log.info("sns_writer: ignoring notificationType=%s", event_type)
        return 0

    inserted = 0
    for recipient in recipients:
        address = (recipient or {}).get("emailAddress")
        if not address:
            continue
        try:
            ok = record_suppression(
                store,
                address=address,
                reason=reason,
                source_event_id=event_id,
            )
            if ok:
                inserted += 1
        except Exception:
            log.exception("sns_writer: failed to record suppression for %s", address)
    return inserted


def handle_lambda_event(event: dict[str, Any], store: Any) -> dict[str, int]:
    """Lambda entry-point — iterates SNS records.

    Returns a small ``{"recorded": N, "skipped": M}`` dict for
    CloudWatch Logs visibility. Lambda invokes this with the standard
    SNS-event shape: ``{"Records": [{"Sns": {"Message": "...",
    "MessageId": "..."}}]}``.

    Designed to be invoked from a tiny Lambda shim:

    ```python
    def lambda_handler(event, _ctx):
        store = _get_or_create_store()
        return handle_lambda_event(event, store)
    ```

    The shim isn't in this repo — it lives with the Lambda deploy
    artifact in ``8th-layer-core`` ops. The handler logic is here so
    it's tested alongside the rest of the suppression code.
    """
    recorded = 0
    skipped = 0
    for record in event.get("Records", []):
        sns = record.get("Sns", {})
        message = sns.get("Message", "")
        message_id = sns.get("MessageId")
        n = handle_sns_message(message, store, event_id=message_id)
        recorded += n
        if n == 0:
            skipped += 1
    return {"recorded": recorded, "skipped": skipped}
