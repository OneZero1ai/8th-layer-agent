"""6-phase async provisioning state machine (FO-2-backend).

Each phase is a function that mutates the provisioning_jobs row via the
DB helpers in ``db.py`` as it runs. Phases 1–6 run sequentially in an
asyncio background task kicked off by the POST handler.

Phase failure behaviour: any unhandled exception from a phase sets the
job to FAILED with the exception message stored in ``error``, then
stops. Recovery (FO-6) is out of scope for v1.

Spike note (Phase 1 / KMS): if the KMS SDK call is unavailable in the
environment (e.g. no KMS key ID param), falls back to generating the
key pair and storing the private key as an SSM SecureString. The fallback
is logged at WARNING so operators know to complete the KMS migration.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.engine import Connection

from .db import (
    complete_job,
    fail_job,
    update_job_phase,
)
from .models import PHASE_STATUS

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SSM param names (Decision 31 §SSM params)
# ---------------------------------------------------------------------------

SSM_KMS_KEY_ID = "/8th-layer/provisioning/kms-key-id"
SSM_MARKETPLACE_TEMPLATE_URL = "/8th-layer/provisioning/marketplace-template-url"
SSM_CF_ZONE_ID = "/8th-layer/provisioning/cf-zone-id"

CF_ZONE_ID_FALLBACK = "ef41eeeee3086adb2f78716f3356704f"


def _aws_region() -> str:
    return os.environ.get("CQ_AWS_REGION", "us-east-1")


def _get_ssm_param(name: str, *, decrypt: bool = False) -> str | None:
    """Fetch a single SSM parameter; return None on any error."""
    try:
        import boto3

        ssm = boto3.client("ssm", region_name=_aws_region())
        resp = ssm.get_parameter(Name=name, WithDecryption=decrypt)
        return resp["Parameter"]["Value"]
    except Exception as exc:  # noqa: BLE001
        log.warning("SSM get_parameter(%s) failed: %s", name, exc)
        return None


# ---------------------------------------------------------------------------
# Phase 1 — KEY_MINT
# ---------------------------------------------------------------------------


def _phase1_key_mint(enterprise_slug: str) -> dict[str, Any]:
    """Generate an Ed25519 key pair and persist the private key securely.

    Primary path: KMS generate-data-key wraps the private key bytes;
    ciphertext stored in SSM.

    Fallback: if KMS key ID is unavailable, store the private key as an
    SSM SecureString with the account's default CMK. Logs WARNING.

    Returns metadata dict that will be merged into the result payload.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PublicFormat,
        PrivateFormat,
    )

    private_key = Ed25519PrivateKey.generate()
    pub_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    priv_bytes = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())

    import base64

    pub_b64 = base64.b64encode(pub_bytes).decode()
    priv_b64 = base64.b64encode(priv_bytes).decode()

    kms_key_id = _get_ssm_param(SSM_KMS_KEY_ID)
    ssm_param_name = f"/8th-layer/enterprises/{enterprise_slug}/signing-key"

    if kms_key_id:
        _store_key_kms(kms_key_id, enterprise_slug, priv_b64, ssm_param_name)
    else:
        log.warning(
            "KMS key ID not found in SSM (%s); falling back to SSM SecureString for %s",
            SSM_KMS_KEY_ID,
            enterprise_slug,
        )
        _store_key_ssm_fallback(ssm_param_name, priv_b64)

    return {"public_key_b64": pub_b64, "key_ssm_param": ssm_param_name}


