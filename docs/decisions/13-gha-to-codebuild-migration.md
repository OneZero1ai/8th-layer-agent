# GHA → CodeBuild + CodeStar Connection migration (design)

**Author**: 8th-Layer.ai cofounder agent
**Date**: 2026-05-08
**Status**: Design pass — no infra created. Awaiting operator approval before cut.
**Repo**: `OneZero1ai/8th-layer-agent` (origin); `mozilla-ai/cq` is the upstream the release workflows still reference (GHCR org, Homebrew tap).
**AWS account**: `8th-layer-app` profile, `us-east-1`. Confirmed empty: `codestar-connections list-connections` → `[]`, `codebuild list-projects` → `[]`.

---

## Why we're doing this

Two reasons, both operator-set:

1. Avoid GitHub-as-pipeline dependency (`feedback_avoid_github_dependencies.md`). Code stays on GitHub; only the runner moves.
2. Standardise on AWS-native CI so the IL/CMMC story later is "the pipeline is in our boundary" rather than "GitHub-hosted runners ran our build". Same reason FIPS, ECR, and ECS are V1-locked.

Out of scope here: moving the *code* off GitHub, replacing release artifact targets (PyPI, Homebrew tap, GHCR), or changing what the workflows actually do. Only the executor changes.

---

## Workflow inventory (14 files)

### CI (9, triggered on push/PR to main, path-filtered)

| Workflow | Trigger | Runtime / lang | Produces | AWS perms needed | Notes |
|---|---|---|---|---|---|
| `ci-cli.yaml` | push/pr `cli/**`, `sdk/go/**`; `workflow_dispatch` | Go (mod-pinned) + golangci-lint v2.10.1 | test result + local build | none | matrix: lint → test |
| `ci-install.yaml` | push/pr `scripts/install/**`, `plugins/cq/**` | uv (Python) | test result | none | exercises installer + plugin hook |
| `ci-licenses.yaml` | push/pr on go.mod/go.sum/NOTICE files | Go + `go-licenses` | test result | none | NOTICE freshness check |
| `ci-plugin.yaml` | push/pr `plugins/cq/**`, `schema/**` | uv | lint result | none | smallest workflow — lint only |
| `ci-prompts-sync.yaml` | push/pr on prompt source/copy paths; `workflow_dispatch` | bash + diff | test result | none | drift check between plugin and SDK prompt copies |
| `ci-schema.yaml` | push/pr `schema/**`; `workflow_dispatch` | uv + Go + Python 3.11/3.12/3.13 matrix | test result | none | 4 jobs: validate, lint-go, test-go, test-python (matrix) |
| `ci-sdk-go.yaml` | push/pr `schema/**`, `sdk/go/**`; `workflow_dispatch` | uv + Go | test result | none | mirror of ci-schema's Go path with SDK scope |
| `ci-sdk-python.yaml` | push/pr `schema/**`, `sdk/python/**` | uv + Python 3.11/3.12/3.13 matrix | test result | none | |
| `ci-server.yaml` | push/pr `schema/**`, `server/**` | uv + pnpm 10 + Node 22 + Python 3.11/3.12/3.13 matrix | test result | none | heaviest CI — backend + frontend, biggest churn risk on cut-over |

### Release (4, triggered on `release: published` + `workflow_dispatch`)

| Workflow | Trigger | Runtime / lang | Produces | AWS perms needed | External secrets |
|---|---|---|---|---|---|
| `release-cli.yaml` | release `cli/v*`; manual | Go cross-compile (linux/darwin/windows × amd64/arm64 = 6 jobs) | binaries + tarballs + sha256 + Homebrew cask | none | `GITHUB_TOKEN` (release upload), `HOMEBREW_TAP_GITHUB_TOKEN` (push to `mozilla-ai/homebrew-tap`) |
| `release-schema.yaml` | release `schema/v*`; manual | uv | wheel | none | PyPI OIDC trusted publisher (`id-token: write`) |
| `release-sdk-python.yaml` | release `sdk/python/*`; manual | uv | wheel | none | PyPI OIDC trusted publisher |
| `release-server-image.yaml` | release `server/v*`; manual | docker buildx (amd64+arm64) | OCI image to GHCR + DockerHub | none | `GITHUB_TOKEN` (GHCR), `DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN` |

### Validation (1)

| `validate-cli-release.yaml` | push/pr on release config; `workflow_dispatch` | Go | test result | none | belt-and-braces check that the release workflow can render the brew template |

