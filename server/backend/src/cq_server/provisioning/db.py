"""DB helpers for provisioning_jobs table.

All operations are synchronous (SQLAlchemy Core) running inside asyncio
via run_in_executor where needed. This mirrors the existing codebase
pattern (SqliteStore uses sync helpers called from async endpoints).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def insert_job(
    conn: Connection,
    *,
    job_id: str,
    enterprise_id: str,
    status: str,
    phase: int,
    ip_hash: str,
) -> None:
    """Insert a new provisioning_jobs row."""
    conn.execute(
        text(
            """
            INSERT INTO provisioning_jobs
                (job_id, enterprise_id, status, phase, started_at, ip_hash)
            VALUES
                (:job_id, :enterprise_id, :status, :phase, :started_at, :ip_hash)
            """
        ),
        {
            "job_id": job_id,
            "enterprise_id": enterprise_id,
            "status": status,
            "phase": phase,
            "started_at": _now_iso(),
            "ip_hash": ip_hash,
        },
    )
    conn.commit()


def update_job_phase(
    conn: Connection,
    *,
    job_id: str,
    status: str,
    phase: int,
) -> None:
    """Advance the job to a new phase/status."""
    conn.execute(
        text(
            """
            UPDATE provisioning_jobs
               SET status = :status, phase = :phase
             WHERE job_id = :job_id
            """
        ),
        {"job_id": job_id, "status": status, "phase": phase},
    )
    conn.commit()


def complete_job(
    conn: Connection,
    *,
    job_id: str,
    result_json: dict[str, Any],
) -> None:
    """Mark the job COMPLETED and persist the result payload."""
    conn.execute(
        text(
            """
            UPDATE provisioning_jobs
               SET status = 'COMPLETED',
                   phase = 6,
                   completed_at = :completed_at,
                   result_json = :result_json
             WHERE job_id = :job_id
            """
        ),
        {
            "job_id": job_id,
            "completed_at": _now_iso(),
            "result_json": json.dumps(result_json),
        },
    )
    conn.commit()


def fail_job(
    conn: Connection,
    *,
    job_id: str,
    error: str,
) -> None:
    """Mark the job FAILED."""
    conn.execute(
        text(
            """
            UPDATE provisioning_jobs
               SET status = 'FAILED',
                   completed_at = :completed_at,
                   error = :error
             WHERE job_id = :job_id
            """
        ),
        {"job_id": job_id, "completed_at": _now_iso(), "error": error},
    )
    conn.commit()


def get_job(conn: Connection, job_id: str) -> dict[str, Any] | None:
    """Return a row dict or None."""
    row = conn.execute(
        text(
            """
            SELECT job_id, enterprise_id, status, phase,
                   started_at, completed_at, error, result_json
              FROM provisioning_jobs
             WHERE job_id = :job_id
            """
        ),
        {"job_id": job_id},
    ).fetchone()
    if row is None:
        return None
    return dict(row._mapping)


def is_slug_taken(conn: Connection, slug: str) -> bool:
    """True if any job row already claims this enterprise_id/slug."""
    row = conn.execute(
        text("SELECT 1 FROM provisioning_jobs WHERE enterprise_id = :slug LIMIT 1"),
        {"slug": slug},
    ).fetchone()
    return row is not None


def count_recent_requests(conn: Connection, ip_hash: str, window_seconds: int = 3600) -> int:
    """Count provisioning_jobs rows for this IP hash in the last window_seconds.

    Rate-limit gate for POST /api/v1/enterprises: 10 req/hr per IP.
    Uses started_at TEXT ISO-8601 comparison which SQLite handles correctly
    for UTC Z-suffix strings.
    """
    row = conn.execute(
        text(
            """
            SELECT COUNT(*) AS cnt
              FROM provisioning_jobs
             WHERE ip_hash = :ip_hash
               AND started_at >= datetime('now', :offset)
            """
        ),
        {"ip_hash": ip_hash, "offset": f"-{window_seconds} seconds"},
    ).fetchone()
    return int(row[0]) if row else 0


def is_job_expired(row: dict[str, Any]) -> bool:
    """True if a COMPLETED job's 24-hour expiry has passed (Decision 31 §Auth)."""
    if row.get("status") != "COMPLETED":
        return False
    completed_at = row.get("completed_at")
    if not completed_at:
        return False
    try:
        completed_dt = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        age_seconds = (datetime.now(UTC) - completed_dt).total_seconds()
        return age_seconds > 86400  # 24 hours
    except (ValueError, AttributeError):
        return False
