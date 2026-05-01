"""Seed a user into the cq remote database."""

import argparse
import sqlite3
import sys
from pathlib import Path

import bcrypt


def main() -> None:
    """Seed a user into the cq remote database.

    SEC-CRIT #32 (PR #42) added an admin gate on /review/* — users
    seeded without ``--role admin`` will get 403 on the review surface.
    Pass ``--role admin`` for any user expected to triage the queue.
    """
    parser = argparse.ArgumentParser(description="Seed a cq remote user.")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--role", default="user", choices=["user", "admin"])
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
            "INSERT INTO users (username, password_hash, created_at, role) "
            "VALUES (?, ?, datetime('now'), ?)",
            (args.username, password_hash, args.role),
        )
        conn.commit()
        print(f"User '{args.username}' created with role='{args.role}'.")
    except sqlite3.IntegrityError:
        conn.execute(
            "UPDATE users SET password_hash = ?, role = ? WHERE username = ?",
            (password_hash, args.role, args.username),
        )
        conn.commit()
        print(f"User '{args.username}' already exists — password + role='{args.role}' updated.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
