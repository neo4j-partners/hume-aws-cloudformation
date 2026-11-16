#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3[crt]", "python-dotenv"]
# ///
"""Set up the Spine demo on a running Hume stack.

It runs the steps of docs/guides/spine-demo.md in the order that gives the
configuration import a database to import into. The template runs the import
only when SpineConfigSecretArn is set, so the ARN is set after the load:

    1. store the Spine config in the secret hume/<stack>/spine-config
    2. copy the backup into the seeds directory of one Neo4j node
       (seed_nodes.py copy)
    3. load the spine database from that node and create its roles
       (seed_nodes.py load)
    4. update the stack with a change set: SpineConfigSecretArn, which adds the
       configuration import and runs it with the spine database in place, and
       OidcRolesMappings, so that the Spine groups of the provider grant the
       Hume roles SPINE_ANALYST and SPINE_OFFICER that see the SPINE graph
    5. check the counts of the loaded database
    6. sign in as spine-analyst through the Cognito test pool and check that
       Hume lists the role SPINE_ANALYST (skipped on a stack that uses another
       provider)

--neo4j-stack and --instance-id name the Neo4j node. seed_nodes.py discover
lists the nodes. The load runs cypher-shell on that node over SSM as the neo4j
superuser. The node reads the password from the secret that the Neo4j stack
names in /neo4j-ee/<neo4j stack>/password-secret-arn, so the password is never
in the command text. Hume itself signs in as the app user, never as neo4j.

The first output is the line "Confirm setting up demo in stack <stack>". The
script then checks the stack, the node and the backup address, and prints the
plan. Apart from staging the public backup in the template bucket, nothing is
changed until you answer yes, or pass --yes. By default the backup address is a
copy staged in the template bucket of this region, presigned for one hour.
--url overrides it. Settings come from the same places as deploy.py: the
process environment, then the repo-root .env (HUME_SPINE_CONFIG_DIR), and the
profile and region of the stack record.
A step that is already done is skipped, so the script can run again after a
failure. It deletes nothing. It never prints a secret value.
"""

import argparse
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.exceptions import ClientError, ProfileNotFound, WaiterError

# The repository modules imported below live in these directories.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_IMPORT_DIRS = [str(_REPO_ROOT / name) for name in ("stack", "verify")]
sys.path[:0] = [entry for entry in _IMPORT_DIRS if entry not in sys.path]

import cognito
import deploy
import seed_nodes
from seed_nodes import StepFailed, check_sso, confirm, error_code

EXPECTED_NODES = 2_596_708
EXPECTED_PERSONS = 193_038
EXPECTED_RELATIONSHIPS = 1_552_236

STACK_UPDATABLE = ("CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE")
ARN_PARAMETER = "SpineConfigSecretArn"
# The configuration import exists only while SpineConfigSecretArn is set, so
# the first set adds it. A change of the ARN modifies it.
# The admin API key secret also exists only with the import.
ADD_IMPORT_CHANGES = {
    ("Add", "ApiKeySecret"),
    ("Add", "ConfigCliLogGroup"),
    ("Add", "ConfigCliTaskDefinition"),
    ("Add", "ConfigCliRun"),
}
IMPORT_CHANGES = {
    ("Modify", "ConfigCliTaskDefinition"),
    ("Modify", "ConfigCliRun"),
}
AUTH_PARAMETER = "AuthProvider"
ISSUER_PARAMETER = "OidcIssuerUri"
ROLES_PARAMETER = "OidcRolesMappings"
# The Hume roles that the imported SPINE knowledge graph grants access to, and
# the group of the Cognito test pool that grants each one. A pair is added only
# for a role that no pair of the stack grants yet, so a mapping from another
# provider is kept.
SPINE_ROLES = {
    "SPINE_ANALYST": cognito.SPINE_ANALYST_GROUP,
    "SPINE_OFFICER": cognito.SPINE_OFFICER_GROUP,
}
# The api and media tasks read OidcRolesMappings, so a change of it replaces
# their task definitions and updates their services.
ROLES_CHANGES = {
    ("Modify", "ApiTaskDefinition"),
    ("Modify", "ApiService"),
    ("Modify", "MediaTaskDefinition"),
    ("Modify", "MediaService"),
}
# The test user that step 6 signs in as, and the role it must get.
DEMO_USER = cognito.SPINE_ANALYST_USER
DEMO_ROLE = "SPINE_ANALYST"


