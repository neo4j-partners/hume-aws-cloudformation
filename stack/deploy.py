#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3[crt]", "python-dotenv"]
# ///
"""Deploy, smoke test, and tear down the Hume ECS foundation stack.

Settings come from a command-line flag, then the process environment, then the
repo-root .env. Each stack gets one record file, .deploy/<stack>.txt, written
before the stack is created so that teardown works after a failed create. The
record holds ids and names only, never secret values.

The stack joins the network of a three-server Neo4j EE stack. It needs the
settings in NEO4J_SETTINGS: the Neo4j EE stack name and the truststore secret
from the truststore command. The template reads the other Neo4j values from
Parameter Store under /neo4j-ee/<neo4j stack>/.

Sign-in is through OIDC by default, which needs HUME_OIDC_ISSUER_URI and
HUME_OIDC_CLIENT_ID. HUME_AUTH_PROVIDER=Native keeps the built-in admin user.
"""

import argparse
import base64
import io
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import NamedTuple

import boto3
from botocore.exceptions import ClientError, WaiterError
from dotenv import dotenv_values

SCRIPT_DIR = Path(__file__).parent
TEMPLATE = SCRIPT_DIR / "hume-ecs.template.yaml"
DEPLOY_DIR = SCRIPT_DIR / ".deploy"
ENV_FILE = SCRIPT_DIR.parent / ".env"
AMI_ID_FILE = SCRIPT_DIR.parent / "ami" / "ami-id.txt"
LICENCE_VAR = "HUME_LICENCE_KEY"
AMI_ID_VAR = "HUME_AMI_ID"
CIDRS_VAR = "ALLOWED_CIDRS"
PROFILE_VAR = "AWS_PROFILE"
# The template has one parameter per address, AllowedCidr1 to AllowedCidr5.
MAX_CIDRS = 5
# Outside the hume/<stack>/ prefix, so teardown never deletes either secret.
LICENCE_SECRET_NAME = "hume/licence-key"
# keytool reads the generated password from this variable, so it stays out of argv.
KEYTOOL_PASSWORD_VAR = "HUME_KEYTOOL_STOREPASS"
# Outside the hume/<stack>/ prefix, so teardown keeps it for the next stack.
TRUSTSTORE_SECRET_NAME = "hume/neo4j-truststore"
CA_FILE_VAR = "HUME_NEO4J_CA_FILE"
# Where the instance mounts the truststore file inside orchestra and api.
TRUSTSTORE_CONTAINER_PATH = "/opt/hume-truststore"
TRUSTSTORE_FILE = "truststore.p12"
# Matches the 10 second TTL of the service discovery records.
JVM_DNS_TTL_SECONDS = 10
# Without a local keytool, docker runs the one from this image. Docker pulls it
# on first use.
KEYTOOL_IMAGE = "neo4j:2026.06.0-enterprise"
PEM_CERTIFICATE = re.compile(
    r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", re.DOTALL
)
BASE_DOMAIN_VAR = "HUME_BASE_DOMAIN"
# The setting that held the full web host name. A value left there would build
# hume.hume.<base>, so resolve_base_domain refuses to run while it is set.
OLD_DOMAIN_VAR = "HUME_DOMAIN_NAME"
ZONE_VAR = "HUME_HOSTED_ZONE_ID"
ZONE_ID_PATTERN = re.compile(r"^Z[A-Z0-9]{8,31}$")
DNS_NAME = r"([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)*[a-z0-9]([a-z0-9-]*[a-z0-9])?"
# The same pattern as the EeStackName parameter in the template.
STACK_NAME = r"[a-zA-Z][-a-zA-Z0-9]{0,127}"
EE_STACK_VAR = "HUME_NEO4J_STACK_NAME"
# The Neo4j EE stack publishes its values under this Parameter Store path.
NEO4J_SSM_PREFIX = "/neo4j-ee/{stack}/"
PUBLIC_SUBNET_NAMES = ("public-subnet-1-id", "public-subnet-2-id", "public-subnet-3-id")
PRIVATE_SUBNET_NAMES = ("private-subnet-1-id", "private-subnet-2-id")
# The values that the Neo4jEeLookup resource of the template reads, by their name
# under NEO4J_SSM_PREFIX.
NEO4J_LOOKUP_NAMES = (
    "advertised-dns",
    "vpc-id",
    *PUBLIC_SUBNET_NAMES,
    *PRIVATE_SUBNET_NAMES,
    "external-sg-id",
    "vpc-endpoint-sg-id",
    "app-username",
    "app-password-secret-arn",
    "kms-key-arn",
)
# A one-server Neo4j EE stack publishes only the first public subnet.
ONE_SERVER_MISSING = {"public-subnet-2-id", "public-subnet-3-id"}
# reimport runs the configuration task again, then restarts the one service that
# holds a Neo4j driver.
CONFIG_CLI_OUTPUT = "ConfigCliTaskDefinitionArn"
REIMPORT_SERVICE = "orchestra"
# The services that read the truststore. Both restart after the file changes.
TRUSTSTORE_SERVICES = ("orchestra", "api")
REIMPORT_OUTPUTS = (
    "ClusterName",
    "CapacityProviderName",
    "InstanceSubnetId",
    "InstanceSecurityGroupId",
)
# refresh-truststore writes the truststore file here on the Hume instance, the
# same path the TruststoreStep of the template writes at boot.
TRUSTSTORE_HOST_PATH = "/opt/hume/truststore/hume-truststore"
TRUSTSTORE_SECRET_KEYS = ("truststore", "java_tool_options")
TRUSTSTORE_PASSWORD_OPTION = re.compile(r"-Djavax\.net\.ssl\.trustStorePassword=(\S+)")
SECRET_ARN = (
    r"arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:\d{12}:secret:"
    r"[A-Za-z0-9/_+=.@-]+-[A-Za-z0-9]{6}"
)


TRUSTSTORE_ARN_VAR = "HUME_NEO4J_TRUSTSTORE_SECRET_ARN"

AUTH_PROVIDER_VAR = "HUME_AUTH_PROVIDER"
OIDC_PROVIDER = "Oidc"
NATIVE_PROVIDER = "Native"
AUTH_PROVIDERS = (OIDC_PROVIDER, NATIVE_PROVIDER)

# The validate command checks the Bolt port of Neo4j from the Hume instance.
NEO4J_BOLT_PORT = 7687
TLS_CHECK_CA_PATH = "/tmp/neo4j-ca.pem"
TLS_CHECK_PASS = "Verify return code: 0 (ok)"
TLS_CHECK_LINES = (
    "Verify return code|Verification|subject=|issuer=|Protocol|Cipher|CONNECTED|errno"
)

# The Spine knowledge graph reaches the config-cli import through a secret, because
# the files are too large for the template. The value is base64 of a bzip2 tar.
SPINE_CONFIG_DIR_VAR = "HUME_SPINE_CONFIG_DIR"
SPINE_SECRET_SUFFIX = "spine-config"
SERVER_ID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
SPINE_REQUIRED_FILE = "spine-kg/knowledge-graph.yml"
# A secret holds 65,536 bytes. The limit leaves room for the files to grow.
SPINE_SECRET_LIMIT = 60_000
# Files that need the webhook server or the HTTP API resource, which the stack does
# not run, or the spine.uk.analytic database, which the backup does not hold.
SPINE_SKIPPED_FILES = (
    "resources/webhook-server.yml",
    "resources/spine-http-api.yml",
    "resources/spine-analytic-neo4j-sa.yml",
    "spine-kg/workflows/ccr-records-import.yml",
    "spine-kg/actions/get-ccr-records-graph.yml",
    "spine-kg/actions/expand-info-about-ccr-imported-phone-numbers.yml",
)


class HumeHosts(NamedTuple):
    """The three host names of a stack."""

    web: str
    api: str
    media: str


class Neo4jSetting(NamedTuple):
    parameter: str
    variable: str
    pattern: str
    example: str


NEO4J_SETTINGS = (
    Neo4jSetting(
        "EeStackName",
        EE_STACK_VAR,
        STACK_NAME,
        "the name of a three-server Neo4j EE stack in this account and Region",
    ),
    Neo4jSetting(
        "Neo4jTruststoreSecretArn",
        TRUSTSTORE_ARN_VAR,
        SECRET_ARN,
        "the secret that the truststore command stores",
    ),
)


# The template parameters that restore a stack from an earlier one, each with
# the deploy flag that sets it. They are set on create only.
RESTORE_FLAGS = {
    "DbSnapshotIdentifier": "db_snapshot_identifier",
    "ExistingFileSystemId": "existing_file_system_id",
    "ExistingDataKeyArn": "existing_data_key_arn",
}


class OidcSetting(NamedTuple):
    parameter: str
    variable: str
    required: bool


