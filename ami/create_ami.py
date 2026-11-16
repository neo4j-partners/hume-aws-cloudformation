#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3[crt]", "python-dotenv"]
# ///
"""Build the Hume AMI on the ECS-optimized Amazon Linux 2023 base.

Flow, following neo4j-ee/marketplace/create-ami.sh: launch a build instance,
build through UserData, let it stop itself, create the image, enforce IMDSv2,
tag, write the ID to a file, terminate.

The registry login and the Hume licence key in the repo-root .env are stored in a
temporary Secrets Manager secret. The build instance reads it through a temporary IAM
role and writes the licence key to a file that only uid 1001 can read. The secret, the
role, and the instance are removed when the script ends, including on failure.

Usage:
    AWS_PROFILE=marketplace ./ami/create_ami.py
"""

import argparse
import base64
import io
import json
import os
import sys
import tarfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values

from aws_helpers import (
    PURPOSE,
    REGION,
    caller_identity,
    confirm,
    console_tail,
    log,
    make_session,
    tag_list,
    temporary_instance,
    temporary_instance_profile,
)

AMI_DIR = Path(__file__).resolve().parent
FILES_DIR = AMI_DIR / "files"
ENV_FILE = AMI_DIR.parent / ".env"
AMI_ID_FILE = AMI_DIR / "ami-id.txt"
BUILD_SCRIPT = AMI_DIR / "build.sh"

BASE_AMI_PARAMETER = "/aws/service/ecs/optimized-ami/amazon-linux-2023/recommended/image_id"
BUILD_INSTANCE_TYPE = "m6i.xlarge"
ROOT_VOLUME_GIB = 40
USERDATA_LIMIT_BYTES = 16384
BUILD_TIMEOUT_SECONDS = 45 * 60

REGISTRY = "docker.graphaware.com"
HUME_VERSION = "3.2.1"
HUME_ATTRIBUTE_NAME = "hume.version"
IMAGES = [
    *(
        f"{REGISTRY}/hume-core/hume-{name}:{HUME_VERSION}"
        for name in ("web", "api", "orchestra", "media", "eventstore", "config-cli")
    ),
    "postgres:17-alpine",
]
# The tasks verify the database certificate against this bundle. Update the
# hash when AWS publishes a new bundle. A mismatch stops the build.
RDS_CA_URL = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
RDS_CA_SHA256 = "90a6f1a7978a1b1b096dca6c57511ca48d3b8cf052f8e704baf358f34ea2f21b"
RDS_CA_FILE = "rds/global-bundle.pem"
# The API reads the key through a Spring config tree, which maps each file name in
# the folder to a property. Keep the folder holding this one file only.
LICENCE_FILE = "api/licence/hume.licence.key"
ECS_CONFIG = "\n".join(
    [
        "ECS_IMAGE_PULL_BEHAVIOR=prefer-cached",
        "ECS_DISABLE_IMAGE_CLEANUP=true",
        "ECS_AWSVPC_BLOCK_IMDS=true",
        'ECS_AVAILABLE_LOGGING_DRIVERS=["json-file","awslogs"]',
        f'ECS_INSTANCE_ATTRIBUTES={{"{HUME_ATTRIBUTE_NAME}":"{HUME_VERSION}"}}',
        "",
    ]
)


@dataclass(frozen=True)
class BuildSecrets:
    username: str
    password: str
    licence_key: str


def read_build_secrets() -> BuildSecrets:
    if not ENV_FILE.is_file():
        sys.exit(f"Missing {ENV_FILE}. Copy .env.example to {ENV_FILE} and fill it in.")
    values = dotenv_values(ENV_FILE)
    username = values.get("REGISTRY_USER") or ""
    password = values.get("REGISTRY_PASSWORD") or ""
    licence_key = (values.get("HUME_LICENCE_KEY") or "").strip()
    if not username or not password:
        sys.exit(f"REGISTRY_USER and REGISTRY_PASSWORD must both be set in {ENV_FILE}.")
    if not licence_key:
        sys.exit(f"HUME_LICENCE_KEY must be set in {ENV_FILE}. The AMI ships the key.")
    return BuildSecrets(username, password, licence_key)


def pack_static_files() -> str:
    """Pack ami/files into a base64 tar.gz that UserData unpacks to /opt/hume/config."""
    buffer = io.BytesIO()

    def scrub(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        return info

    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(FILES_DIR.rglob("*")):
            archive.add(path, arcname=path.relative_to(FILES_DIR), recursive=False, filter=scrub)
    return base64.b64encode(buffer.getvalue()).decode()


def render_userdata(secret_id: str) -> str:
    replacements = {
        "@@REGION@@": REGION,
        "@@SECRET_ID@@": secret_id,
        "@@REGISTRY@@": REGISTRY,
        "@@IMAGES@@": " ".join(IMAGES),
        "@@FILES_B64@@": pack_static_files(),
        "@@ECS_CONFIG@@": ECS_CONFIG,
        "@@RDS_CA_URL@@": RDS_CA_URL,
        "@@RDS_CA_SHA256@@": RDS_CA_SHA256,
        "@@RDS_CA_FILE@@": RDS_CA_FILE,
        "@@LICENCE_FILE@@": LICENCE_FILE,
    }
    userdata = BUILD_SCRIPT.read_text()
    for token, value in replacements.items():
        userdata = userdata.replace(token, value)
    size = len(userdata.encode())
    if size > USERDATA_LIMIT_BYTES:
        sys.exit(f"UserData is {size} bytes, over the {USERDATA_LIMIT_BYTES} byte limit.")
    return userdata


def resolve_base_image(session) -> tuple[str, str, str]:
    """Return the base AMI ID, its name, and its root device name."""
    image_id = session.client("ssm").get_parameter(Name=BASE_AMI_PARAMETER)["Parameter"]["Value"]
    image = session.client("ec2").describe_images(ImageIds=[image_id])["Images"][0]
    return image_id, image["Name"], image["RootDeviceName"]


def wait_for_build(ec2, instance_id: str) -> dict[str, str]:
    """Wait for the instance to stop itself, check its status tag, and return its tags."""
    ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])
    log("Build instance running. Waiting for it to stop itself (5 to 15 minutes)...")
    deadline = time.monotonic() + BUILD_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        state = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
        if state["State"]["Name"] == "stopped":
            tags = {tag["Key"]: tag["Value"] for tag in state.get("Tags", [])}
            log(f"AWS CLI preinstalled on the base AMI: {tags.get('BuildAwsCliPreinstalled', 'unknown')}")
            if tags.get("BuildStatus") != "success":
                log(console_tail(ec2, instance_id))
                sys.exit(f"Build failed. BuildStatus tag: {tags.get('BuildStatus', 'missing')}")
            return tags
        time.sleep(20)
    log(console_tail(ec2, instance_id))
    sys.exit("Timed out waiting for the build instance to stop.")


