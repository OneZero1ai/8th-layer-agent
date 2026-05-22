"""Email sending — L2-side client for the central transactional-mail service.

# Decision 34 migration (2026-05-20)

Pre-Decision-34, each L2 called SES from its own AWS account. That
required every customer to verify ``8th-layer.ai`` as a domain
identity, exit SES sandbox, and wire SNS bounce/complaint — the
opposite of the marketplace "drop into your AWS account" pitch.

After Decision 34, every L2 posts to
``https://directory.8th-layer.ai/api/v1/transactional/send`` with an
HMAC signature; the control plane dispatches via SES from account
``124074140789``. Customer L2s have ZERO SES setup.

The public ``send_invite(...)`` signature is preserved. Callers (the
admin invite route + future ``send_2fa`` / ``send_password_reset``
helpers) don't change. Suppression hits are surfaced cleanly via
:class:`EmailSendOutcome`.

# Backwards-compat fallback (migration window)

If ``TX_SEND_KEY`` env var is missing AND ``CQ_EMAIL_LEGACY_SES=1``
is set, the sender falls back to the old boto3 SES path. This lets
the L2 image roll out ahead of the per-L2 SSM-key backfill +
template update; the operator clears the flag once the per-L2
``tx_send_key`` is in place. A clear deprecation log line fires every
time the legacy path is used.

# Production wiring

* ``TX_SEND_BASE_URL`` — defaults to ``https://directory.8th-layer.ai``.
* ``TX_SEND_KEY`` — per-L2 HMAC key, mounted from SSM at
  ``/8th-layer/l2/{enterprise}/{group}/tx_send_key`` via the ECS
  task definition.
* ``CQ_ENTERPRISE`` + ``CQ_GROUP`` — already wired, used to build
  the ``X-8L-L2-Id`` header.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from .transactional.auth import compute_signature

log = logging.getLogger(__name__)

DEFAULT_TX_BASE_URL = "https://directory.8th-layer.ai"
DEFAULT_FROM_EMAIL = "invites@8th-layer.ai"  # used by MockEmailSender capture only
DEFAULT_AWS_REGION = "us-east-1"
DEFAULT_HTTP_TIMEOUT_SEC = 10.0

# Outcome status values surfaced to callers. The route handler maps
# 202 → "sent", 409 → "suppressed", everything else → "error". This
# is the same envelope the admin invite route wants to log.
SendStatus = Literal["sent", "suppressed", "error"]


@dataclass
class EmailSendOutcome:
    """What ``send_invite`` (and siblings) return.

    Wraps the central-service response so callers don't have to
    introspect raw HTTP status codes. ``status`` is the load-bearing
    field; ``reason`` is populated for ``suppressed`` and ``error``.
    """

    status: SendStatus
    delivery_handle: str | None = None
    ses_message_id: str | None = None
    reason: str | None = None
    raw: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        """Return True when the send status indicates a successful delivery."""
        return self.status == "sent"


# ---------------------------------------------------------------------------
# Env resolution
# ---------------------------------------------------------------------------


def _tx_base_url() -> str:
    return os.environ.get("TX_SEND_BASE_URL", DEFAULT_TX_BASE_URL).rstrip("/")


def _tx_send_key() -> str | None:
    return os.environ.get("TX_SEND_KEY") or None


def _l2_id() -> str:
    enterprise = os.environ.get("CQ_ENTERPRISE", "")
    group = os.environ.get("CQ_GROUP", "")
    return f"{enterprise}/{group}"


def _from_email() -> str:
    return os.environ.get("CQ_INVITE_FROM_EMAIL", DEFAULT_FROM_EMAIL)


def _aws_region() -> str:
    return os.environ.get("CQ_AWS_REGION", DEFAULT_AWS_REGION)


def _public_host(default: str = "https://app.8th-layer.ai") -> str:
    return os.environ.get("CQ_PUBLIC_HOST", default)


def _legacy_ses_enabled() -> bool:
    return os.environ.get("CQ_EMAIL_LEGACY_SES", "").lower() in {"1", "true", "yes"}


# ---------------------------------------------------------------------------
# Body composition helpers (unchanged from pre-Decision-34)
# ---------------------------------------------------------------------------


def _claim_url(jwt: str, *, host: str | None = None) -> str:
    base = (host or _public_host()).rstrip("/")
    return f"{base}/invite/{jwt}"


def _render_subject(inviter_name: str, enterprise_name: str) -> str:
    return f"{inviter_name} invited you to {enterprise_name}"


def _render_text_body(
    *,
    inviter_name: str,
    enterprise_name: str,
    expiry_iso: str,
    claim_url: str,
) -> str:
    return (
        "Hi,\n"
        "\n"
        f"{inviter_name} has invited you to join {enterprise_name} on 8th-Layer.ai.\n"
        "\n"
        f"Click below to claim your account. This link is single-use and expires on {expiry_iso}.\n"
        "\n"
        f"  {claim_url}\n"
        "\n"
        "If you didn't expect this email, you can safely ignore it.\n"
        "\n"
        "— 8th-Layer.ai\n"
    )


def _render_html_body(
    *,
    inviter_name: str,
    enterprise_name: str,
    expiry_iso: str,
    claim_url: str,
) -> str:
    """Plain HTML — high-deliverability bias, no images / minimal CSS."""
    return (
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8">'
        f"<title>{_render_subject(inviter_name, enterprise_name)}</title>"
        "</head>"
        '<body style="font-family:system-ui,sans-serif;color:#222;'
        'background:#fff;padding:24px;max-width:560px;margin:auto;">'
        "<p>Hi,</p>"
        f"<p><strong>{inviter_name}</strong> has invited you to join "
        f"<strong>{enterprise_name}</strong> on 8th-Layer.ai.</p>"
        "<p>Click below to claim your account. This link is single-use "
        f"and expires on <strong>{expiry_iso}</strong>.</p>"
        '<p style="margin:24px 0;">'
        f'<a href="{claim_url}" '
        'style="background:#111;color:#fff;padding:12px 20px;'
        'text-decoration:none;border-radius:6px;display:inline-block;">'
        "Claim your account</a></p>"
        '<p style="font-size:13px;color:#666;">'
        "If the button above doesn't work, paste this link into your "
        f'browser:<br><a href="{claim_url}">{claim_url}</a></p>'
        '<p style="font-size:13px;color:#666;">'
        "If you didn't expect this email, you can safely ignore it.</p>"
        '<p style="font-size:13px;color:#666;">— 8th-Layer.ai</p>'
        "</body></html>"
    )


# ---------------------------------------------------------------------------
# HTTP-based EmailSender (Decision 34)
# ---------------------------------------------------------------------------


class EmailSender:
    """HTTP-backed sender that posts to the central transactional service.

    One instance per process is fine. The httpx client is lazy-
    initialised on first send so import-time cost stays low.

    Tests don't need to construct this directly — they use
    ``MockEmailSender`` via dependency override.
    """

    def __init__(
        self,
        *,
        from_email: str | None = None,
        region: str | None = None,
        public_host: str | None = None,
        base_url: str | None = None,
        tx_key: str | None = None,
        l2_id: str | None = None,
        timeout_sec: float = DEFAULT_HTTP_TIMEOUT_SEC,
    ) -> None:
        """Initialise the transactional-mail HTTP sender.

        All parameters are optional; missing values resolve from env at
        first-send time so import-time cost stays low and test fixtures
        can construct without setting AWS env vars. ``from_email`` and
        ``region`` are retained for backwards-compat with fixtures that
        still pass them — the central service ignores them (sender is
        persona-keyed). See ``_resolve_*`` helpers for defaulting rules.
        """
        self._from_email = from_email or _from_email()
        self._region = region or _aws_region()
        self._public_host = public_host
        self._base_url = (base_url or _tx_base_url()).rstrip("/")
        # Resolve key/l2_id lazily — env vars may be set after construction
        # in some test patterns.
        self._tx_key_override = tx_key
        self._l2_id_override = l2_id
        self._timeout = timeout_sec
        self._client: Any = None
        self._legacy_ses_client: Any = None

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------

    def _get_http(self) -> Any:
        if self._client is None:
            import httpx  # local — keeps cold-start cheap

            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def _resolve_key(self) -> str | None:
        return self._tx_key_override or _tx_send_key()

    def _resolve_l2_id(self) -> str:
        return self._l2_id_override or _l2_id()

    def _post_send(
        self,
        *,
        from_persona: str,
        to: str,
        subject: str,
        text_body: str,
        html_body: str | None,
        category: str,
        audit_ref: str | None = None,
    ) -> EmailSendOutcome:
        """Post one transactional send. Returns a uniform outcome envelope."""
        key = self._resolve_key()
        l2_id = self._resolve_l2_id()
        if not key:
            # Fallback path — see module docstring on the migration window.
            if _legacy_ses_enabled():
                log.warning(
                    "email_sender: TX_SEND_KEY missing; falling back to legacy SES "
                    "(Decision 34 backfill not yet applied to this L2)"
                )
                return self._legacy_ses_send(
                    from_persona=from_persona,
                    to=to,
                    subject=subject,
                    text_body=text_body,
                    html_body=html_body,
                )
            log.error(
                "email_sender: TX_SEND_KEY missing and legacy SES not enabled; "
                "cannot deliver email (set TX_SEND_KEY or CQ_EMAIL_LEGACY_SES=1)"
            )
            return EmailSendOutcome(status="error", reason="tx_send_key_missing")

        body: dict[str, Any] = {
            "from_persona": from_persona,
            "to": to,
            "subject": subject,
            "text": text_body,
            "category": category,
        }
        if html_body:
            body["html"] = html_body
        if audit_ref:
            body["audit_ref"] = audit_ref

        # The signature is over the EXACT body bytes the server reads.
        # Build the JSON once and reuse, so re-serialisation differences
        # (whitespace, key order) can't break the digest.
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers = {
            "X-8L-L2-Id": l2_id,
            "X-8L-Signature": compute_signature(key, body_bytes),
            "Content-Type": "application/json",
        }
        if audit_ref:
            # audit_ref doubles as the default idempotency hint — the
            # server falls back to ``{l2_id}:ref:{audit_ref}`` when no
            # explicit header is provided.
            pass

        url = f"{self._base_url}/api/v1/transactional/send"
        client = self._get_http()
        try:
            resp = client.post(url, content=body_bytes, headers=headers)
        except Exception as exc:
            log.exception("email_sender: HTTP transport error to %s", url)
            return EmailSendOutcome(status="error", reason=f"transport_error:{type(exc).__name__}")

        if resp.status_code == 202:
            payload = resp.json()
            return EmailSendOutcome(
                status="sent",
                delivery_handle=payload.get("delivery_handle"),
                ses_message_id=payload.get("ses_message_id"),
                raw=payload,
            )
        if resp.status_code == 409:
            payload = resp.json()
            detail = payload.get("detail", payload)
            return EmailSendOutcome(
                status="suppressed",
                reason=detail.get("reason"),
                raw=detail,
            )
        log.error(
            "email_sender: unexpected response status=%s body=%s",
            resp.status_code,
            resp.text[:200],
        )
        return EmailSendOutcome(
            status="error",
            reason=f"http_{resp.status_code}",
            raw={"status_code": resp.status_code, "body": resp.text[:500]},
        )

    # ------------------------------------------------------------------
    # Legacy SES fallback — used only during the migration window.
    # ------------------------------------------------------------------

    def _legacy_ses_send(
        self,
        *,
        from_persona: str,
        to: str,
        subject: str,
        text_body: str,
        html_body: str | None,
    ) -> EmailSendOutcome:
        """Old boto3 SES path. Kept until the per-L2 backfill completes."""
        try:
            if self._legacy_ses_client is None:
                import boto3

                self._legacy_ses_client = boto3.client("ses", region_name=self._region)
            client = self._legacy_ses_client
            body_msg: dict[str, Any] = {"Text": {"Data": text_body, "Charset": "UTF-8"}}
            if html_body:
                body_msg["Html"] = {"Data": html_body, "Charset": "UTF-8"}
            resp = client.send_email(
                Source=self._from_email,
                Destination={"ToAddresses": [to]},
                Message={
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": body_msg,
                },
            )
            return EmailSendOutcome(
                status="sent",
                ses_message_id=resp.get("MessageId"),
                raw=resp,
            )
        except Exception as exc:  # pragma: no cover — env-specific
            log.exception("email_sender: legacy SES path failed")
            return EmailSendOutcome(status="error", reason=f"legacy_ses_error:{type(exc).__name__}")

    # ------------------------------------------------------------------
    # Public send_* helpers — preserved signatures.
    # ------------------------------------------------------------------

    def send_invite(
        self,
        to: str,
        jwt: str,
        inviter_name: str,
        enterprise_name: str,
        expiry: datetime,
    ) -> dict[str, Any]:
        """Send an invite email. Backwards-compat shape: returns a dict.

        For backwards compatibility with pre-Decision-34 callers that
        expected the SES ``SendEmail`` response, we wrap the outcome
        in a compatible-enough dict:

        * ``MessageId`` — present on ``sent`` and legacy paths.
        * ``status`` — new field. ``"sent" | "suppressed" | "error"``.
        * ``delivery_handle`` — central-service handle (sent path).
        * ``reason`` — populated for ``suppressed`` and ``error``.

        Callers checking ``response["MessageId"]`` keep working; new
        callers should check ``response["status"]``.
        """
        outcome = self._send_invite_envelope(
            to=to,
            jwt=jwt,
            inviter_name=inviter_name,
            enterprise_name=enterprise_name,
            expiry=expiry,
        )
        return _outcome_to_legacy_dict(outcome)

    def send_invite_outcome(
        self,
        to: str,
        jwt: str,
        inviter_name: str,
        enterprise_name: str,
        expiry: datetime,
    ) -> EmailSendOutcome:
        """Same as ``send_invite`` but returns the structured outcome."""
        return self._send_invite_envelope(
            to=to,
            jwt=jwt,
            inviter_name=inviter_name,
            enterprise_name=enterprise_name,
            expiry=expiry,
        )

    def _send_invite_envelope(
        self,
        *,
        to: str,
        jwt: str,
        inviter_name: str,
        enterprise_name: str,
        expiry: datetime,
    ) -> EmailSendOutcome:
        claim_url = _claim_url(jwt, host=self._public_host)
        expiry_iso = expiry.isoformat()
        subject = _render_subject(inviter_name, enterprise_name)
        text_body = _render_text_body(
            inviter_name=inviter_name,
            enterprise_name=enterprise_name,
            expiry_iso=expiry_iso,
            claim_url=claim_url,
        )
        html_body = _render_html_body(
            inviter_name=inviter_name,
            enterprise_name=enterprise_name,
            expiry_iso=expiry_iso,
            claim_url=claim_url,
        )
        return self._post_send(
            from_persona="invites",
            to=to,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            category="invite_magic_link",
            # Tie idempotency to the invite jti — re-mints with the
            # same jti dedup within the 60s server-side window.
            audit_ref=f"invite:{jwt[:32]}",
        )


# ---------------------------------------------------------------------------
# Mock — preserved for tests that exercise the email channel.
# ---------------------------------------------------------------------------


@dataclass
class CapturedEmail:
    """In-memory representation of a send for test assertions."""

    to: str
    jwt: str
    inviter_name: str
    enterprise_name: str
    expiry: datetime
    subject: str
    text_body: str
    html_body: str
    claim_url: str


@dataclass
class MockEmailSender:
    """Test double — captures every ``send_invite`` call.

    Public attribute: ``sent`` is the captured-call list, in order.
    Existing tests asserting on ``sent[-1].to``, ``sent[-1].subject``,
    etc., keep working unchanged.
    """

    from_email: str = field(default_factory=_from_email)
    public_host: str | None = None
    sent: list[CapturedEmail] = field(default_factory=list)
    # New: simulate the central-service outcome. Default ``"sent"``
    # preserves pre-Decision-34 behaviour; tests of the suppression
    # branch override this.
    next_outcome: SendStatus = "sent"
    next_reason: str | None = None

    def send_invite(
        self,
        to: str,
        jwt: str,
        inviter_name: str,
        enterprise_name: str,
        expiry: datetime,
    ) -> dict[str, Any]:
        """Capture an invite send in-memory for tests and return a mock receipt."""
        claim_url = _claim_url(jwt, host=self.public_host)
        expiry_iso = expiry.isoformat()
        subject = _render_subject(inviter_name, enterprise_name)
        text_body = _render_text_body(
            inviter_name=inviter_name,
            enterprise_name=enterprise_name,
            expiry_iso=expiry_iso,
            claim_url=claim_url,
        )
        html_body = _render_html_body(
            inviter_name=inviter_name,
            enterprise_name=enterprise_name,
            expiry_iso=expiry_iso,
            claim_url=claim_url,
        )
        self.sent.append(
            CapturedEmail(
                to=to,
                jwt=jwt,
                inviter_name=inviter_name,
                enterprise_name=enterprise_name,
                expiry=expiry,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                claim_url=claim_url,
            )
        )
        outcome = EmailSendOutcome(
            status=self.next_outcome,
            delivery_handle=f"mock-handle-{len(self.sent)}" if self.next_outcome == "sent" else None,
            ses_message_id=f"mock-{len(self.sent)}" if self.next_outcome == "sent" else None,
            reason=self.next_reason,
        )
        return _outcome_to_legacy_dict(outcome)


# ---------------------------------------------------------------------------
# Internal — outcome → legacy dict adapter
# ---------------------------------------------------------------------------


def _outcome_to_legacy_dict(outcome: EmailSendOutcome) -> dict[str, Any]:
    """Adapt ``EmailSendOutcome`` to the dict shape pre-Decision-34 callers expect.

    Pre-Decision-34 ``send_invite`` returned the raw SES
    ``SendEmail`` response (which has a ``MessageId`` field). New
    callers should look at ``status``; old callers checking
    ``["MessageId"]`` keep working when status is ``sent`` (also
    populated on the legacy SES fallback path).
    """
    return {
        "status": outcome.status,
        "MessageId": outcome.ses_message_id,
        "delivery_handle": outcome.delivery_handle,
        "reason": outcome.reason,
    }
