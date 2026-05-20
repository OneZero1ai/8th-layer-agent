#!/usr/bin/env python3
"""Backfill ``tx_send_key`` SSM parameters for L2s deployed pre-Decision-34.

Decision 34 introduced a per-L2 HMAC key (``TX_SEND_KEY``) sourced from
SSM at ``/8th-layer/l2/{enterprise}/{group}/tx_send_key`` in the L2's
own AWS account. New L2s get this minted by the provisioning service.
L2s that pre-date Decision 34 need a one-time backfill — that's what
this script does.

# Usage

Mint a key for one L2:

    ./bin/backfill-tx-keys.py \\
        --aws-profile <l2-profile> \\
        --enterprise <ent> \\
        --group <grp>

Bulk-mint for a list of L2s (one ``ent/grp`` per line in a file):

    ./bin/backfill-tx-keys.py \\
        --aws-profile <l2-profile> \\
        --from-file l2s.txt

Dry-run (default if neither --execute nor --from-file is passed):

    ./bin/backfill-tx-keys.py --aws-profile foo --enterprise acme --group eng --dry-run

# Twin write

The control-plane resolver (``SsmKeyResolver``) reads the key from SSM
in the control-plane account (``124074140789``) — the central service
needs to know what each L2 is signing with. So this script writes to
BOTH:

* The L2's own AWS account (so the ECS task can read it at startup).
* The control plane account (so the central service can verify the
  HMAC).

Pass ``--control-plane-profile`` to enable the control-plane write
(omit to skip — useful in dev / when running by hand against just one
account).

# Idempotency

The same key value is written to both accounts. Re-running the script
on an existing L2 with ``--rotate`` mints a fresh key and overwrites
both sides atomically (the central side first, then the L2 side; if
the L2 side fails the central side gets rolled back to the previous
value via a second write).

Without ``--rotate``, the script refuses to overwrite an existing key
(by default — guard against accidental rotation). Use ``--force`` to
override.
"""

from __future__ import annotations

import argparse
import logging
import secrets
import sys
from pathlib import Path
from typing import Iterable

# Boto3 is imported lazily so --help on a machine without boto3 still works.

log = logging.getLogger("backfill-tx-keys")

CONTROL_PLANE_ACCOUNT = "124074140789"


def _mint_key() -> str:
    """Generate a fresh HMAC key.

    64 hex chars (256 bits) of cryptographic randomness. The control-
    plane signature scheme uses HMAC-SHA256, so 256 bits is the
    natural key size. Hex (not base64) for grep-friendly diagnostics.
    """
    return secrets.token_hex(32)


def _ssm_param_name(enterprise: str, group: str) -> str:
    return f"/8th-layer/l2/{enterprise}/{group}/tx_send_key"


def _write_param(
    session: "boto3.session.Session",  # noqa: F821 — forward ref
    enterprise: str,
    group: str,
    value: str,
    *,
    overwrite: bool,
    dry_run: bool,
    label: str,
) -> None:
    name = _ssm_param_name(enterprise, group)
    if dry_run:
        log.info("DRY-RUN [%s]: would put_parameter Name=%s Overwrite=%s", label, name, overwrite)
        return
    client = session.client("ssm")
    client.put_parameter(
        Name=name,
        Description=f"Decision 34 tx_send_key for {enterprise}/{group}",
        Value=value,
        Type="SecureString",
        Overwrite=overwrite,
        Tier="Standard",
    )
    log.info("[%s] put_parameter %s OK (len=%d)", label, name, len(value))


def _read_param(
    session: "boto3.session.Session",  # noqa: F821
    enterprise: str,
    group: str,
) -> str | None:
    name = _ssm_param_name(enterprise, group)
    client = session.client("ssm")
    try:
        resp = client.get_parameter(Name=name, WithDecryption=True)
    except client.exceptions.ParameterNotFound:
        return None
    return resp["Parameter"]["Value"]


