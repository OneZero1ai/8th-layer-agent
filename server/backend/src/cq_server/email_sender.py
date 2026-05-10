"""Email sending — boto3 SES wrapper for invite delivery (FO-1b).

The production sender (``EmailSender``) wraps a boto3 SES v1 client. The
mock variant (``MockEmailSender``) captures sends in memory so tests can
assert on subject / recipient / body without touching AWS.

Both classes share the same ``send_invite(...)`` shape so callers
inject the wiring at construction time and never branch on type.

# Why SES v1 (not SESv2)

The AWS account that ships 8th-Layer.ai's invite-domain identity is
already configured for SES v1; the v2 surface is only marginally
different and we have no need for the extra features (bulk send,
template engine) right now. Stay on v1; revisit if we want
``SendBulkEmail``-style batching later.

# Sandbox vs production

Outside SES sandbox, ``send_email`` works against any verified-sender
identity. Inside the sandbox, the *recipient* must also be verified —
the operator runs the sandbox-exit ticket separately (agent#198). This
module is identical across the two; the difference is operator-side
(IAM + SES-identity setup), not code.

# Source identity + region

* ``CQ_INVITE_FROM_EMAIL`` — the From: header. Defaults to
  ``invites@8th-layer.ai`` per FO-1 spec.
* ``CQ_AWS_REGION`` — the SES regional endpoint. Defaults to
  ``us-east-1`` (matches the ``orion`` profile + Bedrock region).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_FROM_EMAIL = "invites@8th-layer.ai"
DEFAULT_AWS_REGION = "us-east-1"


def _from_email() -> str:
    return os.environ.get("CQ_INVITE_FROM_EMAIL", DEFAULT_FROM_EMAIL)


def _aws_region() -> str:
    return os.environ.get("CQ_AWS_REGION", DEFAULT_AWS_REGION)


def _public_host(default: str = "https://app.8th-layer.ai") -> str:
    """Return the public host used to build the claim URL.

    ``CQ_PUBLIC_HOST`` is the production override. Falls back to the
    well-known marketing domain — never to ``localhost`` because invite
    emails sent from a localhost-baseurl would dead-link.
    """
    return os.environ.get("CQ_PUBLIC_HOST", default)


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


class EmailSender:
    """boto3-backed SES sender. One instance per process is fine.

    The boto3 client is lazy-initialised on first send so import time is
    cheap and unit tests that never call ``send_invite`` don't pay the
    boto3 import cost.
    """

    def __init__(
        self,
        *,
        from_email: str | None = None,
        region: str | None = None,
        public_host: str | None = None,
    ) -> None:
        """Build the sender; resolves env-driven defaults at construction."""
        self._from_email = from_email or _from_email()
        self._region = region or _aws_region()
        self._public_host = public_host  # None → resolved per-call
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3  # local import — keeps cold-start cost off the import graph

            self._client = boto3.client("ses", region_name=self._region)
        return self._client

    def send_invite(
        self,
        to: str,
        jwt: str,
        inviter_name: str,
        enterprise_name: str,
        expiry: datetime,
    ) -> dict[str, Any]:
        """Send an invite email. Returns the SES response (or raises).

        ``expiry`` is rendered as an ISO 8601 string in the email body
        — the reader sees the deadline; the JWT enforces it.
        """
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

        client = self._get_client()
        return client.send_email(
            Source=self._from_email,
            Destination={"ToAddresses": [to]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Text": {"Data": text_body, "Charset": "UTF-8"},
                    "Html": {"Data": html_body, "Charset": "UTF-8"},
                },
            },
        )


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
    Tests assert on ``sent[-1].to``, ``sent[-1].subject``, etc.
    """

    from_email: str = field(default_factory=_from_email)
    public_host: str | None = None
    sent: list[CapturedEmail] = field(default_factory=list)

    def send_invite(
        self,
        to: str,
        jwt: str,
        inviter_name: str,
        enterprise_name: str,
        expiry: datetime,
    ) -> dict[str, Any]:
        """Capture the would-be send and return a fake SES MessageId."""
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
        return {"MessageId": f"mock-{len(self.sent)}"}
