"""SES dispatcher — the central service's SES boto3 wrapper.

The L2-side ``email_sender.EmailSender`` USED to do this; under
Decision 34 it's now central-only. One instance per process. The
boto3 client is lazy-initialised so import time is cheap.

# Account

SES sends originate from the control-plane account
(``124074140789``). DKIM + SPF for ``8th-layer.ai`` are wired in that
account; ``signup-events`` is the configuration set in scope.

# Sender identities

Three persona addresses, all verified in the control-plane account:

* ``invites@8th-layer.ai`` — magic-link / invite mail.
* ``auth@8th-layer.ai`` — password reset, 2FA codes.
* ``notifications@8th-layer.ai`` — account events, security alerts.

The mapping from request ``from_persona`` to sender address lives
here so the route handler stays declarative.

# Why SES v1 (still)

Same reasoning as the previous L2-side EmailSender: v1 already has
prod access, the v2 surface adds features we don't need. Stay on v1.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_AWS_REGION = "us-east-1"
DEFAULT_CONFIGURATION_SET = "signup-events"

# Persona → sender address. The route handler validates the persona
# value (Pydantic Literal) so a missing key here would be a code bug,
# not a runtime input failure.
PERSONA_SENDERS: dict[str, str] = {
    "invites": "invites@8th-layer.ai",
    "auth": "auth@8th-layer.ai",
    "notifications": "notifications@8th-layer.ai",
}


def _aws_region() -> str:
    return os.environ.get("CQ_AWS_REGION", DEFAULT_AWS_REGION)


def _configuration_set() -> str:
    return os.environ.get("CQ_SES_CONFIGURATION_SET", DEFAULT_CONFIGURATION_SET)


@dataclass
class SesDispatcher:
    """boto3-backed SES sender. Production dispatch surface."""

    region: str = field(default_factory=_aws_region)
    configuration_set: str = field(default_factory=_configuration_set)
    _client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3  # local — keep cold-start cost off the import graph

            self._client = boto3.client("ses", region_name=self.region)
        return self._client

    def send(
        self,
        *,
        from_persona: str,
        to: str,
        subject: str,
        text_body: str,
        html_body: str | None = None,
    ) -> dict[str, Any]:
        """Dispatch one SES send. Returns ``{"MessageId": ...}``.

        Raises ``KeyError`` if ``from_persona`` isn't a known persona
        — the route handler's Pydantic ``Literal`` already gates this,
        so a KeyError here is genuinely a code bug.
        """
        sender = PERSONA_SENDERS[from_persona]
        body: dict[str, Any] = {"Text": {"Data": text_body, "Charset": "UTF-8"}}
        if html_body:
            body["Html"] = {"Data": html_body, "Charset": "UTF-8"}
        client = self._get_client()
        return client.send_email(
            Source=sender,
            Destination={"ToAddresses": [to]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": body,
            },
            ConfigurationSetName=self.configuration_set,
        )


@dataclass
class MockSesDispatcher:
    """Test double — captures every send for assertion."""

    sent: list[dict[str, Any]] = field(default_factory=list)

    def send(
        self,
        *,
        from_persona: str,
        to: str,
        subject: str,
        text_body: str,
        html_body: str | None = None,
    ) -> dict[str, Any]:
        self.sent.append(
            {
                "from_persona": from_persona,
                "to": to,
                "subject": subject,
                "text_body": text_body,
                "html_body": html_body,
            }
        )
        return {"MessageId": f"mock-tx-{len(self.sent)}"}
