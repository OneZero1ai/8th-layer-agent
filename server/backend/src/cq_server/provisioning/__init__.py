"""Enterprise Provisioning Service — FO-2-backend.

6-phase async provisioning job state machine:

  1. KEY_MINT            — generate Ed25519 key pair; store private key in KMS
  2. DIRECTORY_REGISTER  — register the enterprise in cq-directory
  3. DNS_PROVISION       — Cloudflare CNAME + ACM cert request (fire-and-continue)
  4. L2_STANDUP          — AssumeRole into customer account, create CFN stack,
                           poll until STACK_COMPLETE
  5. ADMIN_INVITE_SENT   — magic-link invite via SES
  6. COMPLETED           — persist result JSON, set completed_at

Endpoint contract: Decision 31 — FO-2 ↔ FO-2-backend handshake contract.
"""

from .recovery import recover_stuck_jobs
from .routes import router

__all__ = ["recover_stuck_jobs", "router"]
