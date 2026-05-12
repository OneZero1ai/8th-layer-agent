"""Pydantic models for the Enterprise Provisioning Service (Decision 31).

Matches the endpoint contract exactly — do not deviate from field names,
types, or status string values defined here without updating Decision 31.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Allowed AWS regions (v1 — expand later)
# ---------------------------------------------------------------------------

ALLOWED_REGIONS: frozenset[str] = frozenset({"us-east-1"})

# Slug: starts with lowercase letter, then 2–30 lowercase-alphanumeric or hyphen.
_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{2,30}$")

# Minimal 12-digit AWS account id.
_AWS_ACCOUNT_RE = re.compile(r"^\d{12}$")

# ---------------------------------------------------------------------------
# Status constants (Decision 31 §Phases)
# ---------------------------------------------------------------------------

JobStatus = Literal[
    "KEY_MINT_IN_PROGRESS",
    "DIRECTORY_REGISTER_IN_PROGRESS",
    "DNS_PROVISION_IN_PROGRESS",
    "L2_STANDUP_IN_PROGRESS",
    "ADMIN_INVITE_SENT",
    "COMPLETED",
    "FAILED",
    "PROVISIONING",  # initial status returned on POST
]

PHASE_STATUS: dict[int, str] = {
    1: "KEY_MINT_IN_PROGRESS",
    2: "DIRECTORY_REGISTER_IN_PROGRESS",
    3: "DNS_PROVISION_IN_PROGRESS",
    4: "L2_STANDUP_IN_PROGRESS",
    5: "ADMIN_INVITE_SENT",
    6: "COMPLETED",
}

PHASE_LABELS: dict[int, str] = {
    1: "Generating your Enterprise signing key…",
    2: "Registering you in the AI-BGP directory…",
    3: "Allocating your subdomain…",
    4: "Standing up your first L2 in your AWS account…",
    5: "Sending your admin invite email…",
    6: "Done.",
}

PHASE_PROGRESS: dict[int, int] = {
    1: 10,
    2: 25,
    3: 40,
    4: 65,
    5: 90,
    6: 100,
}

# ---------------------------------------------------------------------------
# Request / response shapes
# ---------------------------------------------------------------------------


class CreateEnterpriseRequest(BaseModel):
    """POST /api/v1/enterprises body (Decision 31 §POST endpoint)."""

    enterprise_name: str = Field(min_length=1, max_length=200)
    enterprise_slug: str
    admin_email: str
    aws_account_id: str
    aws_region: str
    marketplace_deploy_role_arn: str
    # HIGH #1: ExternalId prevents confused-deputy attacks. The customer
    # sets this value when creating the IAM role trust policy in their
    # account; we require and store it, then pass it through AssumeRole.
    # Minimum 8 characters; opaque to us.
    assume_role_external_id: str = Field(
        min_length=8,
        max_length=1224,
        description=(
            "ExternalId you set in the trust policy of marketplace_deploy_role_arn. "
            "Required to prevent confused-deputy attacks."
        ),
    )

    @field_validator("enterprise_slug")
    @classmethod
    def _validate_slug(cls, value: str) -> str:
        if not _SLUG_RE.match(value):
            raise ValueError(
                "enterprise_slug must match ^[a-z][a-z0-9-]{2,30}$"
            )
        return value

    @field_validator("admin_email")
    @classmethod
    def _validate_email(cls, value: str) -> str:
        # Minimal RFC 5322 sanity — same pattern as invite_routes.
        v = value.strip()
        if "@" not in v or v.startswith("@") or v.endswith("@"):
            raise ValueError("invalid email address")
        return v

    @field_validator("aws_account_id")
    @classmethod
    def _validate_account_id(cls, value: str) -> str:
        if not _AWS_ACCOUNT_RE.match(value):
            raise ValueError("aws_account_id must be exactly 12 digits")
        return value

    @field_validator("aws_region")
    @classmethod
    def _validate_region(cls, value: str) -> str:
        if value not in ALLOWED_REGIONS:
            raise ValueError(f"region not supported; allowed: {sorted(ALLOWED_REGIONS)}")
        return value

    @field_validator("marketplace_deploy_role_arn")
    @classmethod
    def _validate_role_arn(cls, value: str) -> str:
        if not value.startswith("arn:aws:iam::"):
            raise ValueError("marketplace_deploy_role_arn must be a valid IAM Role ARN")
        return value


class CreateEnterpriseResponse(BaseModel):
    """Immediate 200 response from POST /api/v1/enterprises (Decision 31)."""

    job_id: str
    enterprise_id: str
    status: str = "PROVISIONING"
    poll_url: str


class JobStatusResponse(BaseModel):
    """Response from GET /api/v1/enterprises/jobs/{job_id} (Decision 31)."""

    job_id: str
    enterprise_id: str
    status: str
    phase: int | None = None
    phase_label: str | None = None
    progress_pct: int | None = None
    started_at: str
    completed_at: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None


class ErrorEnvelope(BaseModel):
    """Standard error envelope (Decision 31 §Error envelopes)."""

    error: str
    code: str
    detail: str = ""