@dataclass
class Plan:
    """What the stack and the node look like now, read before anything changes."""

    stack: str
    record: dict[str, str]
    session: boto3.Session
    account: str
    status: str
    parameters: dict[str, str]
    secret_name: str
    secret_arn: str | None
    password_arn: str
    neo4j_stack: str
    instance: str
    instance_name: str
    url: str
    ssm: object
    server: str
    seeds_dir: str
    want: int
    copied: bool
    # The OidcRolesMappings value to set, or None when the stack signs users in
    # natively or every Spine role is granted already.
    roles_mappings: str | None

    @property
    def import_enabled(self) -> bool:
        return bool(self.secret_arn) and (
            self.parameters.get(ARN_PARAMETER) == self.secret_arn
        )

    @property
    def oidc(self) -> bool:
        return self.parameters.get(AUTH_PARAMETER, deploy.OIDC_PROVIDER) == (
            deploy.OIDC_PROVIDER
        )


def spine_roles_mappings(current: str) -> str | None:
    """The mappings with a pair for each Spine role that none grants yet.

    None when every Spine role is granted, so nothing has to change.
    """
    pairs = [pair for pair in current.split(",") if pair]
    granted = {pair.split("=", 1)[-1] for pair in pairs}
    missing = [
        f"{group}={role}" for role, group in SPINE_ROLES.items() if role not in granted
    ]
    if not missing:
        return None
    return ",".join([*pairs, *missing])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("stack_name", nargs="?", help="Default: the only record file")
    parser.add_argument(
        "--profile",
        help="AWS profile (default: the one in the record, then "
        f"{deploy.DEFAULT_PROFILE})",
    )
    parser.add_argument(
        "--region",
        help=f"AWS region (default: the record, then {deploy.DEFAULT_REGION})",
    )
    parser.add_argument(
        "--neo4j-stack", required=True, help="Name of the Neo4j EE stack"
    )
    parser.add_argument(
        "--instance-id",
        required=True,
        help="The Neo4j node that holds the backup, from seed_nodes.py discover",
    )
    parser.add_argument(
        "--url",
        help=(
            "https address of the backup. Default: a staged copy in the "
            "template bucket, presigned for one hour."
        ),
    )
    parser.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt"
    )
    args = parser.parse_args()
    if args.url is not None and not args.url.startswith("https://"):
        parser.error("--url must start with https://")
    return args


def find_secret_arn(sm, name: str) -> str | None:
    try:
        return sm.describe_secret(SecretId=name)["ARN"]
    except ClientError as error:
        if error_code(error) == "ResourceNotFoundException":
            return None
        raise


