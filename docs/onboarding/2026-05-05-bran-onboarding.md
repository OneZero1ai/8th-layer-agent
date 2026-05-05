# Onboarding Report: Bran Agent → moscowmul3

**Date:** 2026-05-05  
**Agent:** Bran (OpenClaw-based personal assistant running on EC2)  
**Enterprise slug:** `moscowmul3`  
**Setup skill used:** `https://setup.8th-layer.onezero1.ai/`  
**Total time:** ~12 minutes (wall clock)  
**Outcome:** ✅ Success — consult sent to `8th-layer/engineering:david`

---

## Executive Summary

First successful B2B onboarding via the setup skill. The skill document is well-structured and agent-executable. Two blockers encountered, both recoverable. The flow works end-to-end.

---

## Timeline

| Step | Time | Status | Notes |
|------|------|--------|-------|
| Preflight | 15:04 | ✅ | AWS creds, uv, jq all present |
| Slug validation | 15:04 | ✅ | `moscowmul3` available |
| CLI install | 15:05 | ✅ | `uv tool install` worked first try |
| Keygen | 15:06 | ✅ | Ed25519 keypair generated |
| Key backup | 15:07 | ✅ | Pushed to Secrets Manager |
| CFN deploy (attempt 1) | 15:07 | ❌ | ECS service-linked role error |
| CFN deploy (attempt 2) | 15:09 | ✅ | Succeeded after stack delete + retry |
| Announce (AAISN) | 15:13 | ✅ | `rec_a326b732b9acb3ed329e51dc8162af75` |
| Peering offer | 15:13 | ✅ | `off_6b99a331028640198f3a3f7148f09ae6` |
| Auto-accept | 15:14 | ✅ | 30 seconds |
| Admin seed | 15:15 | ✅ | Required SSM plugin install |
| JWT + API key | 15:15 | ✅ | |
| First consult | 15:16 | ✅ | `th_4973718be32141a4` |

---

## Blockers Encountered

### 1. ECS Service-Linked Role (CRITICAL → recovered)

**Error:**
```
CreateCluster Invalid Request: Unable to assume the service linked role. 
Please verify that the ECS service linked role exists.
```

**Root cause:** The AWS account (`206152729751`) had never used ECS before. The service-linked role `AWSServiceRoleForECS` existed but wasn't fully propagated or usable.

**Recovery:** Deleted the failed stack, waited, retried. The second attempt succeeded. This is a known AWS eventual-consistency issue with SLRs.

**Recommendation for setup skill:**
- Add a preflight check: `aws iam get-role --role-name AWSServiceRoleForECS` 
- If it fails, run `aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com` and wait 30s
- Or: add retry logic to the CFN step with a note that first-time ECS accounts may need a retry

### 2. Session Manager Plugin Missing (MEDIUM → recovered)

**Error:**
```
SessionManagerPlugin is not found.
```

**Context:** Step 8 uses `aws ecs execute-command` which requires the Session Manager plugin. The EC2 instance didn't have it.

**Recovery:** Installed via:
```bash
curl "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/ubuntu_64bit/session-manager-plugin.deb" \
  -o "/tmp/session-manager-plugin.deb"
sudo dpkg -i /tmp/session-manager-plugin.deb
```

**Recommendation for setup skill:**
- Add to preflight: `which session-manager-plugin || echo "SSM plugin not found"`
- Include install commands inline (the skill already has the macOS hint but not Linux)

### 3. API Key Auth Didn't Work (MINOR → worked around)

**Observation:** The API key minted in Step 9 returned `"Invalid or expired token"` when used. The JWT worked fine.

**Possible causes:**
- API key format issue (`cqa.v1.xxx` vs expected format)
- API key not yet propagated
- Different auth path for API keys vs JWTs

**Workaround:** Used JWT for the consult request. Worked immediately.

**Recommendation:** Investigate whether the `/auth/api-keys` endpoint is fully wired up, or document that JWTs should be used for immediate testing.

---

## What Worked Well

1. **Skill document structure** — Clear steps, numbered, with expected outputs. Easy to follow programmatically.

2. **Slug availability check** — Simple HTTP check before any heavy lifting. Good UX.

3. **Key backup reminder** — The big warning banner is appropriate for the criticality.

4. **Peering auto-accept** — 30 seconds. Fast enough that the polling loop caught it on attempt 2.

5. **Consult API** — Once auth was sorted, the `/consults/request` endpoint worked exactly as expected.

6. **OpenAPI introspection** — When the API paths didn't match the skill doc (`/api/v1/consults` vs `/consults`), I could hit `/openapi.json` to discover the actual schema. Self-documenting API saved time.

---

## Suggestions for Improvement

### High Priority

1. **Add ECS SLR preflight check** — Most agent AWS accounts won't have used ECS. This will be the #1 failure mode.

2. **Add Session Manager plugin to preflight** — Required for Step 8. Easy to miss.

3. **Document both API paths** — The OpenAPI shows both `/consults/request` and `/api/v1/consults/request`. The skill doc uses `/api/v1/` prefix which 404'd. Clarify which is canonical.

### Medium Priority

4. **Add estimated costs to preflight** — Show `~$30/mo` before the user commits. Currently only in the CFN description.

5. **Add `--wait` flag to CFN create** — Instead of a polling loop, `aws cloudformation wait stack-create-complete --stack-name $STACK` is cleaner.

6. **Test API key auth** — Either fix it or document that JWTs are preferred for initial testing.

### Low Priority

7. **Add a "resume from step N" section** — If a user fails at Step 6, they shouldn't re-run Steps 1-5. Document how to detect which steps are already done.

8. **Provide a teardown confirmation** — The teardown section is good but could note that the AAISN is permanent (namespace claimed forever).

---

## Environment Details

```
Agent: Bran (OpenClaw + Claude Opus 4.5)
Host: EC2 t3.medium, us-east-2
AWS Account: 206152729751 (Dirk's personal)
Region: us-east-1 (for L2 deploy)
uv: 0.6.x
8l-directory CLI: 0.1.0.dev0
```

---

## Final State

```
✓ AAISN:          moscowmul3 (sha256:a26616c76bcf...07b87)
✓ L2 URL:         http://moscowmu-Alb-sTC5HyLIW42Y-44294565.us-east-1.elb.amazonaws.com
✓ Peering:        moscowmul3 ↔ 8th-layer (active)
✓ Admin user:     admin@moscowmul3/engineering
✓ First consult:  th_4973718be32141a4 → 8th-layer/engineering:david
```

---

## Questions for the Team

1. Is the API key auth issue known? Should I file a bug?
2. Should there be a `/status` or `/whoami` endpoint for quick health checks with auth?
3. What's the expected response time for David to reply to the test consult?

---

*Report generated by Bran on 2026-05-05. Happy to provide logs or re-test any fixes.*
