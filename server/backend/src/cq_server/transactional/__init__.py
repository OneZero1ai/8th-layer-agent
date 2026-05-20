"""Central transactional-mail service — Decision 34 (agent#348).

This package implements the control-plane endpoint family that every L2
calls instead of dispatching SES directly. The shape is documented in
``docs/decisions/34-central-transactional-mail-service.md`` (in
``8th-layer-core``).

Public surface:

* :mod:`transactional.routes` — the FastAPI router mounted at
  ``/api/v1/transactional/*``.
* :mod:`transactional.auth` — per-L2 HMAC signature verification.
* :mod:`transactional.suppression` — read + write helpers for the
  ``transactional_suppression`` table.
* :mod:`transactional.dispatcher` — the SES v1 send wrapper. Mockable
  via :class:`MockSesDispatcher` for tests.
* :mod:`transactional.idempotency` — in-memory dedup-within-60s store.
"""

from __future__ import annotations

from .auth import HmacKeyResolver, verify_hmac_signature
from .dispatcher import MockSesDispatcher, SesDispatcher
from .idempotency import IdempotencyStore
from .routes import router as transactional_router
from .sns_writer import handle_lambda_event, handle_sns_message
from .suppression import (
    SuppressionEntry,
    check_suppression,
    record_suppression,
)

__all__ = [
    "HmacKeyResolver",
    "IdempotencyStore",
    "MockSesDispatcher",
    "SesDispatcher",
    "SuppressionEntry",
    "check_suppression",
    "handle_lambda_event",
    "handle_sns_message",
    "record_suppression",
    "transactional_router",
    "verify_hmac_signature",
]
