#!/usr/bin/env bash
# Verify the AIGRP peer mesh is converged.
# Usage:  bash bin/verify-mesh.sh
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
    echo "[verify] .env not found — run bin/init-env.sh first" >&2
    exit 2
fi

# shellcheck disable=SC1091
set -a; source .env; set +a

probe() {
    local label="$1" port="$2" peer_key="$3"
    echo
    echo "=== $label  (localhost:$port) ==="
    if ! curl -sS -m 3 -o /dev/null "http://localhost:$port/health" 2>/dev/null; then
        echo "[health] unreachable"
        return
    fi
    echo "[health] ok"
    local body
    body=$(curl -sS -m 5 "http://localhost:$port/api/v1/aigrp/peers" \
        -H "Authorization: Bearer $peer_key" 2>/dev/null || echo "")
    if [ -z "$body" ]; then
        echo "[/aigrp/peers] no response"
        return
    fi
    echo "$body" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception as e:
    print('[/aigrp/peers] parse error:', e); sys.exit(0)
print(f'[/aigrp/peers]  enterprise={d.get(\"enterprise\")}  self={d.get(\"self_l2_id\")}  peer_count={d.get(\"peer_count\")}')
for p in d.get('peers', []):
    last_sig = p.get('last_signature_at') or 'never-polled'
    endpoint = p.get('endpoint_url') or '<stub: no inbound>'
    print(f'  - {p[\"l2_id\"]}  endpoint={endpoint}  last_sig={last_sig}  ku_count={p.get(\"ku_count\",0)}')
"
}

probe "acme-engineering-l2" 4001 "${ACME_PEER_KEY:-}"
probe "acme-solutions-l2"   4002 "${ACME_PEER_KEY:-}"
probe "orion-local-l2"      4003 "${ORION_LOCAL_PEER_KEY:-}"
probe "orion-bridge-l2"     4004 "${ORION_AWS_PEER_KEY:-}"

echo
echo "=== boundary check: hit orion-local-l2's /aigrp/peers using ACME's key (expect 401) ==="
status=$(curl -sS -m 3 -o /dev/null -w "%{http_code}" "http://localhost:4003/api/v1/aigrp/peers" \
    -H "Authorization: Bearer ${ACME_PEER_KEY:-deliberately-wrong}" 2>/dev/null || echo "ERR")
echo "[boundary] orion-local-l2 returned HTTP $status using acme's peer key (401 = boundary working)"