# The template parameters of the Oidc provider. The issuer and the client id have
# no default. The others default in the template to the values proven on Cognito,
# so an unset one leaves the parameter out.
OIDC_SETTINGS = (
    OidcSetting("OidcIssuerUri", "HUME_OIDC_ISSUER_URI", True),
    OidcSetting("OidcClientId", "HUME_OIDC_CLIENT_ID", True),
    OidcSetting("OidcRolesMappings", "HUME_OIDC_ROLES_MAPPINGS", False),
    OidcSetting("OidcRolesClaimPath", "HUME_OIDC_ROLES_CLAIM_PATH", False),
    OidcSetting("OidcPrincipalClaim", "HUME_OIDC_PRINCIPAL_CLAIM", False),
    OidcSetting("OidcJwkSetUri", "HUME_OIDC_JWK_SET_URI", False),
    OidcSetting("OidcScope", "HUME_OIDC_SCOPE", False),
)
# The record file keeps the certificate validation records, one per host, so
# that teardown can delete them after the certificate is gone. Each key holds a
# comma-separated list, and the two lists are in the same order.
VALIDATION_NAMES_KEY = "ValidationRecordNames"
VALIDATION_VALUES_KEY = "ValidationRecordValues"
ACM_VALIDATION_SUFFIX = ".acm-validations.aws"
CERTIFICATE_LOGICAL_ID = "Certificate"
# CloudFormation accepts at most this many bytes as TemplateBody. A larger
# template is staged in a private bucket and passed as TemplateURL.
TEMPLATE_DIRECT_LIMIT = 51_200
TEMPLATE_BUCKET_PREFIX = "hume-ecs-templates"
TEMPLATE_KEY_PREFIX = "hume-ecs/"
BUCKET_RECORD_KEY = "TemplateBucket"
OBJECT_RECORD_KEY = "TemplateObjectKey"
DEFAULT_PROFILE = "marketplace"
DEFAULT_REGION = "us-east-1"
CHECKIP_URL = "https://checkip.amazonaws.com"
INSTANCE_TYPES = ["m6i.large", "m6i.xlarge"]
STACK_TAGS = [
    {"Key": "Project", "Value": "hume-ecs"},
]
TRUNK_ATTRIBUTE = "ecs.awsvpc-trunk-id"
# Errors from the load balancer and database describe calls for a missing one.
GONE_ERROR_CODES = ("LoadBalancerNotFound", "TargetGroupNotFound", "DBInstanceNotFound")
# Direct describe call, method and argument name, and the error code that means
# the resource is gone, for the EC2 types a stack owns.
EC2_DESCRIBE = {
    "security-group": (
        "describe_security_groups",
        "GroupIds",
        "InvalidGroup.NotFound",
    ),
}
DELETE_ATTEMPTS = 3
DELETE_RETRY_WAIT = 120
GROUP_DRAIN_TIMEOUT = 900
GROUP_DRAIN_POLL = 15


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", help=f"AWS profile (default: {DEFAULT_PROFILE})")
    parser.add_argument("--region", help=f"AWS region (default: {DEFAULT_REGION})")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, text in [
        ("deploy", "Create the stack and wait for it"),
        ("up", "Create or update the licence secret, then deploy"),
    ]:
        deploy = sub.add_parser(name, help=text)
        deploy.add_argument(
            "--ami-id",
            help=f"Hume AMI id (default: {AMI_ID_VAR}, then {AMI_ID_FILE.name})",
        )
        deploy.add_argument(
            "--allowed-cidr",
            help=f"Comma-separated IPv4 ranges for the ALB (default: {CIDRS_VAR})",
        )
        deploy.add_argument(
            "--base-domain",
            help=(
                "Base of the Hume host names "
                f"(default: {BASE_DOMAIN_VAR})"
            ),
        )
        deploy.add_argument(
            "--hosted-zone-id",
            help=f"Route 53 zone that holds the host names (default: {ZONE_VAR})",
        )
        deploy.add_argument(
            "--yes", action="store_true", help="Skip the confirmation prompt"
        )
        deploy.add_argument(
            "--instance-type", default="m6i.xlarge", choices=INSTANCE_TYPES
        )
        deploy.add_argument("--stack-name", help="Default: hume-ecs-<epoch seconds>")
        deploy.add_argument(
            "--db-snapshot-identifier",
            help="Restore the database from this snapshot of an earlier stack",
        )
        deploy.add_argument(
            "--existing-file-system-id",
            help="Use the EFS file system that an earlier stack kept",
        )
        deploy.add_argument(
            "--existing-data-key-arn",
            help=(
                "Use the KMS key that an earlier stack kept. Required with "
                "either option above"
            ),
        )

    sub.add_parser("licence", help=f"Create or update the {LICENCE_SECRET_NAME} secret")

    truststore = sub.add_parser(
        "truststore",
        help=f"Create or update the {TRUSTSTORE_SECRET_NAME} secret from a CA file",
    )
    truststore.add_argument(
        "--ca-file",
        help=f"PEM file with the CA certificate of Neo4j (default: {CA_FILE_VAR})",
    )
    truststore.add_argument(
        "--secret-name",
        default=TRUSTSTORE_SECRET_NAME,
        help=f"Secret to write (default: {TRUSTSTORE_SECRET_NAME})",
    )

    truststore_options = sub.add_parser(
        "truststore-options",
        help="Update only java_tool_options in the truststore secret",
    )
    truststore_options.add_argument(
        "--secret-name",
        default=TRUSTSTORE_SECRET_NAME,
        help=f"Secret to update (default: {TRUSTSTORE_SECRET_NAME})",
    )

    smoke = sub.add_parser("smoke", help="Check the instance, then run the test task")
    smoke.add_argument("stack_name", nargs="?", help="Default: the only record file")
    smoke.add_argument(
        "--expect-attribute",
        metavar="NAME=VALUE",
        help="Instance attribute that the AMI must set, such as the Hume version",
    )

    validate = sub.add_parser(
        "validate",
        help=(
            "Check from the Hume instance that Neo4j answers "
            "on the Bolt port with a certificate the CA file verifies"
        ),
    )
    validate.add_argument("stack_name", nargs="?", help="Default: the only record file")
    validate.add_argument(
        "--ca-file",
        help=f"PEM file with the CA certificate of Neo4j (default: {CA_FILE_VAR})",
    )

    reimport = sub.add_parser(
        "reimport",
        help=(
            "Run the configuration import again and restart orchestra, after "
            "the Neo4j app password is rotated"
        ),
    )
    reimport.add_argument("stack_name", nargs="?", help="Default: the only record file")
    reimport.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt"
    )

    refresh_truststore = sub.add_parser(
        "refresh-truststore",
        help=(
            "Write the current truststore secret to the Hume instance, then "
            "restart orchestra and api"
        ),
    )
    refresh_truststore.add_argument(
        "stack_name", nargs="?", help="Default: the only record file"
    )
    refresh_truststore.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt"
    )

    spine_config = sub.add_parser(
        "spine-config",
        help=(
            "Store the Spine knowledge graph files in the stack's spine-config "
            f"secret. They come from the directory in {SPINE_CONFIG_DIR_VAR}"
        ),
    )
    spine_config.add_argument("stack_name", help="Stack whose record file exists")

    teardown = sub.add_parser("teardown", help="Delete the stack and what it leaves")
    teardown.add_argument("stack_name", help="Stack to delete. Asked again to confirm")
    teardown.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt"
    )

    sub.add_parser(
        "delete-template-bucket",
        help="Delete the empty template bucket, after the last stack is gone",
    )
    return parser.parse_args()


def record_path(stack: str) -> Path:
    return DEPLOY_DIR / f"{stack}.txt"


def write_record(stack: str, values: dict[str, str]) -> None:
    DEPLOY_DIR.mkdir(exist_ok=True)
    lines = [f"{key} = {value}" for key, value in values.items()]
    record_path(stack).write_text("\n".join(lines) + "\n")


def read_record(stack: str) -> dict[str, str]:
    path = record_path(stack)
    if not path.exists():
        sys.exit(f"No record file at {path}")
    pairs = (line.split(" = ", 1) for line in path.read_text().splitlines() if line)
    return {key: value for key, value in pairs}


