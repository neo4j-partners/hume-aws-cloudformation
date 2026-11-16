#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3[crt]", "python-dotenv"]
# ///
"""Prove the Hume ECS claims, then check that cleanup left nothing.

Each subcommand produces the evidence for one proof and prints it as plain
lines. Lines that start with HYPOTHESIS are assumptions the run could not
check on its own.

    egress            block docker.graphaware.com, replace a task, show the
                      replacement starts from the cached image
    credentials       confirm no registry credential file is on the instance
    replace-instance  terminate the instance, time the recovery, record what
                      data is lost
    soak              record task restarts, memory and CPU over a period
    teardown-check    list what is left after deploy.py teardown
    delete-ami        deregister the Hume AMI and delete its snapshots

Commands that change anything ask first. Remote commands use SSM Run Command
(AWS-RunShellScript), which needs only the SSM agent and the instance role's
SsmAgent policy.
"""

import argparse
import ipaddress
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

# The repository modules imported below live in these directories.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_IMPORT_DIRS = [str(_REPO_ROOT / name) for name in ("stack",)]
sys.path[:0] = [entry for entry in _IMPORT_DIRS if entry not in sys.path]

from deploy import (
    DEFAULT_PROFILE,
    DEFAULT_REGION,
    DEPLOY_DIR,
    LICENCE_SECRET_NAME,
    STACK_TAGS,
    make_session,
    newest_stack,
    read_record,
    record_path,
)

REGISTRY_HOST = "docker.graphaware.com"
RULE_COMMENT = "hume-prove-egress"
PROJECT_TAG = next(t["Value"] for t in STACK_TAGS if t["Key"] == "Project")
AMI_PURPOSE = "hume-ami"
# deploy.py keeps this secret on purpose, outside the per-stack prefix.
EXPECTED_SECRETS = {LICENCE_SECRET_NAME}
SSM_IN_PROGRESS = {"Pending", "InProgress", "Delayed"}
POLL_SECONDS = 15
SSM_SAMPLE_SECONDS = 300
CLOUDWATCH_LAG_SECONDS = 150


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@dataclass
class Stack:
    name: str
    session: boto3.Session
    cluster: str
    asg: str


@dataclass
class CommandResult:
    status: str
    stdout: str
    stderr: str


def say(text: str = "") -> None:
    print(text, flush=True)


def confirm(question: str, assume_yes: bool) -> None:
    if assume_yes:
        return
    if input(f"{question} [y/N] ").strip().lower() != "y":
        sys.exit("Stopped.")


