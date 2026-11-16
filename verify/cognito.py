#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3[crt]", "python-dotenv"]
# ///
"""Create an Amazon Cognito user pool that stands in for the customer's provider.

This is a test harness, not part of either template. The pool belongs to no
stack. Hume and Neo4j EE both point at it through their OIDC parameters, so it
is created before either stack and torn down after both.

    uv run verify/cognito.py up         # pool, domain, clients, groups, users
    uv run verify/cognito.py up --neo4j-host <advertised-dns>
                                        # also register the Neo4j addresses now
    uv run verify/cognito.py env        # check the issuer, write both .env files,
                                        # set the Neo4j redirect addresses
    uv run verify/cognito.py sync       # add missing groups, users and the trigger
                                        # to a pool that already exists
    uv run verify/cognito.py callbacks  # list or add redirect addresses
    uv run verify/cognito.py callbacks --reset-hume
                                        # keep only the web address on hume-web
    uv run verify/cognito.py check --stack-name <stack>  # sign in, call Hume
    uv run verify/cognito.py status     # print the record
    uv run verify/cognito.py teardown   # delete the pool and the user secrets

State goes into .deploy/<name>.cognito. It holds ids and names only. Each test
user's password is stored in Secrets Manager as hume/<name>/cognito/<user> and
is never printed.

The pool runs a pre token generation Lambda that adds aud to each access token,
because Neo4j refuses a token without one. `up` and `sync` create it, and
`teardown` deletes it.
"""

import argparse
import base64
import contextlib
import io
import json
import re
import secrets
import string
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from dotenv import set_key

# The repository modules imported below live in these directories.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_IMPORT_DIRS = [str(_REPO_ROOT / name) for name in ("stack",)]
sys.path[:0] = [entry for entry in _IMPORT_DIRS if entry not in sys.path]

import deploy