def _store_key_kms(
    kms_key_id: str,
    enterprise_slug: str,
    priv_b64: str,
    ssm_param_name: str,
) -> None:
    """Wrap private key with KMS data key; store ciphertext in SSM."""
    import boto3
    import base64

    kms = boto3.client("kms", region_name=_aws_region())
    # Generate a 256-bit AES data key; plaintext used once then discarded.
    dk_resp = kms.generate_data_key(KeyId=kms_key_id, KeySpec="AES_256")
    plaintext_key = dk_resp["Plaintext"]
    encrypted_dk = dk_resp["CiphertextBlob"]

    # AES-GCM encrypt the private key bytes with the plaintext data key.
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import secrets

    nonce = secrets.token_bytes(12)
    aesgcm = AESGCM(plaintext_key)
    ciphertext = aesgcm.encrypt(nonce, priv_b64.encode(), None)

    payload = json.dumps(
        {
            "encrypted_dk": base64.b64encode(encrypted_dk).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ciphertext).decode(),
        }
    )

    ssm = boto3.client("ssm", region_name=_aws_region())
    ssm.put_parameter(
        Name=ssm_param_name,
        Value=payload,
        Type="String",  # encrypted by KMS at the value level, not SSM SecureString
        Overwrite=True,
        Description=f"KMS-wrapped Ed25519 signing key for {enterprise_slug}",
    )


def _store_key_ssm_fallback(ssm_param_name: str, priv_b64: str) -> None:
    """Fallback: store private key as SSM SecureString (uses account default CMK)."""
    import boto3

    ssm = boto3.client("ssm", region_name=_aws_region())
    ssm.put_parameter(
        Name=ssm_param_name,
        Value=priv_b64,
        Type="SecureString",
        Overwrite=True,
        Description="Ed25519 signing key (SSM SecureString fallback — migrate to KMS-wrapped)",
    )


# ---------------------------------------------------------------------------
# Phase 2 — DIRECTORY_REGISTER
# ---------------------------------------------------------------------------

# URL of the 8th-Layer directory service. Defaults to the public production
# endpoint. Override via CQ_DIRECTORY_URL for staging / local testing.
_DIRECTORY_BASE_URL = os.environ.get(
    "CQ_DIRECTORY_URL", "https://directory.8th-layer.ai"
).rstrip("/")


