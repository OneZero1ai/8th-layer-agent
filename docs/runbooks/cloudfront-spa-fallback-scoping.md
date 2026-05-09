# Runbook: CloudFront SPA-fallback CER masking L2 API responses

**Issue:** [`OneZero1ai/8th-layer-agent#155`](https://github.com/OneZero1ai/8th-layer-agent/issues/155)
**KU:** `ku_9aa329acec444759ae070e827e51efc5` — *CloudFront custom error responses are
distribution-scoped, not behavior-scoped — an SPA fallback can mask upstream API
404s as 200/HTML for sibling cache behaviors.*

## Symptom

A request to a real upstream that returns a 4xx — e.g.

```
GET https://8th-layer.ai/admin/api/l2/8th-layer/api/v1/this-endpoint-does-not-exist
```

— gets rewritten by the apex CloudFront distribution into `200 text/html` (the
SPA's `/index.html` body), instead of the L2 ALB's actual `404 application/json`.
Programmatic L2 callers can't distinguish "endpoint missing" from "endpoint
returned an HTML page."

## Cause

The apex distribution `E3BYVFLFAJLXM` carried the standard SPA-fallback custom
error responses at the **distribution** level:

```
404 → /index.html, response code 200, ttl 60
403 → /index.html, response code 200, ttl 60
```

`CustomErrorResponses` in CloudFront is **distribution-scoped**. There is no
per-cache-behavior `CustomErrorResponses` field on the API surface — confirmed
by `aws cloudfront get-distribution-config`'s `DistributionConfig.CacheBehaviors[*]`
schema, which has no such key. So the SPA's fallback was being applied to *every*
4xx from every origin, including the `/admin/api/l2/*` ALB origins.

## Fix

The apex site at `8th-layer.ai/coming-soon/` is a **multi-page static site**
(distinct `.html` files), not a deep-routed SPA. The CER 404→/index.html was
copy-pasted from a SPA template and is not load-bearing for any real route. The
fix is therefore the simplest viable one:

**Remove `CustomErrorResponses` entirely from the distribution.**

This restores native upstream status codes for all behaviors, including the L2
proxies. Real S3 misses now surface as the native S3 4xx instead of being
laundered into 200/HTML.

### What was considered and rejected

- **Per-behavior CER** — not supported by CloudFront. The `CustomErrorResponses`
  key only exists at `DistributionConfig`, not on individual cache behaviors.
- **CloudFront Function viewer-request rewrite to `/index.html`** — would be the
  right shape for a true deep-routed SPA, but this site isn't one. Adding a
  rewrite would only mask missing pages with the index page, which is the same
  pathology in a different layer.
- **Lambda@Edge origin-response inspector** — could rewrite the S3 404 body
  while leaving the L2 behaviors alone, but is heavier than warranted for a
  static site that doesn't need a custom 404 page today.

If a custom 404 page is wanted later, serve a static `/404.html` from S3 with
its native `404` status. Do not re-introduce a distribution-level CER targeting
4xx unless the L2 cache behaviors are first moved to a separate distribution.

## Procedure

```bash
# 1. Snapshot current config for rollback.
aws cloudfront get-distribution-config --id E3BYVFLFAJLXM \
  --profile 8th-layer-app --region us-east-1 --output json \
  > /tmp/e3byvflfajlxm-full.json
ETAG=$(jq -r .ETag /tmp/e3byvflfajlxm-full.json)
jq '.DistributionConfig' /tmp/e3byvflfajlxm-full.json \
  > /tmp/e3byvflfajlxm-config-pre.json

# 2. Build new config: zero out CustomErrorResponses.
jq '.CustomErrorResponses = {"Quantity": 0}' \
  /tmp/e3byvflfajlxm-config-pre.json \
  > /tmp/e3byvflfajlxm-config-new.json

# 3. Apply.
aws cloudfront update-distribution --id E3BYVFLFAJLXM \
  --if-match "$ETAG" \
  --distribution-config file:///tmp/e3byvflfajlxm-config-new.json \
  --profile 8th-layer-app --region us-east-1

# 4. Invalidate cached SPA-fallback responses.
aws cloudfront create-invalidation --distribution-id E3BYVFLFAJLXM \
  --paths '/*' --profile 8th-layer-app --region us-east-1

# 5. Wait for Status: Deployed (5-10 min typical).
aws cloudfront get-distribution --id E3BYVFLFAJLXM \
  --profile 8th-layer-app --region us-east-1 \
  --query 'Distribution.Status' --output text
```

## Acceptance tests

```bash
# A. Apex SPA loads
curl -s -o /dev/null -w "%{http_code} %{content_type}\n" https://8th-layer.ai/
# expect: 200 text/html

# B. Admin static page loads
curl -s -o /dev/null -w "%{http_code} %{content_type}\n" \
  https://8th-layer.ai/admin/network.html
# expect: 200 text/html

# C. L2 health passes through
curl -s -o /dev/null -w "%{http_code} %{content_type}\n" \
  https://8th-layer.ai/admin/api/l2/team-dw/api/v1/health
# expect: 200 application/json

# D. THE KEY TEST — L2 404 must pass through cleanly
curl -s -o /dev/null -w "%{http_code} %{content_type}\n" \
  https://8th-layer.ai/admin/api/l2/8th-layer/api/v1/this-endpoint-does-not-exist
# expect: 404 application/json (NOT 200 text/html)
```

## Rollback

```bash
aws cloudfront get-distribution-config --id E3BYVFLFAJLXM \
  --profile 8th-layer-app --region us-east-1 \
  --query 'ETag' --output text  # capture current ETag
aws cloudfront update-distribution --id E3BYVFLFAJLXM \
  --if-match "<current-etag>" \
  --distribution-config file:///tmp/e3byvflfajlxm-config-pre.json \
  --profile 8th-layer-app --region us-east-1
aws cloudfront create-invalidation --distribution-id E3BYVFLFAJLXM \
  --paths '/*' --profile 8th-layer-app --region us-east-1
```
