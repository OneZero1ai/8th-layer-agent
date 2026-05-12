"""Startup crash-recovery for orphaned provisioning jobs (HIGH #6).

FastAPI BackgroundTasks are lost when the ECS task restarts mid-job.
This module is called once during the app lifespan startup to detect and
re-queue any jobs that were left in a non-terminal state (i.e. not
COMPLETED or FAILED) and are old enough to be considered orphaned.

Design (DB-recovery approach):
  On startup, query provisioning_jobs for rows with status NOT IN
  (COMPLETED, FAILED) that started more than RECOVERY_THRESHOLD_SEC ago.
  Re-queue each as a new asyncio task against the same DB engine.

The threshold (default 5 min) is intentionally short relative to the
longest phase (phase 4, up to 30 min). If the ECS task restarts, all
in-flight BackgroundTasks are gone — any stuck row on a fresh process
is definitionally orphaned.

Re-queued jobs restart from phase 1 (full re-run). This is safe:
  - Phase 1 KEY_MINT: SSM put_parameter Overwrite=True → idempotent.
  - Phase 2 DIRECTORY_REGISTER: announce returns 200 on update → idempotent.
  - Phase 3 DNS_PROVISION: Cloudflare returns success on existing CNAME.
  - Phase 4 L2_STANDUP: CFN create_stack fails if stack exists → phase
    catches terminal state and raises → job → FAILED. Operator cleans up
    and re-submits. This is acceptable for v1; FO-6 adds resume-from-phase.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from .db import fail_job, get_stuck_jobs

log = logging.getLogger(__name__)

# How old a non-terminal job must be (seconds) before it is considered orphaned.
_RECOVERY_THRESHOLD_SEC = int(os.environ.get("PROVISIONING_RECOVERY_THRESHOLD_SEC", "300"))


async def recover_stuck_jobs(db_engine: Any) -> None:
    """Detect and re-queue orphaned provisioning jobs on startup.

    Called once from the app lifespan after the DB engine is ready.
    Finds jobs in non-terminal states older than RECOVERY_THRESHOLD_SEC
    and re-queues them as asyncio tasks using stored job_params_json.

    HIGH #6: Prevents silent orphan loss when ECS restarts mid-job.
    """
    from .worker import run_provisioning_job

    try:
        with db_engine.connect() as conn:
            stuck = get_stuck_jobs(conn, older_than_seconds=_RECOVERY_THRESHOLD_SEC)
    except Exception:  # noqa: BLE001
        log.exception("provisioning recovery: failed to query stuck jobs — skipping")
        return

    if not stuck:
        log.debug("provisioning recovery: no stuck jobs found")
        return

    log.warning(
        "provisioning recovery: found %d stuck job(s) — re-queueing",
        len(stuck),
    )

    for row in stuck:
        job_id = row["job_id"]
        enterprise_slug = row["enterprise_id"]
        params_json = row.get("job_params_json")

        if not params_json:
            # Job predates job_params_json (created before HIGH #6 was deployed).
            # Cannot safely re-run; mark FAILED so it doesn't hang forever.
            log.error(
                "provisioning recovery: job %s has no job_params_json "
                "(pre-HIGH#6 row) — marking FAILED. Re-submit via the wizard.",
                job_id,
            )
            _mark_failed(db_engine, job_id, "recovery: job predates stored params; re-submit via the signup wizard.")
            continue

        try:
            params = json.loads(params_json)
        except (json.JSONDecodeError, TypeError):
            log.error(
                "provisioning recovery: job %s has malformed job_params_json — marking FAILED",
                job_id,
            )
            _mark_failed(db_engine, job_id, "recovery: malformed job_params_json; re-submit via the signup wizard.")
            continue

        log.info(
            "provisioning recovery: re-queueing job %s enterprise=%s from phase 1",
            job_id,
            enterprise_slug,
        )

        # Schedule as a top-level asyncio task. If this raises, log and continue —
        # crash recovery must not crash the server.
        asyncio.get_event_loop().create_task(
            _requeue_with_guard(
                run_provisioning_job,
                job_id=job_id,
                enterprise_slug=params.get("enterprise_slug", enterprise_slug),
                enterprise_name=params.get("enterprise_name", enterprise_slug),
                admin_email=params.get("admin_email", ""),
                aws_account_id=params.get("aws_account_id", ""),
                aws_region=params.get("aws_region", "us-east-1"),
                marketplace_deploy_role_arn=params.get("marketplace_deploy_role_arn", ""),
                assume_role_external_id=params.get("assume_role_external_id", ""),
                db_engine=db_engine,
            )
        )


async def _requeue_with_guard(fn, **kwargs) -> None:  # type: ignore[no-untyped-def]
    """Run fn(**kwargs); log exceptions so the task never crashes silently."""
    job_id = kwargs.get("job_id", "?")
    try:
        await fn(**kwargs)
    except Exception:  # noqa: BLE001
        log.exception("provisioning recovery: re-queued job %s raised", job_id)


def _mark_failed(db_engine: Any, job_id: str, error: str) -> None:
    try:
        with db_engine.connect() as conn:
            fail_job(conn, job_id=job_id, error=error)
    except Exception:  # noqa: BLE001
        log.exception("provisioning recovery: failed to mark job %s as FAILED", job_id)
