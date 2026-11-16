#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3[crt]", "python-dotenv"]
# ///
"""Launch the Hume AMI and check it over Session Manager.

Checks that every image is cached, no registry credential is on disk, the ECS
agent settings are in place, the static config files are readable by uid 1001,
the licence key file is present and readable only by uid 1001, the EFS client and
the ECS volume plugin are in place, the host rejects the
compromised eMudhra chains, the AMI holds no Java key store file and no customer
settings, and the AMI itself requires IMDSv2. No check prints the licence key. The test instance
and its role are removed on exit.

Usage:
    AWS_PROFILE=marketplace ./ami/test_ami.py [--ami-id ami-...]
"""

import argparse
import os
import shlex
import sys
import time
from pathlib import Path

from botocore.exceptions import ClientError

from aws_helpers import (
    PURPOSE,
    caller_identity,
    confirm,
    log,
    make_session,
    tag_list,
    temporary_instance,
    temporary_instance_profile,
)
from create_ami import (
    AMI_ID_FILE,
    ECS_CONFIG,
    HUME_ATTRIBUTE_NAME,
    HUME_VERSION,
    IMAGES,
    LICENCE_FILE,
    ROOT_VOLUME_GIB,
)

TEST_INSTANCE_TYPE = "m6i.large"
SSM_POLICY_ARN = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
STATIC_FILES = (
    "api/conf/server-logs-config.xml",
    "orchestra/conf/server-logs-config.xml",
    "orchestra/security/security.json",
    "rds/global-bundle.pem",
)
# A stack creates these at boot, so none may exist in the AMI.
STACK_PATHS = ("/opt/hume/truststore", "/opt/hume/data")
# Skipped by the file list check, because the licence key ships in the AMI. The
# licence checks below cover it.
LICENCE_FILE_PATTERN = "*licen[cs]e*"
# Test hosts from https://emudhra.we-have-a-problem.com/. Each one serves a chain
# through a compromised eMudhra intermediate.
COMPROMISED_CHAIN_HOSTS = tuple(
    f"{name}.we-have-a-problem.com"
    for name in (
        "dv-g3b-emudhra",
        "dv-g3b-aaa-emudhra",
        "dv-g3b-noct-emudhra",
        "dv-g3b-aaa-noct-emudhra",
    )
)
CURL_CERT_REJECTED = 60


def remote_script() -> str:
    """Bash that prints one 'PASS name' or 'FAIL name' line per check."""
    checks: list[tuple[str, str]] = []
    for image in IMAGES:
        checks.append((f"image cached: {image}", f"docker image inspect {shlex.quote(image)}"))
    for setting in ECS_CONFIG.splitlines():
        checks.append(
            (f"ecs.config has {setting}", f"grep -Fxq {shlex.quote(setting)} /etc/ecs/ecs.config")
        )
    for path in STATIC_FILES:
        full = f"/opt/hume/config/{path}"
        checks.append(
            (
                f"static file owned by 1001 and readable: {path}",
                f'[ "$(stat -c %u:%g {full})" = 1001:1001 ] && [ "$(stat -c %a {full})" = 644 ]',
            )
        )
    licence = f"/opt/hume/config/{LICENCE_FILE}"
    licence_dir = os.path.dirname(licence)
    checks += [
        (
            f"licence key file owned by 1001, mode 400, not empty: {LICENCE_FILE}",
            f'[ "$(stat -c %u:%g:%a {licence})" = 1001:1001:400 ] && [ -s {licence} ]',
        ),
        (
            f"licence folder owned by 1001, mode 700: {licence_dir}",
            f'[ "$(stat -c %u:%g:%a {licence_dir})" = 1001:1001:700 ]',
        ),
        (
            "licence folder holds only the key file",
            f'[ "$(find {licence_dir} -mindepth 1)" = {shlex.quote(licence)} ]',
        ),
        (
            "no other licence file under /opt/hume",
            (
                f"[ \"$(find /opt/hume -type f -iname {shlex.quote(LICENCE_FILE_PATTERN)})\" "
                f"= {shlex.quote(licence)} ]"
            ),
        ),
    ]
    # Only a certificate rejection passes. A DNS or network failure stays a FAIL,
    # so an unreachable test host cannot look like a patched trust store.
    for host in COMPROMISED_CHAIN_HOSTS:
        checks.append(
            (
                f"TLS rejects compromised chain: {host}",
                f"curl -sS -o /dev/null -m 15 https://{host}/; "
                f"[ $? -eq {CURL_CERT_REJECTED} ]",
            )
        )
    checks.append(
        (
            "no keystore file by name",
            "! find /opt/hume /root /home /etc/ecs -type f \\( -iname '*keystore*' "
            "-o -iname '*.p12' -o -iname '*.pfx' -o -iname '*.jks' -o -iname '*.jceks' \\) "
            "| grep -q .",
        )
    )
    for path in STACK_PATHS:
        checks.append((f"no stack path: {path}", f"[ ! -e {path} ]"))
    static_paths = " ".join(shlex.quote(f"/opt/hume/config/{path}") for path in STATIC_FILES)
    checks += [
        (
            "/opt/hume/config holds only the static files",
            "[ \"$(find /opt/hume/config -type f "
            f"! -iname {shlex.quote(LICENCE_FILE_PATTERN)} | sort)\" "
            f"= \"$(printf '%s\\n' {static_paths} | sort)\" ]",
        ),
        ("no cluster name in ecs.config", "! grep -q '^ECS_CLUSTER=' /etc/ecs/ecs.config"),
        (
            "no env files",
            "! find /opt /root /home /etc/ecs /var/lib/ecs -name '.env*' | grep -q .",
        ),
    ]
    # TODO: Add checks that run inside each Hume image.
    checks += [
        (
            "no Docker config file",
            "! find /root /home -maxdepth 3 -path '*/.docker/config.json' | grep -q .",
        ),
        (
            "no registry auth strings on disk",
            "! grep -rIlE '\"auth\": *\"' /root /home /etc /opt /var/lib/ecs | grep -q .",
        ),
        (
            "no registry host under /root or /home",
            "! grep -rIl docker.graphaware.com /root /home | grep -q .",
        ),
        (
            "no authorized_keys file has content",
            "! find /root/.ssh /home/*/.ssh -name authorized_keys -size +0 | grep -q .",
        ),
        ("no shell history", "! ls /root/.bash_history /home/*/.bash_history | grep -q ."),
        ("AWS CLI present", "command -v aws"),
        (
            f"root volume at least {ROOT_VOLUME_GIB - 2} GiB",
            f"[ \"$(df -BG --output=size / | tail -1 | tr -dc 0-9)\" -ge {ROOT_VOLUME_GIB - 2} ]",
        ),
        (
            "at least 20 GiB free on root",
            '[ "$(df -BG --output=avail / | tail -1 | tr -dc 0-9)" -ge 20 ]',
        ),
        ("docker is active", "systemctl is-active docker"),
        ("amazon-efs-utils installed, for EFS task volumes", "rpm -q amazon-efs-utils"),
        (
            "ECS volume plugin enabled, for EFS task volumes",
            "systemctl is-enabled amazon-ecs-volume-plugin",
        ),
        ("ecs is enabled at boot", "systemctl is-enabled ecs"),
    ]
    lines = [
        'check() { if (eval "$2") >/dev/null 2>&1; then echo "PASS $1"; else echo "FAIL $1"; fi; }'
    ]
    lines += [f"check {shlex.quote(name)} {shlex.quote(cmd)}" for name, cmd in checks]
    lines += ["echo '--- info ---'", "df -h /", "docker images --digests", "cat /etc/ecs/ecs.config"]
    return "\n".join(lines)


