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

## Publishing the marketplace L2 template (`marketplace-l2-publish`)

`deploy/aws/marketplace-l2.yaml` is the **single source of truth** for the
L2 provisioning CloudFormation template. The 8th-Layer directory's
provisioning service fetches it from
`s3://8l-provisioning-templates-124074140789/marketplace-l2/latest/marketplace-l2.yaml`
when it creates a customer L2 stack. There was previously **no pipeline**
syncing repo → S3, which let the repo template, the S3 copy, and the
directory worker's CFN `Parameters` list drift into three inconsistent
param contracts.

`buildspecs/marketplace-l2-publish.yml` is a single-purpose buildspec that
runs `aws s3 cp` on every merge to `main`. It needs its own CodeBuild
project (`marketplace-l2-publish`) — `ci.yml` cannot host it because
`ci.yml` also runs on PRs and must not publish un-reviewed YAML to the
live path.

The project's service role needs `s3:PutObject` on the template key plus
`cloudformation:ValidateTemplate`. Stage a managed policy and pass it via
the `ExtraPolicyArns` parameter of `templates/codebuild-project.yaml`:

```bash
# 1. One-time: a managed policy granting the publish permissions.
aws --profile 8th-layer-app --region us-east-1 iam create-policy \
  --policy-name marketplace-l2-publish-s3 \
  --policy-document '{
    "Version":"2012-10-17",
    "Statement":[
      {"Effect":"Allow","Action":"cloudformation:ValidateTemplate","Resource":"*"},
      {"Effect":"Allow","Action":["s3:PutObject"],
       "Resource":"arn:aws:s3:::8l-provisioning-templates-124074140789/marketplace-l2/latest/*"},
      {"Effect":"Allow","Action":["s3:ListBucket"],
       "Resource":"arn:aws:s3:::8l-provisioning-templates-124074140789"}
    ]}'

# 2. Deploy the CodeBuild project off the shared module.
aws --profile 8th-layer-app --region us-east-1 cloudformation deploy \
  --template-file templates/codebuild-project.yaml \
  --stack-name codebuild-marketplace-l2-publish-agent \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    RepoFullName=OneZero1ai/8th-layer-agent \
    RepoShort=agent \
    WorkflowName=marketplace-l2-publish \
    BuildspecPath=ci/buildspecs/marketplace-l2-publish.yml \
    ConnectionArn=arn:aws:codestar-connections:us-east-1:124074140789:connection/59c82259-9b0e-4a07-990e-732a9eedec71 \
    ExtraPolicyArns=arn:aws:iam::124074140789:policy/marketplace-l2-publish-s3 \
    WebhookFilePathFilter='^deploy/aws/marketplace-l2\.yaml$'
```

The `WebhookFilePathFilter` scopes the trigger to template changes only;
the buildspec also re-validates the template and refuses to publish from
any branch other than `main`.

**One-time catch-up:** after this PR merges the new project does not exist
yet, so the first merged template change is not on S3 until either the
project is created and a build runs, or it is published by hand:

```bash
aws --profile 8th-layer-app --region us-east-1 s3 cp \
  deploy/aws/marketplace-l2.yaml \
  s3://8l-provisioning-templates-124074140789/marketplace-l2/latest/marketplace-l2.yaml \
  --content-type text/yaml
```
