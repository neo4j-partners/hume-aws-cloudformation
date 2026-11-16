#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3[crt]", "python-dotenv"]
# ///
"""Check that an account holds what `deploy.py up` needs.

    uv run stack/check_prereqs.py
    uv run stack/check_prereqs.py --profile <profile> --region <region>

It is read only. It prints the account id first, then one line per check:

    ami           the Hume AMI exists and is available
    hosted zone   the Route 53 zone exists
    secret        hume/licence-key and hume/neo4j-truststore each exist
    cognito pool  the user pool of the issuer exists

The values come from the same places as deploy.py. The AMI is --ami-id, then
HUME_AMI_ID, then ami/ami-id.txt. The zone is HUME_HOSTED_ZONE_ID. The pool id
is the last part of HUME_OIDC_ISSUER_URI. The pool check is skipped when the
issuer is not a Cognito address, or when it is unset and HUME_AUTH_PROVIDER is
Native. Each setting is read from the process environment, then the repo-root
.env.

The exit code is 1 when a check fails or a required value is unset. A secret is
only described, so its value is never read or printed.
"""

import argparse
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass

import boto3
from botocore.exceptions import BotoCoreError, ClientError

import deploy

ISSUER_VAR = "HUME_OIDC_ISSUER_URI"
SECRET_NAMES = (
    deploy.LICENCE_SECRET_NAME,
    deploy.TRUSTSTORE_SECRET_NAME,
)
# A Cognito issuer is https://cognito-idp.<region>.amazonaws.com/<pool id>.
COGNITO_ISSUER = re.compile(
    r"https://cognito-idp\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?/([\w-]+)/?"
)


@dataclass
class Result:
    """One printed line. ok is False when the check failed."""

    label: str
    text: str
    ok: bool = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--profile",
        help=f"AWS profile (default: {deploy.PROFILE_VAR}, then "
        f"{deploy.DEFAULT_PROFILE})",
    )
    parser.add_argument(
        "--region", help=f"AWS region (default: {deploy.DEFAULT_REGION})"
    )
    parser.add_argument(
        "--ami-id",
        help=f"Hume AMI id (default: {deploy.AMI_ID_VAR}, then "
        f"{deploy.AMI_ID_FILE.name})",
    )
    return parser.parse_args()


def check_account(session: boto3.Session) -> list[Result]:
    account = session.client("sts").get_caller_identity()["Account"]
    return [Result("account", account)]


def check_ami(session: boto3.Session, ami_id: str) -> list[Result]:
    images = session.client("ec2").describe_images(ImageIds=[ami_id])["Images"]
    if not images:
        return [Result("ami", f"{ami_id} not found", ok=False)]
    image = images[0]
    state = image["State"]
    return [Result("ami", f"{ami_id} {image['Name']} {state}", ok=state == "available")]


def check_hosted_zone(session: boto3.Session) -> list[Result]:
    zone_id = deploy.env_setting(deploy.ZONE_VAR)
    if not zone_id:
        return [Result("hosted zone", f"not set: {deploy.ZONE_VAR}", ok=False)]
    zone = session.client("route53").get_hosted_zone(Id=zone_id)["HostedZone"]
    kind = "private" if zone["Config"]["PrivateZone"] else "public"
    return [Result("hosted zone", f"{zone_id} {zone['Name']} {kind}")]


def check_secrets(session: boto3.Session) -> list[Result]:
    sm = session.client("secretsmanager")
    results = []
    for name in SECRET_NAMES:
        try:
            secret = sm.describe_secret(SecretId=name)
        except ClientError as error:
            code = error.response["Error"]["Code"]
            text = "missing" if code == "ResourceNotFoundException" else code
            results.append(Result("secret", f"{name} {text}", ok=False))
            continue
        if secret.get("DeletedDate"):
            results.append(Result("secret", f"{name} scheduled for deletion", ok=False))
        else:
            results.append(Result("secret", f"{name} present"))
    return results


def check_user_pool(session: boto3.Session) -> list[Result]:
    issuer = deploy.env_setting(ISSUER_VAR)
    if not issuer:
        native = deploy.resolve_auth_provider() == deploy.NATIVE_PROVIDER
        return [Result("cognito pool", f"not set: {ISSUER_VAR}", ok=native)]
    match = COGNITO_ISSUER.fullmatch(issuer.strip())
    if not match:
        return [Result("cognito pool", "skipped, the issuer is not Cognito")]
    region, pool_id = match.groups()
    cognito = session.client("cognito-idp", region_name=region)
    pool = cognito.describe_user_pool(UserPoolId=pool_id)["UserPool"]
    return [Result("cognito pool", f"{pool_id} {pool['Name']}")]


def run_check(label: str, check: Callable[[], list[Result]]) -> list[Result]:
    """Run one check. An AWS error becomes a failed line, never a traceback."""
    try:
        return check()
    except ClientError as error:
        return [Result(label, f"FAILED {error.response['Error']['Code']}", ok=False)]
    except BotoCoreError as error:
        return [Result(label, f"FAILED {error}", ok=False)]


def run(args: argparse.Namespace) -> int:
    session = deploy.cli_session(args)
    results = run_check("account", lambda: check_account(session))
    show(results)
    if not results[0].ok:
        return 1
    ami_id = deploy.resolve_ami_id(args)
    checks = [
        ("ami", lambda: check_ami(session, ami_id)),
        ("hosted zone", lambda: check_hosted_zone(session)),
        ("secret", lambda: check_secrets(session)),
        ("cognito pool", lambda: check_user_pool(session)),
    ]
    for label, check in checks:
        lines = run_check(label, check)
        show(lines)
        results += lines
    return 0 if all(result.ok for result in results) else 1


def show(results: list[Result]) -> None:
    for result in results:
        print(f"{result.label:13} {result.text}")


def main() -> int:
    return run(parse_args())


if __name__ == "__main__":
    sys.exit(main())
