"""HTTP routes for the Enterprise Provisioning Service (FO-2-backend).

Decision 31 endpoints:
  POST /api/v1/enterprises        — anonymous, IP rate-limited (10 req/hr)
  GET  /api/v1/enterprises/jobs/{job_id} — anonymous, ULID unguessable

CORS: signup.8th-layer.ai allowed for both endpoints.

Error envelopes follow Decision 31 §Error envelopes:
  {"error": "...", "code": "<ERR_CODE>", "detail": "..."}
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse

from .db import (
    count_recent_requests,
    get_job,
    insert_job,
    is_job_expired,
    is_slug_taken,
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


def _ip_hash(request: Request) -> str:
    """Return a sha256 hash of the client IP (Decision 31 §ip_hash).

    Uses X-Forwarded-For when present (ALB terminates TLS and sets this);
    falls back to the raw client host.
    """
    forwarded = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    raw_ip = forwarded or (request.client.host if request.client else "unknown")
    return hashlib.sha256(raw_ip.encode()).hexdigest()


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

        # Slug uniqueness.
        if is_slug_taken(conn, body.enterprise_slug):
            return _error(
                "SLUG_TAKEN",
                f"The enterprise slug '{body.enterprise_slug}' is already taken.",
                status=409,
            )

    # AssumeRole validation — must succeed before we accept the request.
    try:
        _validate_assume_role(body.marketplace_deploy_role_arn, body.enterprise_slug)
    except Exception as exc:  # noqa: BLE001
        return _error(
            "ROLE_NOT_ASSUMABLE",
            "The marketplace_deploy_role_arn could not be assumed. Ensure the role exists and has the correct trust policy.",
            str(exc),
            status=403,
        )

    job_id = generate_job_id()
    poll_url = f"/api/v1/enterprises/jobs/{job_id}"

    with engine.connect() as conn:
        insert_job(
            conn,
            job_id=job_id,
            enterprise_id=body.enterprise_slug,
            status="PROVISIONING",
            phase=0,
            ip_hash=ip_hash,
        )

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
        try:
            result = json.loads(row["result_json"])
        except (json.JSONDecodeError, TypeError):
            pass

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


def _validate_assume_role(role_arn: str, enterprise_slug: str) -> None:
    """Attempt sts:AssumeRole to validate the ARN before accepting the request.

    Decision 31: backend MUST sts:AssumeRole against marketplace_deploy_role_arn
    to validate before accepting.

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
        )
    except botocore.exceptions.ClientError as exc:
        raise RuntimeError(str(exc)) from exc


async def _run_job_background(
    *,
    job_id: str,
    enterprise_slug: str,
    enterprise_name: str,
    admin_email: str,
    aws_account_id: str,
    aws_region: str,
    marketplace_deploy_role_arn: str,
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
            db_engine=engine,
        )
    except Exception:  # noqa: BLE001
        # Worker handles its own fail_job(); this is a belt-and-suspenders guard.
        log.exception("Unhandled error in provisioning background task job=%s", job_id)
