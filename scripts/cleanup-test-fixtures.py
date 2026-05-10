#!/usr/bin/env python3
"""Teardown of 8th-Layer.ai test fixtures.

Default mode: --dry-run (read-only AWS calls, lists candidate resources).
Use --execute to actually delete (in safe ordering).
Use --filter <pattern> to scope to a single cluster (substring match on cluster name).

Keeps:
  - cq-directory-cluster (federated directory)
  - team-dw-l2-cluster (TeamDW production)
  - mvp-cluster (8th-layer dogfood)
  - Enterprise registrations: team-dw, 8th-layer
  - S3 bucket 8l-web-site-us-east-1-124074140789 (NEVER touched)
  - CodeStar Connection OneZero1ai-github (NEVER touched)
  - All IAM roles, Cognito user pools, CloudFront distros, ACM certs (NEVER touched)

Tears down (when --execute):
  - 6 test ECS clusters + their services + task definitions
  - 6 test ALBs + listeners + target groups
  - test-* security groups (Alb/Task/Efs SGs unique to each cluster)
  - test-* CloudWatch log groups
  - 6 test EFS file systems (test-*-efs)
  - Directory entries: BLOCKED — no DELETE endpoint exists, see report
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

try:
    import boto3
    import botocore.exceptions
except ImportError:
    print("ERROR: boto3 not available. pip install boto3", file=sys.stderr)
    sys.exit(2)


# ---- Configuration ----

AWS_PROFILE = "8th-layer-app"
AWS_REGION = "us-east-1"
ACCOUNT_ID = "124074140789"

# Clusters to tear down (exact names)
TEST_CLUSTERS = [
    "test-acme-fin-l2-cluster",
    "test-acme-eng-l2-cluster",
    "test-acme-sol-l2-cluster",
    "test-orion-eng-l2-cluster",
    "test-orion-sol-l2-cluster",
    "test-orion-gtm-l2-cluster",
]

# Cluster name -> short prefix used in ALB / SG / EFS names (CloudFormation truncates)
# We resolve by tag/name substring instead of hard-coding the random suffixes.
CLUSTER_TO_PREFIX = {
    "test-acme-fin-l2-cluster": "test-acme-fin-l2",
    "test-acme-eng-l2-cluster": "test-acme-eng-l2",
    "test-acme-sol-l2-cluster": "test-acme-sol-l2",
    "test-orion-eng-l2-cluster": "test-orion-eng-l2",
    "test-orion-sol-l2-cluster": "test-orion-sol-l2",
    "test-orion-gtm-l2-cluster": "test-orion-gtm-l2",
}

# Clusters / resources we explicitly preserve (defense-in-depth)
PROTECTED_CLUSTERS = {"cq-directory-cluster", "team-dw-l2-cluster", "mvp-cluster"}
PROTECTED_ENTERPRISE_IDS = {"team-dw", "8th-layer"}
PROTECTED_S3_BUCKETS = {"8l-web-site-us-east-1-124074140789"}

# Directory admin probe
DIRECTORY_BASE_URL = "https://directory.8th-layer.ai"

# Estimated monthly cost per test cluster (rough — see report)
# Fargate task: ~$15/mo for 0.25vCPU+0.5GB at ~50% util
# ALB: ~$22/mo idle + LCU
# EFS: small, ~$0.30/GB/mo, all <10MB so <$0.01
# Log groups: ~$0.50/mo each at ~10MB ingested
COST_PER_CLUSTER_MO_USD = 38.0  # task + alb + efs + logs + ENI


# ---- Audit log ----

@dataclass
class AuditLog:
    path: Path
    fh: object = None

    def __post_init__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a", encoding="utf-8")
        self.write(f"=== cleanup run started {datetime.now(UTC).isoformat()} ===")

    def write(self, line: str):
        ts = datetime.now(UTC).isoformat()
        self.fh.write(f"{ts}\t{line}\n")
        self.fh.flush()

    def action(self, kind: str, resource: str, action: str, result: str):
        self.write(f"{kind}\t{resource}\t{action}\t{result}")

    def close(self):
        self.write(f"=== cleanup run finished {datetime.now(UTC).isoformat()} ===")
        self.fh.close()


# ---- Inventory dataclasses ----

@dataclass
class ServiceInfo:
    arn: str
    name: str
    status: str
    desired: int
    running: int
    task_def: str


@dataclass
class ClusterInventory:
    cluster: str
    services: list[ServiceInfo] = field(default_factory=list)
    task_definitions: list[str] = field(default_factory=list)  # active TD ARNs
    log_groups: list[tuple[str, int]] = field(default_factory=list)  # (name, bytes)
    alb_arns: list[str] = field(default_factory=list)
    target_group_arns: list[str] = field(default_factory=list)
    listener_arns: list[str] = field(default_factory=list)
    security_group_ids: list[tuple[str, str]] = field(default_factory=list)  # (id, name)
    efs_ids: list[tuple[str, str, int]] = field(default_factory=list)  # (id, name, bytes)
    efs_mount_targets: list[str] = field(default_factory=list)


# ---- AWS helpers ----

def make_session() -> boto3.Session:
    return boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)


def cluster_exists(ecs, cluster: str) -> bool:
    resp = ecs.describe_clusters(clusters=[cluster])
    if not resp["clusters"]:
        return False
    return resp["clusters"][0]["status"] != "INACTIVE"


def collect_inventory(session: boto3.Session, cluster: str) -> ClusterInventory:
    inv = ClusterInventory(cluster=cluster)
    prefix = CLUSTER_TO_PREFIX[cluster]

    ecs = session.client("ecs")
    elbv2 = session.client("elbv2")
    ec2 = session.client("ec2")
    logs = session.client("logs")
    efs = session.client("efs")

    if not cluster_exists(ecs, cluster):
        return inv  # empty inventory; idempotent skip

    # Services
    svc_arns = ecs.list_services(cluster=cluster).get("serviceArns", [])
    if svc_arns:
        descs = ecs.describe_services(cluster=cluster, services=svc_arns)["services"]
        for s in descs:
            inv.services.append(ServiceInfo(
                arn=s["serviceArn"],
                name=s["serviceName"],
                status=s["status"],
                desired=s["desiredCount"],
                running=s["runningCount"],
                task_def=s["taskDefinition"],
            ))

    # Active task definition families matching the cluster prefix
    paginator = ecs.get_paginator("list_task_definitions")
    for page in paginator.paginate(familyPrefix=prefix, status="ACTIVE"):
        inv.task_definitions.extend(page.get("taskDefinitionArns", []))

    # ALBs (tag/name substring on prefix's first 7 chars due to CFN truncation)
    truncated = prefix[:7]  # e.g. "test-ac"
    lbs = elbv2.describe_load_balancers()["LoadBalancers"]
    matching_lbs = []
    for lb in lbs:
        if lb["LoadBalancerName"].startswith(truncated):
            # Verify by tag rather than name only — name truncation collides
            try:
                tags_resp = elbv2.describe_tags(ResourceArns=[lb["LoadBalancerArn"]])
                tags = {t["Key"]: t["Value"] for t in tags_resp["TagDescriptions"][0]["Tags"]}
                stack = tags.get("aws:cloudformation:stack-name", "")
                if stack.startswith(prefix) or any(prefix in v for v in tags.values()):
                    matching_lbs.append(lb)
            except botocore.exceptions.ClientError:
                pass
    for lb in matching_lbs:
        inv.alb_arns.append(lb["LoadBalancerArn"])
        # Listeners
        for lst in elbv2.describe_listeners(LoadBalancerArn=lb["LoadBalancerArn"])["Listeners"]:
            inv.listener_arns.append(lst["ListenerArn"])
        # Target groups
        for tg in elbv2.describe_target_groups(LoadBalancerArn=lb["LoadBalancerArn"])["TargetGroups"]:
            inv.target_group_arns.append(tg["TargetGroupArn"])

    # Security groups: name starts with prefix
    sgs = ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [f"{prefix}-*"]}]
    )["SecurityGroups"]
    for sg in sgs:
        inv.security_group_ids.append((sg["GroupId"], sg["GroupName"]))

    # CloudWatch log groups: substring on prefix slug (without "-cluster")
    log_prefix = f"/aws/ecs/{prefix}"
    for lg in logs.describe_log_groups(logGroupNamePrefix=log_prefix).get("logGroups", []):
        inv.log_groups.append((lg["logGroupName"], lg.get("storedBytes", 0)))

    # EFS file systems: name tag matches f"{prefix}-efs"
    fss = efs.describe_file_systems()["FileSystems"]
    for fs in fss:
        name = next((t["Value"] for t in fs.get("Tags", []) if t["Key"] == "Name"), fs.get("Name", ""))
        if name == f"{prefix}-efs":
            inv.efs_ids.append((fs["FileSystemId"], name, fs.get("SizeInBytes", {}).get("Value", 0)))
            mts = efs.describe_mount_targets(FileSystemId=fs["FileSystemId"]).get("MountTargets", [])
            inv.efs_mount_targets.extend(mt["MountTargetId"] for mt in mts)

    return inv


# ---- Dry-run rendering ----

def render_table(inventories: list[ClusterInventory]) -> str:
    rows = []
    rows.append(("CLUSTER", "RESOURCE TYPE", "ID/NAME", "STATE/SIZE"))
    for inv in inventories:
        if not inv.services and not inv.alb_arns and not inv.task_definitions \
                and not inv.log_groups and not inv.security_group_ids and not inv.efs_ids:
            rows.append((inv.cluster, "(cluster)", inv.cluster, "ABSENT — skipped"))
            continue
        rows.append((inv.cluster, "ECS cluster", inv.cluster, "ACTIVE"))
        for s in inv.services:
            rows.append((inv.cluster, "ECS service", s.name,
                         f"{s.status} desired={s.desired} running={s.running}"))
        for td in inv.task_definitions:
            rows.append((inv.cluster, "Task def (active)", td.split("/")[-1], "ACTIVE"))
        for arn in inv.alb_arns:
            rows.append((inv.cluster, "ALB", arn.split("/")[-2], "active"))
        for arn in inv.listener_arns:
            rows.append((inv.cluster, "  Listener", arn.split("/")[-1], ""))
        for arn in inv.target_group_arns:
            rows.append((inv.cluster, "  Target group", arn.split("/")[-2], ""))
        for sgid, sgname in inv.security_group_ids:
            rows.append((inv.cluster, "Security group", f"{sgid} ({sgname})", ""))
        for fsid, fsname, bytesz in inv.efs_ids:
            rows.append((inv.cluster, "EFS file system", f"{fsid} ({fsname})", f"{bytesz} bytes"))
        for lg, bytesz in inv.log_groups:
            rows.append((inv.cluster, "CW log group", lg, f"{bytesz} bytes"))

    # column widths
    widths = [max(len(str(r[i])) for r in rows) for i in range(4)]
    out = []
    sep = "  "
    out.append(sep.join(rows[0][i].ljust(widths[i]) for i in range(4)))
    out.append(sep.join("-" * widths[i] for i in range(4)))
    for r in rows[1:]:
        out.append(sep.join(str(r[i]).ljust(widths[i]) for i in range(4)))
    return "\n".join(out)


# ---- Directory probe ----

def probe_directory_admin() -> dict:
    """Return summary of directory admin endpoint surface and enterprise inventory."""
    summary = {
        "openapi_reachable": False,
        "delete_endpoints": [],
        "enterprises": [],
        "blocker": None,
    }
    try:
        with urllib.request.urlopen(f"{DIRECTORY_BASE_URL}/openapi.json", timeout=10) as r:
            spec = json.loads(r.read())
        summary["openapi_reachable"] = True
        for path, ops in spec.get("paths", {}).items():
            for method in ops:
                if method.lower() == "delete":
                    summary["delete_endpoints"].append(f"{method.upper()} {path}")
    except urllib.error.URLError as e:
        summary["blocker"] = f"openapi unreachable: {e}"
        return summary

    try:
        with urllib.request.urlopen(f"{DIRECTORY_BASE_URL}/api/v1/directory/enterprises", timeout=10) as r:
            data = json.loads(r.read())
        summary["enterprises"] = [
            {"id": e["enterprise_id"], "name": e["display_name"]}
            for e in data.get("enterprises", [])
        ]
    except urllib.error.URLError as e:
        summary["blocker"] = f"enterprise list unreachable: {e}"

    if not summary["delete_endpoints"]:
        summary["blocker"] = (
            "No DELETE endpoints exposed in directory OpenAPI. "
            "Cannot remove enterprises via API. Operator decision needed: "
            "(a) add DELETE /admin/api/enterprises/{id} to cq-directory server, "
            "(b) leave test entries in place as historical, "
            "(c) accept SQL-against-RDS as a one-shot — explicitly out of scope here."
        )
    return summary


# ---- Execution (mutation) helpers ----

def execute_cluster_teardown(session: boto3.Session, inv: ClusterInventory, audit: AuditLog):
    ecs = session.client("ecs")
    elbv2 = session.client("elbv2")
    ec2 = session.client("ec2")
    logs = session.client("logs")
    efs = session.client("efs")
    cluster = inv.cluster

    # 1. Scale services to 0, then delete services
    for s in inv.services:
        try:
            ecs.update_service(cluster=cluster, service=s.name, desiredCount=0)
            audit.action("ECS_SERVICE", s.arn, "scale_to_zero", "ok")
        except botocore.exceptions.ClientError as e:
            audit.action("ECS_SERVICE", s.arn, "scale_to_zero", f"error: {e}")

    # Wait for tasks to stop
    if inv.services:
        waiter = ecs.get_waiter("services_stable")
        try:
            waiter.wait(cluster=cluster, services=[s.name for s in inv.services],
                        WaiterConfig={"Delay": 15, "MaxAttempts": 40})
        except botocore.exceptions.WaiterError as e:
            audit.action("ECS_SERVICE", cluster, "wait_stable", f"error: {e}")

    for s in inv.services:
        try:
            ecs.delete_service(cluster=cluster, service=s.name, force=True)
            audit.action("ECS_SERVICE", s.arn, "delete_service", "ok")
        except botocore.exceptions.ClientError as e:
            if "ServiceNotFoundException" in str(e):
                audit.action("ECS_SERVICE", s.arn, "delete_service", "skipped (not found)")
            else:
                audit.action("ECS_SERVICE", s.arn, "delete_service", f"error: {e}")

    # 2. Deregister task definitions
    for td in inv.task_definitions:
        try:
            ecs.deregister_task_definition(taskDefinition=td)
            audit.action("ECS_TASKDEF", td, "deregister", "ok")
        except botocore.exceptions.ClientError as e:
            audit.action("ECS_TASKDEF", td, "deregister", f"error: {e}")

    # 3. ALB: delete listeners, then LB
    for lst_arn in inv.listener_arns:
        try:
            elbv2.delete_listener(ListenerArn=lst_arn)
            audit.action("ALB_LISTENER", lst_arn, "delete", "ok")
        except botocore.exceptions.ClientError as e:
            if "ListenerNotFound" in str(e):
                audit.action("ALB_LISTENER", lst_arn, "delete", "skipped (not found)")
            else:
                audit.action("ALB_LISTENER", lst_arn, "delete", f"error: {e}")

    for lb_arn in inv.alb_arns:
        try:
            elbv2.delete_load_balancer(LoadBalancerArn=lb_arn)
            audit.action("ALB", lb_arn, "delete", "ok")
        except botocore.exceptions.ClientError as e:
            if "LoadBalancerNotFound" in str(e):
                audit.action("ALB", lb_arn, "delete", "skipped (not found)")
            else:
                audit.action("ALB", lb_arn, "delete", f"error: {e}")

    # Wait for LB deletion before TG removal
    for lb_arn in inv.alb_arns:
        waiter = elbv2.get_waiter("load_balancers_deleted")
        try:
            waiter.wait(LoadBalancerArns=[lb_arn], WaiterConfig={"Delay": 15, "MaxAttempts": 40})
        except botocore.exceptions.WaiterError as e:
            audit.action("ALB", lb_arn, "wait_deleted", f"error: {e}")

    # 4. Target groups
    for tg_arn in inv.target_group_arns:
        try:
            elbv2.delete_target_group(TargetGroupArn=tg_arn)
            audit.action("TG", tg_arn, "delete", "ok")
        except botocore.exceptions.ClientError as e:
            if "TargetGroupNotFound" in str(e):
                audit.action("TG", tg_arn, "delete", "skipped (not found)")
            else:
                audit.action("TG", tg_arn, "delete", f"error: {e}")

    # 5. EFS — mount targets first, then file system
    for mt in inv.efs_mount_targets:
        try:
            efs.delete_mount_target(MountTargetId=mt)
            audit.action("EFS_MT", mt, "delete", "ok")
        except botocore.exceptions.ClientError as e:
            if "MountTargetNotFound" in str(e):
                audit.action("EFS_MT", mt, "delete", "skipped (not found)")
            else:
                audit.action("EFS_MT", mt, "delete", f"error: {e}")
    # wait for mount targets to clear
    for fsid, _, _ in inv.efs_ids:
        for _ in range(40):
            mts = efs.describe_mount_targets(FileSystemId=fsid).get("MountTargets", [])
            if not mts:
                break
            time.sleep(15)
    for fsid, fsname, _ in inv.efs_ids:
        try:
            efs.delete_file_system(FileSystemId=fsid)
            audit.action("EFS", fsid, "delete", "ok")
        except botocore.exceptions.ClientError as e:
            if "FileSystemNotFound" in str(e):
                audit.action("EFS", fsid, "delete", "skipped (not found)")
            else:
                audit.action("EFS", fsid, "delete", f"error: {e}")

    # 6. Security groups (after ALB+ENI cleanup)
    for sgid, sgname in inv.security_group_ids:
        try:
            ec2.delete_security_group(GroupId=sgid)
            audit.action("SG", f"{sgid} ({sgname})", "delete", "ok")
        except botocore.exceptions.ClientError as e:
            if "InvalidGroup.NotFound" in str(e):
                audit.action("SG", sgid, "delete", "skipped (not found)")
            else:
                audit.action("SG", f"{sgid} ({sgname})", "delete", f"error: {e}")

    # 7. CloudWatch log groups
    for lg, _ in inv.log_groups:
        try:
            logs.delete_log_group(logGroupName=lg)
            audit.action("LOGS", lg, "delete", "ok")
        except botocore.exceptions.ClientError as e:
            if "ResourceNotFoundException" in str(e):
                audit.action("LOGS", lg, "delete", "skipped (not found)")
            else:
                audit.action("LOGS", lg, "delete", f"error: {e}")

    # 8. Finally the cluster itself
    try:
        ecs.delete_cluster(cluster=cluster)
        audit.action("ECS_CLUSTER", cluster, "delete", "ok")
    except botocore.exceptions.ClientError as e:
        if "ClusterNotFound" in str(e):
            audit.action("ECS_CLUSTER", cluster, "delete", "skipped (not found)")
        else:
            audit.action("ECS_CLUSTER", cluster, "delete", f"error: {e}")


# ---- Main ----

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="Read-only listing of resources to delete (default).")
    ap.add_argument("--execute", action="store_true",
                    help="Actually mutate AWS state. Disables --dry-run.")
    ap.add_argument("--filter", default=None,
                    help="Substring filter on cluster name (e.g. 'orion-eng').")
    ap.add_argument("--log-dir", default="scripts/cleanup/logs",
                    help="Where to write the audit log (default scripts/cleanup/logs).")
    args = ap.parse_args()

    if args.execute:
        args.dry_run = False

    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    log_path = Path(args.log_dir) / f"cleanup-{ts}.log"
    audit = AuditLog(path=log_path)
    audit.write(f"mode={'execute' if args.execute else 'dry-run'} filter={args.filter}")

    # Defense-in-depth: refuse if filter would match a protected cluster
    target_clusters = TEST_CLUSTERS
    if args.filter:
        target_clusters = [c for c in TEST_CLUSTERS if args.filter in c]
        if not target_clusters:
            print(f"Filter '{args.filter}' matched no test clusters.", file=sys.stderr)
            audit.close()
            return 1
    for pc in PROTECTED_CLUSTERS:
        if args.filter and args.filter in pc:
            print(f"REFUSING: filter '{args.filter}' matches protected cluster '{pc}'", file=sys.stderr)
            audit.close()
            return 2

    print(f"# Cleanup ({'DRY-RUN' if args.dry_run else 'EXECUTE'})  account={ACCOUNT_ID} region={AWS_REGION}")
    print(f"# Audit log: {log_path}")
    print(f"# Targets: {', '.join(target_clusters)}")
    print()

    session = make_session()

    # Inventory phase (read-only)
    inventories = []
    for c in target_clusters:
        print(f"# Inspecting {c} ...", file=sys.stderr)
        inv = collect_inventory(session, c)
        inventories.append(inv)
        for s in inv.services:
            audit.action("ECS_SERVICE", s.arn, "would_delete" if args.dry_run else "queued",
                         f"running={s.running}")
        for arn in inv.alb_arns:
            audit.action("ALB", arn, "would_delete" if args.dry_run else "queued", "")
        for arn in inv.target_group_arns:
            audit.action("TG", arn, "would_delete" if args.dry_run else "queued", "")
        for sgid, sgname in inv.security_group_ids:
            audit.action("SG", f"{sgid} ({sgname})", "would_delete" if args.dry_run else "queued", "")
        for fsid, fsname, _ in inv.efs_ids:
            audit.action("EFS", f"{fsid} ({fsname})", "would_delete" if args.dry_run else "queued", "")
        for lg, _ in inv.log_groups:
            audit.action("LOGS", lg, "would_delete" if args.dry_run else "queued", "")
        audit.action("ECS_CLUSTER", c, "would_delete" if args.dry_run else "queued", "")

    print(render_table(inventories))
    print()

    # Cost estimate
    active_count = sum(1 for inv in inventories if inv.services)
    print(f"# Estimated savings: ~${active_count * COST_PER_CLUSTER_MO_USD:.2f}/month "
          f"({active_count} clusters x ~${COST_PER_CLUSTER_MO_USD}/mo each).")
    print("#   - Fargate task: ~$15/mo  ALB: ~$22/mo  ENI: ~$3/mo  Logs: ~$0.50/mo")
    print()

    # Directory probe
    print("# Directory cleanup probe:")
    dprobe = probe_directory_admin()
    print(f"#   openapi_reachable: {dprobe['openapi_reachable']}")
    print(f"#   delete_endpoints: {dprobe['delete_endpoints'] or '(none)'}")
    test_enterprises = [
        e for e in dprobe["enterprises"]
        if e["id"] not in PROTECTED_ENTERPRISE_IDS
    ]
    print(f"#   non-protected enterprises ({len(test_enterprises)}):")
    for e in test_enterprises:
        print(f"     - {e['id']}: {e['name']}")
    if dprobe["blocker"]:
        print(f"#   BLOCKER: {dprobe['blocker']}")
        audit.action("DIRECTORY", "*", "blocked", dprobe["blocker"])
    print()

    if args.dry_run:
        print("# DRY-RUN complete. Re-run with --execute to actually delete.")
        audit.close()
        return 0

    # Execute phase
    print("# EXECUTE: tearing down ...")
    for inv in inventories:
        if inv.cluster in PROTECTED_CLUSTERS:
            audit.action("ECS_CLUSTER", inv.cluster, "skip", "protected")
            continue
        if not inv.services and not inv.alb_arns:
            audit.action("ECS_CLUSTER", inv.cluster, "skip", "already absent")
            continue
        execute_cluster_teardown(session, inv, audit)

    audit.close()
    print(f"# Done. Audit log: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
