# AWS IAM prerequisites for the 8th-Layer.ai signup wizard

Before walking the wizard at https://signup.8th-layer.ai, you'll deploy
[`marketplace-customer-role-snippet.yaml`](./marketplace-customer-role-snippet.yaml)
as a CloudFormation stack in your AWS account. That stack creates one
IAM role — `8thLayerL2Provisioner` — that 8L's provisioning service
later assumes (gated by your ExternalId) to deploy your L2.

This doc covers the permissions **your IAM principal** needs to deploy
that role stack. The permissions the *role itself* needs (ECS, EFS,
ALB, etc.) are baked into the template — you don't have to grant them
to yourself.

## Easiest path

If you already have `AdministratorAccess` on the AWS account you're
deploying into, you're done — skip ahead to the wizard.

## Least-privilege path

Attach the policy below to the IAM user or role you'll use to run
`aws cloudformation create-stack` for the role template. Scoped to
`us-east-1` (the only region the wizard accepts right now) and to the
role name `8thLayerL2Provisioner`.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ProvisionerRoleStackLifecycle",
      "Effect": "Allow",
      "Action": [
        "cloudformation:CreateStack",
        "cloudformation:UpdateStack",
        "cloudformation:DeleteStack",
        "cloudformation:DescribeStacks",
        "cloudformation:DescribeStackEvents",
        "cloudformation:DescribeStackResource",
        "cloudformation:DescribeStackResources",
        "cloudformation:GetTemplate",
        "cloudformation:ListStacks"
      ],
      "Resource": "arn:aws:cloudformation:us-east-1:*:stack/*"
    },
    {
      "Sid": "ProvisionerRoleLifecycle",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:GetRole",
        "iam:UpdateAssumeRolePolicy",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:GetRolePolicy",
        "iam:ListRolePolicies",
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:ListAttachedRolePolicies",
        "iam:TagRole",
        "iam:UntagRole"
      ],
      "Resource": "arn:aws:iam::*:role/8thLayerL2Provisioner"
    },
    {
      "Sid": "SanityCheck",
      "Effect": "Allow",
      "Action": [
        "sts:GetCallerIdentity"
      ],
      "Resource": "*"
    }
  ]
}
```

## Deploy command

After attaching the policy, save the CFN template locally and run:

```bash
# Generate the ExternalId that the wizard will ask for (≥22 chars)
EXTERNAL_ID=$(python3 -c "import uuid; print(uuid.uuid4())")
echo "ExternalId: $EXTERNAL_ID  # paste this into the wizard"

# Deploy the role stack
curl -fsSL \
  https://raw.githubusercontent.com/OneZero1ai/8th-layer-agent/main/docs/marketplace-customer-role-snippet.yaml \
  -o /tmp/8l-provisioner-role.yaml

aws cloudformation create-stack \
  --stack-name eightl-provisioner-role \
  --template-body file:///tmp/8l-provisioner-role.yaml \
  --parameters ParameterKey=ProvisionerExternalId,ParameterValue=$EXTERNAL_ID \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1

aws cloudformation wait stack-create-complete \
  --stack-name eightl-provisioner-role --region us-east-1

# Read back the role ARN — paste into the wizard
aws cloudformation describe-stacks \
  --stack-name eightl-provisioner-role --region us-east-1 \
  --query 'Stacks[0].Outputs[?OutputKey==`RoleArn`].OutputValue' --output text
```

> ⚠️ CloudFormation stack names cannot start with a digit. Use
> `eightl-...` (or any letter-leading name), not `8l-...`.

## After the wizard

You don't need any further AWS permissions during day-to-day use —
the L2 admin UI at `https://<slug>.8th-layer.ai`, the `8l` CLI, and
the `8l-cq` Claude Code plugin all talk to 8L's HTTPS APIs, not AWS.

**Optional** post-deploy perms, useful for edge cases:

| Action | When you'd need it |
|---|---|
| `secretsmanager:GetSecretValue` on `eighth-layer-l2-<slug>/*` | If you need to read auto-generated L2 secrets |
| `ecs:ExecuteCommand` + `ssm:StartSession` on the L2 cluster | Manual admin-seed fallback if the magic-link email fails to deliver |
| `cloudformation:DeleteStack` on the L2 stack | Tearing down the L2 to stop ~$30/mo billing |

## What does NOT need to be in this policy

Anything the role itself does (`ec2:*`, `ecs:*`, `elasticloadbalancing:*`,
`elasticfilesystem:*`, `logs:*`, `secretsmanager:*`, IAM scoped to
`8th-layer-l2-*`) is granted to `8thLayerL2Provisioner` by the CFN
template — those are exercised by 8L's provisioning service when it
assumes the role, not by your user.