def backfill_one(
    *,
    l2_session: "boto3.session.Session",  # noqa: F821
    cp_session: "boto3.session.Session | None",  # noqa: F821
    enterprise: str,
    group: str,
    dry_run: bool,
    rotate: bool,
    force: bool,
) -> bool:
    """Backfill (or rotate) one L2's tx_send_key. Returns True on success."""
    existing = _read_param(l2_session, enterprise, group) if not dry_run else None
    if existing and not (rotate or force):
        log.warning(
            "%s/%s already has a tx_send_key — pass --rotate to mint a new one "
            "or --force to overwrite without rotating",
            enterprise,
            group,
        )
        return False

    new_key = _mint_key()
    overwrite = bool(existing) or rotate or force

    # Control-plane write first (so the central service knows the new
    # key before the L2 starts signing with it).
    if cp_session is not None:
        _write_param(
            cp_session,
            enterprise,
            group,
            new_key,
            overwrite=overwrite,
            dry_run=dry_run,
            label="control-plane",
        )
    else:
        log.info(
            "skipping control-plane write (no --control-plane-profile); "
            "the central service must be updated separately"
        )

    # Then the L2 side. If THIS fails we have a brief window where the
    # control plane has the new key but the L2 doesn't — the L2 keeps
    # signing with whatever's in its env (no SSM re-read on the hot
    # path; the env is set once at task start). That's safe.
    try:
        _write_param(
            l2_session,
            enterprise,
            group,
            new_key,
            overwrite=overwrite,
            dry_run=dry_run,
            label="l2",
        )
    except Exception as exc:
        log.error("L2-side write failed for %s/%s: %s", enterprise, group, exc)
        if cp_session is not None and existing:
            # Roll the control-plane side back so the two stay in sync.
            log.warning("rolling control-plane back to previous value")
            try:
                _write_param(
                    cp_session,
                    enterprise,
                    group,
                    existing,
                    overwrite=True,
                    dry_run=dry_run,
                    label="control-plane-rollback",
                )
            except Exception:
                log.exception("rollback ALSO failed — manual intervention required")
        return False

    log.info("backfill OK: %s/%s", enterprise, group)
    return True


def _read_l2_list(path: Path) -> Iterable[tuple[str, str]]:
    """File format: one ``enterprise/group`` per line. ``#`` comments allowed."""
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "/" not in line:
            log.warning("skipping malformed line: %r (expected enterprise/group)", line)
            continue
        ent, grp = line.split("/", 1)
        yield ent.strip(), grp.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--aws-profile", required=True, help="boto3 profile for the L2's own AWS account")
    parser.add_argument("--control-plane-profile", help="boto3 profile for the control-plane account (124074140789); enables twin-write")
    parser.add_argument("--enterprise", help="single L2's enterprise slug")
    parser.add_argument("--group", help="single L2's group slug")
    parser.add_argument("--from-file", type=Path, help="bulk: file with enterprise/group lines")
    parser.add_argument("--dry-run", action="store_true", help="read-only; print what would happen")
    parser.add_argument("--execute", action="store_true", help="actually write; required for non-dry-run")
    parser.add_argument("--rotate", action="store_true", help="mint a fresh key even if one exists")
    parser.add_argument("--force", action="store_true", help="overwrite without rotating (recovery path)")
    parser.add_argument("--region", default="us-east-1", help="AWS region (default us-east-1)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Default to dry-run if neither --execute nor --dry-run is set.
    # Explicit is better than the inverse default.
    if not args.execute and not args.dry_run:
        log.info("neither --execute nor --dry-run set; defaulting to --dry-run")
        args.dry_run = True

    # Resolve target list.
    if args.from_file:
        targets = list(_read_l2_list(args.from_file))
    elif args.enterprise and args.group:
        targets = [(args.enterprise, args.group)]
    else:
        parser.error("pass either --from-file OR both --enterprise and --group")

    import boto3

    l2_session = boto3.session.Session(
        profile_name=args.aws_profile, region_name=args.region
    )
    cp_session = (
        boto3.session.Session(
            profile_name=args.control_plane_profile, region_name=args.region
        )
        if args.control_plane_profile
        else None
    )

    if cp_session is not None and not args.dry_run:
        # Sanity check: the control-plane account is 124074140789.
        sts = cp_session.client("sts")
        ident = sts.get_caller_identity()
        if ident["Account"] != CONTROL_PLANE_ACCOUNT:
            log.error(
                "control-plane profile resolves to account %s, expected %s — refusing",
                ident["Account"],
                CONTROL_PLANE_ACCOUNT,
            )
            return 2

    ok = 0
    failed = 0
    for ent, grp in targets:
        if backfill_one(
            l2_session=l2_session,
            cp_session=cp_session,
            enterprise=ent,
            group=grp,
            dry_run=args.dry_run,
            rotate=args.rotate,
            force=args.force,
        ):
            ok += 1
        else:
            failed += 1

    log.info("done: %d ok, %d failed", ok, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
