# L2 substrate recovery runbook

Recovery procedures for the EBS-on-EC2 L2 substrate introduced by
[`agent#323`](https://github.com/OneZero1ai/8th-layer-agent/issues/323).
The L2 CFN template is
[`deploy/aws/marketplace-l2.yaml`](../../deploy/aws/marketplace-l2.yaml).

## Where the data lives

Per-L2 inventory:

| What | Where | CFN resource | Why it matters |
|------|-------|--------------|---------------|
| SQLite DB file | `/data/cq.db` inside the cq-server container | bind-mounted from `/mnt/cq-data` on the EC2 host | The single canonical store of KUs, peers, activity log. |
| Filesystem | ext4 on `/dev/nvme1n1` (the EBS data volume) | `CqDataVolume` (`AWS::EC2::Volume`) | Real POSIX locks → SQLite is safe here. |
| EBS volume | gp3, AZ-pinned to `PublicSubnetA`'s AZ | `CqDataVolume` | Zonal. Loss of the AZ = L2 offline until restore. |
| Snapshots | hourly, last 168 retained (7 days) | `CqDataSnapshotPolicy` (`AWS::DLM::LifecyclePolicy`) | RPO=1h. Snapshots are tagged `cq-l2-data=true`. |

Stack outputs to find them fast:

```
aws cloudformation describe-stacks \
  --profile <l2-profile> --region us-east-1 \
  --stack-name <L2-stack-name> \
  --query 'Stacks[0].Outputs[?OutputKey==`CqDataVolumeId`].OutputValue' \
  --output text
```

`CqDataSnapshotPolicyId` is exported the same way.

## Inspect available snapshots

DLM tags every snapshot with `cq-l2-data: true` (copied from the source
volume). Find the snapshot set:

```
aws ec2 describe-snapshots \
  --profile <l2-profile> --region us-east-1 \
  --owner-ids self \
  --filters Name=tag:cq-l2-data,Values=true \
           Name=tag:EnterpriseSlug,Values=<slug> \
  --query 'Snapshots[].[SnapshotId,StartTime,State,VolumeSize]' \
  --output table
```

The `StartTime` column is the snapshot creation time in UTC. Pick the
most recent `completed` snapshot for a vanilla restore; pick an earlier
one if the corruption / data-loss event happened post-snapshot-N and
you want a known-good prior state.

## Restore procedure (volume lost or DB unrecoverable)

The restore is a CFN parameter change, no manual EBS surgery required.

1. **Identify the target snapshot id** with the `describe-snapshots` call
   above. Note it as `snap-XXXX`.

2. **Update the L2 stack with `RestoreFromSnapshotId`:**

   ```
   aws cloudformation update-stack \
     --profile <l2-profile> --region us-east-1 \
     --stack-name <L2-stack-name> \
     --use-previous-template \
     --capabilities CAPABILITY_IAM \
     --parameters \
       ParameterKey=RestoreFromSnapshotId,ParameterValue=snap-XXXX \
       ParameterKey=EnterpriseSlug,UsePreviousValue=true \
       ParameterKey=CqEnterpriseId,UsePreviousValue=true \
       ParameterKey=CqGroupId,UsePreviousValue=true \
       ParameterKey=RootPubkeyB64u,UsePreviousValue=true \
       ParameterKey=ProvisionerExternalId,UsePreviousValue=true \
       ParameterKey=AdminEmail,UsePreviousValue=true \
       ParameterKey=AutoApprovePropose,UsePreviousValue=true \
       ParameterKey=CqServerImage,UsePreviousValue=true \
       ParameterKey=CqDbVolumeSizeGiB,UsePreviousValue=true \
       ParameterKey=AwsRegion,UsePreviousValue=true \
       ParameterKey=DirectoryUrl,UsePreviousValue=true
   ```

   The volume has `UpdateReplacePolicy: Snapshot` + a `SnapshotId`
   property — CFN snapshots the existing (possibly corrupt) volume,
   then creates a new one from the chosen restore point. CFN
   re-creates the `EcsHostVolumeAttachment` against the existing EC2
   host (or recreates the host if the volume's AZ changed), and
   UserData mounts the restored filesystem at /mnt/cq-data on boot.

3. **Wait for stack rollout (~5-10 min):**

   ```
   aws cloudformation wait stack-update-complete \
     --profile <l2-profile> --region us-east-1 \
     --stack-name <L2-stack-name>
   ```

4. **Verify health:**

   ```
   # ALB target healthy:
   aws elbv2 describe-target-health \
     --profile <l2-profile> --region us-east-1 \
     --target-group-arn $(aws cloudformation describe-stack-resources \
       --profile <l2-profile> --region us-east-1 \
       --stack-name <L2-stack-name> \
       --logical-resource-id TargetGroup \
       --query 'StackResources[0].PhysicalResourceId' --output text)

   # L2 /health 200:
   curl -sS https://<slug>.8th-layer.ai/health
   ```

5. **Do NOT clear `RestoreFromSnapshotId` after restore.** `SnapshotId`
   is an immutable property on `AWS::EC2::Volume`. Toggling it from
   `snap-XXX` back to empty triggers volume replacement —
   CloudFormation will snapshot the just-restored volume then create a
   fresh blank gp3 in its place, wiping your restored data. Leave the
   snapshot id in place as a permanent stack parameter; it has no
   runtime effect after the volume is created.

   To migrate off the snapshot reference (e.g., for ops hygiene), the
   only safe path is to take a manual `aws ec2 create-snapshot` of the
   live volume and use that snapshot id as the new permanent parameter
   value, or stand up a fresh stack.

## RTO / RPO

| Metric | MVP target | Notes |
|--------|-----------|-------|
| RPO | 1 hour | DLM cron `cron(0 * * * ? *)` runs hourly on the hour. Worst case: failure at minute 59 → up to ~60 min of writes lost. |
| RTO | ~10 minutes | Update-stack (~5 min CFN apply) + EC2 host replacement (~3 min new instance + UserData) + ECS task placement (~1 min). |

Litestream upgrade (separate PR, post-MVP) brings RPO under 60 seconds
by streaming WAL changes to S3 continuously.

## Single-AZ exposure

The L2 EBS volume is zonal: if `PublicSubnetA`'s AZ goes down, the L2 is
offline until manual recovery in another AZ. MVP-accepted trade-off.

**Manual cross-AZ restore (degraded — operator decision):**

1. Pick the most recent snapshot via `describe-snapshots` above.
2. Edit `deploy/aws/marketplace-l2.yaml` locally — change the
   `CqDataVolume.AvailabilityZone` from `!Select [0, !GetAZs ""]` to
   `!Select [1, !GetAZs ""]`, and change `EcsHost.SubnetId` from
   `!Ref PublicSubnetA` to `!Ref PublicSubnetB`.
3. Republish via the standard PR + merge flow (the CodeBuild publisher
   in `ci/buildspecs/marketplace-l2-publish.yml` syncs to S3).
4. Update the stack with the new template + `RestoreFromSnapshotId`
   pointing at the snapshot from step 1. CFN replaces both the volume
   (new AZ → new resource) and the EC2 host (new subnet → new resource).

Multi-AZ + concurrent-writer-safe substrate (Postgres on RDS) is tracked
as [#309](https://github.com/OneZero1ai/8th-layer-agent/issues/309)
+ [#311](https://github.com/OneZero1ai/8th-layer-agent/issues/311).

## SSH-less host access for inline diagnosis

`InstanceRole` includes `AmazonSSMManagedInstanceCore`, so the EC2 host
is reachable via Session Manager — no SSH keys, no bastion:

```
INSTANCE_ID=$(aws cloudformation describe-stack-resources \
  --profile <l2-profile> --region us-east-1 \
  --stack-name <L2-stack-name> \
  --logical-resource-id EcsHost \
  --query 'StackResources[0].PhysicalResourceId' --output text)
aws ssm start-session --profile <l2-profile> --region us-east-1 \
  --target "$INSTANCE_ID"
```

Inside the session: `ls /mnt/cq-data`, `sqlite3 /mnt/cq-data/cq.db
'PRAGMA integrity_check;'`, etc. For a live task shell (without
stopping the container): `aws ecs execute-command` against the running
task — `EnableExecuteCommand: true` is set on the service.