DEFAULT_NAME = "hume-test-idp"
ADMIN_USER = "hume-admin"
PLAIN_USER = "hume-user"
NEO4J_ADMIN_USER = "neo4j-admin"
SPINE_ANALYST_USER = "spine-analyst"
SPINE_OFFICER_USER = "spine-officer"
ADMIN_GROUP = "hume-admins"
PLAIN_GROUP = "hume-users"
NEO4J_ADMIN_GROUP = "neo4j-admins"
SPINE_ANALYST_GROUP = "spine-analysts"
SPINE_OFFICER_GROUP = "spine-officers"
GROUPS = {
    ADMIN_GROUP: "Mapped to the Hume role ADMINISTRATOR",
    PLAIN_GROUP: "Mapped to the Hume role USER",
    NEO4J_ADMIN_GROUP: "Mapped to the Neo4j role admin",
    SPINE_ANALYST_GROUP: "Mapped to the Neo4j role spine_analyst",
    SPINE_OFFICER_GROUP: "Mapped to the Neo4j role spine_officer",
}
# Each test user and the Cognito groups it joins. The plain user joins only the
# group mapped to USER, because Hume rejects a token whose groups map to no role.
# The Spine users sign in to Hume first, so they join that group as well. The
# admin user joins the Spine officers group to read Spine, and no Neo4j admin group.
USERS = {
    ADMIN_USER: [ADMIN_GROUP, SPINE_OFFICER_GROUP],
    PLAIN_USER: [PLAIN_GROUP],
    NEO4J_ADMIN_USER: [NEO4J_ADMIN_GROUP],
    SPINE_ANALYST_USER: [PLAIN_GROUP, SPINE_ANALYST_GROUP],
    SPINE_OFFICER_USER: [PLAIN_GROUP, SPINE_OFFICER_GROUP],
}
# The app client of each product, by the name the --client flag takes.
HUME_CLIENT = "hume"
NEO4J_CLIENT = "neo4j"
CLIENT_NAMES = {HUME_CLIENT: "hume-web", NEO4J_CLIENT: "neo4j"}
CLIENT_RECORD_KEYS = {HUME_CLIENT: "HumeClientId", NEO4J_CLIENT: "Neo4jClientId"}
# The sign-in flows of each client besides the code flow. ADMIN_USER_PASSWORD_AUTH
# lets `check` get tokens without a browser. It needs AWS credentials for the
# pool, so a password alone gets no token. The neo4j client needs no password
# flow.
CLIENT_AUTH_FLOWS = {
    HUME_CLIENT: ["ALLOW_ADMIN_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
    NEO4J_CLIENT: ["ALLOW_REFRESH_TOKEN_AUTH"],
}
# Cognito needs a redirect address for the code flow. Without --neo4j-host, `up`
# registers this placeholder and `env` replaces it.
NEO4J_PLACEHOLDER_URL = "https://localhost:7473/browser/"
NEO4J_HTTPS_PORT = 7473
# Browser returns from the provider with this query string, and Cognito matches
# the whole address, so the registered address has to carry it.
NEO4J_BROWSER_QUERY = "?idp_id=sso&auth_flow_step=redirect_uri"
# The .env that `env` writes for the Neo4j EE deploy.py. The claims have to be on
# both the access token Hume forwards and the ID token Browser presents. sub is
# on both. username is on the access token only, and cognito:username is on the
# ID token only.
NEO4J_ENV_FILE = ".env.neo4jee"
NEO4J_PRINCIPAL_CLAIM = "sub"
NEO4J_GROUPS_CLAIM = "cognito:groups"
NEO4J_GROUP_ROLE_MAPPING = (
    f"{SPINE_ANALYST_GROUP}=spine_analyst;{SPINE_OFFICER_GROUP}=spine_officer"
)
NEO4J_ENV_RECORD_KEYS = ("Issuer", "HumeClientId", "Neo4jClientId")
# The root .env settings that `env` reads and writes.
ADVERTISED_DNS_VAR = "NEO4J_ADVERTISED_DNS"
# The switch the Neo4j EE deploy.py reads to skip Bloom.
NO_BLOOM_VAR = "NO_BLOOM"
HUME_ISSUER_VAR = "HUME_OIDC_ISSUER_URI"
HUME_CLIENT_ID_VAR = "HUME_OIDC_CLIENT_ID"
SCOPES = ["openid", "profile", "email"]
# Fields that update_user_pool_client accepts. Any field left out is reset, so
# every field present in the current client is passed back.
CLIENT_FIELDS = (
    "ClientName",
    "RefreshTokenValidity",
    "AccessTokenValidity",
    "IdTokenValidity",
    "TokenValidityUnits",
    "ReadAttributes",
    "WriteAttributes",
    "ExplicitAuthFlows",
    "SupportedIdentityProviders",
    "CallbackURLs",
    "LogoutURLs",
    "DefaultRedirectURI",
    "AllowedOAuthFlows",
    "AllowedOAuthScopes",
    "AllowedOAuthFlowsUserPoolClient",
    "AnalyticsConfiguration",
    "PreventUserExistenceErrors",
    "EnableTokenRevocation",
    "EnablePropagateAdditionalUserContextData",
    "AuthSessionValidity",
    "RefreshTokenRotation",
)
# Fields that update_user_pool accepts, under the names describe_user_pool uses.
# Any field left out is reset, so every field present is passed back.
# LambdaConfig, AdminCreateUserConfig and the pool name are set in
# pool_update_input.
POOL_FIELDS = (
    "AccountRecoverySetting",
    "AcrConfiguration",
    "AutoVerifiedAttributes",
    "DeletionProtection",
    "DeviceConfiguration",
    "EmailConfiguration",
    "EmailVerificationMessage",
    "EmailVerificationSubject",
    "IssuerConfiguration",
    "KeyConfiguration",
    "MfaConfiguration",
    "Policies",
    "SmsAuthenticationMessage",
    "SmsConfiguration",
    "SmsVerificationMessage",
    "UserAttributeUpdateSettings",
    "UserPoolAddOns",
    "UserPoolTags",
    "UserPoolTier",
    "VerificationMessageTemplate",
)
# The pre token generation trigger. Cognito accepts aud on an access token only
# when it is the client id of the session, so the value comes from the event.
TOKEN_TRIGGER_SOURCE = """\
def handler(event, context):
    claims = {"aud": event["callerContext"]["clientId"]}
    event["response"]["claimsAndScopeOverrideDetails"] = {
        "accessTokenGeneration": {"claimsToAddOrOverride": claims}
    }
    return event
"""
TOKEN_TRIGGER_FILE = "index.py"
TOKEN_TRIGGER_HANDLER = "index.handler"
TOKEN_TRIGGER_RUNTIME = "python3.13"
# Access token customization needs version 2 of the trigger event.
TOKEN_TRIGGER_VERSION = "V2_0"
TOKEN_TRIGGER_STATEMENT = "cognito-invoke"
COGNITO_PRINCIPAL = "cognito-idp.amazonaws.com"
LAMBDA_BASIC_POLICY = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
LAMBDA_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}
# Lambda can refuse a new role for some seconds, until IAM has spread it.
ROLE_RETRIES = 10
ROLE_RETRY_SECONDS = 3
# Claims printed by `check`. None of them is secret.
SHOWN_CLAIMS = (
    "token_use",
    "iss",
    "aud",
    "client_id",
    "username",
    "cognito:username",
    "preferred_username",
    "email",
    "cognito:groups",
    "scope",
)
USERS_ME_PATH = "/users/me"
UPLOAD_CONFIGS_PATH = "/media/management/uploadConfigs"
TIMEOUT_SECONDS = 20


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--profile", help="AWS profile (default: from the record)")
    parser.add_argument("--region", help="AWS region (default: from the record)")
    parser.add_argument(
        "--name", default=DEFAULT_NAME, help=f"Pool name (default: {DEFAULT_NAME})"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up", help="Create the user pool, clients, groups and users")
    up.add_argument(
        "--domain-prefix",
        help="Prefix of the Cognito login domain (default: the pool name)",
    )
    up.add_argument(
        "--neo4j-host",
        help=(
            "Name the Neo4j stack advertises. Registers the Browser and Bloom "
            "addresses now, instead of a placeholder that `env` replaces"
        ),
    )
    env = sub.add_parser(
        "env", help="Check the issuer and write the Hume and Neo4j EE settings"
    )
    env.add_argument(
        "--advertised-dns",
        help=f"Name the Neo4j stack advertises (default: {ADVERTISED_DNS_VAR})",
    )
    env.add_argument(
        "--private-dns-zone",
        help="Zone that holds the advertised name (default: all but its first label)",
    )
    env.add_argument(
        "--no-bloom",
        action="store_true",
        help=f"The Neo4j EE stack installs no Bloom (default: {NO_BLOOM_VAR})",
    )
    check = sub.add_parser("check", help="Sign in with a password and call Hume")
    check.add_argument("--user", default=ADMIN_USER, choices=sorted(USERS))
    check.add_argument("--stack-name", help="Hume stack (default: the only record)")
    sub.add_parser(
        "sync", help="Add missing groups, users and the trigger to an existing pool"
    )
    callbacks = sub.add_parser(
        "callbacks", help="List, add or reset redirect addresses"
    )
    callbacks.add_argument(
        "--client", default=HUME_CLIENT, choices=sorted(CLIENT_NAMES)
    )
    action = callbacks.add_mutually_exclusive_group()
    action.add_argument("--add", metavar="URL", help="Allow this redirect address")
    action.add_argument(
        "--reset-hume",
        action="store_true",
        help="Make the web address the only hume-web redirect address",
    )
    sub.add_parser("status", help="Show the record")
    sub.add_parser("teardown", help="Delete the user pool and the user secrets")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------


def cognito_record_path(name: str):
    # Not .txt, because newest_stack() treats every .txt file as a stack record.
    return deploy.DEPLOY_DIR / f"{name}.cognito"


def read_cognito_record(name: str) -> dict[str, str]:
    path = cognito_record_path(name)
    if not path.exists():
        return {}
    pairs = (line.split(" = ", 1) for line in path.read_text().splitlines() if line)
    return {key: value for key, value in pairs}


def write_cognito_record(name: str, values: dict[str, str]) -> None:
    deploy.DEPLOY_DIR.mkdir(exist_ok=True)
    lines = [f"{key} = {value}" for key, value in values.items()]
    cognito_record_path(name).write_text("\n".join(lines) + "\n")


def require(record: dict[str, str], name: str) -> dict[str, str]:
    if not record:
        sys.exit(f"No Cognito record for {name}. Run `cognito.py up` first.")
    return record


def make_session(args: argparse.Namespace, record: dict[str, str]) -> boto3.Session:
    """Flag first, then the record, then the deploy.py defaults."""
    return boto3.Session(
        profile_name=args.profile
        or record.get("Profile")
        or deploy.env_setting(deploy.PROFILE_VAR)
        or deploy.DEFAULT_PROFILE,
        region_name=args.region or record.get("Region") or deploy.DEFAULT_REGION,
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def issuer_url(region: str, pool_id: str) -> str:
    return f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"


def secret_name(name: str, user: str) -> str:
    return f"hume/{name}/cognito/{user}"


def hume_web_url() -> str:
    """The Hume web address, from the base name setting that deploy.py uses."""
    return f"https://{deploy.hume_hosts(deploy.resolve_base_domain()).web}"


def redirect_urls(web_url: str) -> list[str]:
    """The web address with and without the trailing slash.

    The Hume docs do not name the redirect path. These two are the first guess,
    and `callbacks --add` adds the one the login page reports.
    """
    base = web_url.rstrip("/")
    return [base, f"{base}/"]


def make_password() -> str:
    """A random password that meets the default Cognito policy."""
    alphabet = string.ascii_letters + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(28))
    return f"{body}Aa1!"


def decode_claims(token: str) -> dict:
    """The payload of a JWT, without checking the signature. Only for display."""
    payload = token.split(".")[1]
    padded = payload + "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def shown_claims(claims: dict) -> dict:
    return {key: claims[key] for key in SHOWN_CLAIMS if key in claims}


def neo4j_env_path():
    return deploy.DEPLOY_DIR / NEO4J_ENV_FILE


def normalize_dns(name: str) -> str:
    return name.strip().lower().rstrip(".")


def private_dns_zone(advertised_dns: str) -> str:
    """The zone that holds the advertised name: all but its first label."""
    label, dot, zone = advertised_dns.partition(".")
    if not label or not dot or not zone:
        sys.exit(
            f"Cannot read a zone from {advertised_dns}. Pass --private-dns-zone, "
            "or use a name with at least two labels, such as neo4j-oidc.neo4j.local."
        )
    return zone


def resolve_no_bloom(flag: bool) -> bool:
    """Read --no-bloom, then NO_BLOOM. Bloom is on unless one of them says no."""
    if flag:
        return True
    text = (deploy.env_setting(NO_BLOOM_VAR) or "false").strip().lower()
    if text not in ("true", "false"):
        sys.exit(f"{NO_BLOOM_VAR}={text!r} is not valid. Use true or false.")
    return text == "true"


def neo4j_env_values(
    record: dict[str, str], advertised_dns: str, zone: str, no_bloom: bool
) -> dict[str, str]:
    """The settings the Neo4j EE deploy.py reads from its .env."""
    values = {
        "OIDC_ISSUER_URI": record["Issuer"],
        "OIDC_CLIENT_ID": f"{record['HumeClientId']},{record['Neo4jClientId']}",
        "OIDC_BROWSER_CLIENT_ID": record["Neo4jClientId"],
        "OIDC_PRINCIPAL_CLAIM": NEO4J_PRINCIPAL_CLAIM,
        "OIDC_GROUPS_CLAIM": NEO4J_GROUPS_CLAIM,
        "OIDC_ADMIN_GROUP": NEO4J_ADMIN_GROUP,
        "OIDC_GROUP_ROLE_MAPPING": NEO4J_GROUP_ROLE_MAPPING,
        "ADVERTISED_DNS": advertised_dns,
        "CREATE_PRIVATE_DNS": "true",
        "PRIVATE_DNS_ZONE": zone,
    }
    if no_bloom:
        values[NO_BLOOM_VAR] = "true"
    return values


def hume_env_values(record: dict[str, str]) -> dict[str, str]:
    """The OIDC settings the Hume deploy.py reads from the root .env."""
    return {
        HUME_ISSUER_VAR: record["Issuer"],
        HUME_CLIENT_ID_VAR: record["HumeClientId"],
    }


def env_text(values: dict[str, str]) -> str:
    return "".join(f"{key}={value}\n" for key, value in values.items())


def neo4j_callback_urls(advertised_dns: str, bloom: bool = True) -> list[str]:
    """The addresses Browser and Bloom send the provider back to."""
    base = f"https://{advertised_dns}:{NEO4J_HTTPS_PORT}"
    urls = [f"{base}/browser/{NEO4J_BROWSER_QUERY}", f"{base}/browser/"]
    if bloom:
        urls.append(f"{base}/bloom/")
    return urls


def client_update_input(
    client: dict,
    callback_urls: list[str] | None = None,
    flows: list[str] | None = None,
) -> dict:
    """The update of client that changes only the given addresses or flows."""
    kwargs = {key: client[key] for key in CLIENT_FIELDS if key in client}
    kwargs["UserPoolId"] = client["UserPoolId"]
    kwargs["ClientId"] = client["ClientId"]
    if callback_urls is not None:
        kwargs["CallbackURLs"] = callback_urls
        kwargs["LogoutURLs"] = callback_urls
    if flows is not None:
        kwargs["ExplicitAuthFlows"] = flows
    return kwargs


def pool_update_input(pool: dict, function_arn: str) -> dict:
    """The update of pool that changes only the pre token generation trigger."""
    kwargs = {key: pool[key] for key in POOL_FIELDS if key in pool}
    kwargs["UserPoolId"] = pool["Id"]
    kwargs["PoolName"] = pool["Name"]
    # describe_user_pool returns this old field beside its replacement in
    # Policies, and an update must not send both.
    kwargs["AdminCreateUserConfig"] = {
        key: value
        for key, value in pool["AdminCreateUserConfig"].items()
        if key != "UnusedAccountValidityDays"
    }
    # The legacy field has to match the new one when both are set, so it goes.
    lambda_config = {
        key: value
        for key, value in pool.get("LambdaConfig", {}).items()
        if key != "PreTokenGeneration"
    }
    kwargs["LambdaConfig"] = lambda_config | {
        "PreTokenGenerationConfig": token_trigger_config(function_arn)
    }
    return kwargs


def token_trigger_name(name: str) -> str:
    """The name of both the Lambda function and its role."""
    return f"{name}-pre-token"


def token_trigger_config(function_arn: str) -> dict[str, str]:
    return {"LambdaArn": function_arn, "LambdaVersion": TOKEN_TRIGGER_VERSION}


def token_trigger_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(TOKEN_TRIGGER_FILE, TOKEN_TRIGGER_SOURCE)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Pre token generation trigger
# ---------------------------------------------------------------------------


def ensure_token_role(iam, name: str) -> str:
    """Create the function role, or find it. Returns its ARN."""
    role = token_trigger_name(name)
    try:
        created = iam.create_role(
            RoleName=role,
            AssumeRolePolicyDocument=json.dumps(LAMBDA_TRUST_POLICY),
            Description="Runs the pre token generation trigger of the test pool",
            Tags=[{"Key": "hume-idp", "Value": name}],
        )
    except iam.exceptions.EntityAlreadyExistsException:
        created = iam.get_role(RoleName=role)
    iam.attach_role_policy(RoleName=role, PolicyArn=LAMBDA_BASIC_POLICY)
    return created["Role"]["Arn"]


def create_token_function(lam, name: str, role_arn: str) -> str:
    """Create the function, waiting for IAM to spread the role. Returns its ARN."""

    def create() -> str:
        return lam.create_function(
            FunctionName=token_trigger_name(name),
            Runtime=TOKEN_TRIGGER_RUNTIME,
            Role=role_arn,
            Handler=TOKEN_TRIGGER_HANDLER,
            Code={"ZipFile": token_trigger_zip()},
            Description="Adds aud to Cognito access tokens",
            Tags={"hume-idp": name},
        )["FunctionArn"]

    for _ in range(ROLE_RETRIES):
        with contextlib.suppress(lam.exceptions.InvalidParameterValueException):
            return create()
        time.sleep(ROLE_RETRY_SECONDS)
    return create()


def ensure_token_function(lam, name: str, role_arn: str) -> str:
    """Create the function, or update its code. Returns its ARN."""
    function = token_trigger_name(name)
    try:
        existing = lam.get_function(FunctionName=function)
    except lam.exceptions.ResourceNotFoundException:
        function_arn = create_token_function(lam, name, role_arn)
        lam.get_waiter("function_active_v2").wait(FunctionName=function)
        return function_arn
    lam.update_function_code(FunctionName=function, ZipFile=token_trigger_zip())
    lam.get_waiter("function_updated_v2").wait(FunctionName=function)
    return existing["Configuration"]["FunctionArn"]


def allow_pool_to_invoke(lam, name: str, pool_arn: str) -> None:
    # The statement is already there when `sync` runs a second time.
    with contextlib.suppress(lam.exceptions.ResourceConflictException):
        lam.add_permission(
            FunctionName=token_trigger_name(name),
            StatementId=TOKEN_TRIGGER_STATEMENT,
            Action="lambda:InvokeFunction",
            Principal=COGNITO_PRINCIPAL,
            SourceArn=pool_arn,
        )


def set_token_trigger(cognito, pool: dict, function_arn: str) -> None:
    current = pool.get("LambdaConfig", {}).get("PreTokenGenerationConfig")
    if current == token_trigger_config(function_arn):
        print(f"{pool['Name']} already runs the pre token generation trigger")
        return
    cognito.update_user_pool(**pool_update_input(pool, function_arn))
    print(f"Set the pre token generation trigger of {pool['Name']}")


def install_token_trigger(session, name: str, pool_id: str) -> None:
    """Create the role and the function, then wire them to the pool.

    The pool has to exist first, because the permission names its ARN. The
    permission comes before the wiring, so no sign-in meets a function that
    Cognito cannot call.
    """
    cognito = session.client("cognito-idp")
    lam = session.client("lambda")
    pool = cognito.describe_user_pool(UserPoolId=pool_id)["UserPool"]
    role_arn = ensure_token_role(session.client("iam"), name)
    function_arn = ensure_token_function(lam, name, role_arn)
    allow_pool_to_invoke(lam, name, pool["Arn"])
    set_token_trigger(cognito, pool, function_arn)


def delete_token_trigger(session, name: str) -> None:
    """Delete the function, its log group and its role, whichever exist."""
    resource = token_trigger_name(name)
    lam = session.client("lambda")
    try:
        lam.delete_function(FunctionName=resource)
        print(f"Deleted function {resource}")
    except lam.exceptions.ResourceNotFoundException:
        print(f"Function {resource} was already gone")
    logs = session.client("logs")
    # The log group appears only after the first invocation.
    with contextlib.suppress(logs.exceptions.ResourceNotFoundException):
        logs.delete_log_group(logGroupName=f"/aws/lambda/{resource}")
    iam = session.client("iam")
    try:
        iam.detach_role_policy(RoleName=resource, PolicyArn=LAMBDA_BASIC_POLICY)
        iam.delete_role(RoleName=resource)
        print(f"Deleted role {resource}")
    except iam.exceptions.NoSuchEntityException:
        print(f"Role {resource} was already gone")


# ---------------------------------------------------------------------------
# up
# ---------------------------------------------------------------------------


def create_pool(cognito, name: str) -> str:
    pool = cognito.create_user_pool(
        PoolName=name,
        AdminCreateUserConfig={"AllowAdminCreateUserOnly": True},
        UsernameConfiguration={"CaseSensitive": False},
        UserPoolTags={"hume-idp": name},
    )
    return pool["UserPool"]["Id"]


def create_client(
    cognito, pool_id: str, client_name: str, urls: list[str], flows: list[str]
) -> str:
    client = cognito.create_user_pool_client(
        UserPoolId=pool_id,
        ClientName=client_name,
        GenerateSecret=False,
        AllowedOAuthFlows=["code"],
        AllowedOAuthFlowsUserPoolClient=True,
        AllowedOAuthScopes=SCOPES,
        SupportedIdentityProviders=["COGNITO"],
        CallbackURLs=urls,
        LogoutURLs=urls,
        ExplicitAuthFlows=flows,
        PreventUserExistenceErrors="ENABLED",
        EnableTokenRevocation=True,
    )
    return client["UserPoolClient"]["ClientId"]


def create_users(
    cognito,
    sm,
    name: str,
    pool_id: str,
    groups: dict[str, str],
    users: dict[str, list[str]],
) -> None:
    for group, description in groups.items():
        cognito.create_group(
            UserPoolId=pool_id, GroupName=group, Description=description
        )
    for user, user_groups in users.items():
        password = make_password()
        cognito.admin_create_user(
            UserPoolId=pool_id,
            Username=user,
            TemporaryPassword=make_password(),
            MessageAction="SUPPRESS",
        )
        cognito.admin_set_user_password(
            UserPoolId=pool_id, Username=user, Password=password, Permanent=True
        )
        for group in user_groups:
            cognito.admin_add_user_to_group(
                UserPoolId=pool_id, Username=user, GroupName=group
            )
        sm.create_secret(
            Name=secret_name(name, user),
            Description=f"Password of the Cognito test user {user}",
            SecretString=password,
            Tags=[{"Key": "hume-idp", "Value": name}],
        )
        joined = ", ".join(user_groups) or "none"
        print(f"  user {user}, groups {joined}, secret {secret_name(name, user)}")


def command_up(args, session, name: str) -> int:
    cognito_record = read_cognito_record(name)
    if cognito_record:
        sys.exit(
            f"{cognito_record_path(name)} exists. Run `cognito.py status`, or "
            "`cognito.py teardown` first to start over."
        )
    hume_urls = redirect_urls(hume_web_url())
    neo4j_urls = [NEO4J_PLACEHOLDER_URL]
    if args.neo4j_host:
        neo4j_host = normalize_dns(args.neo4j_host)
        if not re.fullmatch(deploy.DNS_NAME, neo4j_host):
            sys.exit(f"--neo4j-host {args.neo4j_host!r} is not a DNS name")
        neo4j_urls = neo4j_callback_urls(neo4j_host, bloom=not resolve_no_bloom(False))
    cognito = session.client("cognito-idp")
    domain = args.domain_prefix or name
    print(f"Creating user pool {name} in {session.region_name}")
    pool_id = create_pool(cognito, name)
    # Written now, so that teardown finds the pool if a later step fails.
    cognito_record = {
        "Name": name,
        "Profile": session.profile_name,
        "Region": session.region_name,
        "UserPoolId": pool_id,
    }
    write_cognito_record(name, cognito_record)
    try:
        cognito.create_user_pool_domain(
            Domain=domain, UserPoolId=pool_id, ManagedLoginVersion=1
        )
    except ClientError as exc:
        sys.exit(
            f"Domain {domain} was refused: {exc.response['Error']['Message']}. "
            "Run teardown, then up again with --domain-prefix."
        )
    cognito_record["Domain"] = domain
    write_cognito_record(name, cognito_record)
    urls = {HUME_CLIENT: hume_urls, NEO4J_CLIENT: neo4j_urls}
    for client, client_name in CLIENT_NAMES.items():
        client_id = create_client(
            cognito, pool_id, client_name, urls[client], CLIENT_AUTH_FLOWS[client]
        )
        cognito_record[CLIENT_RECORD_KEYS[client]] = client_id
        write_cognito_record(name, cognito_record)
    cognito_record["Issuer"] = issuer_url(session.region_name, pool_id)
    write_cognito_record(name, cognito_record)
    create_users(
        cognito, session.client("secretsmanager"), name, pool_id, GROUPS, USERS
    )
    cognito_record["Users"] = ",".join(USERS)
    write_cognito_record(name, cognito_record)
    install_token_trigger(session, name, pool_id)
    print(f"Issuer:          {cognito_record['Issuer']}")
    print(f"Hume client ID:  {cognito_record['HumeClientId']}")
    print(f"Neo4j client ID: {cognito_record['Neo4jClientId']}")
    print(f"Login:     https://{domain}.auth.{session.region_name}.amazoncognito.com")
    print(f"Hume redirects:  {', '.join(hume_urls)}")
    if args.neo4j_host:
        print(f"Neo4j redirects: {', '.join(neo4j_urls)}")
    else:
        print(f"Neo4j redirect:  {NEO4J_PLACEHOLDER_URL} (placeholder)")
    print(
        "Next: run `cognito.py env` to check the issuer, write both .env files "
        "and set the Neo4j redirect addresses"
    )
    return 0


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


def command_sync(args, session, name: str) -> int:
    """Give a pool from an older `up` what a new pool gets.

    It adds only the groups and users the pool lacks, so existing passwords stay.
    Run `env` afterwards for the client settings.
    """
    cognito_record = require(read_cognito_record(name), name)
    pool_id = cognito_record["UserPoolId"]
    cognito = session.client("cognito-idp")
    groups = cognito.list_groups(UserPoolId=pool_id)["Groups"]
    users = cognito.list_users(UserPoolId=pool_id)["Users"]
    present_groups = {group["GroupName"] for group in groups}
    present_users = {user["Username"] for user in users}
    create_users(
        cognito,
        session.client("secretsmanager"),
        name,
        pool_id,
        {key: value for key, value in GROUPS.items() if key not in present_groups},
        {key: value for key, value in USERS.items() if key not in present_users},
    )
    cognito_record["Users"] = ",".join(USERS)
    write_cognito_record(name, cognito_record)
    install_token_trigger(session, name, pool_id)
    return 0


# ---------------------------------------------------------------------------
# env
# ---------------------------------------------------------------------------


def check_issuer(issuer: str) -> None:
    """Stop unless the discovery document names the same issuer."""
    status, body = fetch(f"{issuer}/.well-known/openid-configuration")
    if status != 200:
        sys.exit(f"Discovery document returned {status}")
    named = json.loads(body).get("issuer")
    if named != issuer:
        sys.exit(f"Discovery document names the issuer {named}, not {issuer}")
    print(f"Issuer checked: {issuer}")


def write_root_env(values: dict[str, str]) -> None:
    """Replace each line of the root .env in place, or append it when missing."""
    env_file = deploy.ENV_FILE
    if not env_file.exists():
        sys.exit(f"{env_file} does not exist. Copy .env.example to it first.")
    for var, value in values.items():
        set_key(env_file, var, value, quote_mode="never")
    print(f"Saved {', '.join(values)} to {env_file}")


def set_auth_flows(cognito, client: dict, flows: list[str]) -> dict:
    """Make flows the sign-in flows of client. Returns the client as it now is."""
    if set(client.get("ExplicitAuthFlows", [])) == set(flows):
        print(f"{client['ClientName']} sign-in flows are already set")
        return client
    cognito.update_user_pool_client(**client_update_input(client, flows=flows))
    print(f"Set the {client['ClientName']} sign-in flows: {', '.join(flows)}")
    return client | {"ExplicitAuthFlows": flows}


def set_callbacks(cognito, client: dict, urls: list[str]) -> None:
    """Make urls the only redirect and sign-out addresses of the client."""
    current = client.get("CallbackURLs", [])
    # Cognito may return the addresses in its own order, so compare sets.
    wanted = set(urls)
    if set(current) == wanted and set(client.get("LogoutURLs", [])) == wanted:
        print(f"{client['ClientName']} redirect addresses are already set")
        return
    cognito.update_user_pool_client(**client_update_input(client, urls))
    print(f"Set the {client['ClientName']} redirect addresses:")
    for url in urls:
        print(f"  {'+' if url not in current else ' '} {url}")
    for url in current:
        if url not in urls:
            print(f"  - {url}")


def command_env(args, session, name: str) -> int:
    """Check the issuer, write the Hume and Neo4j EE OIDC settings, set the
    sign-in flows of both clients, and set the redirect addresses of the neo4j
    client.

    The issuer check and the read of both clients come before any write, so a
    bad issuer or bad AWS credentials stop it with nothing written. The client
    updates come last. The neo4j addresses are replaced, so any added by hand
    with `callbacks --add` are dropped.
    """
    cognito_record = require(read_cognito_record(name), name)
    missing = [key for key in NEO4J_ENV_RECORD_KEYS if key not in cognito_record]
    if missing:
        sys.exit(
            f"The record of {name} lacks {', '.join(missing)}. `up` did not finish. "
            "Run `cognito.py teardown`, then `up` again."
        )
    raw_dns = args.advertised_dns or deploy.env_setting(ADVERTISED_DNS_VAR)
    if not raw_dns:
        sys.exit(
            f"{ADVERTISED_DNS_VAR} is not set in the environment or in "
            f"{deploy.ENV_FILE}. Set it, or pass --advertised-dns."
        )
    advertised_dns = normalize_dns(raw_dns)
    if args.private_dns_zone:
        zone = normalize_dns(args.private_dns_zone)
        if not advertised_dns.endswith(f".{zone}"):
            sys.exit(f"{advertised_dns} is not inside the zone {zone}")
    else:
        zone = private_dns_zone(advertised_dns)
    no_bloom = resolve_no_bloom(args.no_bloom)
    check_issuer(cognito_record["Issuer"])
    cognito = session.client("cognito-idp")
    clients = {
        client: cognito.describe_user_pool_client(
            UserPoolId=cognito_record["UserPoolId"],
            ClientId=cognito_record[CLIENT_RECORD_KEYS[client]],
        )["UserPoolClient"]
        for client in CLIENT_NAMES
    }
    hume_values = hume_env_values(cognito_record)
    print(env_text(hume_values), end="")
    write_root_env(hume_values)
    text = env_text(neo4j_env_values(cognito_record, advertised_dns, zone, no_bloom))
    print(f"\n{text}", end="")
    path = neo4j_env_path()
    deploy.DEPLOY_DIR.mkdir(exist_ok=True)
    path.write_text(text)
    print(f"Wrote {path}")
    print(
        "Next: copy it into the Neo4j project, then create the EE stack from there:\n"
        f"  cp {path} <neo4j-aws-cloudformation>/neo4j-ee/.env"
    )
    for client, flows in CLIENT_AUTH_FLOWS.items():
        clients[client] = set_auth_flows(cognito, clients[client], flows)
    urls = neo4j_callback_urls(advertised_dns, bloom=not no_bloom)
    set_callbacks(cognito, clients[NEO4J_CLIENT], urls)
    return 0


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def fetch(url: str, token: str | None = None) -> tuple[int, bytes]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as reply:
            return reply.status, reply.read()
    except urllib.error.HTTPError as err:
        with err:
            return err.code, err.read()
    except urllib.error.URLError as err:
        sys.exit(f"Could not reach {url}: {err.reason}")


def describe_body(body: bytes) -> str:
    """Name and role fields of a JSON body. Other values are left out."""
    try:
        value = json.loads(body)
    except ValueError:
        return "body is not JSON"
    if not isinstance(value, dict):
        return f"body is a JSON {type(value).__name__}"
    shown = {
        key: item
        for key, item in value.items()
        if "name" in key.lower() or "role" in key.lower()
    }
    return json.dumps(shown) if shown else f"fields: {', '.join(sorted(value))}"


def sign_in(
    session, name: str, cognito_record: dict[str, str], user: str
) -> dict[str, str]:
    """The access and ID tokens of a test user, through the admin API.

    The password comes from the user's secret and is dropped after the call.
    """
    password = session.client("secretsmanager").get_secret_value(
        SecretId=secret_name(name, user)
    )["SecretString"]
    result = session.client("cognito-idp").admin_initiate_auth(
        UserPoolId=cognito_record["UserPoolId"],
        ClientId=cognito_record["HumeClientId"],
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": user, "PASSWORD": password},
    )["AuthenticationResult"]
    del password
    return {"access": result["AccessToken"], "id": result["IdToken"]}


def hume_roles(api_host: str, token: str) -> tuple[int, list[str]]:
    """The status of GET /users/me with the token, and the roles it lists."""
    status, body = fetch(f"https://{api_host}{USERS_ME_PATH}", token)
    try:
        value = json.loads(body)
    except ValueError:
        return status, []
    roles = value.get("roles", []) if isinstance(value, dict) else []
    return status, [str(role) for role in roles]


def command_check(args, session, name: str) -> int:
    cognito_record = require(read_cognito_record(name), name)
    stack = args.stack_name or deploy.newest_stack()
    api_host = deploy.hume_hosts(deploy.read_record(stack)["BaseDomainName"]).api
    issuer = cognito_record["Issuer"]
    status, body = fetch(f"{issuer}/.well-known/openid-configuration")
    if status != 200:
        sys.exit(f"Discovery document returned {status}")
    discovery = json.loads(body)
    print("Discovery document")
    for key in ("issuer", "authorization_endpoint", "end_session_endpoint"):
        print(f"  {key}: {discovery.get(key, '(absent)')}")

    tokens = sign_in(session, name, cognito_record, args.user)

    for kind, token in tokens.items():
        print(f"\n{kind} token of {args.user}")
        for key, value in shown_claims(decode_claims(token)).items():
            print(f"  {key}: {value}")
        api_status, api_body = fetch(f"https://{api_host}{USERS_ME_PATH}", token)
        print(f"  API   GET {USERS_ME_PATH}: {api_status}  {describe_body(api_body)}")
        media_status, _ = fetch(f"https://{api_host}{UPLOAD_CONFIGS_PATH}", token)
        print(f"  media GET {UPLOAD_CONFIGS_PATH}: {media_status}")
    return 0


# ---------------------------------------------------------------------------
# callbacks, status, teardown
# ---------------------------------------------------------------------------


def command_callbacks(args, session, name: str) -> int:
    cognito_record = require(read_cognito_record(name), name)
    cognito = session.client("cognito-idp")
    client = cognito.describe_user_pool_client(
        UserPoolId=cognito_record["UserPoolId"],
        ClientId=cognito_record[CLIENT_RECORD_KEYS[args.client]],
    )["UserPoolClient"]
    if args.reset_hume:
        if args.client != HUME_CLIENT:
            sys.exit("--reset-hume applies only to the hume client")
        set_callbacks(cognito, client, redirect_urls(hume_web_url()))
        return 0
    urls = client.get("CallbackURLs", [])
    if args.add and args.add not in urls:
        if not args.add.startswith("https://"):
            sys.exit("Cognito accepts only https redirect addresses here")
        urls = [*urls, args.add]
        cognito.update_user_pool_client(**client_update_input(client, urls))
        print(f"Added {args.add} to {CLIENT_NAMES[args.client]}")
    for url in urls:
        print(f"  {url}")
    return 0


def command_status(args, session, name: str) -> int:
    cognito_record = read_cognito_record(name)
    if not cognito_record:
        print(f"No Cognito record for {name}.")
    for key, value in cognito_record.items():
        print(f"{key} = {value}")
    return 0


def command_teardown(args, session, name: str) -> int:
    cognito_record = require(read_cognito_record(name), name)
    cognito = session.client("cognito-idp")
    sm = session.client("secretsmanager")
    pool_id = cognito_record["UserPoolId"]
    if domain := cognito_record.get("Domain"):
        try:
            cognito.delete_user_pool_domain(Domain=domain, UserPoolId=pool_id)
            print(f"Deleted domain {domain}")
        except cognito.exceptions.InvalidParameterException as exc:
            print(f"Domain {domain}: {exc.response['Error']['Message']}")
    try:
        cognito.delete_user_pool(UserPoolId=pool_id)
        print(f"Deleted user pool {pool_id}")
    except cognito.exceptions.ResourceNotFoundException:
        print(f"User pool {pool_id} was already gone")
    for user in USERS:
        try:
            sm.delete_secret(
                SecretId=secret_name(name, user), ForceDeleteWithoutRecovery=True
            )
            print(f"Deleted secret {secret_name(name, user)}")
        except sm.exceptions.ResourceNotFoundException:
            print(f"Secret {secret_name(name, user)} was already gone")
    delete_token_trigger(session, name)
    cognito_record_path(name).unlink()
    return 0


COMMANDS = {
    "up": command_up,
    "sync": command_sync,
    "env": command_env,
    "check": command_check,
    "callbacks": command_callbacks,
    "status": command_status,
    "teardown": command_teardown,
}


def main() -> int:
    args = parse_args()
    session = make_session(args, read_cognito_record(args.name))
    return COMMANDS[args.command](args, session, args.name)


if __name__ == "__main__":
    sys.exit(main())