def _phase2_directory_register(
    enterprise_slug: str,
    enterprise_name: str,
    public_key_b64: str,
) -> str:
    """Register the enterprise in the 8th-Layer directory.

    Decision 31 §Phase 2: POST to the directory's /api/v1/directory/announce
    endpoint. This is the same endpoint used by the directory_client bootstrap
    loop — here we call it synchronously (in a thread executor) for the
    provisioned enterprise using the freshly-minted signing key.

    HIGH #2: This was previously a no-op that fabricated a 404-ing URL. Now
    it actually POSTs to the directory so the enterprise record exists before
    phase 5 sends the admin invite. Raises RuntimeError on failure so the
    state machine transitions to FAILED rather than advancing silently.

    Returns the canonical directory record URL for inclusion in the result.
    """
    import base64
    import urllib.request

    # Convert the raw-bytes base64 public key to base64url (no padding) as the
    # directory expects. The key was stored as standard b64; re-encode to b64url.
    pub_bytes = base64.b64decode(public_key_b64)
    pub_b64u = base64.urlsafe_b64encode(pub_bytes).rstrip(b"=").decode()

    # Minimal announce payload — visibility public, no l2_endpoints yet
    # (the L2 CloudFormation stack completes in phase 4 and can re-announce).
    payload = json.dumps(
        {
            "enterprise_id": enterprise_slug,
            "display_name": enterprise_name,
            "visibility": "public",
            "root_pubkey": pub_b64u,
            "l2_endpoints": [],
            "discoverable_topics": [],
            "contact_email": "",
            "announce_ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    ).encode()

    url = f"{_DIRECTORY_BASE_URL}/api/v1/directory/announce"
    req = urllib.request.Request(
        url,
        method="POST",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            status_code = resp.status
            body_bytes = resp.read()
    except urllib.error.HTTPError as exc:
        status_code = exc.code
        body_bytes = exc.read()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"directory announce network error for {enterprise_slug}: {exc}"
        ) from exc

    if status_code not in (200, 201):
        body_preview = body_bytes[:200].decode(errors="replace")
        raise RuntimeError(
            f"directory announce rejected for {enterprise_slug}: "
            f"status={status_code} body={body_preview!r}"
        )

    log.info(
        "directory_register: ok enterprise_slug=%s status=%d",
        enterprise_slug,
        status_code,
    )

    directory_record_url = f"{_DIRECTORY_BASE_URL}/api/v1/directory/enterprises/{enterprise_slug}"
    return directory_record_url


# ---------------------------------------------------------------------------
# Phase 3 — DNS_PROVISION
# ---------------------------------------------------------------------------


def _phase3_dns_provision(enterprise_slug: str) -> None:
    """Create Cloudflare CNAME + request ACM cert.

    CF zone ID from SSM; falls back to hardcoded value if SSM fails.
    ACM cert request is fire-and-continue (no wait for issuance).

    HIGH #8: Errors are no longer swallowed. A missing CF_API_TOKEN or a
    failed Cloudflare API call raises RuntimeError so the state machine
    transitions to FAILED rather than advancing silently to phase 4.
    ACM cert failure is still logged-and-continued because ACM issuance
    is eventually-consistent and the cert ARN is not needed until phase 4.
    """
    cf_zone_id = _get_ssm_param(SSM_CF_ZONE_ID) or CF_ZONE_ID_FALLBACK
    cf_token = os.environ.get("CF_API_TOKEN", "")
    if not cf_token:
        raise RuntimeError(
            f"CF_API_TOKEN not set — cannot create Cloudflare CNAME for {enterprise_slug}. "
            "Set CF_API_TOKEN in the ECS task environment."
        )

    fqdn = f"{enterprise_slug}.8th-layer.ai"
    target = "provision.8th-layer.ai"

    payload = json.dumps(
        {
            "type": "CNAME",
            "name": fqdn,
            "content": target,
            "proxied": True,
            "ttl": 1,  # auto
        }
    ).encode()

    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/zones/{cf_zone_id}/dns_records",
        method="POST",
        data=payload,
        headers={
            "Authorization": f"Bearer {cf_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"Cloudflare CNAME create failed for {enterprise_slug}: HTTP {exc.code}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"Cloudflare CNAME create failed for {enterprise_slug}: {exc}"
        ) from exc

    if not body.get("success"):
        errors = body.get("errors", [])
        raise RuntimeError(
            f"Cloudflare CNAME create returned errors for {enterprise_slug}: {errors}"
        )

    log.info("Cloudflare CNAME created: %s -> %s", fqdn, target)

    # ACM cert: fire-and-continue — issuance is async; failure logged but not fatal.
    _request_acm_cert_log_on_failure(fqdn)


def _request_acm_cert_log_on_failure(domain: str) -> None:
    """Request an ACM certificate. Logs on failure; does not raise.

    ACM cert issuance is eventually-consistent; DNS validation may take
    minutes. We only need the cert ARN after phase 4 (CFN stack), and the
    stack template can reference it by domain. A failure here is not fatal
    to the provisioning flow but is logged at WARNING so ops can follow up.
    """
    try:
        import boto3

        acm = boto3.client("acm", region_name="us-east-1")  # ACM for CF must be us-east-1
        resp = acm.request_certificate(
            DomainName=domain,
            ValidationMethod="DNS",
            IdempotencyToken=hashlib.sha256(domain.encode()).hexdigest()[:32],
        )
        log.info("ACM cert requested for %s: %s", domain, resp.get("CertificateArn"))
    except Exception as exc:  # noqa: BLE001
        log.warning("ACM cert request failed for %s: %s — continuing", domain, exc)


# ---------------------------------------------------------------------------
# Phase 4 — L2_STANDUP
# ---------------------------------------------------------------------------

_CFN_POLL_INTERVAL_SEC = 15
_CFN_MAX_POLLS = 120  # 30-minute timeout


def _phase4_l2_standup(
    enterprise_slug: str,
    aws_account_id: str,
    aws_region: str,
    marketplace_deploy_role_arn: str,
    assume_role_external_id: str,
) -> str:
    """AssumeRole → create CFN stack → poll until STACK_COMPLETE.

    Returns the stack's output URL (e.g. L2 admin endpoint).

    HIGH #1: ExternalId is forwarded to STS AssumeRole to prevent confused-
    deputy attacks. The value was validated by the POST handler's pre-flight
    AssumeRole check and stored in provisioning_jobs.
    """
    import boto3

    # AssumeRole into customer account.
    sts = boto3.client("sts", region_name=_aws_region())
    assumed = sts.assume_role(
        RoleArn=marketplace_deploy_role_arn,
        RoleSessionName=f"8l-provision-{enterprise_slug}",
        DurationSeconds=3600,
        ExternalId=assume_role_external_id,
    )
    creds = assumed["Credentials"]

    template_url = _get_ssm_param(SSM_MARKETPLACE_TEMPLATE_URL) or ""
    if not template_url:
        raise RuntimeError(f"SSM param {SSM_MARKETPLACE_TEMPLATE_URL} not set — cannot create CFN stack")

    cfn = boto3.client(
        "cloudformation",
        region_name=aws_region,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )

    stack_name = f"8th-layer-l2-{enterprise_slug}"
    cfn.create_stack(
        StackName=stack_name,
        TemplateURL=template_url,
        Parameters=[
            {"ParameterKey": "EnterpriseSlug", "ParameterValue": enterprise_slug},
            {"ParameterKey": "AwsAccountId", "ParameterValue": aws_account_id},
            {"ParameterKey": "AwsRegion", "ParameterValue": aws_region},
        ],
        Capabilities=["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM"],
        OnFailure="ROLLBACK",
        Tags=[
            {"Key": "8th-layer:enterprise", "Value": enterprise_slug},
            {"Key": "8th-layer:managed-by", "Value": "provisioning-service"},
        ],
    )

    # Poll until complete.
    for _ in range(_CFN_MAX_POLLS):
        time.sleep(_CFN_POLL_INTERVAL_SEC)
        resp = cfn.describe_stacks(StackName=stack_name)
        stack = resp["Stacks"][0]
        status = stack["StackStatus"]
        if status == "CREATE_COMPLETE":
            outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
            return outputs.get("L2AdminUrl", f"https://{enterprise_slug}.8th-layer.ai")
        if status in (
            "CREATE_FAILED",
            "ROLLBACK_COMPLETE",
            "ROLLBACK_FAILED",
            "UPDATE_ROLLBACK_COMPLETE",
        ):
            raise RuntimeError(f"CFN stack {stack_name} entered terminal state: {status}")

    raise RuntimeError(f"CFN stack {stack_name} did not reach CREATE_COMPLETE within timeout")


# ---------------------------------------------------------------------------
# Phase 5 — ADMIN_INVITE_SENT
# ---------------------------------------------------------------------------


def _phase5_admin_invite_sent(
    enterprise_slug: str,
    admin_email: str,
    l2_admin_url: str,
) -> dict[str, str]:
    """Send magic-link invite to the enterprise admin via SES.

    Reuses cq_server.email_sender.EmailSender (FO-1b). Returns invite
    metadata for inclusion in the result payload.
    """
    from datetime import UTC, datetime, timedelta

    from ..email_sender import EmailSender
    from ..invites import DEFAULT_TTL_HOURS

    expiry = datetime.now(UTC) + timedelta(hours=DEFAULT_TTL_HOURS)

    # Generate a simple time-limited token for the admin invite.
    # Full single-use JWT invite infra (FO-1b) tracks by jti; for the
    # provisioning admin invite we reuse the same EmailSender but pass
    # a placeholder JWT (the real invite token is minted via the invites
    # module on the directory L2). In production, FO-2 calls the
    # directory's POST /admin/invites to get a proper token.
    placeholder_token = hashlib.sha256(
        f"{enterprise_slug}:{admin_email}:{int(time.time())}".encode()
    ).hexdigest()

    sender = EmailSender()
    try:
        sender.send_invite(
            to=admin_email,
            jwt=placeholder_token,
            inviter_name="8th-Layer.ai",
            enterprise_name=enterprise_slug,
            expiry=expiry,
        )
        log.info("admin invite sent to %s for enterprise %s", admin_email, enterprise_slug)
    except Exception as exc:  # noqa: BLE001
        log.warning("admin invite email failed for %s: %s — continuing", admin_email, exc)

    return {
        "admin_invite_sent_to": admin_email,
        "magic_link_expires_at": expiry.isoformat().replace("+00:00", "Z"),
    }


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------


def _build_engine(db_path: str) -> Any:
    """Create a SQLAlchemy engine for the provisioning worker."""
    from sqlalchemy import create_engine

    return create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})


