# `ci/` — AWS CodeBuild migration scaffolding

Companion to GitHub Actions during the GHA → CodeBuild cut-over described in
[`docs/decisions/13-gha-to-codebuild-migration.md`](../docs/decisions/13-gha-to-codebuild-migration.md).

## Layout

- `buildspecs/<workflow>.yml` — CodeBuild buildspec, one per workflow. Should
  mirror the corresponding `.github/workflows/<workflow>.yaml` exactly. Don't
  tighten or loosen behaviour during migration.
- `../templates/codebuild-project.yaml` — reusable CloudFormation module that
  emits one CodeBuild project + service role + log group per workflow. Same
  module is copied verbatim into `8th-layer-marketing-website` and
  `8th-layer-marketplace` for issues #169 and #170.

## Deploying a project

The shared CodeStar Connection (`OneZero1ai-github`) must be in `AVAILABLE`
state before any project can pull source. After the operator authorises it
in the AWS console, deploy the canary with:

```bash
aws --profile 8th-layer-app --region us-east-1 cloudformation deploy \
  --template-file templates/codebuild-project.yaml \
  --stack-name codebuild-ci-plugin-agent \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    RepoFullName=OneZero1ai/8th-layer-agent \
    RepoShort=agent \
    WorkflowName=ci-plugin \
    BuildspecPath=ci/buildspecs/ci-plugin.yml \
    ConnectionArn=arn:aws:codestar-connections:us-east-1:124074140789:connection/59c82259-9b0e-4a07-990e-732a9eedec71 \
    WebhookFilePathFilter='^(plugins/cq/.*|schema/.*|\.github/workflows/ci-plugin\.yaml|ci/buildspecs/ci-plugin\.yml)$'
```

The `WebhookFilePathFilter` mirrors the GHA `paths:` filter for `ci-plugin.yaml`.

## Rollback

`aws cloudformation delete-stack --stack-name codebuild-ci-plugin-agent`. The
existing `.github/workflows/ci-plugin.yaml` is left in place during the
canary period — branch protection still required-checks the GHA name. Flip
the required check to `ci-plugin-agent` only after the CodeBuild project
has been green for 5 PRs and one forced-red cycle (per decision doc).
