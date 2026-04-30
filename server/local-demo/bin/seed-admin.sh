#!/usr/bin/env bash
# Seed an admin user inside a running L2 container — same shape as the
# Fargate post-deploy admin seed. Generates a fresh password, runs the
# inline-python bcrypt hash + UPSERT inside the container, prints the
# password to stdout (you'll paste it into the admin login).
#
# Usage:  bash bin/seed-admin.sh acme-engineering-l2
set -euo pipefail

CONTAINER="${1:?usage: $0 <container-name>}"

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    echo "[seed-admin] container '$CONTAINER' not running" >&2
    exit 2
fi

ADMIN_PW=$(openssl rand -base64 32 | tr -d '/+=' | head -c 32)

docker exec -e ADMIN_PW="$ADMIN_PW" "$CONTAINER" /app/.venv/bin/python -c "
import os, sqlite3, datetime
from cq_server.auth import hash_password
pw = os.environ['ADMIN_PW']
con = sqlite3.connect('/data/cq.db')
con.execute('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, created_at TEXT NOT NULL)')
now = datetime.datetime.now(datetime.UTC).isoformat()
try:
    con.execute('INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)', ('admin', hash_password(pw), now))
    con.commit()
    print('admin created')
except sqlite3.IntegrityError:
    con.execute('UPDATE users SET password_hash = ? WHERE username = ?', (hash_password(pw), 'admin'))
    con.commit()
    print('admin password reset')
con.close()
"

echo
echo "[seed-admin] container=$CONTAINER admin=admin password=$ADMIN_PW"
echo "[seed-admin] save this; it is not stored anywhere else"