def read_plan(args: argparse.Namespace, stack: str) -> Plan:
    """Read the settings, the stack and the node.

    Nothing is written, apart from staging the backup.
    """
    record = deploy.read_record(stack)
    deploy.resolve_spine_config_dir()
    if not record.get("ClusterName"):
        sys.exit(f"{stack} has no ClusterName. Its create did not finish.")
    try:
        session = deploy.make_session(args, record)
    except ProfileNotFound as error:
        sys.exit(str(error))
    account = check_sso(session)
    url = args.url or seed_nodes.stage_backup(session)
    info = session.client("cloudformation").describe_stacks(StackName=stack)["Stacks"][
        0
    ]
    if info["StackStatus"] not in STACK_UPDATABLE:
        raise StepFailed(
            "stack",
            f"{stack} is {info['StackStatus']}. It must be one of "
            f"{', '.join(STACK_UPDATABLE)}.",
        )
    parameters = {
        p["ParameterKey"]: p.get("ParameterValue", "") for p in info["Parameters"]
    }
    password_arn = seed_nodes.superuser_secret_arn(session, args.neo4j_stack)
    if not re.fullmatch(deploy.SECRET_ARN, password_arn):
        raise StepFailed(
            "node",
            f"{seed_nodes.SUPERUSER_SECRET_PARAMETER.format(stack=args.neo4j_stack)} "
            "does not hold a full secret ARN.",
        )
    ssm, item, probe = seed_nodes.check_node(
        session, args.neo4j_stack, args.instance_id, url, "node"
    )
    server = seed_nodes.server_id(probe)
    if not server:
        raise StepFailed("node", f"The server id of {args.instance_id} was not found.")
    seeds_dir, want, copied = seed_nodes.plan_copy(probe, None)
    secret_name = deploy.spine_secret_name(stack)
    oidc = parameters.get(AUTH_PARAMETER, deploy.OIDC_PROVIDER) == deploy.OIDC_PROVIDER
    roles_mappings = (
        spine_roles_mappings(parameters.get(ROLES_PARAMETER, "")) if oidc else None
    )
    return Plan(
        stack=stack,
        record=record,
        session=session,
        account=account,
        status=info["StackStatus"],
        parameters=parameters,
        secret_name=secret_name,
        secret_arn=find_secret_arn(session.client("secretsmanager"), secret_name),
        password_arn=password_arn,
        neo4j_stack=args.neo4j_stack,
        instance=args.instance_id,
        instance_name=item["name"] or "-",
        url=url,
        ssm=ssm,
        server=server,
        seeds_dir=seeds_dir,
        want=want,
        copied=copied,
        roles_mappings=roles_mappings,
    )


def show_plan(plan: Plan) -> None:
    session = plan.session
    backup = f"{plan.seeds_dir}/{seed_nodes.BACKUP_NAME}"
    print(f"Profile:   {session.profile_name}")
    print(f"Region:    {session.region_name}")
    print(f"Account:   {plan.account}")
    print(f"Stack:     {plan.stack} ({plan.status})")
    print(f"Neo4j:     {plan.neo4j_stack}")
    print(f"Seed node: {plan.instance} ({plan.instance_name}), server {plan.server}")
    print("This will:")
    if plan.secret_arn:
        print(f"  1. Keep the config secret {plan.secret_name}, it exists")
    else:
        print(f"  1. Create the config secret {plan.secret_name}")
    if plan.copied:
        print(f"  2. Keep the backup {backup}, it is complete")
    else:
        print(
            f"  2. Download the backup to {backup} on the seed node, "
            f"{plan.want / 2**20:.0f} MiB. The other nodes copy it from there."
        )
    print(
        "  3. Create the spine database and its roles from the seed node, if "
        "they are missing. It takes about 3 minutes."
    )
    changed = []
    if not plan.import_enabled:
        changed.append(ARN_PARAMETER)
    if plan.roles_mappings:
        changed.append(ROLES_PARAMETER)
    if not changed:
        print(
            f"  4. Keep the stack as it is, {ARN_PARAMETER} is already set and "
            "the Spine roles are mapped"
        )
    else:
        print(f"  4. Update the stack with a change set: {', '.join(changed)}.")
        if ARN_PARAMETER in changed:
            print(
                "     The configuration import runs then, with the spine database "
                "in place."
            )
        if ROLES_PARAMETER in changed:
            print(
                f"     {ROLES_PARAMETER} becomes {plan.roles_mappings}, so the "
                "Spine groups grant the Hume roles that see the SPINE graph. The "
                "api and media services restart."
            )
        print("     The template is staged in the template bucket.")
    print(
        f"  5. Check the counts: {EXPECTED_NODES:,} nodes, "
        f"{EXPECTED_PERSONS:,} Person nodes, "
        f"{EXPECTED_RELATIONSHIPS:,} relationships"
    )
    print(
        f"  6. Sign in as {DEMO_USER} through the Cognito test pool and check "
        f"that Hume lists the role {DEMO_ROLE}, when the stack uses that pool"
    )
    print("Nothing is deleted. A failure stops the run. Run it again to continue.")