def create_image(
    ec2, instance_id: str, name: str, base_ami: str, base_name: str, build_tags: dict[str, str]
) -> str:
    tags = tag_list(
        Name=name,
        Purpose=PURPOSE,
        BaseOS="Amazon Linux 2023 ECS-optimized",
        HumeVersion=HUME_VERSION,
        IMDSv2="v2.0-required",
        BaseAmiId=base_ami,
        BaseAmiName=base_name,
        EcsInitPackage=build_tags.get("BuildEcsInit", "unknown"),
        DockerPackage=build_tags.get("BuildDocker", "unknown"),
        EfsUtilsPackage=build_tags.get("BuildEfsUtils", "unknown"),
        AwsCliPreinstalled=build_tags.get("BuildAwsCliPreinstalled", "unknown"),
    )
    image_id = ec2.create_image(
        InstanceId=instance_id,
        Name=name,
        Description=f"Hume {HUME_VERSION} AMI: ECS agent, Hume images, no credentials",
        TagSpecifications=[
            {"ResourceType": "image", "Tags": tags},
            {"ResourceType": "snapshot", "Tags": tags},
        ],
    )["ImageId"]
    log(f"Creating AMI {image_id}, waiting until available...")
    ec2.get_waiter("image_available").wait(
        ImageIds=[image_id], WaiterConfig={"Delay": 15, "MaxAttempts": 120}
    )
    # create-image does not accept an IMDS setting, so enforce it afterward.
    ec2.modify_image_attribute(ImageId=image_id, ImdsSupport={"Value": "v2.0"})
    return image_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()

    build_secrets = read_build_secrets()
    session = make_session(args.profile)
    account_id, caller_arn = caller_identity(session)
    ec2, iam, secrets = (session.client(name) for name in ("ec2", "iam", "secretsmanager"))

    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    ami_name = f"neo4j-hume-{HUME_VERSION}-{run_id}"
    base_ami, base_name, root_device = resolve_base_image(session)

    log("=== Hume AMI builder ===")
    log(f"Account:  {account_id}")
    log(f"Identity: {caller_arn}")
    log(f"Region:   {REGION}")
    log(f"Base AMI: {base_ami} {base_name} (root device {root_device}, {ROOT_VOLUME_GIB} GiB gp3)")
    log(f"AMI name: {ami_name}")
    log("Creates: one secret, one IAM role and instance profile, one build instance, one AMI.")
    confirm("Proceed in this account?", args.yes)

    secret = secrets.create_secret(
        Name=f"hume-ami-build/{run_id}",
        SecretString=json.dumps(
            {
                "username": build_secrets.username,
                "password": build_secrets.password,
                "licence_key": build_secrets.licence_key,
            }
        ),
        Tags=tag_list(Purpose=PURPOSE),
    )
    try:
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "secretsmanager:GetSecretValue",
                    "Resource": secret["ARN"],
                },
                {
                    "Effect": "Allow",
                    "Action": "ec2:CreateTags",
                    "Resource": f"arn:aws:ec2:{REGION}:{account_id}:instance/*",
                    "Condition": {"StringEquals": {"ec2:ResourceTag/Purpose": PURPOSE}},
                },
            ],
        }
        with temporary_instance_profile(iam, f"hume-ami-build-{run_id}", policy) as profile:
            with temporary_instance(
                ec2,
                ImageId=base_ami,
                InstanceType=BUILD_INSTANCE_TYPE,
                UserData=render_userdata(secret["Name"]),
                IamInstanceProfile={"Name": profile},
                MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
                BlockDeviceMappings=[
                    {
                        "DeviceName": root_device,
                        "Ebs": {
                            "VolumeSize": ROOT_VOLUME_GIB,
                            "VolumeType": "gp3",
                            "DeleteOnTermination": True,
                        },
                    }
                ],
                TagSpecifications=[
                    {
                        "ResourceType": "instance",
                        "Tags": tag_list(Name="hume-ami-build", Purpose=PURPOSE),
                    }
                ],
            ) as instance_id:
                build_tags = wait_for_build(ec2, instance_id)
                image_id = create_image(
                    ec2, instance_id, ami_name, base_ami, base_name, build_tags
                )
    finally:
        log(f"Deleting secret {secret['Name']}")
        secrets.delete_secret(SecretId=secret["ARN"], ForceDeleteWithoutRecovery=True)

    AMI_ID_FILE.write_text(f"{image_id}\n")
    log("")
    log(f"AMI ready: {image_id} ({ami_name})")
    log(f"AMI ID written to {AMI_ID_FILE}")
    log("Next: ./ami/test_ami.py")


if __name__ == "__main__":
    main()
