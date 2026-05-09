# scripts/

Operational scripts for the 8th-Layer.ai infrastructure.

## `cleanup-test-fixtures.py`

Tear down test fixtures (test-acme-*, test-orion-* clusters) from the
`8th-layer-app` AWS account, leaving TeamDW + the dogfood `mvp-cluster`
+ the `cq-directory-cluster` intact.

### Usage

```bash
# DEFAULT: dry-run; lists everything that would be deleted as a table.
# Pipe to tee for review.
./scripts/cleanup-test-fixtures.py --dry-run | tee cleanup-preview.txt

# Scope to a single cluster during the actual cutover (substring match):
./scripts/cleanup-test-fixtures.py --dry-run --filter orion-eng
./scripts/cleanup-test-fixtures.py --execute  --filter orion-eng

# Full execute (after you've reviewed dry-run):
./scripts/cleanup-test-fixtures.py --execute
```

### What it deletes (when `--execute`)

For each of the six test clusters
(`test-acme-{fin,eng,sol}-l2-cluster`, `test-orion-{eng,sol,gtm}-l2-cluster`):

1. **ECS services** — scaled to 0, waited stable, deleted (force=true).
2. **Task definitions** — all `ACTIVE` revisions in the cluster's family deregistered.
3. **ALB listeners** — deleted before the LB.
4. **ALBs** — deleted; waiter blocks until gone before TG cleanup.
5. **Target groups** — deleted (after their LB is gone, so no orphan refs).
6. **EFS mount targets** then **EFS file systems** — `test-*-efs` only.
7. **Security groups** — `test-{cluster}-{Alb,Task,Efs}Sg-*` only (matched by
   `group-name` filter, not by tag, since CFN-created SGs aren't always tagged).
8. **CloudWatch log groups** — `/aws/ecs/test-{cluster}/*` only.
9. **ECS cluster** — finally.

### What it does NOT touch

- `cq-directory-cluster`, `team-dw-l2-cluster`, `mvp-cluster` (allowlisted).
- S3 bucket `8l-web-site-us-east-1-124074140789`.
- CodeStar Connection `OneZero1ai-github`.
- Any IAM role, Cognito user pool, CloudFront distro, or ACM certificate.
- The shared VPC / subnets.
- Directory entries — see "Directory cleanup" below.

### Idempotency

Re-running on already-deleted resources records `skipped (not found)` in
the audit log instead of erroring. Safe to re-run after a partial failure.

### Audit log

Each invocation writes `scripts/cleanup/logs/cleanup-<timestamp>.log` with
one tab-delimited line per action:

```
2026-05-09T...  ECS_SERVICE  arn:aws:...  would_delete  running=1
```

For execute runs, `would_delete` becomes `queued` then a follow-up line
records the result (`ok`, `skipped (not found)`, or `error: ...`).

### Directory cleanup (BLOCKED)

The federated directory at `https://directory.8th-layer.ai` does **not**
expose any DELETE endpoints in its OpenAPI surface (verified
2026-05-09). The script enumerates non-protected enterprises (everything
except `team-dw` and `8th-layer`) and surfaces them as a follow-up
blocker rather than going to SQL.

Resolution path:

- Add `DELETE /admin/api/enterprises/{id}` (and matching peerings DELETE)
  to the `cq-directory` server.
- Re-run this script — it will pick up the new endpoint via OpenAPI probe.

### Cost estimate

Each torn-down cluster is approximately **\$38/month** (Fargate task
+ ALB + ENI + log group). At 6 clusters that's ~**\$228/month** in
recovered spend.
