"""Helpers shared by create_ami.py and test_ami.py."""

import contextlib
import json
import sys
import time
from collections.abc import Iterator, Sequence

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
PURPOSE = "hume-ami"
EC2_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "ec2.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}


def log(message: str = "") -> None:
    print(message, flush=True)


def make_session(profile: str | None) -> boto3.Session:
    if not profile:
        sys.exit("Set AWS_PROFILE or pass --profile. This script never guesses an account.")
    return boto3.Session(profile_name=profile, region_name=REGION)


def caller_identity(session: boto3.Session) -> tuple[str, str]:
    identity = session.client("sts").get_caller_identity()
    return identity["Account"], identity["Arn"]


def confirm(question: str, assume_yes: bool) -> None:
    if assume_yes:
        return
    if input(f"{question} [y/N] ").strip().lower() != "y":
        sys.exit("Aborted. Nothing was created.")


def tag_list(**tags: str) -> list[dict[str, str]]:
    return [{"Key": key, "Value": value} for key, value in tags.items()]


@contextlib.contextmanager
def temporary_instance_profile(
    iam,
    name: str,
    inline_policy: dict | None,
    managed_policy_arns: Sequence[str] = (),
) -> Iterator[str]:
    """Create a role and instance profile, and delete both on exit."""
    tags = tag_list(Purpose=PURPOSE)
    log(f"Creating IAM role and instance profile {name}")
    try:
        iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(EC2_TRUST_POLICY),
            Tags=tags,
        )
        if inline_policy is not None:
            iam.put_role_policy(
                RoleName=name,
                PolicyName="build",
                PolicyDocument=json.dumps(inline_policy),
            )
        for arn in managed_policy_arns:
            iam.attach_role_policy(RoleName=name, PolicyArn=arn)
        iam.create_instance_profile(InstanceProfileName=name, Tags=tags)
        iam.add_role_to_instance_profile(InstanceProfileName=name, RoleName=name)
        yield name
    finally:
        log(f"Deleting IAM role and instance profile {name}")
        _delete_profile_and_role(iam, name)


def _delete_profile_and_role(iam, name: str) -> None:
    steps = (
        lambda: iam.remove_role_from_instance_profile(
            InstanceProfileName=name, RoleName=name
        ),
        lambda: iam.delete_instance_profile(InstanceProfileName=name),
        lambda: iam.delete_role_policy(RoleName=name, PolicyName="build"),
        lambda: _detach_managed_policies(iam, name),
        lambda: iam.delete_role(RoleName=name),
    )
    for step in steps:
        try:
            step()
        except ClientError as err:
            if err.response["Error"]["Code"] != "NoSuchEntity":
                log(f"WARNING: IAM cleanup step failed for {name}: {err}")


def _detach_managed_policies(iam, name: str) -> None:
    attached = iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]
    for policy in attached:
        iam.detach_role_policy(RoleName=name, PolicyArn=policy["PolicyArn"])


@contextlib.contextmanager
def temporary_instance(ec2, **run_args) -> Iterator[str]:
    """Launch one instance and terminate it, waiting for termination, on exit."""
    instance_id = _run_instance_with_retry(ec2, **run_args)
    log(f"Launched instance {instance_id}")
    try:
        yield instance_id
    finally:
        log(f"Terminating instance {instance_id}")
        ec2.terminate_instances(InstanceIds=[instance_id])
        ec2.get_waiter("instance_terminated").wait(InstanceIds=[instance_id])


def _run_instance_with_retry(ec2, attempts: int = 12, **run_args) -> str:
    """A new instance profile can take several seconds to become usable."""
    for attempt in range(attempts):
        try:
            response = ec2.run_instances(MinCount=1, MaxCount=1, **run_args)
            return response["Instances"][0]["InstanceId"]
        except ClientError as err:
            code = err.response["Error"]["Code"]
            message = err.response["Error"]["Message"]
            propagating = code == "InvalidParameterValue" and "instance profile" in message.lower()
            if not propagating or attempt == attempts - 1:
                raise
            time.sleep(10)
    raise AssertionError("unreachable")


def console_tail(ec2, instance_id: str, lines: int = 60) -> str:
    output = ec2.get_console_output(InstanceId=instance_id, Latest=True).get("Output", "")
    return "\n".join(output.splitlines()[-lines:])