def load_stack(args: argparse.Namespace) -> Stack:
    name = args.stack_name or newest_stack()
    session = make_session(args, read_record(name))
    cfn = session.client("cloudformation")
    stack = cfn.describe_stacks(StackName=name)["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    for key in ("ClusterName", "AutoScalingGroupName"):
        if key not in outputs:
            sys.exit(f"Stack {name} has no {key} output. Is it fully created?")
    return Stack(
        name=name,
        session=session,
        cluster=outputs["ClusterName"],
        asg=outputs["AutoScalingGroupName"],
    )


def run_on_instance(
    session: boto3.Session, instance_id: str, script: str, timeout: int = 300
) -> CommandResult:
    ssm = session.client("ssm")
    command_id = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [script]},
        TimeoutSeconds=max(30, timeout),
    )["Command"]["CommandId"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(3)
        try:
            result = ssm.get_command_invocation(
                CommandId=command_id, InstanceId=instance_id
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "InvocationDoesNotExist":
                continue
            raise
        if result["Status"] not in SSM_IN_PROGRESS:
            return CommandResult(
                result["Status"],
                result["StandardOutputContent"],
                result["StandardErrorContent"],
            )
    sys.exit(f"SSM command {command_id} did not finish in {timeout}s")


def active_instance_id(stack: Stack) -> str:
    ecs = stack.session.client("ecs")
    arns = ecs.list_container_instances(cluster=stack.cluster, status="ACTIVE")[
        "containerInstanceArns"
    ]
    if len(arns) != 1:
        sys.exit(f"Expected one ACTIVE container instance, found {len(arns)}")
    described = ecs.describe_container_instances(
        cluster=stack.cluster, containerInstances=arns
    )["containerInstances"][0]
    return described["ec2InstanceId"]


def describe_all_services(ecs, cluster: str) -> list[dict]:
    arns = [
        arn
        for page in ecs.get_paginator("list_services").paginate(cluster=cluster)
        for arn in page["serviceArns"]
    ]
    services: list[dict] = []
    for start in range(0, len(arns), 10):
        services += ecs.describe_services(
            cluster=cluster, services=arns[start : start + 10]
        )["services"]
    return services


def service_line(service: dict) -> str:
    return (
        f"{service['serviceName']}: running {service['runningCount']} "
        f"of desired {service['desiredCount']}"
    )


def running_task_arns(ecs, cluster: str, service: str) -> list[str]:
    return ecs.list_tasks(
        cluster=cluster, serviceName=service, desiredStatus="RUNNING"
    )["taskArns"]


def target_health(session: boto3.Session, services: list[dict]) -> dict[str, str]:
    elb = session.client("elbv2")
    states: dict[str, str] = {}
    groups = {
        lb["targetGroupArn"]
        for service in services
        for lb in service.get("loadBalancers", [])
        if "targetGroupArn" in lb
    }
    for group in groups:
        descriptions = elb.describe_target_health(TargetGroupArn=group)[
            "TargetHealthDescriptions"
        ]
        states[group.split("/")[1]] = (
            ",".join(sorted(d["TargetHealth"]["State"] for d in descriptions))
            or "no targets"
        )
    return states


def minutes(seconds: float) -> str:
    return f"{seconds / 60:.1f} min"


# ---------------------------------------------------------------------------
# egress: block the registry on the host, replace a task, show a cached start
# ---------------------------------------------------------------------------
# Why an iptables rule on the instance. Security groups and network ACLs match
# addresses, not hostnames. The instance reaches AWS through the Neo4j VPC's
# endpoints and NAT gateways on 443, so removing 443 egress from the instance
# security group would also cut the ECS agent, Secrets Manager and CloudWatch
# Logs, and the replacement task could fail for a reason that has nothing to do
# with the image cache. A host rule that rejects only the registry's addresses
# removes exactly one thing.
#
# HYPOTHESIS: iptables is installed (ecs-init uses it for the task credential
#   endpoint), and dockerd image pulls leave the host through the OUTPUT chain.
#   The control step below checks the second one: a docker pull of a name that
#   does not exist must change from an authentication error to a connection
#   error once the rule is in.
# HYPOTHESIS: the registry addresses are stable for the length of the run. The
#   name is resolved five times and every address is blocked. The rule hit
#   counter shows whether anything tried to reach a blocked address.

PRECHECK_SCRIPT = f"""#!/bin/bash
command -v iptables >/dev/null || {{ echo NO_IPTABLES; exit 3; }}
iptables --version
for _ in 1 2 3 4 5; do
  getent ahostsv4 {REGISTRY_HOST} | awk '{{print "IP " $1}}'
  sleep 1
done | sort -u > /tmp/prove-ips
cat /tmp/prove-ips
awk '{{print $2}}' /tmp/prove-ips | while read -r ip; do
  echo "PTR $ip $(getent hosts "$ip" | awk '{{print $2}}' | head -n 1)"
done
rm -f /tmp/prove-ips
"""

PROBE_SCRIPT = f"""#!/bin/bash
out=$(curl -sS -o /dev/null -m 8 -w '%{{http_code}}' https://{REGISTRY_HOST}/v2/ 2>&1)
echo "curl https://{REGISTRY_HOST}/v2/ -> $out (exit $?)"
out=$(timeout 90 docker pull {REGISTRY_HOST}/hume-core/prove-block-check:none 2>&1 | tail -n 2)
echo "docker pull of a name that does not exist -> $out"
"""

COUNTER_SCRIPT = f"""#!/bin/bash
iptables -L OUTPUT -n -v -x | grep -E 'pkts|{RULE_COMMENT}'
"""

REMOVE_RULES_SCRIPT = f"""#!/bin/bash
while iptables -S OUTPUT | grep -q {RULE_COMMENT}; do
  rule=$(iptables -S OUTPUT | grep -m1 {RULE_COMMENT} | sed 's/^-A/-D/')
  eval "iptables $rule" || break
done
echo "rules left: $(iptables -S OUTPUT | grep -c {RULE_COMMENT})"
echo "OUTPUT chain after removal:"
iptables -S OUTPUT
"""


def add_rules_script(addresses: list[str]) -> str:
    rules = "\n".join(
        f"iptables -I OUTPUT 1 -d {address} -p tcp --dport 443 "
        f"-m comment --comment {RULE_COMMENT} -j REJECT"
        for address in addresses
    )
    return f"#!/bin/bash\nset -e\n{rules}\niptables -S OUTPUT | grep {RULE_COMMENT}\n"


def registry_addresses(output: str) -> list[str]:
    found = re.findall(r"^IP (\S+)$", output, flags=re.MULTILINE)
    addresses = sorted({str(ipaddress.IPv4Address(ip)) for ip in found})
    if not addresses:
        sys.exit(f"Could not resolve {REGISTRY_HOST} on the instance:\n{output}")
    return addresses


def parse_counters(output: str) -> dict[str, int]:
    """Packets per blocked address, from iptables -L OUTPUT -n -v -x lines."""
    counts: dict[str, int] = {}
    for line in output.splitlines():
        if RULE_COMMENT not in line:
            continue
        fields = line.split()
        counts[fields[8]] = int(fields[0])
    return counts


def counter_deltas(baseline: dict[str, int], final: dict[str, int]) -> dict[str, int]:
    """Packets counted between two readings. Both must show the same rules."""
    if not baseline or baseline.keys() != final.keys():
        sys.exit(f"The rules changed between readings: {baseline} then {final}")
    return {address: final[address] - baseline[address] for address in baseline}


def wait_for_new_running_task(
    ecs, cluster: str, service: str, before: set[str], timeout: int
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        arns = [a for a in running_task_arns(ecs, cluster, service) if a not in before]
        if arns:
            task = ecs.describe_tasks(cluster=cluster, tasks=arns)["tasks"][0]
            if task["lastStatus"] == "RUNNING":
                return task
        time.sleep(5)
    sys.exit(f"No replacement task for {service} reached RUNNING in {timeout}s")


def cmd_egress(args: argparse.Namespace) -> int:
    stack = load_stack(args)
    ecs = stack.session.client("ecs")
    instance_id = active_instance_id(stack)
    before = set(running_task_arns(ecs, stack.cluster, args.service))
    if not before:
        sys.exit(f"Service {args.service} has no running task to replace")

    say(f"Stack {stack.name}, instance {instance_id}, service {args.service}")
    say("This stops one task of that service. The service starts a replacement.")
    say(f"It adds and then removes a host firewall rule for {REGISTRY_HOST}.")
    confirm("Proceed?", args.yes)

    pre = run_on_instance(stack.session, instance_id, PRECHECK_SCRIPT)
    if pre.status != "Success":
        sys.exit(f"Precheck failed ({pre.status}):\n{pre.stdout}{pre.stderr}")
    addresses = registry_addresses(pre.stdout)
    say(f"iptables present. {REGISTRY_HOST} resolves to {', '.join(addresses)}")
    say("Reverse names, to judge whether the addresses are shared:")
    for line in re.findall(r"^PTR .*$", pre.stdout, flags=re.MULTILINE):
        say(f"  {line}")
    say("HYPOTHESIS: if these names belong to a shared CDN, the block also cuts")
    say("  other sites on the same addresses. Read the names before trusting it.")

    control = run_on_instance(stack.session, instance_id, PROBE_SCRIPT, 180)
    say("Control before the block (expect an HTTP status or an auth error):")
    say(control.stdout.rstrip())

    try:
        rules = run_on_instance(stack.session, instance_id, add_rules_script(addresses))
        if rules.status != "Success":
            sys.exit(f"Could not add the rules ({rules.status}): {rules.stderr}")
        say("Rules in place:")
        say(rules.stdout.rstrip())

        blocked = run_on_instance(stack.session, instance_id, PROBE_SCRIPT, 180)
        say("Control after the block (expect connection refused or exit 7):")
        say(blocked.stdout.rstrip())
        baseline = run_on_instance(stack.session, instance_id, COUNTER_SCRIPT)
        say("Rule hit counters after the probes, before the task stops:")
        say(baseline.stdout.rstrip())

        old_arn = min(before)
        ecs.stop_task(
            cluster=stack.cluster, task=old_arn, reason="prove.py egress test"
        )
        say(f"Stopped task {old_arn.split('/')[-1]}. Waiting for the replacement.")
        task = wait_for_new_running_task(
            ecs, stack.cluster, args.service, before, args.timeout
        )
        final = run_on_instance(stack.session, instance_id, COUNTER_SCRIPT)
    finally:
        cleanup = run_on_instance(stack.session, instance_id, REMOVE_RULES_SCRIPT)
        say(f"Rule removal: {cleanup.stdout.strip() or cleanup.stderr.strip()}")
        if "rules left: 0" not in cleanup.stdout:
            say(
                "WARNING: the block may still be in place. On the instance run "
                f"iptables -S OUTPUT and delete every rule with comment {RULE_COMMENT}."
            )

    pulled = [task.get("pullStartedAt"), task.get("pullStoppedAt")]
    window = (
        f"{(pulled[1] - pulled[0]).total_seconds():.1f}s" if all(pulled) else "none"
    )
    say("")
    say("RESULT egress")
    say(f"  replacement task: {task['taskArn'].split('/')[-1]} {task['lastStatus']}")
    for container in task["containers"]:
        say(f"  container {container['name']}: {container['image']}")
        say(f"    digest {container.get('imageDigest', 'not reported')}")
    say(f"  created {task['createdAt']}, started {task.get('startedAt')}")
    say(f"  image pull window: {window}")
    deltas = counter_deltas(
        parse_counters(baseline.stdout), parse_counters(final.stdout)
    )
    say("  rule hit counters after the replacement is RUNNING:")
    say(final.stdout.rstrip())
    say("  packets to each blocked address since the reading after the probes:")
    for address, delta in deltas.items():
        say(f"    {address}: {delta}")
    if not any(deltas.values()) and window == "none":
        say("  verdict: delta 0 on every rule, the task is RUNNING, and ECS records")
        say("  no image pull. Nothing tried to reach the registry during the start.")
    else:
        say("  verdict: the delta is not 0 or ECS recorded an image pull. Read both")
        say("  before claiming the cache served the task.")
        say("HYPOTHESIS: other senders to these addresses are the ECS agent, dockerd")
        say("  checking the image digest, or another task starting on the host.")
        say("  The counters cannot tell them apart.")
    return 0


# ---------------------------------------------------------------------------
# credentials: nothing that can log in to the registry is on the instance
# ---------------------------------------------------------------------------

CREDENTIAL_SCRIPT = f"""#!/bin/bash
check() {{ if (eval "$2") >/dev/null 2>&1; then echo "PASS $1"; else echo "FAIL $1"; fi; }}
check "no docker config.json anywhere" \\
  "! find / -xdev -name config.json -path '*.docker*' 2>/dev/null | grep -q ."
check "no registry auth string on disk" \\
  "! grep -rIlE '\\"auth\\": *\\"' /root /home /etc /opt /var/lib/ecs 2>/dev/null | grep -q ."
check "no registry host under /root or /home" \\
  "! grep -rIl {REGISTRY_HOST} /root /home 2>/dev/null | grep -q ."
check "no ecs.config registry auth" \\
  "! grep -qiE 'ECS_ENGINE_AUTH' /etc/ecs/ecs.config"
check "no authorized_keys with content" \\
  "! find /root/.ssh /home/*/.ssh -name authorized_keys -size +0 2>/dev/null | grep -q ."
check "no shell history" "! ls /root/.bash_history /home/*/.bash_history 2>/dev/null | grep -q ."
check "docker has no stored login" "! docker system info 2>/dev/null | grep -qi 'username'"
echo "--- listing"
find / -xdev \\( -name config.json -path '*.docker*' -o -name authorized_keys \\) \\
  -exec ls -la {{}} \\; 2>/dev/null
echo "--- ecs.config keys only"
cut -d= -f1 /etc/ecs/ecs.config
"""


def cmd_credentials(args: argparse.Namespace) -> int:
    stack = load_stack(args)
    instance_id = active_instance_id(stack)
    say(f"Stack {stack.name}, instance {instance_id}. Read-only checks.")
    result = run_on_instance(stack.session, instance_id, CREDENTIAL_SCRIPT, 600)
    say(result.stdout.rstrip())
    if result.stderr.strip():
        say(f"stderr: {result.stderr.strip()}")
    failed = [line for line in result.stdout.splitlines() if line.startswith("FAIL")]
    say("")
    say(f"RESULT credentials: {'FAIL' if failed else 'PASS'} ({len(failed)} failed)")
    say("Checked over SSM Run Command (AWS-RunShellScript), as root.")
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# replace-instance: terminate it and watch the stack recover
# ---------------------------------------------------------------------------

MARKER_PATH = "/opt/hume/data/prove-marker"
INVENTORY_SCRIPT = """#!/bin/bash
echo "--- host data, sizes only"
du -sh /opt/hume/data/* 2>/dev/null
echo "--- marker"
ls -la {marker} 2>&1
"""
WRITE_MARKER_SCRIPT = "#!/bin/bash\necho prove-{stamp} > {marker}\nls -la {marker}\n"


def database_lines(databases: list[dict], stack: str) -> list[str]:
    """Describe the databases of one stack. Their identifiers start with its name."""
    return [
        f"{db['DBInstanceIdentifier']} {db['DBInstanceStatus']} "
        f"created {db['InstanceCreateTime']:%Y-%m-%d %H:%M:%S}"
        for db in databases
        if db["DBInstanceIdentifier"].startswith(f"{stack}-")
        and any(
            tag["Key"] == "Project" and tag["Value"] == PROJECT_TAG
            for tag in db.get("TagList", [])
        )
    ]


def database_state(session: boto3.Session, stack: str) -> list[str]:
    rds = session.client("rds")
    return database_lines(rds.describe_db_instances()["DBInstances"], stack)


def root_volume_lines(session: boto3.Session, instance_id: str) -> list[str]:
    ec2 = session.client("ec2")
    instance = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0][
        "Instances"
    ][0]
    return [
        f"{m['DeviceName']} {m['Ebs']['VolumeId']} "
        f"DeleteOnTermination={m['Ebs']['DeleteOnTermination']}"
        for m in instance.get("BlockDeviceMappings", [])
    ]


def cmd_replace_instance(args: argparse.Namespace) -> int:
    stack = load_stack(args)
    session = stack.session
    ecs, asg = session.client("ecs"), session.client("autoscaling")
    old_id = active_instance_id(stack)

    say(f"Stack {stack.name}, instance {old_id}")
    volumes = root_volume_lines(session, old_id)
    say("Volumes attached to the instance:")
    for line in volumes:
        say(f"  {line}")
    say("Databases of this stack:")
    databases_before = database_state(session, stack.name)
    for line in databases_before:
        say(f"  {line}")
    services = describe_all_services(ecs, stack.cluster)
    say("Services before:")
    for service in services:
        say(f"  {service_line(service)}")

    say("This writes a marker file on the instance and then TERMINATES the instance.")
    confirm("Terminate it?", args.yes)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    marker = run_on_instance(
        session,
        old_id,
        WRITE_MARKER_SCRIPT.format(stamp=stamp, marker=MARKER_PATH),
    )
    say(marker.stdout.rstrip())
    inventory = run_on_instance(
        session, old_id, INVENTORY_SCRIPT.format(marker=MARKER_PATH)
    )
    say(inventory.stdout.rstrip())

    started = time.monotonic()
    session.client("ec2").terminate_instances(InstanceIds=[old_id])
    say(f"Terminated {old_id} at {datetime.now(UTC):%H:%M:%S}Z")

    marks: dict[str, float] = {}
    deadline = started + args.timeout * 60
    new_id = ""
    while time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        elapsed = time.monotonic() - started
        group = asg.describe_auto_scaling_groups(AutoScalingGroupNames=[stack.asg])[
            "AutoScalingGroups"
        ][0]
        fresh = [
            i
            for i in group["Instances"]
            if i["InstanceId"] != old_id and i["LifecycleState"] == "InService"
        ]
        if fresh and "asg_in_service" not in marks:
            new_id = fresh[0]["InstanceId"]
            marks["asg_in_service"] = elapsed
            say(f"  {minutes(elapsed)}: ASG has {new_id} InService")
        if new_id and "registered" not in marks:
            arns = ecs.list_container_instances(cluster=stack.cluster, status="ACTIVE")[
                "containerInstanceArns"
            ]
            if arns:
                described = ecs.describe_container_instances(
                    cluster=stack.cluster, containerInstances=arns
                )["containerInstances"]
                if any(c["ec2InstanceId"] == new_id for c in described):
                    marks["registered"] = elapsed
                    say(f"  {minutes(elapsed)}: {new_id} registered with ECS")
        if "registered" in marks:
            services = describe_all_services(ecs, stack.cluster)
            if all(s["runningCount"] >= s["desiredCount"] for s in services):
                marks["services_running"] = elapsed
                say(f"  {minutes(elapsed)}: every service is at its desired count")
                health = target_health(session, services)
                if all(set(v.split(",")) == {"healthy"} for v in health.values()):
                    marks["targets_healthy"] = elapsed
                    say(f"  {minutes(elapsed)}: load balancer targets {health}")
                    break
        else:
            say(f"  {minutes(elapsed)}: waiting")
    else:
        say("TIMEOUT: the stack did not fully recover in time. Read the state below.")

    if new_id:
        say("Checks on the new instance:")
        after = run_on_instance(
            session, new_id, INVENTORY_SCRIPT.format(marker=MARKER_PATH)
        )
        say(after.stdout.rstrip())
    old_state = ecs.list_container_instances(cluster=stack.cluster)[
        "containerInstanceArns"
    ]
    if old_state:
        for described in ecs.describe_container_instances(
            cluster=stack.cluster, containerInstances=old_state
        )["containerInstances"]:
            say(
                f"  container instance {described['ec2InstanceId']}: "
                f"{described['status']}, agentConnected={described['agentConnected']}"
            )
    databases_after = database_state(session, stack.name)

    say("")
    say("RESULT replace-instance")
    for name, seconds in marks.items():
        say(f"  {name}: {minutes(seconds)} after terminate")
    say(f"  old instance {old_id}, new instance {new_id or 'none'}")
    say(f"  database unchanged: {databases_before == databases_after}")
    say(f"  database after: {databases_after}")
    say("  Data lost: whatever was under /opt/hume/data (sizes above) if the")
    say("  marker file is absent on the new instance. The check above shows it.")
    say("HYPOTHESIS: the root volume has DeleteOnTermination=true, so the host")
    say("  data (orchestra, audit-logs, media) goes with the instance.")
    say("  RDS, Secrets Manager and CloudWatch Logs live outside the instance.")
    return 0 if "targets_healthy" in marks or "services_running" in marks else 1


# ---------------------------------------------------------------------------
# soak: restarts, memory and CPU over a period
# ---------------------------------------------------------------------------

SAMPLE_SCRIPT = """#!/bin/bash
echo "--- docker stats (name, memory, memory percent, cpu percent)"
docker stats --no-stream --format '{{.Name}} {{.MemUsage}} {{.MemPerc}} {{.CPUPerc}}'
echo "--- host"
free -m | awk 'NR==2 {print "mem used MiB " $3 " of " $2 ", available " $7}'
cut -d' ' -f1-3 /proc/loadavg
echo "ecs restarts: $(systemctl show ecs -p NRestarts --value)"
echo "docker restarts: $(systemctl show docker -p NRestarts --value)"
echo "oom lines in dmesg: $(dmesg 2>/dev/null | grep -ci 'out of memory')"
"""


def metric_queries(cluster: str, services: list[str]) -> list[dict]:
    scopes = [("cluster", [{"Name": "ClusterName", "Value": cluster}])]
    scopes += [
        (
            name,
            [
                {"Name": "ClusterName", "Value": cluster},
                {"Name": "ServiceName", "Value": name},
            ],
        )
        for name in services
    ]
    queries = []
    for label, dimensions in scopes:
        for metric in ("CPUUtilization", "MemoryUtilization"):
            for stat in ("Average", "Maximum"):
                queries.append(
                    {
                        "Id": f"q{len(queries)}",
                        "Label": f"{label} {metric} {stat}",
                        "MetricStat": {
                            "Metric": {
                                "Namespace": "AWS/ECS",
                                "MetricName": metric,
                                "Dimensions": dimensions,
                            },
                            "Period": 60,
                            "Stat": stat,
                        },
                    }
                )
    return queries


def print_metrics(
    session: boto3.Session,
    cluster: str,
    services: list[str],
    start: datetime,
    end: datetime,
) -> None:
    pages = (
        session.client("cloudwatch")
        .get_paginator("get_metric_data")
        .paginate(
            MetricDataQueries=metric_queries(cluster, services),
            StartTime=start,
            EndTime=end,
        )
    )
    values: dict[str, list[float]] = {}
    for page in pages:
        for result in page["MetricDataResults"]:
            values.setdefault(result["Label"], []).extend(result["Values"])
    for label, series in values.items():
        if not series:
            say(f"  {label}: no datapoints")
        elif label.endswith("Maximum"):
            say(f"  {label}: peak {max(series):.1f}% over {len(series)} minutes")
        else:
            say(
                f"  {label}: mean {statistics.fmean(series):.1f}%, "
                f"highest minute {max(series):.1f}% over {len(series)} minutes"
            )


def stopped_tasks_since(ecs, cluster: str, since: datetime) -> list[dict]:
    arns = [
        arn
        for page in ecs.get_paginator("list_tasks").paginate(
            cluster=cluster, desiredStatus="STOPPED"
        )
        for arn in page["taskArns"]
    ]
    tasks: list[dict] = []
    for start in range(0, len(arns), 100):
        tasks += ecs.describe_tasks(cluster=cluster, tasks=arns[start : start + 100])[
            "tasks"
        ]
    return [t for t in tasks if t.get("stoppedAt") and t["stoppedAt"] >= since]


def cmd_soak(args: argparse.Namespace) -> int:
    stack = load_stack(args)
    session = stack.session
    ecs = session.client("ecs")
    instance_id = active_instance_id(stack)
    names = sorted(s["serviceName"] for s in describe_all_services(ecs, stack.cluster))
    seen = {n: set(running_task_arns(ecs, stack.cluster, n)) for n in names}
    start = datetime.now(UTC)
    deadline = time.monotonic() + args.minutes * 60
    next_sample = time.monotonic()
    new_tasks: list[str] = []

    say(f"Stack {stack.name}, instance {instance_id}, {args.minutes} minutes")
    say(f"Services: {', '.join(names)}")
    while time.monotonic() < deadline:
        if time.monotonic() >= next_sample:
            sample = run_on_instance(session, instance_id, SAMPLE_SCRIPT, 120)
            say(f"--- host sample at {datetime.now(UTC):%H:%M:%S}Z")
            say(sample.stdout.rstrip())
            next_sample = time.monotonic() + SSM_SAMPLE_SECONDS
        for name in names:
            current = set(running_task_arns(ecs, stack.cluster, name))
            for arn in sorted(current - seen[name]):
                new_tasks.append(f"{name} {arn.split('/')[-1]}")
                say(f"  NEW TASK for {name}: {arn.split('/')[-1]}")
            seen[name] |= current
        time.sleep(60)

    say(f"Waiting {CLOUDWATCH_LAG_SECONDS}s for CloudWatch to catch up")
    time.sleep(CLOUDWATCH_LAG_SECONDS)
    end = datetime.now(UTC)
    stopped = stopped_tasks_since(ecs, stack.cluster, start)

    say("")
    say(f"RESULT soak {start:%H:%M:%S}Z to {end:%H:%M:%S}Z")
    say(f"  new tasks seen after the start: {len(new_tasks)}")
    for line in new_tasks:
        say(f"    {line}")
    say(f"  tasks stopped after the start: {len(stopped)}")
    for task in stopped:
        containers = ", ".join(
            f"{c['name']} exit {c.get('exitCode')} {c.get('reason', '')}".strip()
            for c in task["containers"]
        )
        say(f"    {task['group']} {task.get('stoppedReason')} | {containers}")
    say("  CloudWatch AWS/ECS, one minute periods:")
    print_metrics(session, stack.cluster, names, start, end)
    say("HYPOTHESIS: service MemoryUtilization is relative to the task memory")
    say("  reservation, not to the host. Read the docker stats samples above for")
    say("  real use, and the host line for what is left on the instance.")
    say("NOTE: Container Insights is off, so there is no per-task history.")
    return 0


# ---------------------------------------------------------------------------
# teardown-check: what is left after deploy.py teardown
# ---------------------------------------------------------------------------

LEFTOVER_STACK_PREFIX = "hume-ecs-"
LEFTOVER_ROLE_PREFIXES = ("hume-ami-", LEFTOVER_STACK_PREFIX)
LEFTOVER_LOG_PREFIXES = ("/hume/", f"/aws/lambda/{LEFTOVER_STACK_PREFIX}")


def paginate(client, operation: str, key: str, **kwargs) -> list:
    return [
        item
        for page in client.get_paginator(operation).paginate(**kwargs)
        for item in page[key]
    ]


def owned_amis(session: boto3.Session) -> list[dict]:
    ec2 = session.client("ec2")
    return ec2.describe_images(
        Owners=["self"], Filters=[{"Name": "tag:Purpose", "Values": [AMI_PURPOSE]}]
    )["Images"]


def image_snapshot_ids(image: dict) -> list[str]:
    return [
        m["Ebs"]["SnapshotId"]
        for m in image.get("BlockDeviceMappings", [])
        if "Ebs" in m and "SnapshotId" in m["Ebs"]
    ]


STACK_TAG_KEY = "aws:cloudformation:stack-name"
EC2_LOOKUPS = {
    "security-group": ("describe_security_groups", "GroupIds"),
}


@dataclass
class Findings:
    """What teardown-check found, one line per resource.

    The tagging index and the direct checks often see the same resource, so
    each is added under a key that names it and prints once. The first line
    added for a key wins, so add the direct checks before the tagged ARNs.
    """

    left: dict[str, str] = field(default_factory=dict)
    kept: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)

    def add(self, key: str, text: str, kept: bool = False) -> None:
        if key in self.left or key in self.kept:
            return
        (self.kept if kept else self.left)[key] = text


def record_secret(found: Findings, secret: dict) -> None:
    """Add one secret. Only a secret that deploy.py keeps on purpose counts as kept."""
    name = secret["Name"]
    line = f"secret: {name}" + (
        " (scheduled for deletion)" if secret.get("DeletedDate") else ""
    )
    found.add(f"secret: {name}", line, kept=name in EXPECTED_SECRETS)


def arn_key(arn: str) -> str:
    """Name a resource the way the direct checks do, so duplicates collapse."""
    parts = arn.split(":", 5)
    service, resource = parts[2], parts[5]
    kind, _, name = resource.partition("/")
    if service == "secretsmanager" and resource.startswith("secret:"):
        return "secret: " + resource.removeprefix("secret:").rsplit("-", 1)[0]
    if service == "logs" and resource.startswith("log-group:"):
        return "log group: " + resource.removeprefix("log-group:").removesuffix(":*")
    if service == "ec2" and kind == "instance":
        return f"instance: {name}"
    if service == "ec2" and kind == "image":
        return f"ami: {name}"
    if service == "ec2" and kind == "snapshot":
        return f"snapshot: {name}"
    if service == "cloudformation" and kind == "stack":
        return f"stack: {name.split('/')[0]}"
    if service == "iam" and kind == "role":
        return f"iam role: {name.rsplit('/', 1)[-1]}"
    if service == "iam" and kind == "instance-profile":
        return f"instance profile: {name.rsplit('/', 1)[-1]}"
    return arn


def stale_reason(session: boto3.Session, arn: str) -> str | None:
    """Return why a tagged ARN is no longer a live resource, or None if it is.

    The tagging index can list a deleted resource for a while, and ECS keeps
    INACTIVE clusters and task definitions visible. Anything this cannot
    prove stale is reported as left.
    """
    parts = arn.split(":", 5)
    service, resource = parts[2], parts[5]
    kind, _, name = resource.partition("/")
    try:
        if service == "ecs" and kind == "cluster":
            found = session.client("ecs").describe_clusters(clusters=[name])
            if not found["clusters"] or found["clusters"][0]["status"] == "INACTIVE":
                return "ECS cluster is INACTIVE"
        elif service == "ecs" and kind == "service" and "/" in name:
            cluster, _, service_name = name.partition("/")
            described = session.client("ecs").describe_services(
                cluster=cluster, services=[service_name]
            )
            if (
                described["failures"]
                or described["services"][0]["status"] == "INACTIVE"
            ):
                return "ECS service is INACTIVE"
        elif service == "ecs" and kind == "task-definition":
            ecs = session.client("ecs")
            if (
                ecs.describe_task_definition(taskDefinition=arn)["taskDefinition"][
                    "status"
                ]
                == "INACTIVE"
            ):
                return "task definition is INACTIVE"
        elif service == "ec2" and kind in EC2_LOOKUPS:
            method, param = EC2_LOOKUPS[kind]
            getattr(session.client("ec2"), method)(**{param: [name]})
        elif service == "ec2" and kind == "instance":
            described = session.client("ec2").describe_instances(InstanceIds=[name])
            if not described["Reservations"]:
                return "instance no longer exists"
            state = described["Reservations"][0]["Instances"][0]["State"]["Name"]
            if state == "terminated":
                return "instance is terminated"
        elif service == "secretsmanager" and resource.startswith("secret:"):
            session.client("secretsmanager").describe_secret(SecretId=arn)
        elif service == "logs" and resource.startswith("log-group:"):
            group = resource.removeprefix("log-group:").removesuffix(":*")
            found = session.client("logs").describe_log_groups(logGroupNamePrefix=group)
            if not any(g["logGroupName"] == group for g in found["logGroups"]):
                return "log group no longer exists"
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        missing = ("ResourceNotFoundException", "ClusterNotFoundException")
        if code.endswith(".NotFound") or code in missing:
            return f"{kind or 'resource'} no longer exists ({code})"
        raise
    return None


def cmd_teardown_check(args: argparse.Namespace) -> int:
    session = boto3.Session(
        profile_name=args.profile or DEFAULT_PROFILE,
        region_name=args.region or DEFAULT_REGION,
    )
    stack = args.stack_name
    sts = session.client("sts").get_caller_identity()
    say(f"Account {sts['Account']}, region {session.region_name}")
    say(f"Scope: stack {stack}" if stack else "Scope: the whole account")
    found = Findings()

    cfn = session.client("cloudformation")
    found_stacks = paginate(cfn, "describe_stacks", "Stacks")
    for item in found_stacks:
        name = item["StackName"]
        in_scope = name == stack if stack else name.startswith(LEFTOVER_STACK_PREFIX)
        if in_scope:
            found.add(f"stack: {name}", f"stack: {name} {item['StackStatus']}")

    ec2 = session.client("ec2")
    live = {
        "Name": "instance-state-name",
        "Values": ["pending", "running", "stopping", "stopped"],
    }
    instance_tags = (
        [(STACK_TAG_KEY, stack)]
        if stack
        else [("Project", PROJECT_TAG), ("Purpose", AMI_PURPOSE)]
    )
    for tag_key, tag_value in instance_tags:
        reservations = paginate(
            ec2,
            "describe_instances",
            "Reservations",
            Filters=[{"Name": f"tag:{tag_key}", "Values": [tag_value]}, live],
        )
        for reservation in reservations:
            for item in reservation["Instances"]:
                found.add(
                    f"instance: {item['InstanceId']}",
                    f"instance: {item['InstanceId']} {item['State']['Name']}",
                )

    if not stack:
        images = owned_amis(session)
        snapshot_ids = {sid for image in images for sid in image_snapshot_ids(image)}
        snapshot_ids |= {
            s["SnapshotId"]
            for s in ec2.describe_snapshots(
                OwnerIds=["self"],
                Filters=[{"Name": "tag:Purpose", "Values": [AMI_PURPOSE]}],
            )["Snapshots"]
        }
        for image in images:
            found.add(
                f"ami: {image['ImageId']}",
                f"ami: {image['ImageId']} {image['Name']} "
                f"public={image.get('Public', False)}",
            )
        for snapshot_id in sorted(snapshot_ids):
            found.add(f"snapshot: {snapshot_id}", f"snapshot: {snapshot_id}")

    sm = session.client("secretsmanager")
    secret_prefixes = [f"hume/{stack}/"] if stack else ["hume/", "hume-ami"]
    for prefix in secret_prefixes:
        for secret in paginate(
            sm,
            "list_secrets",
            "SecretList",
            IncludePlannedDeletion=True,
            Filters=[{"Key": "name", "Values": [prefix]}],
        ):
            record_secret(found, secret)

    logs = session.client("logs")
    log_prefixes = (
        (f"/hume/{stack}/", f"/aws/lambda/{stack}-") if stack else LEFTOVER_LOG_PREFIXES
    )
    for prefix in log_prefixes:
        for group in paginate(
            logs, "describe_log_groups", "logGroups", logGroupNamePrefix=prefix
        ):
            name = group["logGroupName"]
            found.add(f"log group: {name}", f"log group: {name}")

    iam = session.client("iam")
    role_prefixes = (f"{stack}-",) if stack else LEFTOVER_ROLE_PREFIXES
    for role in paginate(iam, "list_roles", "Roles"):
        if role["RoleName"].startswith(role_prefixes):
            found.add(f"iam role: {role['RoleName']}", f"iam role: {role['RoleName']}")
    for profile in paginate(iam, "list_instance_profiles", "InstanceProfiles"):
        name = profile["InstanceProfileName"]
        if name.startswith(role_prefixes):
            found.add(f"instance profile: {name}", f"instance profile: {name}")

    tagging = session.client("resourcegroupstaggingapi")
    tag_filter = (
        {"Key": STACK_TAG_KEY, "Values": [stack]}
        if stack
        else {"Key": "Project", "Values": [PROJECT_TAG]}
    )
    tagged = paginate(
        tagging,
        "get_resources",
        "ResourceTagMappingList",
        TagFilters=[tag_filter],
    )
    say(f"Tagging index entries: {len(tagged)}")
    for mapping in tagged:
        arn = mapping["ResourceARN"]
        reason = stale_reason(session, arn)
        if reason:
            found.skipped.append(f"{arn}: {reason}")
        else:
            found.add(arn_key(arn), f"tagged: {arn}")

    say("")
    say("RESULT teardown-check")
    for line in found.kept.values():
        say(f"  KEPT BY DESIGN {line}")
    for line in found.left.values():
        say(f"  LEFT {line}")
    for line in found.skipped:
        say(f"  HIDDEN (tagged but not live) {line}")
    say(
        f"  {len(found.left)} left, {len(found.kept)} kept by design, "
        f"{len(found.skipped)} hidden"
    )
    if stack:
        exists = record_path(stack).exists()
        state = "still exists" if exists else "gone"
        say(f"  record file {record_path(stack)}: {state}")
    else:
        records = sorted(p.stem for p in DEPLOY_DIR.glob("*.txt"))
        say(f"  record files in {DEPLOY_DIR}: {', '.join(records) or 'none'}")
    say("HYPOTHESIS: the tagging index lags a few minutes behind deletes. Entries")
    say("  it still lists are hidden above only when a direct call shows they are")
    say("  gone or INACTIVE. The other checks call each service directly.")
    say("NOTE: this cannot see resources that are neither tagged nor named with")
    say("  the prefixes above. A Cost Explorer check the next day would.")
    return 1 if found.left else 0


def cmd_delete_ami(args: argparse.Namespace) -> int:
    session = boto3.Session(
        profile_name=args.profile or DEFAULT_PROFILE,
        region_name=args.region or DEFAULT_REGION,
    )
    ec2 = session.client("ec2")
    images = ec2.describe_images(ImageIds=[args.ami_id], Owners=["self"])["Images"]
    if not images:
        sys.exit(f"{args.ami_id} is not an image owned by this account")
    image = images[0]
    tags = {t["Key"]: t["Value"] for t in image.get("Tags", [])}
    if tags.get("Purpose") != AMI_PURPOSE:
        sys.exit(f"{args.ami_id} is not tagged Purpose={AMI_PURPOSE}. Not deleting.")
    snapshots = image_snapshot_ids(image)
    say(f"AMI {image['ImageId']} {image['Name']}, public={image.get('Public')}")
    say(f"Snapshots: {', '.join(snapshots)}")
    confirm("Deregister the AMI and delete its snapshots?", args.yes)

    ec2.deregister_image(ImageId=image["ImageId"])
    for snapshot in snapshots:
        ec2.delete_snapshot(SnapshotId=snapshot)
        say(f"Deleted snapshot {snapshot}")
    gone = not ec2.describe_images(ImageIds=[args.ami_id], Owners=["self"])["Images"]
    say(f"RESULT delete-ami: image gone={gone}")
    return 0 if gone else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--profile", help=f"AWS profile (default: {DEFAULT_PROFILE})")
    parser.add_argument("--region", help=f"AWS region (default: {DEFAULT_REGION})")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_stack(name: str, text: str) -> argparse.ArgumentParser:
        command = sub.add_parser(name, help=text)
        command.add_argument(
            "stack_name", nargs="?", help="Default: the only record file"
        )
        command.add_argument("--yes", action="store_true", help="Skip the prompt")
        return command

    egress = with_stack("egress", "Block the registry, replace a task")
    egress.add_argument("--service", default="web", help="Service to replace")
    egress.add_argument("--timeout", type=int, default=600, help="Seconds to wait")

    with_stack("credentials", "Check for credential files on the instance")

    replace = with_stack("replace-instance", "Terminate the instance, time recovery")
    replace.add_argument("--timeout", type=int, default=40, help="Minutes to wait")

    soak = with_stack("soak", "Record restarts, memory and CPU")
    soak.add_argument("--minutes", type=int, default=30)

    check = sub.add_parser("teardown-check", help="List what is left after teardown")
    check.add_argument(
        "stack_name", nargs="?", help="Only this stack. Default: the whole account"
    )

    delete = sub.add_parser("delete-ami", help="Deregister the AMI and its snapshots")
    delete.add_argument("ami_id")
    delete.add_argument("--yes", action="store_true", help="Skip the prompt")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    commands = {
        "egress": cmd_egress,
        "credentials": cmd_credentials,
        "replace-instance": cmd_replace_instance,
        "soak": cmd_soak,
        "teardown-check": cmd_teardown_check,
        "delete-ami": cmd_delete_ami,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