**Aggregate observation:** **None of the 14 workflows currently consume AWS resources.** Every job is self-contained or talks to GitHub/PyPI/DockerHub. So the IAM story for CodeBuild is *much* simpler than I expected: the service role mostly needs `logs:*` to its own log group + `codestar-connections:UseConnection` for source pull. The release workflows still need the equivalent of `GITHUB_TOKEN`, PyPI OIDC, and DockerHub creds — those move from GHA secrets to **AWS Secrets Manager** + a CodeBuild env-var binding. PyPI OIDC trusted-publisher won't work from CodeBuild without reconfiguring PyPI to trust the CodeBuild ARN — flagged below.

---

## CodeStar Connection setup

One **GitHub** connection per AWS account is sufficient — it's a host-level GitHub App install, not per-repo. Creation is two steps and the second step **requires the operator** (cannot be automated):

1. `aws codestar-connections create-connection --provider-type GitHub --connection-name 8th-layer-github` → returns `PENDING` connection ARN.
2. Operator opens the AWS console, clicks **Update pending connection**, authorises the AWS Connector for GitHub App on the `OneZero1ai` org (and `mozilla-ai` if we keep the upstream-targeting release flows), selects which repos to grant the App access to. Status flips to `AVAILABLE`.

The App install on the GitHub side is per-org, not per-repo — pre-existing org-level installs of "AWS Connector for GitHub" can be reused; we just add the new connection ARN underneath. Once `AVAILABLE`, every CodeBuild project in this account can reuse the same connection ARN — no per-repo ceremony.

Reference: <https://docs.aws.amazon.com/dtconsole/latest/userguide/connections-create-github.html>

---

## One CodeBuild project per workflow vs single project + multiple buildspecs

**Recommendation: one CodeBuild project per workflow file (14 projects).**

Reasons, ranked:

1. **Mental-model parity.** Every developer here knows the GHA mental model — one workflow, one badge, one set of triggers, one log surface. CodeBuild project granularity preserves it. PR check names stay 1:1 (`Server CI / test` → `codebuild/8l-ci-server-test`).
2. **Trigger filters live on the project.** GHA path-filters become CodeBuild webhook `FILE_PATH` filter groups, scoped per project. With a single project you'd encode all 14 path-filter sets inside one buildspec dispatch script, which becomes a parser of `CODEBUILD_WEBHOOK_HEAD_REF` / changed files — fragile and unobservable.
3. **IAM scoping.** Most projects need *zero* AWS perms. The release projects need very specific ones (Secrets Manager read for the Homebrew/DockerHub tokens). One project per workflow lets each role be exactly as small as it needs. A unified project means one fat role.
4. **Sprawl is real but cheap.** 14 CFN resources + 14 service roles is annoying once; after that they're just rows in `list-projects`. Codify them in a single CFN/Terraform stack (`ci/codebuild.yaml`), parameterise the common bits.
5. **Cost is identical** — CodeBuild bills on build-minutes, not on project count.

The only argument for the single-project approach is "less to maintain". With a CFN module that takes `(name, buildspec_path, source_paths, env)` as parameters and emits the project + role + log group, the maintenance delta evaporates.