def format_age(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min"
    return f"{hours // 24} d {hours % 24} h"


def newest_stack() -> str:
    """Return the stack of the only record file. Several records need a name."""
    records = sorted(DEPLOY_DIR.glob("*.txt"), key=lambda p: p.stat().st_mtime)
    if not records:
        sys.exit(f"No record files in {DEPLOY_DIR}")
    if len(records) > 1:
        now = time.time()
        listing = "\n".join(
            f"  {path.stem}  (record {format_age(now - path.stat().st_mtime)} old)"
            for path in reversed(records)
        )
        sys.exit(
            f"{len(records)} record files in {DEPLOY_DIR}. "
            f"Pass the stack name:\n{listing}"
        )
    return records[0].stem


def make_session(args: argparse.Namespace, record: dict[str, str]) -> boto3.Session:
    return boto3.Session(
        profile_name=args.profile or record.get("Profile", DEFAULT_PROFILE),
        region_name=args.region or record.get("Region", DEFAULT_REGION),
    )


def env_setting(name: str) -> str | None:
    """Read one setting from the process environment, then the repo-root .env."""
    return os.environ.get(name) or dotenv_values(ENV_FILE).get(name) or None


def normalise_cidr(text: str) -> str:
    """Return an IPv4 range in CIDR form, or exit with a message.

    A bare address becomes address/32. A range must name its network address,
    such as 198.51.100.0/24, because the security group rejects host bits.
    """
    try:
        return str(ipaddress.IPv4Network(text.strip(), strict=True))
    except ValueError:
        sys.exit(
            f"{text!r} is not an IPv4 address or range. Find your address at "
            f"{CHECKIP_URL}"
        )


def parse_cidrs(text: str) -> list[str]:
    """Parse a comma-separated list. Order is kept and repeats are dropped."""
    cidrs = list(
        dict.fromkeys(normalise_cidr(part) for part in text.split(",") if part.strip())
    )
    if not cidrs:
        sys.exit(f"At least one address is required. Find yours at {CHECKIP_URL}")
    if len(cidrs) > MAX_CIDRS:
        sys.exit(f"At most {MAX_CIDRS} addresses are supported, got {len(cidrs)}.")
    return cidrs


def cidr_parameters(cidrs: list[str]) -> dict[str, str]:
    """Map addresses to the template slots. Unused slots are set empty."""
    slots = cidrs + [""] * (MAX_CIDRS - len(cidrs))
    return {f"AllowedCidr{n}": cidr for n, cidr in enumerate(slots, start=1)}


@dataclass
class Settings:
    profile: str
    region: str
    ami_id: str
    cidrs: list[str]
    instance_type: str
    base_domain: str
    hosted_zone_id: str
    neo4j: dict[str, str] = field(default_factory=dict)
    auth_provider: str = OIDC_PROVIDER
    oidc: dict[str, str] = field(default_factory=dict)
    restore: dict[str, str] = field(default_factory=dict)


def resolve_neo4j_settings() -> dict[str, str]:
    """Map the template parameters to the NEO4J_SETTINGS variables, all required."""
    values = {
        setting.parameter: text.strip()
        for setting in NEO4J_SETTINGS
        if (text := env_setting(setting.variable))
    }
    missing = [s.variable for s in NEO4J_SETTINGS if s.parameter not in values]
    if missing:
        sys.exit(
            f"The stack needs: {', '.join(missing)}. Set them in the "
            f"environment or in {ENV_FILE}."
        )
    for setting in NEO4J_SETTINGS:
        if not re.fullmatch(setting.pattern, values[setting.parameter]):
            sys.exit(
                f"{setting.variable}={values[setting.parameter]!r} is not valid. "
                f"Expected {setting.example}."
            )
    return values


def neo4j_parameters(settings: Settings) -> dict[str, str]:
    """Template parameters for the Neo4j cluster and its network."""
    return dict(settings.neo4j)


def resolve_auth_provider() -> str:
    """Read HUME_AUTH_PROVIDER. It is Oidc unless set to Native."""
    provider = env_setting(AUTH_PROVIDER_VAR) or OIDC_PROVIDER
    if provider not in AUTH_PROVIDERS:
        sys.exit(
            f"{AUTH_PROVIDER_VAR}={provider!r} is not valid. "
            f"Use one of: {', '.join(AUTH_PROVIDERS)}"
        )
    return provider


def resolve_oidc_settings() -> dict[str, str]:
    """Map the Oidc template parameters to the HUME_OIDC_* settings that are set."""
    values = {
        setting.parameter: text.strip()
        for setting in OIDC_SETTINGS
        if (text := env_setting(setting.variable))
    }
    missing = [
        s.variable for s in OIDC_SETTINGS if s.required and s.parameter not in values
    ]
    if missing:
        sys.exit(
            f"The {OIDC_PROVIDER} sign-in needs: {', '.join(missing)}. Set them in "
            f"the environment or in {ENV_FILE}, or set {AUTH_PROVIDER_VAR}="
            f"{NATIVE_PROVIDER}."
        )
    return values


def auth_parameters(settings: Settings) -> dict[str, str]:
    """Template parameters for the sign-in. Oidc is the template default."""
    if settings.auth_provider == NATIVE_PROVIDER:
        return {"AuthProvider": NATIVE_PROVIDER}
    return dict(settings.oidc)


def resolve_ami_id(args: argparse.Namespace) -> str:
    ami_id = args.ami_id or env_setting(AMI_ID_VAR)
    if not ami_id and AMI_ID_FILE.exists():
        ami_id = AMI_ID_FILE.read_text().strip()
    if not ami_id:
        sys.exit(
            f"No AMI id. Pass --ami-id, set {AMI_ID_VAR} in the environment or "
            f"in {ENV_FILE}, or build one so that {AMI_ID_FILE} exists."
        )
    return ami_id


def resolve_restore(args: argparse.Namespace) -> dict[str, str]:
    """The restore parameters that are set, checked like the template rules."""
    restore = {
        parameter: value
        for parameter, dest in RESTORE_FLAGS.items()
        if (value := getattr(args, dest))
    }
    kept_data = {"DbSnapshotIdentifier", "ExistingFileSystemId"} & set(restore)
    if kept_data and "ExistingDataKeyArn" not in restore:
        sys.exit(
            "--existing-data-key-arn is required with --db-snapshot-identifier "
            "or --existing-file-system-id, because the kept data stays encrypted "
            "with the key of the earlier stack. Teardown of that stack printed it."
        )
    return restore


def resolve_settings(args: argparse.Namespace) -> Settings:
    cidr_text = args.allowed_cidr or env_setting(CIDRS_VAR)
    if not cidr_text:
        sys.exit(
            f"No allowed addresses. Pass --allowed-cidr or set {CIDRS_VAR} in the "
            f"environment or in {ENV_FILE}. Find your address at {CHECKIP_URL}"
        )
    zone_id = args.hosted_zone_id or env_setting(ZONE_VAR)
    if not zone_id:
        sys.exit(
            f"No hosted zone. Pass --hosted-zone-id or set {ZONE_VAR} in the "
            f"environment or in {ENV_FILE}."
        )
    if not ZONE_ID_PATTERN.match(zone_id):
        sys.exit(
            f"{zone_id!r} is not a bare hosted zone id, such as Z0123456789ABC. "
            "Leave off the /hostedzone/ prefix."
        )
    base_domain = resolve_base_domain(args.base_domain)
    provider = resolve_auth_provider()
    return Settings(
        profile=args.profile or env_setting(PROFILE_VAR) or DEFAULT_PROFILE,
        region=args.region or DEFAULT_REGION,
        ami_id=resolve_ami_id(args),
        cidrs=parse_cidrs(cidr_text),
        instance_type=args.instance_type,
        base_domain=base_domain,
        hosted_zone_id=zone_id,
        neo4j=resolve_neo4j_settings(),
        auth_provider=provider,
        oidc=resolve_oidc_settings() if provider == OIDC_PROVIDER else {},
        restore=resolve_restore(args),
    )


def resolve_base_domain(flag: str | None = None) -> str:
    """The base of the Hume host names: the flag, else the setting."""
    if env_setting(OLD_DOMAIN_VAR):
        sys.exit(
            f"{OLD_DOMAIN_VAR} is no longer read. Remove it and set "
            f"{BASE_DOMAIN_VAR} to the base name, such as example.com."
        )
    base_domain = flag or env_setting(BASE_DOMAIN_VAR)
    if not base_domain:
        sys.exit(
            f"No base domain. Pass --base-domain or set {BASE_DOMAIN_VAR} "
            "to the base name, such as example.com."
        )
    return base_domain.strip().lower().rstrip(".")


def hume_hosts(base_domain: str) -> HumeHosts:
    """The three host names of a stack, as the template builds them."""
    return HumeHosts(
        web=f"hume.{base_domain}",
        api=f"hume-api.{base_domain}",
        media=f"hume-media.{base_domain}",
    )


def domain_in_zone(base_domain: str, zone_name: str) -> bool:
    """True when the base is the zone or a name below it.

    The hosts sit one label below the base, so no record lands at the apex.
    """
    zone = zone_name.rstrip(".").lower()
    base = base_domain.lower()
    return base == zone or base.endswith(f".{zone}")


def check_domain(session: boto3.Session, settings: Settings) -> str:
    """Exit unless the base sits inside the public zone. Return the zone name.

    CloudFormation cannot compare the two parameters, so this is the only check
    before the stack writes records into a zone that other sites share.
    """
    route53 = session.client("route53")
    try:
        zone = route53.get_hosted_zone(Id=settings.hosted_zone_id)["HostedZone"]
    except route53.exceptions.NoSuchHostedZone:
        sys.exit(f"Hosted zone {settings.hosted_zone_id} does not exist.")
    zone_name = zone["Name"].rstrip(".")
    if zone.get("Config", {}).get("PrivateZone"):
        sys.exit(f"Hosted zone {zone_name} is private. A public zone is required.")
    if not domain_in_zone(settings.base_domain, zone_name):
        sys.exit(
            f"{settings.base_domain} is not the zone {zone_name} or a name inside "
            "it. The stack only writes records under the base name."
        )
    return zone_name


def describe_all(
    problems: list[str], kind: str, describe: Callable, ids: list[str], key: str
) -> list[dict]:
    """Describe the ids, and note in problems each one that does not exist."""
    try:
        found = describe(ids)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        problems.append(f"{kind} {', '.join(ids)}: {code}")
        return []
    for missing in sorted(set(ids) - {item[key] for item in found}):
        problems.append(f"{kind} {missing} does not exist")
    return found


def has_default_route(routes: list[dict]) -> bool:
    return any(
        route.get("DestinationCidrBlock") == "0.0.0.0/0"
        and route.get("State") == "active"
        for route in routes
    )


def check_default_routes(
    problems: list[str], ec2, vpc_id: str, private: list[str]
) -> None:
    """Note each private subnet whose route table has no active default route.

    The Hume instance sits in these subnets. The ECS agent endpoints and the
    identity provider have no VPC endpoint, so the instance needs the route.
    A subnet without its own route table uses the main table of the VPC.
    """
    try:
        tables = ec2.describe_route_tables(
            Filters=[{"Name": "association.subnet-id", "Values": private}]
        )["RouteTables"]
        routes = {
            association["SubnetId"]: table.get("Routes", [])
            for table in tables
            for association in table.get("Associations", [])
            if association.get("SubnetId")
        }
        if any(subnet not in routes for subnet in private):
            main = ec2.describe_route_tables(
                Filters=[
                    {"Name": "vpc-id", "Values": [vpc_id]},
                    {"Name": "association.main", "Values": ["true"]},
                ]
            )["RouteTables"]
            main_routes = [route for table in main for route in table.get("Routes", [])]
            for subnet in private:
                routes.setdefault(subnet, main_routes)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        problems.append(f"Route tables of {', '.join(private)}: {code}")
        return
    for subnet in private:
        if not has_default_route(routes[subnet]):
            problems.append(
                f"Private subnet {subnet} has no active default route. Hume's "
                "instance needs one for the ECS agent and the identity provider. "
                "Build the Neo4j stack with EnableInternetEgress true."
            )


def read_neo4j_stack(ssm, stack: str) -> dict[str, str]:
    """The Parameter Store values of a Neo4j EE stack, by the name after the prefix.

    The result is empty when no stack of that name publishes values here.
    """
    prefix = NEO4J_SSM_PREFIX.format(stack=stack)
    pages = ssm.get_paginator("get_parameters_by_path").paginate(Path=prefix)
    return {
        p["Name"].removeprefix(prefix): p["Value"]
        for page in pages
        for p in page["Parameters"]
    }


def neo4j_stack_problem(stack: str, values: dict[str, str]) -> str | None:
    """The message that the Neo4jEeLookup resource fails with, None when it fits."""
    missing = [name for name in NEO4J_LOOKUP_NAMES if name not in values]
    if len(missing) == len(NEO4J_LOOKUP_NAMES):
        return (
            f"No Neo4j EE stack named {stack} in this account and Region. "
            "Deploy Neo4j EE from AWS Marketplace first."
        )
    if set(missing) == ONE_SERVER_MISSING:
        return (
            f"Neo4j EE stack {stack} has one server. Hume needs a "
            "three-server Neo4j EE stack. Deploy Neo4j EE with "
            "NumberOfServers 3."
        )
    if missing:
        return (
            f"Neo4j EE stack {stack} does not publish "
            f"{', '.join(missing)} under /neo4j-ee/{stack}/."
        )
    return None


def check_neo4j_network(ec2, neo4j: dict[str, str]) -> list[str]:
    """Return what is wrong with the network, an empty list when it fits.

    neo4j holds the Parameter Store values of the Neo4j EE stack. CloudFormation
    would fail late and slowly on a subnet from another VPC, or on a load
    balancer without two zones. Each private subnet also needs an active default
    route, which a Neo4j stack built with EnableInternetEgress true provides.
    """
    problems: list[str] = []
    vpc_id = neo4j["vpc-id"]
    public = [neo4j[name] for name in PUBLIC_SUBNET_NAMES]
    private = [neo4j[name] for name in PRIVATE_SUBNET_NAMES]
    groups = [neo4j["external-sg-id"], neo4j["vpc-endpoint-sg-id"]]
    describe_all(
        problems,
        "VPC",
        lambda ids: ec2.describe_vpcs(VpcIds=ids)["Vpcs"],
        [vpc_id],
        "VpcId",
    )
    subnets = describe_all(
        problems,
        "Subnet",
        lambda ids: ec2.describe_subnets(SubnetIds=ids)["Subnets"],
        list(dict.fromkeys(public + private)),
        "SubnetId",
    )
    for subnet in subnets:
        if subnet["VpcId"] != vpc_id:
            problems.append(
                f"Subnet {subnet['SubnetId']} is in {subnet['VpcId']}, not {vpc_id}"
            )
    for kind, ids in (("public", public), ("private", private)):
        zones = {s["AvailabilityZone"] for s in subnets if s["SubnetId"] in ids}
        if len(zones) < 2:
            problems.append(
                f"The {kind} subnets must be in two different availability zones, "
                f"found {len(zones)}"
            )
    if subnets:
        check_default_routes(problems, ec2, vpc_id, private)
    for group in describe_all(
        problems,
        "Security group",
        lambda ids: ec2.describe_security_groups(GroupIds=ids)["SecurityGroups"],
        list(dict.fromkeys(groups)),
        "GroupId",
    ):
        if group["VpcId"] != vpc_id:
            problems.append(
                f"Security group {group['GroupId']} is in {group['VpcId']}, "
                f"not {vpc_id}"
            )
    return problems


def confirm_settings(settings: Settings, assume_yes: bool) -> None:
    """Print what will be created, never a secret, and ask before creating."""
    session = boto3.Session(profile_name=settings.profile, region_name=settings.region)
    identity = session.client("sts").get_caller_identity()
    zone_name = check_domain(session, settings)
    ee_stack = settings.neo4j["EeStackName"]
    neo4j = read_neo4j_stack(session.client("ssm"), ee_stack)
    if problem := neo4j_stack_problem(ee_stack, neo4j):
        sys.exit(problem)
    problems = check_neo4j_network(session.client("ec2"), neo4j)
    if problems:
        listing = "\n".join(f"  {problem}" for problem in problems)
        sys.exit(f"The network of the Neo4j cluster does not fit:\n{listing}")
    print(f"Account:       {identity['Account']}")
    print(f"Profile:       {settings.profile}")
    print(f"Region:        {settings.region}")
    print(f"AMI:           {settings.ami_id}")
    print(f"Instance type: {settings.instance_type}")
    print(f"Allowed:       {', '.join(settings.cidrs)}")
    print(f"Host names:    {', '.join(hume_hosts(settings.base_domain))}")
    print(f"Hosted zone:   {zone_name} ({settings.hosted_zone_id})")
    print(f"Neo4j stack:   {ee_stack}")
    print(f"Neo4j:         {neo4j['advertised-dns']}")
    print(f"VPC:           {neo4j['vpc-id']}")
    if settings.auth_provider == NATIVE_PROVIDER:
        print("Sign-in:       Native, with the admin password secret")
    else:
        print(f"Sign-in:       {settings.oidc.get('OidcIssuerUri', '')}")
        print(f"Client id:     {settings.oidc.get('OidcClientId', '')}")
    for parameter, value in settings.restore.items():
        print(f"Restore:       {parameter}={value}")
    if not assume_yes and input("Create the stack? [y/N] ").strip().lower() != "y":
        sys.exit("Cancelled.")


def load_licence_key() -> str:
    """Read the licence key from the environment, then the repo-root .env.

    The value is never printed and never written to the record file.
    """
    key = env_setting(LICENCE_VAR)
    if not key:
        sys.exit(f"{LICENCE_VAR} is not set in the environment or in {ENV_FILE}")
    return key


def cli_session(args: argparse.Namespace) -> boto3.Session:
    """Session for the commands that run before a stack record exists."""
    return boto3.Session(
        profile_name=args.profile or env_setting(PROFILE_VAR) or DEFAULT_PROFILE,
        region_name=args.region or DEFAULT_REGION,
    )


def upsert_secret(sm, name: str, value: str, description: str) -> None:
    """Create the secret, or add a new version. The value is never printed."""
    try:
        sm.put_secret_value(SecretId=name, SecretString=value)
        print(f"Updated secret {name}")
    except sm.exceptions.ResourceNotFoundException:
        sm.create_secret(
            Name=name, Description=description, SecretString=value, Tags=STACK_TAGS
        )
        print(f"Created secret {name}")


def cmd_licence(args: argparse.Namespace) -> int:
    """Create or update the licence secret that stacks reference by ARN."""
    key = load_licence_key()
    sm = cli_session(args).client("secretsmanager")
    upsert_secret(
        sm,
        LICENCE_SECRET_NAME,
        key,
        "Hume licence key, shared by the hume-ecs stacks.",
    )
    return 0


def read_ca_certificates(path: Path) -> list[str]:
    """PEM certificates in the file. A private key or other content exits."""
    try:
        text = path.read_text()
    except UnicodeDecodeError:
        sys.exit(f"{path} is not a PEM file")
    if "PRIVATE KEY" in text:
        sys.exit(f"{path} holds a private key. Pass the CA certificate only.")
    certificates = PEM_CERTIFICATE.findall(text)
    if not certificates:
        sys.exit(f"{path} holds no PEM certificate")
    return certificates


def keytool_runs_locally() -> bool:
    """True for a local keytool, False for docker. Exit when there is neither."""
    if shutil.which("keytool"):
        return True
    if shutil.which("docker"):
        return False
    sys.exit(
        "keytool was not found and neither was docker. Install a JDK, or install "
        f"docker, which runs keytool from {KEYTOOL_IMAGE}."
    )


def keytool_import_command(work: Path, index: int, local: bool) -> list[str]:
    """The command that adds certificate number index to the truststore.

    Docker sees the work directory as /work and runs as the calling user, so
    the files stay removable.
    """
    base = work if local else Path("/work")
    arguments = [
        "-importcert", "-noprompt",
        "-alias", f"privca{index}",
        "-file", str(base / f"ca{index}.crt"),
        "-keystore", str(base / TRUSTSTORE_FILE),
        "-storetype", "PKCS12",
        "-storepass:env", KEYTOOL_PASSWORD_VAR,
    ]  # fmt: skip
    if local:
        return ["keytool", *arguments]
    return [
        "docker", "run", "--rm",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{work}:/work",
        "-e", KEYTOOL_PASSWORD_VAR,
        "--entrypoint", "keytool",
        KEYTOOL_IMAGE,
        *arguments,
    ]  # fmt: skip


def build_truststore(certificates: list[str]) -> tuple[bytes, str]:
    """Run keytool to make a PKCS12 truststore. Return its bytes and password.

    The password reaches keytool through the environment, so it stays out of argv.
    """
    local = keytool_runs_locally()
    password = secrets.token_hex(16)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for index, certificate in enumerate(certificates, start=1):
            (work / f"ca{index}.crt").write_text(certificate + "\n")
            try:
                result = subprocess.run(
                    keytool_import_command(work, index, local),
                    env={**os.environ, KEYTOOL_PASSWORD_VAR: password},
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except FileNotFoundError:
                sys.exit("keytool or docker disappeared from the PATH")
            if result.returncode != 0:
                sys.exit(f"keytool failed: {result.stderr.strip()}")
        return (work / TRUSTSTORE_FILE).read_bytes(), password


def truststore_java_options(password: str) -> str:
    """The JAVA_TOOL_OPTIONS value for the orchestra and api JVMs.

    It points the JVM at the mounted truststore and sets the DNS cache time.
    The password is public by design. The file holds CA certificates only, and the
    JVM prints this value to stderr at start, so it reaches CloudWatch Logs.
    The DNS flag is sun.net.inetaddr.ttl. networkaddress.cache.ttl is a security
    property, and the JVM ignores it as a -D flag.
    """
    return " ".join(
        [
            f"-Djavax.net.ssl.trustStore={TRUSTSTORE_CONTAINER_PATH}",
            "-Djavax.net.ssl.trustStoreType=PKCS12",
            f"-Djavax.net.ssl.trustStorePassword={password}",
            f"-Dsun.net.inetaddr.ttl={JVM_DNS_TTL_SECONDS}",
        ]
    )


def truststore_secret_string(store: bytes, password: str) -> str:
    """JSON for the truststore secret: the file as base64 and the JVM options."""
    return json.dumps(
        {
            "truststore": base64.b64encode(store).decode("ascii"),
            "java_tool_options": truststore_java_options(password),
        }
    )


def cmd_truststore(args: argparse.Namespace) -> int:
    """Build a truststore from the CA file and store it as a secret."""
    file_name = args.ca_file or env_setting(CA_FILE_VAR)
    if not file_name:
        sys.exit(f"No CA file. Pass --ca-file or set {CA_FILE_VAR}.")
    path = Path(file_name).expanduser()
    if not path.is_file():
        sys.exit(f"CA file {path} does not exist")
    store, password = build_truststore(read_ca_certificates(path))
    sm = cli_session(args).client("secretsmanager")
    upsert_secret(
        sm,
        args.secret_name,
        truststore_secret_string(store, password),
        "Truststore for the private CA of Neo4j, for the hume-ecs stacks.",
    )
    arn = sm.describe_secret(SecretId=args.secret_name)["ARN"]
    print(f"Set {TRUSTSTORE_ARN_VAR}={arn}")
    return 0


def cmd_truststore_options(args: argparse.Namespace) -> int:
    """Update java_tool_options in the truststore secret.

    The store and its password stay, so the file on each instance stays valid.
    A restart of orchestra and api picks up the new options.
    """
    sm = cli_session(args).client("secretsmanager")
    secret = json.loads(sm.get_secret_value(SecretId=args.secret_name)["SecretString"])
    current = secret.get("java_tool_options", "")
    match = TRUSTSTORE_PASSWORD_OPTION.search(current)
    if not match:
        sys.exit(f"{args.secret_name} has no truststore password option to keep.")
    updated = truststore_java_options(match.group(1))
    if updated == current:
        print(f"java_tool_options in {args.secret_name} is already current.")
        return 0
    secret["java_tool_options"] = updated
    sm.put_secret_value(SecretId=args.secret_name, SecretString=json.dumps(secret))
    print(f"Updated java_tool_options in {args.secret_name}.")
    print(f"Restart {' and '.join(TRUSTSTORE_SERVICES)} to apply it.")
    return 0


def resolve_spine_config_dir() -> Path:
    """The Spine config directory from HUME_SPINE_CONFIG_DIR.

    A relative path is read from the repository root, so it works from any
    directory. The repository holds a copy in demo/spine/hume-config.
    """
    text = env_setting(SPINE_CONFIG_DIR_VAR)
    if not text:
        sys.exit(
            f"{SPINE_CONFIG_DIR_VAR} is not set in the environment or in {ENV_FILE}. "
            "It names the Spine config directory. The copy in this repository is "
            "demo/spine/hume-config."
        )
    root = SCRIPT_DIR.parent / Path(text).expanduser()
    if not (root / SPINE_REQUIRED_FILE).is_file():
        sys.exit(f"{root / SPINE_REQUIRED_FILE} not found. Is this the Spine config?")
    return root


def spine_config_files(root: Path) -> list[Path]:
    """Paths under root, relative and sorted, without the skipped files."""
    skipped = set(SPINE_SKIPPED_FILES)
    relatives = (path.relative_to(root) for path in root.rglob("*") if path.is_file())
    return sorted(
        relative
        for relative in relatives
        if relative.as_posix() not in skipped
        and not any(part.startswith(".") for part in relative.parts)
    )


def build_spine_config(root: Path) -> str:
    """Base64 of a bzip2 tar of the Spine files. The same files give the same text."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:bz2") as archive:
        for relative in spine_config_files(root):
            info = archive.gettarinfo(root / relative, arcname=relative.as_posix())
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ""
            with (root / relative).open("rb") as handle:
                archive.addfile(info, handle)
    text = base64.b64encode(buffer.getvalue()).decode("ascii")
    if len(text) > SPINE_SECRET_LIMIT:
        sys.exit(
            f"The Spine config is {len(text)} bytes as a secret. The limit is "
            f"{SPINE_SECRET_LIMIT}."
        )
    return text


def spine_secret_name(stack: str) -> str:
    return f"hume/{stack}/{SPINE_SECRET_SUFFIX}"


def put_spine_config(session: boto3.Session, stack: str) -> str:
    """Store the Spine files in the stack's secret and return its full ARN.

    Teardown removes the secret by prefix. ECS reads it only by full ARN.
    """
    value = build_spine_config(resolve_spine_config_dir())
    sm = session.client("secretsmanager")
    name = spine_secret_name(stack)
    upsert_secret(
        sm,
        name,
        value,
        "Spine knowledge graph files for the Hume configuration import.",
    )
    print(f"The Spine config secret holds {len(value)} bytes.")
    return sm.describe_secret(SecretId=name)["ARN"]


def cmd_spine_config(args: argparse.Namespace) -> int:
    """Store the Spine files and print the ARN that the import update needs."""
    record = read_record(args.stack_name)
    arn = put_spine_config(make_session(args, record), args.stack_name)
    print(f"SpineConfigSecretArn={arn}")
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    """Resolve and confirm the settings, then run licence, then deploy."""
    settings = resolve_settings(args)
    confirm_settings(settings, args.yes)
    cmd_licence(args)
    return deploy_stack(settings, args.stack_name)


def secret_arn(session: boto3.Session, name: str, command: str) -> str:
    sm = session.client("secretsmanager")
    try:
        return sm.describe_secret(SecretId=name)["ARN"]
    except sm.exceptions.ResourceNotFoundException:
        sys.exit(f"Secret {name} not found. Run: ./deploy.py {command}")


def licence_secret_arn(session: boto3.Session) -> str:
    return secret_arn(session, LICENCE_SECRET_NAME, "licence")


def template_bucket_name(session: boto3.Session) -> str:
    account = session.client("sts").get_caller_identity()["Account"]
    return f"{TEMPLATE_BUCKET_PREFIX}-{account}-{session.region_name}"


def ensure_template_bucket(session: boto3.Session) -> str:
    """Return the private template bucket, creating it when it is missing."""
    s3 = session.client("s3")
    bucket = template_bucket_name(session)
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("404", "NoSuchBucket"):
            sys.exit(f"Cannot use the template bucket {bucket}: {exc}")
        create_args = {"Bucket": bucket}
        if session.region_name != "us-east-1":
            create_args["CreateBucketConfiguration"] = {
                "LocationConstraint": session.region_name
            }
        try:
            s3.create_bucket(**create_args)
            print(f"Created template bucket {bucket}")
        except ClientError as err:
            # A parallel deploy may have created it first.
            if err.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
                raise
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    return bucket


def template_arguments(
    session: boto3.Session, stack: str, record: dict[str, str]
) -> dict[str, str]:
    """TemplateBody when the template fits, otherwise a staged TemplateURL."""
    body = TEMPLATE.read_text()
    data = body.encode()
    if len(data) <= TEMPLATE_DIRECT_LIMIT:
        return {"TemplateBody": body}
    bucket = ensure_template_bucket(session)
    key = f"{TEMPLATE_KEY_PREFIX}{stack}.yaml"
    session.client("s3").put_object(
        Bucket=bucket, Key=key, Body=data, ContentType="application/x-yaml"
    )
    record[BUCKET_RECORD_KEY] = bucket
    record[OBJECT_RECORD_KEY] = key
    print(f"Staged the template ({len(data)} bytes) at s3://{bucket}/{key}")
    return {
        "TemplateURL": f"https://{bucket}.s3.{session.region_name}.amazonaws.com/{key}"
    }


def delete_template_object(session: boto3.Session, record: dict[str, str]) -> None:
    """Delete the staged template. The bucket stays for other stacks."""
    bucket, key = record.get(BUCKET_RECORD_KEY), record.get(OBJECT_RECORD_KEY)
    if not (bucket and key):
        return
    s3 = session.client("s3")
    try:
        s3.delete_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        # A bucket that is already gone holds no template.
        if exc.response["Error"]["Code"] != "NoSuchBucket":
            raise
    print(f"Deleted staged template s3://{bucket}/{key}")


def cmd_delete_template_bucket(args: argparse.Namespace) -> int:
    """Delete the bucket only when no stack has a staged template left in it."""
    session = cli_session(args)
    s3 = session.client("s3")
    bucket = template_bucket_name(session)
    try:
        listing = s3.list_objects_v2(Bucket=bucket, MaxKeys=20)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NoSuchBucket":
            raise
        print(f"No template bucket {bucket}")
        return 0
    if listing.get("KeyCount"):
        print(f"{bucket} still holds templates, so it was kept:", file=sys.stderr)
        for item in listing["Contents"]:
            print(f"  {item['Key']}", file=sys.stderr)
        print("Tear down those stacks first.", file=sys.stderr)
        return 1
    s3.delete_bucket(Bucket=bucket)
    print(f"Deleted template bucket {bucket}")
    return 0


def print_failed_events(cfn, stack: str) -> None:
    events = cfn.describe_stack_events(StackName=stack)["StackEvents"]
    for event in reversed(events):
        if event["ResourceStatus"].endswith("FAILED"):
            reason = event.get("ResourceStatusReason", "")
            print(f"  {event['LogicalResourceId']}: {reason}", file=sys.stderr)


def record_failed_create(session: boto3.Session, cfn, record: dict[str, str]) -> None:
    """Keep the certificate in the record so that teardown can find its record.

    The waiter returns after a rollback has finished, and the rollback deletes
    the certificate. The validation records are only found when the certificate
    still exists, for example when the waiter timed out.
    """
    arn = stack_resource_id(cfn, record["StackName"], CERTIFICATE_LOGICAL_ID)
    if arn:
        record["CertificateArn"] = arn
        write_record(record["StackName"], record)
        capture_validation_records(session, record)


def cmd_deploy(args: argparse.Namespace) -> int:
    settings = resolve_settings(args)
    confirm_settings(settings, args.yes)
    return deploy_stack(settings, args.stack_name)


def deploy_stack(settings: Settings, stack_name: str | None) -> int:
    stack = stack_name or f"hume-ecs-{int(time.time())}"
    profile, region = settings.profile, settings.region
    session = boto3.Session(profile_name=profile, region_name=region)
    cfn = session.client("cloudformation")
    licence_arn = licence_secret_arn(session)

    record = {
        "StackName": stack,
        "Region": region,
        "Profile": profile,
        "AmiId": settings.ami_id,
        "AllowedCidrs": ",".join(settings.cidrs),
        "InstanceType": settings.instance_type,
        "BaseDomainName": settings.base_domain,
        "HostedZoneId": settings.hosted_zone_id,
        "AuthProvider": settings.auth_provider,
        **settings.restore,
    }
    template = template_arguments(session, stack, record)
    write_record(stack, record)

    params = {
        "HumeAmiId": settings.ami_id,
        **cidr_parameters(settings.cidrs),
        "InstanceType": settings.instance_type,
        "HumeLicenceSecretArn": licence_arn,
        "BaseDomainName": settings.base_domain,
        "HostedZoneId": settings.hosted_zone_id,
        **neo4j_parameters(settings),
        **auth_parameters(settings),
        **settings.restore,
    }
    created = cfn.create_stack(
        StackName=stack,
        **template,
        Parameters=[
            {"ParameterKey": key, "ParameterValue": value}
            for key, value in params.items()
        ],
        Capabilities=["CAPABILITY_IAM"],
        Tags=STACK_TAGS,
    )
    record["StackId"] = created["StackId"]
    write_record(stack, record)
    print(f"Creating {stack} in {region}. This takes about 15 minutes for RDS.")

    waiter = cfn.get_waiter("stack_create_complete")
    try:
        waiter.wait(StackName=stack, WaiterConfig={"Delay": 15, "MaxAttempts": 120})
    except WaiterError:
        print(f"Stack {stack} failed. Failed resources:", file=sys.stderr)
        print_failed_events(cfn, stack)
        record_failed_create(session, cfn, record)
        return 1

    outputs = cfn.describe_stacks(StackName=stack)["Stacks"][0]["Outputs"]
    record.update({o["OutputKey"]: o["OutputValue"] for o in outputs})
    write_record(stack, record)
    print(f"CREATE_COMPLETE. Record: {record_path(stack)}")
    print(f"Next: {sys.argv[0]} smoke {stack}")
    return 0


def wait_for_container_instance(ecs, cluster: str, timeout: int = 900) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        failed = ecs.list_container_instances(
            cluster=cluster, status="REGISTRATION_FAILED"
        )["containerInstanceArns"]
        if failed:
            detail = ecs.describe_container_instances(
                cluster=cluster, containerInstances=failed
            )["containerInstances"][0]
            raise RuntimeError(f"Registration failed: {detail.get('statusReason')}")
        active = ecs.list_container_instances(cluster=cluster, status="ACTIVE")
        if active["containerInstanceArns"]:
            return ecs.describe_container_instances(
                cluster=cluster, containerInstances=active["containerInstanceArns"]
            )["containerInstances"][0]
        time.sleep(15)
    raise TimeoutError(f"No active container instance in {cluster} after {timeout}s")


def fetch_task_logs(logs, group: str, task_arn: str) -> list[str]:
    return fetch_log_stream(logs, group, f"smoke/smoke/{task_arn.rsplit('/', 1)[-1]}")


def fetch_log_stream(logs, group: str, stream: str) -> list[str]:
    for _ in range(5):
        try:
            events = logs.get_log_events(
                logGroupName=group, logStreamName=stream, startFromHead=True
            )["events"]
        except logs.exceptions.ResourceNotFoundException:
            events = []
        if events:
            return [event["message"] for event in events]
        time.sleep(3)
    return []


def cmd_smoke(args: argparse.Namespace) -> int:
    stack = args.stack_name or newest_stack()
    record = read_record(stack)
    cluster = record.get("ClusterName")
    if not cluster:
        sys.exit(f"{stack} has no ClusterName. Its create did not finish.")
    subnet = record.get("InstanceSubnetId")
    if not subnet:
        sys.exit(f"{stack} has no InstanceSubnetId. Its create did not finish.")
    session = make_session(args, record)
    ecs = session.client("ecs")
    logs = session.client("logs")

    instance = wait_for_container_instance(ecs, cluster)
    attributes = {a["name"]: a.get("value") for a in instance["attributes"]}
    print(f"Instance {instance['ec2InstanceId']} is {instance['status']}")
    if TRUNK_ATTRIBUTE not in attributes:
        print(f"FAIL: attribute {TRUNK_ATTRIBUTE} is missing", file=sys.stderr)
        return 1
    print(f"Trunk interface: {attributes[TRUNK_ATTRIBUTE]}")
    custom = {k: v for k, v in attributes.items() if not k.startswith("ecs.")}
    print(f"Custom attributes: {custom}")
    if args.expect_attribute:
        name, _, value = args.expect_attribute.partition("=")
        if attributes.get(name) != value:
            print(f"FAIL: expected {name}={value}, got {attributes.get(name)}")
            return 1

    run = ecs.run_task(
        cluster=cluster,
        taskDefinition=record["SmokeTestTaskDefinitionArn"],
        count=1,
        startedBy="deploy.py smoke",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": [subnet],
                "securityGroups": [record["InstanceSecurityGroupId"]],
                "assignPublicIp": "DISABLED",
            }
        },
    )
    if run["failures"]:
        print(f"FAIL: run_task failures: {run['failures']}", file=sys.stderr)
        return 1
    task_arn = run["tasks"][0]["taskArn"]
    ecs.get_waiter("tasks_stopped").wait(
        cluster=cluster,
        tasks=[task_arn],
        WaiterConfig={"Delay": 5, "MaxAttempts": 60},
    )
    task = ecs.describe_tasks(cluster=cluster, tasks=[task_arn])["tasks"][0]
    container = task["containers"][0]
    exit_code = container.get("exitCode")
    pull_started, pull_stopped = task.get("pullStartedAt"), task.get("pullStoppedAt")
    if pull_started and pull_stopped:
        seconds = (pull_stopped - pull_started) / timedelta(seconds=1)
        print(f"Image pull window: {seconds:.1f}s for {container['image']}")
    lines = fetch_task_logs(logs, f"/hume/{stack}/smoke-test", task_arn)
    print(f"Task log: {lines}")
    if exit_code != 0:
        print(
            f"FAIL: exit code {exit_code}, reason: {task.get('stoppedReason')}",
            file=sys.stderr,
        )
        return 1
    print("PASS: the awsvpc task ran and exited 0")
    return 0


def stack_outputs(session: boto3.Session, stack: str) -> dict[str, str]:
    """The current outputs of the stack, read live, not from the record."""
    (found,) = session.client("cloudformation").describe_stacks(StackName=stack)[
        "Stacks"
    ]
    return {o["OutputKey"]: o["OutputValue"] for o in found.get("Outputs", [])}


def confirm_reimport(stack: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    print(f"This runs the configuration import in {stack} again.")
    print(f"It then restarts {REIMPORT_SERVICE}, which is down for about a minute.")
    try:
        answer = input("Continue? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer != "y":
        print("Nothing was changed.", file=sys.stderr)
        return False
    return True


def cmd_reimport(args: argparse.Namespace) -> int:
    """Copy a rotated Neo4j app password into Hume.

    The configuration import copies the password into Hume's own database, and
    orchestra holds the open driver. No other service reads the password. Rotate
    the secret in place, so its ARN stays the same, then run this command.
    """
    stack = args.stack_name or newest_stack()
    record = read_record(stack)
    session = make_session(args, record)
    outputs = stack_outputs(session, stack)
    if not outputs.get(CONFIG_CLI_OUTPUT):
        sys.exit(
            f"{stack} has no {CONFIG_CLI_OUTPUT} output. The stack runs the "
            "configuration import only when SpineConfigSecretArn is set."
        )
    if missing := [key for key in REIMPORT_OUTPUTS if not outputs.get(key)]:
        sys.exit(
            f"{stack} has no {', '.join(missing)} output. Its create did not finish."
        )
    if not confirm_reimport(stack, args.yes):
        return 1
    cluster = outputs["ClusterName"]
    ecs = session.client("ecs")
    run = ecs.run_task(
        cluster=cluster,
        taskDefinition=outputs[CONFIG_CLI_OUTPUT],
        count=1,
        startedBy="deploy.py reimport",
        capacityProviderStrategy=[
            {"capacityProvider": outputs["CapacityProviderName"], "weight": 1}
        ],
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": [outputs["InstanceSubnetId"]],
                "securityGroups": [outputs["InstanceSecurityGroupId"]],
                "assignPublicIp": "DISABLED",
            }
        },
    )
    if run["failures"]:
        print(f"FAIL: run_task failures: {run['failures']}", file=sys.stderr)
        return 1
    task_arn = run["tasks"][0]["taskArn"]
    print(f"Started the configuration import {task_arn}. Waiting for it to stop.")
    try:
        ecs.get_waiter("tasks_stopped").wait(
            cluster=cluster,
            tasks=[task_arn],
            WaiterConfig={"Delay": 10, "MaxAttempts": 90},
        )
    except WaiterError:
        print(
            f"FAIL: the configuration import {task_arn} did not stop within 15 "
            f"minutes. Read the log group /hume/{stack}/config-cli.",
            file=sys.stderr,
        )
        return 1
    task = ecs.describe_tasks(cluster=cluster, tasks=[task_arn])["tasks"][0]
    if failed := [
        f"{c['name']} exited {c.get('exitCode')}"
        for c in task["containers"]
        if c.get("exitCode") != 0
    ]:
        print(
            f"FAIL: {', '.join(failed)}. Reason: {task.get('stoppedReason')}. "
            f"Read the log group /hume/{stack}/config-cli.",
            file=sys.stderr,
        )
        return 1
    print("The configuration import exited 0.")
    if restart_services(ecs, cluster, stack, (REIMPORT_SERVICE,)):
        return 1
    print(f"PASS: the import ran and {REIMPORT_SERVICE} restarted.")
    return 0


def restart_services(ecs, cluster: str, stack: str, services: tuple[str, ...]) -> int:
    """Start a new deployment of each service and wait. 0 when all are stable."""
    for service in services:
        ecs.update_service(cluster=cluster, service=service, forceNewDeployment=True)
    names = " and ".join(services)
    print(f"Restarting {names}. Waiting for each to be stable.")
    try:
        ecs.get_waiter("services_stable").wait(
            cluster=cluster,
            services=list(services),
            WaiterConfig={"Delay": 15, "MaxAttempts": 60},
        )
    except WaiterError:
        groups = " and ".join(f"/hume/{stack}/{service}" for service in services)
        print(
            f"FAIL: {names} was not stable within 15 minutes. Read the "
            f"log group {groups}.",
            file=sys.stderr,
        )
        return 1
    return 0


# Decodes the truststore secret JSON on stdin and writes the file to stdout,
# as the TruststoreStep of the template does.
TRUSTSTORE_DECODER = f"""
import base64, json, sys
secret = json.load(sys.stdin)
for key in {TRUSTSTORE_SECRET_KEYS!r}:
    if not secret.get(key):
        sys.exit(f"The truststore secret has no {{key}} value")
data = "".join(secret["truststore"].split())
sys.stdout.buffer.write(base64.b64decode(data, validate=True))
"""


def truststore_write_commands(secret_arn: str, region: str) -> list[str]:
    """Shell commands that write the truststore file from its secret.

    The commands name the secret ARN only. The instance role reads the value.
    """
    tmp = f"{TRUSTSTORE_HOST_PATH}.tmp"
    return [
        "set -eu",
        (
            f"aws secretsmanager get-secret-value --region {shlex.quote(region)} "
            f"--secret-id {shlex.quote(secret_arn)} "
            "--query SecretString --output text | "
            f"python3 -c {shlex.quote(TRUSTSTORE_DECODER)} > {tmp}"
        ),
        f"chown 1001:1001 {tmp}",
        f"chmod 0600 {tmp}",
        f"mv -f {tmp} {TRUSTSTORE_HOST_PATH}",
    ]


def confirm_refresh(stack: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    print(f"This writes the truststore file on the Hume instance in {stack} from")
    print(f"its secret. It then restarts {' and '.join(TRUSTSTORE_SERVICES)},")
    print("which are down for about a minute.")
    try:
        answer = input("Continue? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer != "y":
        print("Nothing was changed.", file=sys.stderr)
        return False
    return True


def cmd_refresh_truststore(args: argparse.Namespace) -> int:
    """Write a rebuilt truststore to the running Hume instance.

    The instance writes the file only at boot, and each build has a new
    password. orchestra and api must restart after the file changes, so each
    reads the new file and the matching password together.
    """
    stack = args.stack_name or newest_stack()
    record = read_record(stack)
    group = record.get("AutoScalingGroupName")
    if not group:
        sys.exit(f"{stack} has no AutoScalingGroupName. Its create did not finish.")
    session = make_session(args, record)
    stack_info = session.client("cloudformation").describe_stacks(StackName=stack)
    parameters = {
        p["ParameterKey"]: p.get("ParameterValue", "")
        for p in stack_info["Stacks"][0]["Parameters"]
    }
    secret_arn = parameters.get("Neo4jTruststoreSecretArn")
    if not secret_arn:
        sys.exit(f"{stack} has no Neo4jTruststoreSecretArn, so it has no truststore.")
    cluster = stack_outputs(session, stack).get("ClusterName")
    if not cluster:
        sys.exit(f"{stack} has no ClusterName output. Its create did not finish.")
    instance = hume_instance_id(session, stack, group)
    if not confirm_refresh(stack, args.yes):
        return 1

    print(f"Writing the truststore on {instance}")
    ssm = session.client("ssm")
    command_id = ssm.send_command(
        InstanceIds=[instance],
        DocumentName="AWS-RunShellScript",
        Parameters={
            "commands": truststore_write_commands(secret_arn, session.region_name)
        },
        Comment="deploy.py refresh-truststore",
    )["Command"]["CommandId"]
    try:
        ssm.get_waiter("command_executed").wait(
            CommandId=command_id,
            InstanceId=instance,
            WaiterConfig={"Delay": 5, "MaxAttempts": 24},
        )
    except WaiterError as error:
        print(f"The SSM command did not succeed: {error}", file=sys.stderr)
    invocation = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance)
    if invocation["Status"] != "Success":
        print(
            f"FAIL: the truststore write ended {invocation['Status']}. "
            f"{invocation['StandardErrorContent']}",
            file=sys.stderr,
        )
        return 1
    print("The truststore file was written.")
    if restart_services(session.client("ecs"), cluster, stack, TRUSTSTORE_SERVICES):
        return 1
    print(
        "PASS: the truststore was written and "
        f"{' and '.join(TRUSTSTORE_SERVICES)} restarted."
    )
    return 0


def tls_check_commands(host: str, ca_pem: str) -> list[str]:
    """Shell commands that verify the Neo4j certificate from the Hume instance."""
    ca_b64 = base64.b64encode(ca_pem.encode()).decode()
    name = shlex.quote(host)
    return [
        f"echo {ca_b64} | base64 -d > {TLS_CHECK_CA_PATH}",
        (
            f"echo | openssl s_client -connect {name}:{NEO4J_BOLT_PORT} "
            f"-servername {name} -CAfile {TLS_CHECK_CA_PATH} -verify_hostname {name} "
            f"2>&1 | grep -E '{TLS_CHECK_LINES}'"
        ),
        f"rm -f {TLS_CHECK_CA_PATH}",
    ]


def hume_instance_id(session: boto3.Session, stack: str, group: str) -> str:
    """Return the in-service instance of the Hume Auto Scaling group."""
    groups = session.client("autoscaling").describe_auto_scaling_groups(
        AutoScalingGroupNames=[group]
    )["AutoScalingGroups"]
    instances = [
        instance["InstanceId"]
        for found in groups
        for instance in found["Instances"]
        if instance["LifecycleState"] == "InService"
    ]
    if not instances:
        sys.exit(f"{stack} has no in-service instance in {group}")
    return instances[0]


def cmd_validate(args: argparse.Namespace) -> int:
    """Run the TLS check of the run book on the Hume instance through SSM."""
    stack = args.stack_name or newest_stack()
    record = read_record(stack)
    group = record.get("AutoScalingGroupName")
    if not group:
        sys.exit(f"{stack} has no AutoScalingGroupName. Its create did not finish.")
    file_name = args.ca_file or env_setting(CA_FILE_VAR)
    if not file_name:
        sys.exit(f"No CA file. Pass --ca-file or set {CA_FILE_VAR}.")
    path = Path(file_name).expanduser()
    if not path.is_file():
        sys.exit(f"CA file {path} does not exist")
    ca_pem = "\n".join(read_ca_certificates(path)) + "\n"

    session = make_session(args, record)
    host = stack_outputs(session, stack).get("Neo4jHost")
    if not host:
        sys.exit(f"{stack} has no Neo4jHost output.")
    instance = hume_instance_id(session, stack, group)
    print(f"Checking {host}:{NEO4J_BOLT_PORT} from {instance}")

    ssm = session.client("ssm")
    command_id = ssm.send_command(
        InstanceIds=[instance],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": tls_check_commands(host, ca_pem)},
        Comment="deploy.py validate",
    )["Command"]["CommandId"]
    try:
        ssm.get_waiter("command_executed").wait(
            CommandId=command_id,
            InstanceId=instance,
            WaiterConfig={"Delay": 5, "MaxAttempts": 24},
        )
    except WaiterError as error:
        print(f"The SSM command did not succeed: {error}", file=sys.stderr)
    invocation = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance)
    output = invocation["StandardOutputContent"]
    for line in output.splitlines():
        print(line)
    if invocation["StandardErrorContent"]:
        print(invocation["StandardErrorContent"], file=sys.stderr)
    if TLS_CHECK_PASS not in output:
        print(f"FAIL: no {TLS_CHECK_PASS!r} in the output", file=sys.stderr)
        return 1
    print(f"PASS: Hume reaches {host} over TLS and the CA file verifies it")
    return 0


def delete_leftover_secrets(session: boto3.Session, stack: str) -> None:
    """CloudFormation schedules secrets for deletion, so remove them now."""
    sm = session.client("secretsmanager")
    pages = sm.get_paginator("list_secrets").paginate(
        IncludePlannedDeletion=True,
        Filters=[{"Key": "name", "Values": [f"hume/{stack}/"]}],
    )
    for page in pages:
        for secret in page["SecretList"]:
            sm.delete_secret(SecretId=secret["ARN"], ForceDeleteWithoutRecovery=True)
            print(f"Force deleted secret {secret['Name']}")


def leftover_log_group_names(stack: str) -> list[str]:
    """Prefixes of the log groups that a stack delete leaves behind."""
    return [
        f"/hume/{stack}/",
        f"/aws/lambda/{stack}-run-task",
        f"/aws/lambda/{stack}-db-master-reset",
    ]


def delete_leftover_log_groups(session: boto3.Session, stack: str) -> None:
    logs = session.client("logs")
    for prefix in leftover_log_group_names(stack):
        pages = logs.get_paginator("describe_log_groups").paginate(
            logGroupNamePrefix=prefix
        )
        for page in pages:
            for group in page["logGroups"]:
                logs.delete_log_group(logGroupName=group["logGroupName"])
                print(f"Deleted log group {group['logGroupName']}")


def normalise_dns_name(name: str) -> str:
    return name.rstrip(".").lower()


def is_acm_validation_record(name: str, value: str, hosts: Iterable[str]) -> bool:
    """True for a record that ACM wrote for one of these hosts, and nothing else.

    The zone is shared with other sites, and the base may be the zone itself, so
    a suffix check would match the records of other sites. Teardown deletes only
    a CNAME one label below a host whose value points at ACM validation.
    """
    _, _, parent = normalise_dns_name(name).partition(".")
    return parent in {
        normalise_dns_name(host) for host in hosts
    } and normalise_dns_name(value).endswith(ACM_VALIDATION_SUFFIX)


def find_validation_record(
    record_sets: list[dict], name: str, value: str
) -> dict | None:
    """Return the record set that matches the name and value exactly."""
    for record_set in record_sets:
        values = [r["Value"] for r in record_set.get("ResourceRecords", [])]
        if (
            record_set["Type"] == "CNAME"
            and normalise_dns_name(record_set["Name"]) == normalise_dns_name(name)
            and [normalise_dns_name(v) for v in values] == [normalise_dns_name(value)]
        ):
            return record_set
    return None


def capture_validation_records(session: boto3.Session, record: dict[str, str]) -> None:
    """Save the certificate's DNS validation records, because teardown removes them.

    ACM validation through CloudFormation creates the CNAMEs but never deletes
    them, and the certificate that names them is gone after the stack delete.
    """
    arn = record.get("CertificateArn")
    if not arn:
        return
    acm = session.client("acm")
    try:
        certificate = acm.describe_certificate(CertificateArn=arn)["Certificate"]
    except acm.exceptions.ResourceNotFoundException:
        return
    found = [
        dns
        for option in certificate.get("DomainValidationOptions", [])
        if (dns := option.get("ResourceRecord")) and dns["Type"] == "CNAME"
    ]
    if found:
        record[VALIDATION_NAMES_KEY] = ",".join(dns["Name"] for dns in found)
        record[VALIDATION_VALUES_KEY] = ",".join(dns["Value"] for dns in found)
        write_record(record["StackName"], record)


def delete_validation_records(session: boto3.Session, record: dict[str, str]) -> None:
    names = record.get(VALIDATION_NAMES_KEY)
    values = record.get(VALIDATION_VALUES_KEY)
    zone_id = record.get("HostedZoneId")
    base_domain = record.get("BaseDomainName")
    if not (names and values and zone_id and base_domain):
        return
    hosts = hume_hosts(base_domain)
    pairs = zip(names.split(","), values.split(","), strict=True)
    for name, value in pairs:
        if is_acm_validation_record(name, value, hosts):
            delete_validation_record(session, zone_id, name, value)
        else:
            print(f"Not deleting {name}: it is not an ACM record for a Hume host.")


def delete_validation_record(
    session: boto3.Session, zone_id: str, name: str, value: str
) -> None:
    route53 = session.client("route53")
    record_sets = route53.list_resource_record_sets(
        HostedZoneId=zone_id,
        StartRecordName=name,
        StartRecordType="CNAME",
        MaxItems="5",
    )["ResourceRecordSets"]
    match = find_validation_record(record_sets, name, value)
    if match is None:
        print(f"Validation record {name} is already gone")
        return
    route53.change_resource_record_sets(
        HostedZoneId=zone_id,
        ChangeBatch={"Changes": [{"Action": "DELETE", "ResourceRecordSet": match}]},
    )
    print(f"Deleted validation record {name}")


def list_remaining(session: boto3.Session, stack: str) -> list[str]:
    tagging = session.client("resourcegroupstaggingapi")
    pages = tagging.get_paginator("get_resources").paginate(
        TagFilters=[{"Key": "aws:cloudformation:stack-name", "Values": [stack]}]
    )
    return [m["ResourceARN"] for page in pages for m in page["ResourceTagMappingList"]]


def ecs_skip_reason(ecs, kind: str, arn: str, rest: str) -> str | None:
    if kind == "cluster":
        found = ecs.describe_clusters(clusters=[arn])
        if found["failures"]:
            return "the cluster no longer exists"
        if found["clusters"][0]["status"] == "INACTIVE":
            return "the cluster is INACTIVE"
    elif kind == "task-definition":
        status = ecs.describe_task_definition(taskDefinition=arn)["taskDefinition"][
            "status"
        ]
        if status == "INACTIVE":
            return "the task definition is INACTIVE"
    elif kind == "service":
        cluster = rest.split("/")[0]
        try:
            found = ecs.describe_services(cluster=cluster, services=[arn])
        except ecs.exceptions.ClusterNotFoundException:
            return "the cluster of the service is gone"
        if found["failures"]:
            return "the service no longer exists"
        if found["services"][0]["status"] == "INACTIVE":
            return "the service is INACTIVE"
    return None


def ec2_skip_reason(ec2, kind: str, rest: str) -> str | None:
    if kind == "instance":
        try:
            found = ec2.describe_instances(InstanceIds=[rest])
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "InvalidInstanceID.NotFound":
                return "the instance no longer exists"
            raise
        states = [
            instance["State"]["Name"]
            for reservation in found["Reservations"]
            for instance in reservation["Instances"]
        ]
        return "the instance is terminated" if states == ["terminated"] else None
    if kind not in EC2_DESCRIBE:
        return None
    method, argument, not_found = EC2_DESCRIBE[kind]
    try:
        found = getattr(ec2, method)(**{argument: [rest]})
    except ClientError as exc:
        if exc.response["Error"]["Code"] == not_found:
            return f"the {kind} no longer exists"
        raise
    described = (value for key, value in found.items() if key != "ResponseMetadata")
    return None if any(described) else f"the {kind} no longer exists"


def skip_reason(session: boto3.Session, arn: str) -> str | None:
    """Why a tagged resource no longer counts, by asking its own service.

    The tagging index lags behind deletes. None means the resource is live, or
    that it is of a kind not checked here or the check failed, so it still counts.
    """
    parts = arn.split(":", 5)
    if len(parts) < 6:
        return None
    service, resource = parts[2], parts[5]
    kind, _, rest = resource.partition("/")
    try:
        match service:
            case "ecs":
                return ecs_skip_reason(session.client("ecs"), kind, arn, rest)
            case "ec2":
                return ec2_skip_reason(session.client("ec2"), kind, rest)
            case "elasticloadbalancing":
                elb = session.client("elbv2")
                if kind == "loadbalancer":
                    elb.describe_load_balancers(LoadBalancerArns=[arn])
                elif kind == "targetgroup":
                    elb.describe_target_groups(TargetGroupArns=[arn])
            case "rds" if resource.startswith("db:"):
                session.client("rds").describe_db_instances(
                    DBInstanceIdentifier=resource.removeprefix("db:")
                )
    except ClientError as exc:
        if exc.response["Error"]["Code"] in GONE_ERROR_CODES:
            return "the resource no longer exists"
    return None


def split_remaining(
    session: boto3.Session, arns: list[str]
) -> tuple[list[str], list[tuple[str, str]]]:
    """Split tagged ARNs into live ones and ones skipped with the reason."""
    live: list[str] = []
    skipped: list[tuple[str, str]] = []
    for arn in arns:
        reason = skip_reason(session, arn)
        if reason:
            skipped.append((arn, reason))
        else:
            live.append(arn)
    return live, skipped


def stack_resource_id(cfn, stack: str, logical_id: str) -> str | None:
    """Physical id of a stack resource, or None when the stack is gone."""
    try:
        detail = cfn.describe_stack_resource(
            StackName=stack, LogicalResourceId=logical_id
        )
    except ClientError as exc:
        if "does not exist" in str(exc):
            return None
        raise
    return detail["StackResourceDetail"].get("PhysicalResourceId")


def running_task_count(ecs, cluster: str | None) -> int:
    """Running tasks in the cluster, 0 when the cluster is already gone."""
    if cluster is None:
        return 0
    found = ecs.describe_clusters(clusters=[cluster])["clusters"]
    return found[0]["runningTasksCount"] if found else 0


def scale_group_to_zero(session: boto3.Session, stack: str) -> None:
    """Stop the instance and its tasks while the stack still has a route out.

    The ECS agent needs the internet to report a task stopped. If the stack
    delete removes the default route first, the agent cannot report, the
    instance keeps its public address and the gateway detach fails. Setting
    the group to 0 first lets the agent drain with the network intact.
    """
    cfn = session.client("cloudformation")
    autoscaling = session.client("autoscaling")
    ecs = session.client("ecs")
    group = stack_resource_id(cfn, stack, "AutoScalingGroup")
    if group is None:
        return
    found = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[group])
    if not found["AutoScalingGroups"]:
        return
    cluster = stack_resource_id(cfn, stack, "Cluster")
    autoscaling.update_auto_scaling_group(
        AutoScalingGroupName=group, MinSize=0, DesiredCapacity=0
    )
    print(f"Set {group} to 0 and waiting for the instance and tasks to stop")
    deadline = time.monotonic() + GROUP_DRAIN_TIMEOUT
    while True:
        groups = autoscaling.describe_auto_scaling_groups(
            AutoScalingGroupNames=[group]
        )["AutoScalingGroups"]
        instances = groups[0]["Instances"] if groups else []
        tasks = running_task_count(ecs, cluster)
        if not instances and tasks == 0:
            print("The instance is terminated and no tasks are running")
            return
        if time.monotonic() >= deadline:
            print(
                f"Still {len(instances)} instance(s) and {tasks} task(s) after "
                f"{GROUP_DRAIN_TIMEOUT}s. Deleting the stack anyway.",
                file=sys.stderr,
            )
            return
        time.sleep(GROUP_DRAIN_POLL)


def delete_stack_with_retry(
    cfn, stack: str, scale_in: Callable[[], None] | None = None
) -> bool:
    """Delete the stack, retrying a failed delete before any force cleanup.

    The cluster delete fails while the container instance is still registered,
    and the Auto Scaling instance drains late. A later attempt usually works.
    scale_in runs before every attempt, so a retry also stops a new instance.
    """
    for attempt in range(1, DELETE_ATTEMPTS + 1):
        if scale_in is not None:
            scale_in()
        cfn.delete_stack(StackName=stack)
        print(f"Deleting {stack} (attempt {attempt} of {DELETE_ATTEMPTS})")
        try:
            cfn.get_waiter("stack_delete_complete").wait(
                StackName=stack, WaiterConfig={"Delay": 15, "MaxAttempts": 120}
            )
        except WaiterError:
            print(f"Delete attempt {attempt} failed:", file=sys.stderr)
            print_failed_events(cfn, stack)
            if attempt < DELETE_ATTEMPTS:
                print(f"Waiting {DELETE_RETRY_WAIT}s before retrying")
                time.sleep(DELETE_RETRY_WAIT)
            continue
        return True
    return False


def confirm_teardown(stack: str, assume_yes: bool) -> bool:
    """Ask for the stack name, because the database and its data are deleted."""
    if assume_yes:
        return True
    print(f"This deletes the stack {stack}, its database and its secrets.")
    print("The database keeps a final snapshot. The encryption key is retained.")
    print("The EFS file system with the Hume data is retained.")
    try:
        typed = input("Type the stack name to confirm: ").strip()
    except EOFError:
        typed = ""
    if typed != stack:
        print("The name does not match. Nothing was deleted.", file=sys.stderr)
        return False
    return True


def disable_database_protection(session: boto3.Session, stack: str) -> bool:
    """Switch off DeletionProtection, which would fail the stack delete.

    The template turns it on, and CloudFormation cannot delete a protected
    instance. This runs only after the confirmation.
    """
    cfn = session.client("cloudformation")
    database = stack_resource_id(cfn, stack, "Database")
    if not database:
        return True
    rds = session.client("rds")
    try:
        rds.modify_db_instance(
            DBInstanceIdentifier=database,
            DeletionProtection=False,
            ApplyImmediately=True,
        )
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "DBInstanceNotFound":
            return True
        print(
            f"Could not switch off deletion protection on {database} ({code}). "
            "The stack delete would fail. Fix the database state or switch the "
            "protection off by hand, then run teardown again.",
            file=sys.stderr,
        )
        return False
    print(f"Switched off deletion protection on {database}")
    return True


def empty_access_log_bucket(session: boto3.Session, stack: str) -> None:
    """Delete the load balancer logs, since a bucket with objects cannot be deleted.

    The bucket is not versioned, so a plain delete of each key is enough.
    """
    cfn = session.client("cloudformation")
    bucket = stack_resource_id(cfn, stack, "AccessLogBucket")
    if not bucket:
        return
    s3 = session.client("s3")
    removed = 0
    try:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            keys = [{"Key": item["Key"]} for item in page.get("Contents", [])]
            if not keys:
                continue
            result = s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})
            for error in result.get("Errors", []):
                print(f"Could not delete {error['Key']}: {error['Message']}")
            removed += len(result.get("Deleted", []))
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NoSuchBucket":
            raise
        return
    print(f"Emptied {bucket}: {removed} object(s) deleted")


def prepare_delete(session: boto3.Session, stack: str) -> None:
    """Stop the instance, then empty the log bucket before a delete attempt.

    The group is scaled in first so that no new traffic adds log objects, and
    the bucket is emptied again on a retry.
    """
    scale_group_to_zero(session, stack)
    empty_access_log_bucket(session, stack)


class Retained(NamedTuple):
    """The key and file system that outlive the stack."""

    key_id: str | None
    key_arn: str | None
    file_system_id: str | None


def retained_resources(session: boto3.Session, stack: str) -> Retained:
    """Read the kept key and file system before the stack is deleted.

    The outputs name them even when the stack adopted them from an earlier
    stack. A stack whose create failed has no outputs, so its own resources are
    read instead.
    """
    cfn = session.client("cloudformation")
    try:
        outputs = stack_outputs(session, stack)
    except ClientError as exc:
        if "does not exist" in str(exc):
            return Retained(None, None, None)
        raise
    key_arn = outputs.get("DataKeyArn")
    key_id = (
        key_arn.rsplit("/", 1)[-1]
        if key_arn
        else stack_resource_id(cfn, stack, "DataKey")
    )
    file_system_id = outputs.get("FileSystemId") or stack_resource_id(
        cfn, stack, "DataFileSystem"
    )
    return Retained(key_id, key_arn, file_system_id)


def is_kept_on_purpose(
    arn: str, key_id: str | None, file_system_id: str | None = None
) -> bool:
    """True for the key, the file system and the snapshots that outlive the stack.

    The template retains the key and the file system and takes a final
    snapshot. All keep the stack tag, so the leftover check would otherwise fail
    every teardown.
    """
    parts = arn.split(":", 5)
    if len(parts) < 6:
        return False
    service, resource = parts[2], parts[5]
    if service == "kms":
        return key_id is not None and resource == f"key/{key_id}"
    if service == "elasticfilesystem":
        return (
            file_system_id is not None and resource == f"file-system/{file_system_id}"
        )
    return service == "rds" and resource.startswith("snapshot:")


def cmd_teardown(args: argparse.Namespace) -> int:
    stack = args.stack_name
    record = read_record(stack)
    if not confirm_teardown(stack, args.yes):
        return 1
    session = make_session(args, record)
    cfn = session.client("cloudformation")

    capture_validation_records(session, record)
    retained = retained_resources(session, stack)
    key_id, file_system_id = retained.key_id, retained.file_system_id
    if not disable_database_protection(session, stack):
        return 1
    try:
        if not delete_stack_with_retry(
            cfn, stack, scale_in=lambda: prepare_delete(session, stack)
        ):
            return 1
    except ClientError as exc:
        # An already deleted stack is fine. Anything else is not.
        if "does not exist" not in str(exc):
            raise
    print("Stack deleted")
    if key_id:
        print(
            f"The encryption key {key_id} is retained, so the final database "
            "snapshot and the file system stay readable. Schedule its deletion "
            "when both are gone: aws kms schedule-key-deletion "
            f"--key-id {key_id} --pending-window-in-days 30"
        )
    if file_system_id:
        print(f"The file system {file_system_id} is retained with the Hume data.")
    if retained.key_arn and file_system_id:
        print("To restore into a new stack, deploy with:")
        print(f"  --existing-data-key-arn {retained.key_arn}")
        print(f"  --existing-file-system-id {file_system_id}")
        print("  --db-snapshot-identifier <the final snapshot of the database>")

    delete_leftover_secrets(session, stack)
    delete_leftover_log_groups(session, stack)
    delete_validation_records(session, record)
    delete_template_object(session, record)

    tagged = list_remaining(session, stack)
    kept = [arn for arn in tagged if is_kept_on_purpose(arn, key_id, file_system_id)]
    for arn in kept:
        print(f"Kept on purpose: {arn}")
    remaining, skipped = split_remaining(
        session, [arn for arn in tagged if arn not in kept]
    )
    for arn, reason in skipped:
        print(f"Skipped {arn}: {reason}. The tagging index lags.")
    if remaining:
        print("Resources still tagged with this stack:", file=sys.stderr)
        for arn in remaining:
            print(f"  {arn}", file=sys.stderr)
        print("The tagging index can lag. Check again in a few minutes.")
        return 1
    record_path(stack).unlink(missing_ok=True)
    print("Teardown complete. The AMI is not part of this stack and is kept.")
    return 0


def main() -> int:
    args = parse_args()
    commands = {
        "deploy": cmd_deploy,
        "licence": cmd_licence,
        "truststore": cmd_truststore,
        "truststore-options": cmd_truststore_options,
        "up": cmd_up,
        "smoke": cmd_smoke,
        "validate": cmd_validate,
        "reimport": cmd_reimport,
        "refresh-truststore": cmd_refresh_truststore,
        "spine-config": cmd_spine_config,
        "teardown": cmd_teardown,
        "delete-template-bucket": cmd_delete_template_bucket,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
