"""HTTP routes for the Enterprise Provisioning Service (FO-2-backend).

Decision 31 endpoints:
  POST /api/v1/enterprises        — anonymous, IP rate-limited (10 req/hr)
  GET  /api/v1/enterprises/jobs/{job_id} — anonymous, ULID unguessable

CORS: signup.8th-layer.ai allowed for both endpoints.

Error envelopes follow Decision 31 §Error envelopes:
  {"error": "...", "code": "<ERR_CODE>", "detail": "..."}
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse

from .db import (
    count_recent_requests,
    get_active_job_for_slug,
    get_job,
    insert_job,
    is_job_expired,
)
from .ids import generate_job_id
from .models import (
    PHASE_LABELS,
    PHASE_PROGRESS,
    CreateEnterpriseRequest,
    CreateEnterpriseResponse,
    JobStatusResponse,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["provisioning"])

# Rate limit: 10 requests per hour per IP.
_RATE_LIMIT_MAX = int(os.environ.get("PROVISIONING_RATE_LIMIT_MAX", "10"))
_RATE_LIMIT_WINDOW_SEC = int(os.environ.get("PROVISIONING_RATE_LIMIT_WINDOW_SEC", "3600"))

# HIGH #5: Only trust X-Forwarded-For from known proxy/load-balancer CIDRs.
# Set PROVISIONING_TRUSTED_PROXY_IPS as a comma-separated list of IPs (exact
# match, not CIDR expansion) that are known to set X-Forwarded-For correctly.
# When empty, X-Forwarded-For is never trusted; request.client.host is used.
# In production behind AWS ALB, add the ALB source IPs here.
_TRUSTED_PROXY_IPS: frozenset[str] = frozenset(
    ip.strip() for ip in os.environ.get("PROVISIONING_TRUSTED_PROXY_IPS", "").split(",") if ip.strip()
)


def _ip_hash(request: Request) -> str:
    """Return a sha256 hash of the real client IP (Decision 31 §ip_hash).

    HIGH #5: X-Forwarded-For is only trusted when the immediate caller
    (request.client.host) is in the PROVISIONING_TRUSTED_PROXY_IPS allowlist.
    If no trusted proxies are configured, or the caller is not a known proxy,
    the raw transport IP is used directly — preventing spoofed-header bypass.
    """
    raw_client_host = request.client.host if request.client else "unknown"
    if raw_client_host in _TRUSTED_PROXY_IPS:
        # The immediate caller is a trusted proxy; take the leftmost (client)
        # IP from X-Forwarded-For. The rightmost entry would be the proxy
        # itself; the leftmost is the original client.
        forwarded = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        real_ip = forwarded if forwarded else raw_client_host
    else:
        # No trusted proxy or unknown caller — use transport IP directly.
        real_ip = raw_client_host
    return hashlib.sha256(real_ip.encode()).hexdigest()


def _error(code: str, error: str, detail: str = "", status: int = 400) -> JSONResponse:
    """Return a Decision 31-compliant error envelope."""
    return JSONResponse(
        status_code=status,
        content={"error": error, "code": code, "detail": detail},
    )


def _get_engine(request: Request) -> Any:
    """Extract the SQLAlchemy engine from app state.

    The provisioning module works directly with the engine (not the
    SqliteStore) because its tables are not managed by SqliteStore's
    async helpers — they're schema-only Alembic tables.
    """
    store = request.app.state.store
    return store._engine  # noqa: SLF001


@router.post(
    "/enterprises",
    response_model=CreateEnterpriseResponse,
    status_code=200,
    summary="Create a new Enterprise (async provisioning)",
)
async def create_enterprise(
    body: CreateEnterpriseRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> CreateEnterpriseResponse | JSONResponse:
    """Anonymous. IP rate-limited (10 req/hr). Returns job_id + poll_url.

    Validation errors return the Decision 31 error envelope (code=VALIDATION,
    SLUG_TAKEN, EMAIL_TAKEN, ROLE_NOT_ASSUMABLE, REGION_NOT_SUPPORTED, RATE_LIMIT).

    Pydantic validates slug shape + email + aws_account_id + region
    at the model layer (raises 422 before this handler runs for those);
    custom codes for uniqueness + AssumeRole validation happen here.
    """
    engine = _get_engine(request)
    ip_hash = _ip_hash(request)

    with engine.connect() as conn:
        # Rate limit check.
        count = count_recent_requests(conn, ip_hash, _RATE_LIMIT_WINDOW_SEC)
        if count >= _RATE_LIMIT_MAX:
            return _error(
                "RATE_LIMIT",
                "Too many signup requests from your IP. Please try again later.",
                f"limit={_RATE_LIMIT_MAX}/hr",
                status=429,
            )

        # HIGH #7: Idempotency — if a non-FAILED job already exists for this
        # slug, return the existing job_id and poll_url without creating new
        # AWS resources. This handles network retries and double-submits.
        existing = get_active_job_for_slug(conn, body.enterprise_slug)
        if existing is not None:
            existing_job_id = existing["job_id"]
            return CreateEnterpriseResponse(
                job_id=existing_job_id,
                enterprise_id=body.enterprise_slug,
                status=existing["status"],
                poll_url=f"/api/v1/enterprises/jobs/{existing_job_id}",
            )

    # AssumeRole validation — must succeed before we accept the request.
    try:
        _validate_assume_role(
            body.marketplace_deploy_role_arn,
            body.enterprise_slug,
            body.assume_role_external_id,
        )
    except Exception as exc:  # noqa: BLE001
        return _error(
            "ROLE_NOT_ASSUMABLE",
            (
                "The marketplace_deploy_role_arn could not be assumed. "
                "Ensure the role exists and has the correct trust policy."
            ),
            str(exc),
            status=403,
        )

    job_id = generate_job_id()
    poll_url = f"/api/v1/enterprises/jobs/{job_id}"

    # Serialize all job parameters for crash recovery (HIGH #6).
    job_params = json.dumps(
        {
            "enterprise_slug": body.enterprise_slug,
            "enterprise_name": body.enterprise_name,
            "admin_email": body.admin_email,
            "aws_account_id": body.aws_account_id,
            "aws_region": body.aws_region,
            "marketplace_deploy_role_arn": body.marketplace_deploy_role_arn,
            "assume_role_external_id": body.assume_role_external_id,
        }
    )

    try:
        with engine.connect() as conn:
            insert_job(
                conn,
                job_id=job_id,
                enterprise_id=body.enterprise_slug,
                status="PROVISIONING",
                phase=0,
                ip_hash=ip_hash,
                assume_role_external_id=body.assume_role_external_id,
                job_params_json=job_params,
            )
    except Exception as exc:  # noqa: BLE001
        # HIGH #4: UNIQUE constraint on enterprise_id fires when a concurrent
        # request raced past the idempotency check above. Translate to SLUG_TAKEN.
        # Also catches genuine duplicate-slug inserts (TOCTOU window closed).
        from sqlalchemy.exc import IntegrityError

        if isinstance(exc, IntegrityError):
            # HIGH #3: Do not echo the slug in the error body — avoids
            # enumerating the customer roster via error messages.
            return _error(
                "SLUG_TAKEN",
                "This slug is not available.",
                status=409,
            )
        raise

    # Kick off the background provisioning job.
    background_tasks.add_task(
        _run_job_background,
        job_id=job_id,
        enterprise_slug=body.enterprise_slug,
        enterprise_name=body.enterprise_name,
        admin_email=body.admin_email,
        aws_account_id=body.aws_account_id,
        aws_region=body.aws_region,
        marketplace_deploy_role_arn=body.marketplace_deploy_role_arn,
        assume_role_external_id=body.assume_role_external_id,
        engine=engine,
    )

    log.info(
        "provisioning job created: job_id=%s enterprise=%s ip_hash=%.8s",
        job_id,
        body.enterprise_slug,
        ip_hash,
    )

    return CreateEnterpriseResponse(
        job_id=job_id,
        enterprise_id=body.enterprise_slug,
        status="PROVISIONING",
        poll_url=poll_url,
    )


@router.get(
    "/enterprises/jobs/{job_id}",
    response_model=JobStatusResponse,
    summary="Poll provisioning job status",
)
async def get_provisioning_job(
    job_id: str,
    request: Request,
) -> JobStatusResponse | JSONResponse:
    """Anonymous. ULID job_id; expires 24h after COMPLETED.

    Returns the current phase, status, progress_pct, and result on
    COMPLETED. Returns 404 for unknown job IDs or expired jobs.
    """
    engine = _get_engine(request)

    with engine.connect() as conn:
        row = get_job(conn, job_id)

    if row is None:
        return _error("NOT_FOUND", "Provisioning job not found.", status=404)

    if is_job_expired(row):
        return _error("NOT_FOUND", "Provisioning job has expired.", status=404)

    phase = row.get("phase") or 0
    status = row.get("status", "PROVISIONING")
    phase_label = PHASE_LABELS.get(phase) if phase else None
    progress_pct = PHASE_PROGRESS.get(phase) if phase else 0

    result = None
    if row.get("result_json"):
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            result = json.loads(row["result_json"])

    return JobStatusResponse(
        job_id=row["job_id"],
        enterprise_id=row["enterprise_id"],
        status=status,
        phase=phase if phase else None,
        phase_label=phase_label,
        progress_pct=progress_pct,
        started_at=row["started_at"],
        completed_at=row.get("completed_at"),
        error=row.get("error"),
        result=result,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_assume_role(
    role_arn: str,
    enterprise_slug: str,
    external_id: str,
) -> None:
    """Attempt sts:AssumeRole to validate the ARN before accepting the request.

    Decision 31: backend MUST sts:AssumeRole against marketplace_deploy_role_arn
    to validate before accepting.

    HIGH #1: ExternalId is required and forwarded to STS to prevent confused-
    deputy attacks. The customer must set the same ExternalId in their role's
    trust policy condition; without it AssumeRole will be denied.

    MEDIUM (8l-reviewer): session policy ``_assume_role_session_policy(slug)``
    is attached so the validation session can ONLY introspect CloudFormation
    on this enterprise's stack — even if the customer's role grants admin.
    The validate session does nothing else; tight scope is safe here.

    Raises RuntimeError on failure.
    """
    import boto3
    import botocore.exceptions

    sts = boto3.client("sts", region_name=os.environ.get("CQ_AWS_REGION", "us-east-1"))
    try:
        sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=f"8l-validate-{enterprise_slug}",
            DurationSeconds=900,
            ExternalId=external_id,
            Policy=_assume_role_session_policy(enterprise_slug),
        )
    except botocore.exceptions.ClientError as exc:
        raise RuntimeError(str(exc)) from exc


def _assume_role_session_policy(enterprise_slug: str) -> str:
    """Return a JSON-encoded inline session policy scoped to this slug's stack.

    Applied to all ``sts:AssumeRole`` calls into the customer's
    ``marketplace_deploy_role_arn`` (MEDIUM from 8l-reviewer). Even if the
    customer makes their role broader than strictly necessary, this inline
    session policy intersects with the role's permissions so OUR session
    can only touch the CloudFormation stack for THIS enterprise.

    CFN service-side privileges (EC2/IAM/ECS resource creation) still run
    under the customer role's broader permissions when CFN itself acts on
    the stack — we just don't exercise them directly through our session.

    The stack name pattern matches ``_phase4_l2_standup``:
    ``8th-layer-l2-<slug>``. The ARN includes a wildcard for the stack UUID
    that CFN appends.
    """
    import json

    stack_arn = f"arn:aws:cloudformation:*:*:stack/8th-layer-l2-{enterprise_slug}/*"
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ScopedCfnStackOps",
                "Effect": "Allow",
                "Action": [
                    "cloudformation:CreateStack",
                    "cloudformation:DescribeStacks",
                    "cloudformation:DescribeStackEvents",
                    "cloudformation:DescribeStackResources",
                    "cloudformation:GetTemplate",
                    "cloudformation:DeleteStack",
                    "cloudformation:UpdateStack",
                ],
                "Resource": stack_arn,
            },
            {
                "Sid": "ListAllStacksForExistenceCheck",
                "Effect": "Allow",
                "Action": ["cloudformation:ListStacks"],
                "Resource": "*",
            },
        ],
    }
    return json.dumps(policy, separators=(",", ":"))


async def _run_job_background(
    *,
    job_id: str,
    enterprise_slug: str,
    enterprise_name: str,
    admin_email: str,
    aws_account_id: str,
    aws_region: str,
    marketplace_deploy_role_arn: str,
    assume_role_external_id: str,
    engine: Any,
) -> None:
    """Thin async wrapper that calls the provisioning worker."""
    from .worker import run_provisioning_job

    try:
        await run_provisioning_job(
            job_id=job_id,
            enterprise_slug=enterprise_slug,
            enterprise_name=enterprise_name,
            admin_email=admin_email,
            aws_account_id=aws_account_id,
            aws_region=aws_region,
            marketplace_deploy_role_arn=marketplace_deploy_role_arn,
            assume_role_external_id=assume_role_external_id,
            db_engine=engine,
        )
    except Exception:  # noqa: BLE001
        # Worker handles its own fail_job(); this is a belt-and-suspenders guard.
        log.exception("Unhandled error in provisioning background task job=%s", job_id)