def default_subnet(ec2) -> str:
    subnets = ec2.describe_subnets(
        Filters=[{"Name": "default-for-az", "Values": ["true"]}]
    )["Subnets"]
    if not subnets:
        sys.exit("No default VPC subnet in this account and region.")
    return subnets[0]["SubnetId"]


def wait_for_ssm(session, instance_id: str, timeout: int = 600) -> None:
    ssm = session.client("ssm")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
        )["InstanceInformationList"]
        if info and info[0]["PingStatus"] == "Online":
            return
        time.sleep(10)
    sys.exit("Instance did not register with Session Manager in time.")


def run_remote(session, instance_id: str) -> str:
    ssm = session.client("ssm")
    command_id = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [remote_script()]},
    )["Command"]["CommandId"]
    for _ in range(60):
        time.sleep(5)
        try:
            result = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except ClientError as err:
            if err.response["Error"]["Code"] == "InvocationDoesNotExist":
                continue
            raise
        if result["Status"] not in ("Pending", "InProgress", "Delayed"):
            return result["StandardOutputContent"] + result["StandardErrorContent"]
    sys.exit("Remote checks timed out.")


def check_image_attributes(ec2, ami_id: str) -> list[str]:
    image = ec2.describe_images(ImageIds=[ami_id])["Images"][0]
    tags = {tag["Key"]: tag["Value"] for tag in image.get("Tags", [])}
    checks = {
        "AMI requires IMDSv2": image.get("ImdsSupport") == "v2.0",
        f"AMI tagged Purpose={PURPOSE}": tags.get("Purpose") == PURPOSE,
        f"AMI tagged HumeVersion={HUME_VERSION}": tags.get("HumeVersion") == HUME_VERSION,
        "AMI is available": image["State"] == "available",
    }
    return [f"{'PASS' if ok else 'FAIL'} {name}" for name, ok in checks.items()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    parser.add_argument("--ami-id", help="default: the ID in ami/ami-id.txt")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()

    ami_id = args.ami_id or Path(AMI_ID_FILE).read_text().strip()
    session = make_session(args.profile)
    account_id, caller_arn = caller_identity(session)
    ec2, iam = session.client("ec2"), session.client("iam")

    log(f"Account:  {account_id}")
    log(f"Identity: {caller_arn}")
    log(f"AMI:      {ami_id}")
    log(f"Creates: one IAM role and instance profile, one {TEST_INSTANCE_TYPE} instance (terminated on exit).")
    confirm("Proceed in this account?", args.yes)

    results = check_image_attributes(ec2, ami_id)
    run_id = str(int(time.time()))
    with temporary_instance_profile(
        iam, f"hume-ami-test-{run_id}", None, [SSM_POLICY_ARN]
    ) as profile:
        with temporary_instance(
            ec2,
            ImageId=ami_id,
            InstanceType=TEST_INSTANCE_TYPE,
            IamInstanceProfile={"Name": profile},
            SubnetId=default_subnet(ec2),
            MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": tag_list(Name="hume-ami-test", Purpose=PURPOSE),
                }
            ],
        ) as instance_id:
            ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])
            wait_for_ssm(session, instance_id)
            output = run_remote(session, instance_id)

    log("")
    log(output)
    results += [line for line in output.splitlines() if line.startswith(("PASS", "FAIL"))]
    failures = [line for line in results if line.startswith("FAIL")]
    log("")
    log(f"{len(results) - len(failures)} passed, {len(failures)} failed")
    for line in failures:
        log(line)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
