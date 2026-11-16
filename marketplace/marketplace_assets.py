#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3[crt]"]
# ///
"""Create the AWS Marketplace ingestion role and publish the listing assets.

Creates, in the seller account and us-east-1:
  1. The IAM role AWS Marketplace assumes to copy and scan the AMI.
  2. A public S3 bucket holding the template and the architecture diagram.

Each step is safe to run again. It prints the values the seller portal asks for.

Usage:
    ./marketplace/marketplace_assets.py --profile marketplace
"""

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "stack" / "hume-ecs.template.yaml"
DIAGRAM = ROOT / "docs" / "images" / "hume-on-ecs-architecture.png"
ROLE_NAME = "MarketplaceAmiIngestion"
INGESTION_POLICY_ARN = "arn:aws:iam::aws:policy/AWSMarketplaceAmiIngestion"
TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "assets.marketplace.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    parser.add_argument(
        "--version", default="3.2.1", help="Key prefix for this listing version."
    )
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation.")
    return parser.parse_args()


def ensure_role(iam) -> str:
    """Create the ingestion role if missing, attach its policy, return its ARN."""
    try:
        role = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(TRUST_POLICY),
            Description="Lets AWS Marketplace copy and scan the Hume AMI.",
        )["Role"]
        print(f"Created role {ROLE_NAME}.")
    except ClientError as error:
        if error.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        role = iam.get_role(RoleName=ROLE_NAME)["Role"]
        print(f"Role {ROLE_NAME} already exists.")
    iam.attach_role_policy(RoleName=ROLE_NAME, PolicyArn=INGESTION_POLICY_ARN)
    return role["Arn"]


def ensure_public_bucket(s3, bucket: str) -> None:
    """Create the bucket if missing and allow public reads of its objects."""
    try:
        s3.create_bucket(Bucket=bucket)
        print(f"Created bucket {bucket}.")
    except ClientError as error:
        if error.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
            raise
        print(f"Bucket {bucket} already exists.")
    # ACLs stay blocked. Only the bucket policy below grants public reads.
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": False,
            "RestrictPublicBuckets": False,
        },
    )
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "PublicReadForMarketplace",
                "Effect": "Allow",
                "Principal": "*",
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{bucket}/*",
            }
        ],
    }
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))


def upload(s3, bucket: str, key: str, path: Path, content_type: str) -> str:
    """Upload one file, check that it reads back publicly, return its URL."""
    s3.upload_file(str(path), bucket, key, ExtraArgs={"ContentType": content_type})
    url = f"https://{bucket}.s3.{REGION}.amazonaws.com/{key}"
    with urllib.request.urlopen(url) as response:
        if response.status != 200:
            sys.exit(f"{url} returned {response.status}, not 200.")
    return url


def main() -> None:
    args = parse_args()
    if not args.profile:
        sys.exit("Set AWS_PROFILE or pass --profile. This script never guesses.")
    for path in (TEMPLATE, DIAGRAM):
        if not path.is_file():
            sys.exit(f"Missing {path}.")

    session = boto3.Session(profile_name=args.profile, region_name=REGION)
    identity = session.client("sts").get_caller_identity()
    account = identity["Account"]
    bucket = f"hume-marketplace-assets-{account}"

    print(f"Account:  {account}")
    print(f"Identity: {identity['Arn']}")
    print(f"Creates:  IAM role {ROLE_NAME}, public S3 bucket {bucket}")
    print(f"Uploads:  {TEMPLATE.name}, {DIAGRAM.name} under {args.version}/")
    if not args.yes and input("Continue? [y/N] ").strip().lower() != "y":
        sys.exit("Aborted. Nothing was created.")

    role_arn = ensure_role(session.client("iam"))
    s3 = session.client("s3")
    ensure_public_bucket(s3, bucket)
    template_url = upload(
        s3, bucket, f"{args.version}/{TEMPLATE.name}", TEMPLATE, "text/yaml"
    )
    diagram_url = upload(
        s3, bucket, f"{args.version}/hume-architecture.png", DIAGRAM, "image/png"
    )

    print()
    print("Seller portal values:")
    print(f"  IAM access role ARN:          {role_arn}")
    print(f"  CloudFormation template link: {template_url}")
    print(f"  Architecture diagram link:    {diagram_url}")


if __name__ == "__main__":
    main()
