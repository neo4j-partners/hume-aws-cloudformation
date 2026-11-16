#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3[crt]", "python-dotenv"]
# ///
"""Find the Neo4j cluster nodes, put the Spine backup on one, and load it.

The Spine database is seeded from a file in the seeds directory of one cluster
node, and every node copies it from there with ServerSeedProvider. Three commands:

    seed_nodes.py discover <neo4j stack>
        Read only on the nodes. It may first stage the backup in the template
        bucket, see stage_backup. Lists the running instances of the Neo4j
        stack and runs one read-only probe on each over SSM. The probe reports
        whether it is a Neo4j node, its Neo4j home, seeds directory, free disk,
        the server id, the seed providers in neo4j.conf, and whether the node
        can reach the backup address over https (a GET of one byte, nothing
        more is read).

    seed_nodes.py copy <neo4j stack> --instance-id <id>
        Downloads the backup into the seeds directory of that node. It stages
        the backup if needed, then prints the plan and asks for a yes, or takes
        --yes. It deletes nothing.

    seed_nodes.py load <neo4j stack> --instance-id <id>
        Creates the spine database from the backup on that node, then its
        roles, and prints the counts. cypher-shell runs on the node over SSM
        and connects to localhost as the neo4j superuser. The node reads the
        password from Secrets Manager itself, so the password is never in the
        command text. The secret is --password-secret-arn, or the ARN in the
        SSM parameter /neo4j-ee/<stack>/password-secret-arn. Hume itself never
        signs in as the superuser. It stages the backup if needed, then prints
        the plan and asks for a yes, or takes --yes.

The server id comes from the file data/server_id, which holds a version byte
and the 16 bytes of the id. That matches SHOW SERVERS on the local test
(2026.06). It is not yet checked on the 2026.10 package install, so the load
stops unless SHOW SERVERS lists the id. Neo4j home defaults to /var/lib/neo4j
and the seeds directory to <home>/seeds, which is a guess for the package
install.

A Private Neo4j node reaches only S3 in its own region. By default each
command copies the public backup once into the template bucket of this region
and gives the node an address for it, presigned for one hour. --url overrides
that address, and staging is then skipped. The probe reads one byte with a GET,
so a presigned GET address works. SSM keeps the command text, so give a
presigned --url a short expiry. Nothing here prints the presigned address or a
secret value.

spine_demo.py runs copy and load as two of its steps.
"""

import argparse
import base64
import re
import shlex
import sys
import time
from pathlib import Path
from typing import NamedTuple

import boto3
from boto3.exceptions import Boto3Error
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    NoCredentialsError,
    SSOTokenLoadError,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
)

# The repository modules imported below live in these directories.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_IMPORT_DIRS = [str(_REPO_ROOT / name) for name in ("stack",)]
sys.path[:0] = [entry for entry in _IMPORT_DIRS if entry not in sys.path]

import deploy

BACKUP_URL = (
    "https://s3.eu-west-1.amazonaws.com/spine.dev.graphaware.io/"
    "spine-neo4j-backups/spine-latest.backup"
)
# BACKUP_URL is in eu-west-1, which a Private node cannot reach. The commands
# copy it server side into the template bucket and presign that copy instead.
BACKUP_SOURCE_BUCKET = "spine.dev.graphaware.io"
BACKUP_SOURCE_KEY = "spine-neo4j-backups/spine-latest.backup"
BACKUP_SOURCE_REGION = "eu-west-1"
STAGED_KEY = "spine/spine-latest.backup"
# The copy on the node may take up to 1800 seconds, so one hour leaves margin.
PRESIGN_SECONDS = 3600
BACKUP_NAME = "spine-latest.backup"
NEO4J_CONF = "/etc/neo4j/neo4j.conf"
DISK_MARGIN = 1.1
ROLES_FILE = Path(__file__).resolve().parent / "roles.cypher"
# The Neo4j stack publishes the ARN of the superuser password secret here.
SUPERUSER_SECRET_PARAMETER = "/neo4j-ee/{stack}/password-secret-arn"
# The Neo4j EE node tool contract, in docs/architecture/template-architecture.md
# under "Node tool contract", promises these two paths and nothing about PATH.
# SSM runs the load as root, so both tools are named in full and checked first.
CYPHER_SHELL = "/usr/bin/cypher-shell"
AWS_CLI = "/usr/local/bin/aws"
# CREATE DATABASE waits up to 900 seconds. The rest of the script is short.
LOAD_TIMEOUT = 1200
LOAD_MARKERS = (
    "spine-status",
    "spine-nodes",
    "spine-persons",
    "spine-relationships",
)
COMMAND_RUNNING = ("Pending", "InProgress", "Delayed")
SSO_ERRORS = (
    NoCredentialsError,
    SSOTokenLoadError,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
)
EXPIRED_CODES = (
    "ExpiredToken",
    "ExpiredTokenException",
    "InvalidClientTokenId",
    "UnrecognizedClientException",
)