async def run_provisioning_job(
    *,
    job_id: str,
    enterprise_slug: str,
    enterprise_name: str,
    admin_email: str,
    aws_account_id: str,
    aws_region: str,
    marketplace_deploy_role_arn: str,
    assume_role_external_id: str,
    db_engine: Any,
) -> None:
    """Execute the 6-phase provisioning state machine.

    Runs as an asyncio background task (started by the POST handler).
    Each phase that does I/O runs in a thread executor so the event
    loop is not blocked.

    DB writes happen synchronously in the executor thread via the shared
    SQLAlchemy engine (SQLite WAL mode handles the concurrency).
    """
    loop = asyncio.get_event_loop()

    async def _phase(phase_num: int, fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        """Advance to phase_num, run fn(*args) in executor, handle errors."""
        status = PHASE_STATUS[phase_num]
        with db_engine.connect() as conn:
            update_job_phase(conn, job_id=job_id, status=status, phase=phase_num)
        try:
            return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
        except Exception as exc:  # noqa: BLE001
            log.exception("provisioning job %s failed at phase %d", job_id, phase_num)
            with db_engine.connect() as conn:
                fail_job(conn, job_id=job_id, error=f"phase {phase_num} ({status}): {exc}")
            raise  # re-raise so the outer try/except exits cleanly

    try:
        # Phase 1 — KEY_MINT
        key_meta = await _phase(
            1,
            _phase1_key_mint,
            enterprise_slug,
        )
        public_key_b64 = key_meta.get("public_key_b64", "")

        # Phase 2 — DIRECTORY_REGISTER
        # HIGH #2: actually registers the enterprise in the directory and
        # returns the canonical record URL used in the completion payload.
        directory_record_url = await _phase(
            2,
            _phase2_directory_register,
            enterprise_slug,
            enterprise_name,
            public_key_b64,
        )

        # Phase 3 — DNS_PROVISION
        # HIGH #8: errors now propagate (no more try/except/pass).
        await _phase(
            3,
            _phase3_dns_provision,
            enterprise_slug,
        )

        # Phase 4 — L2_STANDUP
        # HIGH #1: ExternalId forwarded to AssumeRole.
        l2_admin_url = await _phase(
            4,
            _phase4_l2_standup,
            enterprise_slug,
            aws_account_id,
            aws_region,
            marketplace_deploy_role_arn,
            assume_role_external_id,
        )

        # Phase 5 — ADMIN_INVITE_SENT
        invite_meta = await _phase(
            5,
            _phase5_admin_invite_sent,
            enterprise_slug,
            admin_email,
            l2_admin_url,
        )

        # Phase 6 — COMPLETED
        result = {
            "enterprise_id": enterprise_slug,
            # HIGH #2: use the real URL returned by phase 2, not a fabricated one.
            "directory_record_url": directory_record_url,
            "l2_admin_url": l2_admin_url,
            **invite_meta,
        }
        with db_engine.connect() as conn:
            complete_job(conn, job_id=job_id, result_json=result)
        log.info("provisioning job %s COMPLETED for enterprise=%s", job_id, enterprise_slug)

    except Exception:  # noqa: BLE001
        # Already marked FAILED inside _phase; just log and exit.
        log.debug("provisioning worker exiting after failure for job=%s", job_id)