def store_config(plan: Plan) -> str:
    print("Step 1. Config secret")
    if plan.secret_arn:
        print(f"Skip: {plan.secret_name} exists.")
        return plan.secret_arn
    return deploy.put_spine_config(plan.session, plan.stack)


def copy_backup(plan: Plan) -> None:
    print("Step 2. Backup copy")
    if plan.copied:
        print(f"Skip: {plan.seeds_dir}/{seed_nodes.BACKUP_NAME} is complete.")
        return
    seed_nodes.run_copy(plan.ssm, plan.instance, plan.seeds_dir, plan.url, plan.want)
    print(f"Copied {plan.want} bytes.")


def load_data(plan: Plan) -> seed_nodes.SpineResult:
    print("Step 3. Database load")
    return seed_nodes.run_load(plan.ssm, plan.instance, plan.server, plan.password_arn)


def parameter_entries(current: dict[str, str], changed: dict[str, str]) -> list[dict]:
    """Keep every parameter of the stack, then set the ones in changed."""
    kept = [
        {"ParameterKey": key, "UsePreviousValue": True}
        for key in current
        if key not in changed
    ]
    return [
        *kept,
        *({"ParameterKey": k, "ParameterValue": v} for k, v in changed.items()),
    ]


def import_config(plan: Plan, arn: str) -> None:
    print("Step 4. Stack update, configuration import and Spine roles")
    changed: dict[str, str] = {}
    expected: set[tuple[str, str]] = set()
    if not plan.import_enabled:
        changed[ARN_PARAMETER] = arn
        expected |= ADD_IMPORT_CHANGES | IMPORT_CHANGES
    if plan.roles_mappings:
        changed[ROLES_PARAMETER] = plan.roles_mappings
        expected |= ROLES_CHANGES
    if not changed:
        print(
            f"Skip: {ARN_PARAMETER} is already set to this secret and the Spine "
            "roles are mapped."
        )
        return
    if not plan.oidc:
        print(
            "The stack signs users in natively, so only an administrator sees "
            "the SPINE graph."
        )
    update_stack(plan, changed, expected, "spine-import")


def update_stack(
    plan: Plan,
    changed: dict[str, str],
    expected: set[tuple[str, str]],
    prefix: str,
) -> None:
    stack = plan.stack
    cfn = plan.session.client("cloudformation")
    record = dict(plan.record)
    template = deploy.template_arguments(plan.session, stack, record)
    if record != plan.record:
        deploy.write_record(stack, record)
    name = f"{prefix}-{int(time.time())}"
    cfn.create_change_set(
        StackName=stack,
        ChangeSetName=name,
        ChangeSetType="UPDATE",
        Capabilities=["CAPABILITY_IAM"],
        Parameters=parameter_entries(plan.parameters, changed),
        **template,
    )
    print(f"Created change set {name}")
    try:
        cfn.get_waiter("change_set_create_complete").wait(
            StackName=stack,
            ChangeSetName=name,
            WaiterConfig={"Delay": 5, "MaxAttempts": 60},
        )
    except WaiterError as error:
        detail = cfn.describe_change_set(StackName=stack, ChangeSetName=name)
        raise StepFailed(
            "update",
            f"Change set {name} is {detail['Status']}: "
            f"{detail.get('StatusReason', error)}. It was not executed.",
        ) from error
    changes = {
        (c["ResourceChange"]["Action"], c["ResourceChange"]["LogicalResourceId"])
        for c in cfn.describe_change_set(StackName=stack, ChangeSetName=name)["Changes"]
    }
    for action, logical_id in sorted(changes):
        print(f"  {action} {logical_id}")
    unexpected = changes - expected
    if unexpected:
        listing = ", ".join(f"{a} {i}" for a, i in sorted(unexpected))
        raise StepFailed(
            "update",
            f"Change set {name} holds changes that this script does not expect: "
            f"{listing}. It was not executed.",
        )
    cfn.execute_change_set(StackName=stack, ChangeSetName=name)
    print(f"Executing {name}. This took about 5 minutes on the test run.")
    try:
        cfn.get_waiter("stack_update_complete").wait(
            StackName=stack, WaiterConfig={"Delay": 15, "MaxAttempts": 80}
        )
    except WaiterError as error:
        print(f"Stack {stack} failed to update. Failed resources:", file=sys.stderr)
        deploy.print_failed_events(cfn, stack)
        raise StepFailed(
            "update", f"The update of {stack} failed or did not finish."
        ) from error
    print(f"Updated {stack}.")