# Read only. NEO4J_CONF and the URL are set by the caller. Nothing is written.
PROBE_SCRIPT = r"""
conf=${NEO4J_CONF:-__CONF__}
url=__URL__
[ -f "$conf" ] && echo neo4j_conf=yes || echo neo4j_conf=no
setting() { grep -E "^$1=" "$conf" 2>/dev/null | tail -1 | cut -d= -f2-; }
home=$(setting server.directories.neo4j_home)
[ -n "$home" ] || home=$(ps -eo args= | grep -o -- '--home-dir=[^ ]*' | head -1 | cut -d= -f2-)
[ -n "$home" ] || home=/var/lib/neo4j
data=$(setting server.directories.data)
[ -n "$data" ] || data=$home/data
seeds=$(setting server.directories.seeds)
[ -n "$seeds" ] || seeds=$home/seeds
echo home=$home
echo seeds_dir=$seeds
[ -d "$seeds" ] && echo seeds_exists=yes || echo seeds_exists=no
echo providers=$(setting dbms.databases.seed_from_uri_providers)
have=$({ wc -c < "$seeds/__NAME__"; } 2>/dev/null | tr -d ' ')
echo have=$have
if [ -f "$data/server_id" ]; then
  hex=$(od -An -tx1 -v "$data/server_id" | tr -d ' \n')
  echo server_id=${hex:2:8}-${hex:10:4}-${hex:14:4}-${hex:18:4}-${hex:22:12}
else
  echo server_id=
fi
parent=$seeds
while [ ! -d "$parent" ] && [ "$parent" != / ]; do parent=$(dirname "$parent"); done
kb=$(df -P -k "$parent" | awk 'NR==2{print $4}')
echo free=$((kb * 1024))
echo neo4j_active=$(systemctl is-active neo4j 2>/dev/null || true)
hdr=$(curl -s -m 15 -r 0-0 -D - -o /dev/null "$url" 2>/dev/null)
echo https_rc=$?
hdr=$(echo "$hdr" | tr -d '\r')
echo https_status=$(echo "$hdr" | head -1 | awk '{print $2}')
echo want=$(echo "$hdr" | awk 'tolower($1)=="content-range:"{sub(/.*\//, "", $3); print $3}')
"""

# Writes one file. The download goes to a .part file and moves into place only
# after curl succeeds. The directory is created only if it is missing.
COPY_SCRIPT = r"""
set -eu
dir=__DIR__
url=__URL__
parent=$(dirname "$dir")
owner=$(ls -ldn "$parent" | awk '{print $3":"$4}')
[ -d "$dir" ] || install -d -o "${owner%:*}" -g "${owner#*:}" -m 0750 "$dir"
curl -fsS -o "$dir/__NAME__.part" "$url"
mv "$dir/__NAME__.part" "$dir/__NAME__"
chown "$owner" "$dir/__NAME__"
ls -l "$dir"
"""

