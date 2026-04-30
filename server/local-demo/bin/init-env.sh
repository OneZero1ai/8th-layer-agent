#!/usr/bin/env bash
# Generate fresh secrets into .env for the local demo.
# Idempotent: re-run to rotate everything; pulls AWS-side values from SSM.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -f .env ]; then
    echo "[init-env] .env already exists; backing up to .env.bak before regenerating"
    cp .env .env.bak
fi

gen_secret() { openssl rand -base64 48 | tr -d '/+=' | head -c 48; }

ACME_PEER_KEY=$(gen_secret)
ACME_JWT_SECRET=$(gen_secret)
ACME_API_KEY_PEPPER=$(gen_secret)

ORION_LOCAL_PEER_KEY=$(gen_secret)
ORION_LOCAL_JWT_SECRET=$(gen_secret)
ORION_LOCAL_API_KEY_PEPPER=$(gen_secret)

ORION_BRIDGE_JWT_SECRET=$(gen_secret)
ORION_BRIDGE_API_KEY_PEPPER=$(gen_secret)

# Pull the AWS-side orion peer key + seed URL from SSM + describe-stacks.
# These resources live in the 8th-layer-app account specifically — NOT in
# whatever $AWS_PROFILE the operator's shell is currently set to. Allow
# override via AWS_PROFILE_FOR_LOOKUP, otherwise hard-default to the right
# profile so the lookup works regardless of shell context.
AWS_PROFILE_FOR_LOOKUP="${AWS_PROFILE_FOR_LOOKUP:-8th-layer-app}"
ORION_AWS_PEER_KEY=$(aws --profile "$AWS_PROFILE_FOR_LOOKUP" --region us-east-1 \
    ssm get-parameter --name /8l-aigrp/orion/peer-key --with-decryption \
    --query Parameter.Value --output text 2>/dev/null || echo "")
ORION_AWS_SEED_URL=$(aws --profile "$AWS_PROFILE_FOR_LOOKUP" --region us-east-1 \
    cloudformation describe-stacks --stack-name test-orion-eng-l2 \
    --query "Stacks[0].Outputs[?OutputKey=='CqEndpoint'].OutputValue|[0]" \
    --output text 2>/dev/null || echo "")
# AWS_PROFILE for the *runtime* containers (Bedrock embed). Defaults to the
# same account that hosts the test stack so cost attribution + audit trail
# stay consistent. Operator can override via AWS_PROFILE_FOR_RUNTIME if they
# want to point Bedrock calls at a different account. Note: we deliberately
# do NOT track the operator's shell $AWS_PROFILE here — that's almost always
# wrong (e.g. shell set to `orion` from another project leaks Bedrock cost
# into the wrong account).
AWS_PROFILE_USE="${AWS_PROFILE_FOR_RUNTIME:-$AWS_PROFILE_FOR_LOOKUP}"

cat > .env <<EOF
# Generated $(date -u +%Y-%m-%dT%H:%M:%SZ) by bin/init-env.sh

# Shape A: acme Enterprise (local two-peer mesh)
ACME_PEER_KEY=$ACME_PEER_KEY
ACME_JWT_SECRET=$ACME_JWT_SECRET
ACME_API_KEY_PEPPER=$ACME_API_KEY_PEPPER

# Shape A: orion-local (separate Enterprise, separate network — for boundary proof)
ORION_LOCAL_PEER_KEY=$ORION_LOCAL_PEER_KEY
ORION_LOCAL_JWT_SECRET=$ORION_LOCAL_JWT_SECRET
ORION_LOCAL_API_KEY_PEPPER=$ORION_LOCAL_API_KEY_PEPPER

# Shape B/C: orion-bridge — local L2 joining AWS-hosted orion mesh
ORION_AWS_SEED_URL=$ORION_AWS_SEED_URL
ORION_AWS_PEER_KEY=$ORION_AWS_PEER_KEY
ORION_BRIDGE_GROUP=laptop-demo
ORION_BRIDGE_SELF_URL=
ORION_BRIDGE_JWT_SECRET=$ORION_BRIDGE_JWT_SECRET
ORION_BRIDGE_API_KEY_PEPPER=$ORION_BRIDGE_API_KEY_PEPPER

AWS_PROFILE=$AWS_PROFILE_USE
EOF

chmod 600 .env

echo "[init-env] wrote .env"
echo "[init-env]   ACME_PEER_KEY len: ${#ACME_PEER_KEY}"
echo "[init-env]   ORION_LOCAL_PEER_KEY len: ${#ORION_LOCAL_PEER_KEY} (different from acme — boundary proof)"
if [ -n "$ORION_AWS_PEER_KEY" ]; then
    echo "[init-env]   ORION_AWS_PEER_KEY: pulled from SSM ($ORION_AWS_SEED_URL)"
else
    echo "[init-env]   ORION_AWS_PEER_KEY: SSM lookup failed — shape B/C unavailable until you populate manually"
fi
echo "[init-env] done"