**The matrix workflows** (`ci-schema`, `ci-sdk-python`, `ci-server` — Python 3.11/3.12/3.13; `release-cli` — 6 OS×arch combos) become **batch builds** within a single project (`buildspec`'s `batch:` section), not separate projects. CodeBuild batch builds support fan-out + a single roll-up status. This is the closest analog to GHA's `strategy.matrix`.

Reference: <https://docs.aws.amazon.com/codebuild/latest/userguide/batch-build-buildspec.html>

---

## PR status reporting

CodeBuild + CodeStar Connection reports each build as a **GitHub commit status** on the PR head SHA. The status name is the CodeBuild project name — that's why per-project granularity matters for the merge-gate UX. Required-check enforcement on the GitHub side works against these reported contexts identically to GHA's check names; you flip the required checks in branch protection from `Server CI / test` to `codebuild/8l-ci-server` when ready.

Reference: <https://docs.aws.amazon.com/codebuild/latest/userguide/sample-github-pull-request.html> — confirms PR webhook trigger, commit-status reporting, and that you can scope to PR events (`PULL_REQUEST_CREATED`, `PULL_REQUEST_UPDATED`, `PULL_REQUEST_REOPENED`).

For batch builds: the parent build reports a single commit status; child builds don't pollute the PR check list. (This is desirable for matrix workflows.)

---

## IAM model

CodeBuild has a **service role** per project (it doesn't use OIDC for itself — OIDC is only relevant if your *build* needs to assume an external role). The base role grants:

- `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents` on the project's own log group ARN
- `codestar-connections:UseConnection` on the connection ARN (source pull)
- (For VPC builds only — none of ours need it yet) `ec2:*NetworkInterface*`

Per-workflow additions:

- **`release-cli`** → `secretsmanager:GetSecretValue` on `arn:aws:secretsmanager:us-east-1:<acct>:secret:ci/homebrew-tap-token-*`. The `gh release upload` and `git push` to `homebrew-tap` are HTTPS calls with a PAT — the PAT lives in Secrets Manager, gets bound as a CodeBuild `secrets-manager` env var.
- **`release-server-image`** → Secrets Manager read for DockerHub creds. **Plus**: if we eventually push to ECR Public (V1-locked surface — see `02-cloud-portability.md`), this project gets `ecr-public:*` on a specific repo ARN. Not yet — current workflow targets GHCR + DockerHub.
- **`release-schema`, `release-sdk-python`** → PyPI's GHA-OIDC trusted-publisher won't accept a CodeBuild OIDC token. Two options: (a) **PyPI API token** in Secrets Manager (simpler, less safe), or (b) **reconfigure PyPI trusted publisher** to trust an AWS Cognito / IAM identity via the GitHub-Actions-shaped JWT that CodeBuild can mint with `oidc-provider`. (a) is the pragmatic V1; flag for hardening. **This is a real gap** — needs operator decision.

All other 10 projects need only the base role.

---

## Migration order

**Canary: `ci-plugin`.** Smallest workflow (lint-only, single uv setup, no matrix, no AWS perms). One buildspec, one webhook, one PR-status check. If the canary lands cleanly, the only new dimensions for the next ones are (a) multi-job DAG (`ci-cli`'s `lint → test`), (b) matrix (any of the `*-python.yaml`), (c) Docker-in-Docker (`release-server-image`), (d) cross-org GitHub PAT (`release-cli`).

**Recommended order** (each is a strict superset of the prior):

1. `ci-plugin` (canary — single job, no needs, no matrix)
2. `ci-licenses` (single job, Go-only, NOTICE check)
3. `ci-prompts-sync` (proves the bash + diff path)
4. `ci-cli` (introduces multi-job DAG via `dependsOn` in batch buildspec)
5. `ci-install`
6. `ci-sdk-go`
7. `ci-sdk-python` (introduces Python matrix)
8. `ci-schema` (mixed matrix + multi-language)
9. `ci-server` (heaviest — pnpm + Node + Python matrix; touch this last to absorb prior lessons)
10. `validate-cli-release`
11. `release-schema` (PyPI auth — flag the trusted-publisher gap)
12. `release-sdk-python`
13. `release-cli` (large OS×arch matrix; cross-org GitHub PAT)
14. `release-server-image` (DiD; multi-arch buildx; dual-registry push)

Land each as its own PR. The release workflows can wait until after the next release tag — no point rotating the secret store ahead of need.

---

## Rollback plan

For each workflow:

1. **Week 0**: ship the CodeBuild project. GHA workflow stays untouched. Branch protection still required-checks the GHA name. CodeBuild commit-status reports alongside as informational.
2. **Week 1**: confirm the CodeBuild check has been green on every PR-merge for at least 5 PRs *and* one full failing-test cycle (force a red to verify it actually blocks). Flip branch-protection required checks from the GHA name to the CodeBuild name.
3. **Week 2**: delete the GHA workflow file. Keep the CodeBuild project log group for 90 days for audit.

Dual-CI cost during the overlap is trivial: GHA-hosted runners are free for public repos, near-free for private at our scale; CodeBuild small.medium runs at ~$0.005/min — even the heaviest `ci-server` (~5 min) is $0.025 per build.

The rollback is "delete the CodeBuild project, restore the required-check name to the GHA one" — fully reversible until step 3.

---

## Estimate

Per `~/CLAUDE.md` house rule (no single-point estimates):

- **Naive (per workflow)**: 1d to write buildspec + CFN module entry + service role + smoke-test on a draft PR. With a reusable CFN module after #1, drops to ~0.5d each.
- **Touch-points**: 14 workflow files × (1 `buildspec.yaml` + 1 CFN stack-row + 1 IAM role + 1 webhook config) = **~56 logical touch-points**. Plus the shared CFN module, the connection bootstrap runbook, the branch-protection flip runbook = **~60 total**. >5-files multiplier applies.
- **Range**:
  - **Naive** (everything goes well, reusable module written once): **6 dev-days** for all 14.
  - **Realistic** (×2 — first canary takes 2d, matrix surprises in `ci-server`, PyPI trusted-publisher rework, secret rotation runbook): **12 dev-days** ≈ 2.5 calendar weeks at 1 person.
  - **If unknowns hit** (×3 — PyPI OIDC genuinely won't work and we need a real auth refactor; CodeBuild webhook quirks on path-filter behaviour for monorepos; Homebrew-tap PAT scoped wrong on first try): **18 dev-days** ≈ 4 calendar weeks.
- **Excluded**: branch-protection migration coordination (operator-blocking), runbook docs in `docs/runbooks/`, post-cut audit that no CI minutes are charged to GHA, cleanup of old GHA secrets in repo settings, cost-monitoring dashboard.
- **Past calibration**: no prior CI migration in this repo. The closest reference is the L2 SSM-secret rollout (issue #4) which was estimated as a small CFN edit and turned into a 1.5-week story. Same shape risk here — "small CFN" times N.

**Net: budget 12 dev-days, expect 18.** Don't promise <2 weeks externally.

---

## Open questions for the operator

1. **PyPI trusted-publisher (release-schema, release-sdk-python).** The current GHA flow uses OIDC trusted publishing with PyPI. CodeBuild can't slot into that without PyPI-side reconfiguration. Are we OK reverting to API tokens in Secrets Manager for V1, or do we want to invest in CodeBuild→PyPI OIDC trust as part of this migration? (Recommendation: API tokens for V1, OIDC as a follow-up.)
2. **Homebrew tap target.** `release-cli.yaml` pushes to `mozilla-ai/homebrew-tap` (upstream), not to a `OneZero1ai`-owned tap. Do we keep that target (then we need a PAT for the upstream org), or fork the tap to `OneZero1ai/homebrew-tap` first? (This is independent of CodeBuild but the migration forces the question because the secret needs re-provisioning.)
3. **`workflow_dispatch` (manual trigger) parity.** 5 of the 14 workflows expose `workflow_dispatch`. CodeBuild's equivalent is "click Start build" in console or `aws codebuild start-build`. Do we need a UI surface for non-AWS-console users (operator, Dirk)? If yes, a tiny internal page or a `make ci-run-<name>` CLI wrapper is the cleanest answer.
4. **Schedule triggers.** None of the current workflows use `schedule:`. Confirming there's nothing planned that would need EventBridge wiring.
5. **`release-server-image.yaml` dual-registry policy.** Today it pushes to GHCR (`ghcr.io/mozilla-ai/cq/server`) *and* DockerHub (`mzdotai/cq-server`). Both targets are *external public surfaces* tied to the `mozilla-ai` upstream identity. Is the intent to (a) preserve both as-is and migrate the executor only, (b) cut over to `OneZero1ai`-owned ECR Public during this migration, or (c) deprecate one of GHCR/DockerHub? V1-locked decision is "ECR Public" — but the workflow still says GHCR. Worth resolving before touching this one.
6. **`upstream` remote.** The repo still has `mozilla-ai/cq` as `upstream` and the release workflows reference Mozilla GitHub-org artefacts (GHCR, Homebrew tap). Is `OneZero1ai/8th-layer-agent` the durable home for the agent code, or are we still in a "soft fork" stance? If durable, the release workflows (a separate PR) need to retarget; the CodeBuild migration shouldn't paper over that question.

---

## Looks fine — don't reinvent

- **Path-filter semantics** (`paths:` in GHA) map cleanly to CodeBuild webhook `FILE_PATH` filters. No behavioural difference.
- **`needs:` DAG** maps to `batch:` `dependsOn:` in the buildspec. Same shape.
- **Matrix** (`strategy.matrix`) maps to `batch: build-list:` with one entry per matrix cell. Same shape.
- **Secrets** (`${{ secrets.X }}`) → CodeBuild `secrets-manager` env-var binding (`secret-name:secret-key`). Cleaner story (one place to rotate), and we get an audit trail via CloudTrail.
- **Concurrency / cancel-in-progress** — CodeBuild's webhook supports "auto-cancel in-progress builds for the same branch" out of the box; we don't need to encode it.

---

## What's *not* in this design

- The actual buildspec.yaml files (one per workflow). Will land per-PR alongside the CFN entry.
- The CFN module itself (`ci/codebuild.yaml`). Strawman in PR for canary.
- Cost dashboard / CloudWatch alarms on CI spend. Follow-up after cut-over completes.
- IL/CMMC alignment story for the new pipeline boundary. Out of V1 scope; will live in a separate decision once we're past funding.

---

## Files referenced

- `.github/workflows/{ci-*,release-*,validate-cli-release}.yaml` — 14 files, all read for this design.
- AWS docs:
  - <https://docs.aws.amazon.com/dtconsole/latest/userguide/connections-create-github.html>
  - <https://docs.aws.amazon.com/codebuild/latest/userguide/sample-github-pull-request.html>
  - <https://docs.aws.amazon.com/codebuild/latest/userguide/batch-build-buildspec.html>

Design closed 2026-05-08. Awaiting operator approval before any infra is created.