# Creates the spine database and its roles. The password comes from Secrets
# Manager into the environment of cypher-shell, never into argv or the command
# text. neo4j+ssc accepts the self-signed certificate of the node. The roles
# file is base64, so its quotes and backticks reach cypher-shell unchanged.
LOAD_SCRIPT = r"""
set -eu
cypher_shell=__CYPHER_SHELL__
aws=__AWS__
for tool in "$cypher_shell" "$aws"; do
  [ -x "$tool" ] || { echo "$tool is missing or not executable on this node" >&2; exit 1; }
done
id=__ID__
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=$("$aws" secretsmanager get-secret-value --region __REGION__ --secret-id __SECRET__ --query SecretString --output text)
export NEO4J_USERNAME NEO4J_PASSWORD
run() { "$cypher_shell" -a neo4j+ssc://localhost:7687 --format plain "$@"; }
known=$(run "SHOW SERVERS YIELD serverId WHERE serverId = '$id' RETURN count(*)" | tail -n 1)
if [ "$known" != 1 ]; then
  echo "SHOW SERVERS does not list the server id $id" >&2
  exit 1
fi
servers=$(run "SHOW SERVERS YIELD name RETURN count(*)" | tail -n 1)
run "CREATE DATABASE spine IF NOT EXISTS TOPOLOGY $servers PRIMARIES 0 SECONDARIES OPTIONS {seedURI: 'server://$id/__NAME__'} WAIT 900 SECONDS"
roles=$(mktemp)
trap 'rm -f "$roles"' EXIT
echo __ROLES__ | base64 -d > "$roles"
run -f "$roles"
status=$(run "SHOW DATABASE spine YIELD currentStatus RETURN currentStatus" | tail -n 1 | tr -d '"')
nodes=$(run -d spine "MATCH (n) RETURN count(n)" | tail -n 1)
persons=$(run -d spine "MATCH (p:Person) RETURN count(p)" | tail -n 1)
relationships=$(run -d spine "MATCH ()-[r]->() RETURN count(r)" | tail -n 1)
echo "spine-status=$status"
echo "spine-nodes=$nodes"
echo "spine-persons=$persons"
echo "spine-relationships=$relationships"
"""


class StepFailed(Exception):
    """A step stopped the run. The message says what to fix."""

    def __init__(self, step: str, message: str):
        super().__init__(message)
        self.step = step


class SpineResult(NamedTuple):
    status: str | None
    nodes: int | None
    persons: int | None
    relationships: int | None


def check_sso(session: boto3.Session) -> str:
    """Return the account id. An expired sign-in stops with the fix, no traceback."""
    hint = (
        f"The AWS sign-in for profile {session.profile_name} is missing or has "
        f"expired. Run: aws sso login --profile {session.profile_name}"
    )
    try:
        return session.client("sts").get_caller_identity()["Account"]
    except SSO_ERRORS as error:
        raise StepFailed("sso", hint) from error
    except ClientError as error:
        if error.response["Error"]["Code"] in EXPIRED_CODES:
            raise StepFailed("sso", hint) from error
        raise


def error_code(error: ClientError) -> str:
    return error.response["Error"]["Code"]