def check_counts(result: seed_nodes.SpineResult) -> None:
    print("Step 5. Counts")
    expected = seed_nodes.SpineResult(
        "online", EXPECTED_NODES, EXPECTED_PERSONS, EXPECTED_RELATIONSHIPS
    )
    differences = [
        f"{field}: got {got}, expected {want}"
        for field, got, want in zip(result._fields, result, expected, strict=True)
        if got != want
    ]
    if result.status != "online" or None in result:
        raise StepFailed(
            "counts",
            "The load passed but the database is not online or the counts are "
            "missing. " + "; ".join(differences),
        )
    if differences:
        print(
            "WARNING: the load passed but the counts differ from the local "
            "load. " + "; ".join(differences)
        )
        return
    print(
        f"PASS: {result.nodes:,} nodes, {result.persons:,} Person nodes, "
        f"{result.relationships:,} relationships"
    )


def check_sign_in(plan: Plan) -> None:
    """Sign in as the demo user through the Cognito test pool and check the roles.

    The check runs only on a stack that uses that pool. Hume grants the mapped
    roles after the first request of a new user, so a second request is made
    when the first lists no Spine role.
    """
    print(f"Step 6. Sign-in as {DEMO_USER}")
    hint = "Sign in as a user in a Spine group to see the SPINE graph."
    if not plan.oidc:
        print(f"Skip: the stack signs users in natively. {hint}")
        return
    name = cognito.DEFAULT_NAME
    record = cognito.read_cognito_record(name)
    if not record:
        print(f"Skip: no Cognito record for {name}. {hint}")
        return
    if record.get("Issuer") != plan.parameters.get(ISSUER_PARAMETER):
        print(f"Skip: the stack uses another provider than the pool {name}. {hint}")
        return
    token = cognito.sign_in(plan.session, name, record, DEMO_USER)["access"]
    api_host = deploy.hume_hosts(plan.record["BaseDomainName"]).api
    for _ in range(2):
        status, roles = cognito.hume_roles(api_host, token)
        if DEMO_ROLE in roles:
            break
    if status != 200:
        raise StepFailed(
            "sign-in",
            f"GET {cognito.USERS_ME_PATH} as {DEMO_USER} returned {status}.",
        )
    if DEMO_ROLE not in roles:
        raise StepFailed(
            "sign-in",
            f"{DEMO_USER} has the roles {', '.join(roles) or 'none'} and not "
            f"{DEMO_ROLE}. Check {ROLES_PARAMETER} on the stack and the groups "
            f"of {DEMO_USER} in the pool.",
        )
    print(f"PASS: {DEMO_USER} has the roles {', '.join(roles)}")


def run(args: argparse.Namespace, stack: str) -> int:
    plan = read_plan(args, stack)
    show_plan(plan)
    confirm(args.yes)
    arn = store_config(plan)
    copy_backup(plan)
    result = load_data(plan)
    import_config(plan, arn)
    check_counts(result)
    check_sign_in(plan)
    print(
        f"The demo is set up in stack {stack}. Sign in to Hume as a user in a "
        f"Spine group, such as {DEMO_USER}, to see the SPINE graph."
    )
    return 0


def main() -> int:
    args = parse_args()
    stack = args.stack_name or deploy.newest_stack()
    print(f"Confirm setting up demo in stack {stack}")
    try:
        return run(args, stack)
    except StepFailed as failed:
        print(f"FAIL at step {failed.step}: {failed}", file=sys.stderr)
        print(
            "Fix that, then run the same command again. It skips the steps "
            "that are done.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
