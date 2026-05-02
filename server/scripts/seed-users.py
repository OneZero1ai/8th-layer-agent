"""Seed a user into the cq remote database."""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

import bcrypt


def main() -> None:
    """Seed a user into the cq remote database.

    SEC-CRIT #32 (PR #42) added an admin gate on /review/* — users
    seeded without ``--role admin`` will get 403 on the review surface.
    Pass ``--role admin`` for any user expected to triage the queue.

    Cross-Enterprise tenancy: the user row carries ``enterprise_id`` +
    ``group_id`` columns that scope every query, propose, and consult
    operation. Defaults read from ``CQ_ENTERPRISE`` / ``CQ_GROUP`` env
    vars (the same vars the cq-server process uses for its own
    identity). Override per-user via ``--enterprise`` / ``--group``.
    Without a real tenancy, cross-Enterprise consults break: the
    sender's body claims ``default-enterprise/default-group`` instead
    of the L2's actual identity, and the receiver rejects with a
    forwarder-mismatch 403.
    """
    parser = argparse.ArgumentParser(description="Seed a cq remote user.")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--role", default="user", choices=["user", "admin"])
    parser.add_argument(
        "--enterprise",
        default=os.environ.get("CQ_ENTERPRISE", "default-enterprise"),
        help="Enterprise id for this user (defaults to $CQ_ENTERPRISE).",
    )
    parser.add_argument(
        "--group",
        default=os.environ.get("CQ_GROUP", "default-group"),
        help="Group id for this user (defaults to $CQ_GROUP).",
    )
    parser.add_argument("--db", default="/data/cq.db")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    password_hash = bcrypt.hashpw(args.password.encode(), bcrypt.gensalt()).decode()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at, role, enterprise_id, group_id) "
            "VALUES (?, ?, datetime('now'), ?, ?, ?)",
            (args.username, password_hash, args.role, args.enterprise, args.group),
        )
        conn.commit()
        print(
            f"User '{args.username}' created with role='{args.role}', "
            f"enterprise='{args.enterprise}', group='{args.group}'."
        )
    except sqlite3.IntegrityError:
        conn.execute(
            "UPDATE users SET password_hash = ?, role = ?, enterprise_id = ?, group_id = ? WHERE username = ?",
            (password_hash, args.role, args.enterprise, args.group, args.username),
        )
        conn.commit()
        print(
            f"User '{args.username}' already exists — password + role='{args.role}' "
            f"+ enterprise='{args.enterprise}' + group='{args.group}' updated."
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