def confirm(assume_yes: bool) -> None:
    if assume_yes:
        print("--yes was given. Continuing.")
        return
    try:
        answer = input("Continue? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        sys.exit("Cancelled. Nothing was changed.")


def probe_script(url: str = BACKUP_URL, conf: str = NEO4J_CONF) -> str:
    return (
        PROBE_SCRIPT.replace("__CONF__", conf)
        .replace("__URL__", shlex.quote(url))
        .replace("__NAME__", BACKUP_NAME)
    )


def copy_script(seeds_dir: str, url: str = BACKUP_URL) -> str:
    return (
        COPY_SCRIPT.replace("__DIR__", shlex.quote(seeds_dir))
        .replace("__URL__", shlex.quote(url))
        .replace("__NAME__", BACKUP_NAME)
    )


def load_script(
    server: str,
    secret_arn: str,
    cypher_shell: str = CYPHER_SHELL,
    aws_cli: str = AWS_CLI,
) -> str:
    """The load script for this server id and password secret, both checked here.

    cypher_shell and aws_cli are the full paths of the tools on the node.
    """
    if not deploy.SERVER_ID_PATTERN.fullmatch(server):
        raise ValueError(f"{server!r} is not a server id")
    if not re.fullmatch(deploy.SECRET_ARN, secret_arn):
        raise ValueError(f"{secret_arn!r} is not a secret ARN")
    roles = base64.b64encode(ROLES_FILE.read_bytes()).decode()
    return (
        LOAD_SCRIPT.replace("__CYPHER_SHELL__", shlex.quote(cypher_shell))
        .replace("__AWS__", shlex.quote(aws_cli))
        .replace("__ID__", server)
        .replace("__REGION__", secret_arn.split(":")[3])
        .replace("__SECRET__", shlex.quote(secret_arn))
        .replace("__NAME__", BACKUP_NAME)
        .replace("__ROLES__", roles)
    )


def parse_probe(text: str) -> dict[str, str]:
    pairs = (line.strip().partition("=") for line in text.splitlines())
    return {key: value for key, separator, value in pairs if separator}


def parse_load(text: str) -> SpineResult:
    """Read the spine-* marker lines of the load script."""
    values = {k: v for k, v in parse_probe(text).items() if k in LOAD_MARKERS}

    def count(key: str) -> int | None:
        value = values.get(key, "")
        return int(value) if value.isdigit() else None

    return SpineResult(
        values.get("spine-status"),
        count("spine-nodes"),
        count("spine-persons"),
        count("spine-relationships"),
    )


def as_int(value: str | None) -> int:
    return int(value) if value and value.isdigit() else 0


def server_id(probe: dict[str, str]) -> str:
    """The server id the probe read, or an empty string if it is not in the format."""
    found = probe.get("server_id", "")
    return found if deploy.SERVER_ID_PATTERN.fullmatch(found) else ""


def is_node(probe: dict[str, str]) -> bool:
    return probe.get("neo4j_conf") == "yes"


def reaches_backup(probe: dict[str, str]) -> bool:
    """A status of 200, 206 or 403 means the route works. A timeout gives none."""
    return probe.get("https_rc") == "0" and probe.get("https_status") in (
        "200",
        "206",
        "403",
    )


def run_on_host(
    ssm, instance: str, commands: list[str], timeout: int, step: str
) -> str:
    """Run shell commands over SSM and return their output. Failure stops the step."""
    sent = ssm.send_command(
        InstanceIds=[instance],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": commands, "executionTimeout": [str(timeout)]},
        Comment="seed_nodes.py",
    )
    command_id = sent["Command"]["CommandId"]
    deadline = time.monotonic() + timeout + 120
    while True:
        try:
            result = ssm.get_command_invocation(
                CommandId=command_id, InstanceId=instance
            )
        except ClientError as error:
            # The invocation is not visible for a moment after the send.
            if error_code(error) != "InvocationDoesNotExist":
                raise
            result = {"Status": "Pending"}
        if result["Status"] not in COMMAND_RUNNING:
            break
        if time.monotonic() > deadline:
            raise StepFailed(step, f"SSM command {command_id} did not finish in time.")
        time.sleep(5)
    if result["Status"] != "Success":
        stderr = result.get("StandardErrorContent", "").strip()[-500:]
        raise StepFailed(
            step, f"SSM command {command_id} ended {result['Status']}. {stderr}"
        )
    return result["StandardOutputContent"]


def run_script(ssm, instance: str, script: str, timeout: int, step: str) -> str:
    encoded = base64.b64encode(script.encode()).decode()
    return run_on_host(
        ssm, instance, [f"echo {encoded} | base64 -d | bash"], timeout, step
    )


def list_instances(session: boto3.Session, stack: str) -> list[dict[str, str]]:
    """The running instances that CloudFormation made for the stack."""
    pages = (
        session.client("ec2")
        .get_paginator("describe_instances")
        .paginate(
            Filters=[
                {"Name": "tag:aws:cloudformation:stack-name", "Values": [stack]},
                {"Name": "instance-state-name", "Values": ["running"]},
            ]
        )
    )
    found = []
    for page in pages:
        for reservation in page["Reservations"]:
            for item in reservation["Instances"]:
                tags = {t["Key"]: t["Value"] for t in item.get("Tags", [])}
                found.append(
                    {
                        "id": item["InstanceId"],
                        "name": tags.get("Name", ""),
                        "zone": item["Placement"]["AvailabilityZone"],
                        "ip": item.get("PrivateIpAddress", ""),
                    }
                )
    return sorted(found, key=lambda item: (item["name"], item["id"]))


def online_instances(ssm, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    info = ssm.describe_instance_information(
        Filters=[{"Key": "InstanceIds", "Values": ids}]
    )["InstanceInformationList"]
    return {i["InstanceId"] for i in info if i["PingStatus"] == "Online"}


def probe_instance(ssm, instance: str, url: str) -> dict[str, str]:
    return parse_probe(run_script(ssm, instance, probe_script(url), 120, "probe"))


def check_node(
    session: boto3.Session, stack: str, instance: str, url: str, step: str
) -> tuple[object, dict[str, str], dict[str, str]]:
    """Return the SSM client, the instance and its probe. Stop unless it is a node."""
    items = [i for i in list_instances(session, stack) if i["id"] == instance]
    if not items:
        raise StepFailed(step, f"{instance} is not a running instance of {stack}.")
    ssm = session.client("ssm")
    if instance not in online_instances(ssm, [instance]):
        raise StepFailed(step, f"{instance} is not online in SSM.")
    probe = probe_instance(ssm, instance, url)
    if not is_node(probe):
        raise StepFailed(step, f"{instance} is not a Neo4j node.")
    return ssm, items[0], probe


def show_probe(item: dict[str, str], probe: dict[str, str] | None) -> None:
    print(f"{item['id']}  {item['name'] or '-'}  {item['zone']}  {item['ip']}")
    if probe is None:
        print("    not online in SSM, not probed")
        return
    if not is_node(probe):
        print(f"    not a Neo4j node, there is no {NEO4J_CONF}")
        return
    want = as_int(probe.get("want"))
    print(f"    neo4j service:   {probe.get('neo4j_active') or 'unknown'}")
    print(f"    neo4j home:      {probe.get('home')}")
    exists = "exists" if probe.get("seeds_exists") == "yes" else "missing"
    print(f"    seeds directory: {probe.get('seeds_dir')} ({exists})")
    print(f"    seed providers:  {probe.get('providers') or 'not set in neo4j.conf'}")
    print(f"    free disk:       {as_int(probe.get('free')) / 2**20:.0f} MiB")
    print(f"    server id:       {server_id(probe) or 'not found or not valid'}")
    have = as_int(probe.get("have"))
    print(f"    backup on node:  {have} bytes")
    reach = "yes" if reaches_backup(probe) else "NO"
    print(
        f"    outbound https:  {reach} (curl exit {probe.get('https_rc')}, "
        f"status {probe.get('https_status') or 'none'}, size {want} bytes)"
    )


def no_route_help() -> str:
    return (
        "The node cannot reach the backup address. The default address is "
        "already a copy staged in the template bucket of this region, "
        "presigned for one hour. The node reaches it through the S3 gateway "
        "endpoint, so the route table of its subnet needs the S3 prefix list "
        "route, or the subnet needs a NAT gateway. Check the Neo4j stack for "
        "one of them. An address given with --url must be reachable in the "
        "same way."
    )


def stage_backup(session: boto3.Session) -> str:
    """Return a presigned address of the Spine backup in the template bucket.

    The first run copies the public backup into the bucket server side, with no
    download here. The address is never printed. It carries the identity of the
    signer, and SSM keeps the command text.
    """
    bucket = deploy.ensure_template_bucket(session)
    staged = f"s3://{bucket}/{STAGED_KEY}"
    # The regional host is the one the S3 gateway endpoint of the VPC serves.
    s3 = session.client(
        "s3",
        config=Config(
            signature_version="s3v4",
            s3={"us_east_1_regional_endpoint": "regional"},
        ),
    )
    try:
        s3.head_object(Bucket=bucket, Key=STAGED_KEY)
        print(f"Using the staged Spine backup {staged}")
    except ClientError as error:
        if error_code(error) not in ("404", "NoSuchKey", "NotFound"):
            raise StepFailed(
                "stage", f"Could not read {staged}: {error_code(error)}"
            ) from error
        print(
            f"Staging the Spine backup to {staged}, about 600 MB, once per "
            "account and region"
        )
        copy_to_template_bucket(session, s3, bucket)
        print(f"Staged the Spine backup to {staged}")
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": STAGED_KEY},
        ExpiresIn=PRESIGN_SECONDS,
    )


def copy_to_template_bucket(session: boto3.Session, s3, bucket: str) -> None:
    """Copy the public backup into the bucket server side, in parts."""
    source = session.client("s3", region_name=BACKUP_SOURCE_REGION)
    try:
        s3.copy(
            CopySource={"Bucket": BACKUP_SOURCE_BUCKET, "Key": BACKUP_SOURCE_KEY},
            Bucket=bucket,
            Key=STAGED_KEY,
            SourceClient=source,
        )
    except (ClientError, Boto3Error) as error:
        code = (
            error_code(error)
            if isinstance(error, ClientError)
            else type(error).__name__
        )
        raise StepFailed(
            "stage",
            f"Could not copy the Spine backup from s3://{BACKUP_SOURCE_BUCKET}/"
            f"{BACKUP_SOURCE_KEY} to s3://{bucket}/{STAGED_KEY}: {code}. A "
            "server-side copy cannot cross partitions, so in GovCloud upload "
            "the file to that key yourself and run the command again, or pass "
            "--url.",
        ) from error


def plan_copy(probe: dict[str, str], seeds_dir: str | None) -> tuple[str, int, bool]:
    """Return the target directory, the backup size and whether the copy is done.

    Stops when the node cannot reach the backup, cannot read its size, or has
    too little free disk for it.
    """
    if not reaches_backup(probe):
        raise StepFailed("copy", no_route_help())
    want = as_int(probe.get("want"))
    if not want:
        raise StepFailed("copy", "The node could not read the size of the backup.")
    target = seeds_dir or probe["seeds_dir"]
    if as_int(probe.get("have")) == want:
        return target, want, True
    free = as_int(probe.get("free"))
    if want * DISK_MARGIN > free:
        raise StepFailed(
            "copy", f"The backup is {want} bytes and the node has {free} free."
        )
    return target, want, False


def run_copy(ssm, instance: str, seeds_dir: str, url: str, want: int) -> dict:
    """Download the backup on the node, then probe again to check its size."""
    run_script(ssm, instance, copy_script(seeds_dir, url), 1800, "copy")
    after = probe_instance(ssm, instance, url)
    if as_int(after.get("have")) != want:
        raise StepFailed(
            "copy", f"The file is {as_int(after.get('have'))} bytes, expected {want}."
        )
    return after


def run_load(ssm, instance: str, server: str, secret_arn: str) -> SpineResult:
    """Run the load script on the node. Stop unless spine is online with nodes."""
    output = run_script(
        ssm, instance, load_script(server, secret_arn), LOAD_TIMEOUT, "load"
    )
    for line in output.splitlines():
        print(line.rstrip())
    result = parse_load(output)
    if result.status != "online" or not result.nodes:
        raise StepFailed(
            "load", f"spine status {result.status!r}, node count {result.nodes!r}"
        )
    return result


def discover(args: argparse.Namespace) -> int:
    session = deploy.cli_session(args)
    check_sso(session)
    url = args.url or stage_backup(session)
    items = list_instances(session, args.neo4j_stack)
    if args.instance_id:
        items = [i for i in items if i["id"] == args.instance_id]
    if not items:
        raise StepFailed(
            "discover", f"No running instance found for {args.neo4j_stack}."
        )
    ssm = session.client("ssm")
    online = online_instances(ssm, [i["id"] for i in items])
    nodes = []
    unreachable = 0
    for item in items:
        probe = probe_instance(ssm, item["id"], url) if item["id"] in online else None
        show_probe(item, probe)
        if probe and is_node(probe):
            nodes.append((item, probe))
            unreachable += not reaches_backup(probe)
    if not nodes:
        raise StepFailed("discover", "None of the instances is a Neo4j node.")
    if unreachable:
        print(no_route_help())
    first = nodes[0][0]["id"]
    print(
        f"\n{len(nodes)} Neo4j node(s). To seed from {first}, run:\n"
        f"  ./demo/spine/seed_nodes.py copy {args.neo4j_stack} --instance-id {first}\n"
        f"  ./demo/spine/seed_nodes.py load {args.neo4j_stack} --instance-id {first}\n"
        "Or let spine_demo.py run both with the rest of the demo setup:\n"
        f"  ./demo/spine/spine_demo.py <hume stack> --neo4j-stack {args.neo4j_stack} "
        f"--instance-id {first}"
    )
    return 0


def copy(args: argparse.Namespace) -> int:
    session = deploy.cli_session(args)
    check_sso(session)
    url = args.url or stage_backup(session)
    ssm, item, probe = check_node(
        session, args.neo4j_stack, args.instance_id, url, "copy"
    )
    seeds_dir, want, done = plan_copy(probe, args.seeds_dir)
    if done:
        print(f"Skip: {seeds_dir}/{BACKUP_NAME} is already complete, {want} bytes.")
        return 0
    print(f"Node:      {args.instance_id} ({item['name'] or '-'})")
    print(f"Target:    {seeds_dir}/{BACKUP_NAME}, {want / 2**20:.0f} MiB")
    print(f"Server id: {server_id(probe) or 'not found or not valid'}")
    print("This downloads the file on the node over SSM. It deletes nothing.")
    confirm(args.yes)
    after = run_copy(ssm, args.instance_id, seeds_dir, url, want)
    print(
        f"Copied {want} bytes. Server id: "
        f"{server_id(after) or 'not found or not valid'}"
    )
    return 0


def superuser_secret_arn(session: boto3.Session, neo4j_stack: str) -> str:
    """The superuser password secret ARN that the Neo4j stack publishes in SSM."""
    name = SUPERUSER_SECRET_PARAMETER.format(stack=neo4j_stack)
    try:
        return session.client("ssm").get_parameter(Name=name)["Parameter"]["Value"]
    except ClientError as error:
        if error_code(error) != "ParameterNotFound":
            raise
        raise StepFailed(
            "load",
            f"The SSM parameter {name} was not found. Check the stack and the "
            "region, or pass --password-secret-arn.",
        ) from error


def load(args: argparse.Namespace) -> int:
    session = deploy.cli_session(args)
    check_sso(session)
    url = args.url or stage_backup(session)
    secret_arn = args.password_secret_arn or superuser_secret_arn(
        session, args.neo4j_stack
    )
    if not re.fullmatch(deploy.SECRET_ARN, secret_arn):
        raise StepFailed(
            "load",
            f"{secret_arn!r} is not the full ARN of a secret, including the "
            "six-character suffix.",
        )
    ssm, item, probe = check_node(
        session, args.neo4j_stack, args.instance_id, url, "load"
    )
    server = server_id(probe)
    if not server:
        raise StepFailed("load", f"The server id of {args.instance_id} was not found.")
    if not as_int(probe.get("have")):
        raise StepFailed(
            "load",
            f"{probe.get('seeds_dir')}/{BACKUP_NAME} is missing on "
            f"{args.instance_id}. Run the copy command first.",
        )
    print(f"Node:      {args.instance_id} ({item['name'] or '-'})")
    print(f"Server id: {server}")
    print(f"Seed:      server://{server}/{BACKUP_NAME}")
    print(
        "This creates the spine database and its roles over SSM, if they are "
        "missing. It takes about 3 minutes. It deletes nothing."
    )
    confirm(args.yes)
    result = run_load(ssm, args.instance_id, server, secret_arn)
    print(
        f"PASS: the spine database is online with {result.nodes} nodes, "
        f"{result.persons} Person nodes and {result.relationships} relationships"
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", help="AWS profile")
    parser.add_argument("--region", help="AWS region")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("discover", "Read only. List the nodes and probe each one"),
        ("copy", "Download the backup into the seeds directory of one node"),
        ("load", "Create the spine database from the backup on one node"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("neo4j_stack", help="Name of the Neo4j EE stack")
        command.add_argument(
            "--url",
            help=(
                "https address of the backup. Default: a staged copy in the "
                "template bucket, presigned for one hour. Use the same --url "
                "for copy and load."
            ),
        )
        if name == "discover":
            command.add_argument("--instance-id", help="Probe only this instance")
            continue
        command.add_argument("--instance-id", required=True)
        if name == "copy":
            command.add_argument(
                "--seeds-dir", help="Default: the seeds directory the probe found"
            )
        else:
            command.add_argument(
                "--password-secret-arn",
                help=(
                    "Neo4j superuser password secret (default: the ARN in "
                    f"{SUPERUSER_SECRET_PARAMETER})"
                ),
            )
        command.add_argument(
            "--yes", action="store_true", help="Skip the confirmation prompt"
        )
    args = parser.parse_args()
    if args.url is not None and not args.url.startswith("https://"):
        parser.error("--url must start with https://")
    return args


def main() -> int:
    args = parse_args()
    commands = {"discover": discover, "copy": copy, "load": load}
    try:
        return commands[args.command](args)
    except StepFailed as failed:
        print(f"FAIL at step {failed.step}: {failed}", file=sys.stderr)
        return 1
    except ClientError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
