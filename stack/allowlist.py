#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3[crt]", "python-dotenv"]
# ///
"""List, add, or remove the addresses allowed to reach the load balancer.

The deployed stack is the source of truth. An update changes one AllowedCidr
slot parameter and keeps every other parameter and the stored template, so only
the load balancer security group rules can change. The script builds a change
set first and refuses to run it if anything else would change.

After a successful change the new list is written back to ALLOWED_CIDRS in the
repo-root .env, so a later `deploy.py up` does not undo it.
"""

import argparse
import sys
import time
from pathlib import Path

from botocore.exceptions import ClientError, WaiterError
from dotenv import set_key

import deploy

INGRESS_TYPE = "AWS::EC2::SecurityGroupIngress"
SLOT_KEYS = [f"AllowedCidr{n}" for n in range(1, deploy.MAX_CIDRS + 1)]
HINT = f"Find your address at {deploy.CHECKIP_URL}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", help="AWS profile (default: from the record)")
    parser.add_argument("--region", help="AWS region (default: from the record)")
    parser.add_argument("--stack-name", help="Default: the only record file")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="Show the addresses the stack allows")
    for name, text in [("add", "Allow an address"), ("remove", "Stop allowing one")]:
        command = sub.add_parser(name, help=text)
        command.add_argument(
            "address", help=f"IPv4 address or range, such as 198.51.100.0/24. {HINT}"
        )
    return parser.parse_args()


def stack_parameters(cfn, stack: str) -> dict[str, str]:
    description = cfn.describe_stacks(StackName=stack)["Stacks"][0]
    params = {p["ParameterKey"]: p["ParameterValue"] for p in description["Parameters"]}
    if SLOT_KEYS[0] not in params:
        sys.exit(
            f"Stack {stack} has no {SLOT_KEYS[0]} parameter. It was created from "
            "an older template and cannot be changed with this script."
        )
    return params


def current_slots(params: dict[str, str]) -> dict[str, str]:
    return {key: params.get(key, "") for key in SLOT_KEYS}


def allowed(slots: dict[str, str]) -> list[str]:
    return [value for value in slots.values() if value]


def plan_add(slots: dict[str, str], address: str) -> tuple[str, str] | None:
    """Return the slot to fill, or None when the address is already allowed."""
    if address in slots.values():
        return None
    for key, value in slots.items():
        if not value:
            return key, address
    sys.exit(f"All {deploy.MAX_CIDRS} slots are used. Remove an address first.")


def plan_remove(slots: dict[str, str], address: str) -> dict[str, str]:
    """Return the slot values to change. The last address cannot be removed.

    The template requires an address in the first slot. Removing the first
    slot's address moves the last filled slot's address into it and clears that
    slot in the same change set.
    """
    matches = [key for key, value in slots.items() if value == address]
    if not matches:
        sys.exit(f"{address} is not in the allowlist: {', '.join(allowed(slots))}")
    if len(allowed(slots)) == 1:
        sys.exit(f"Refusing to remove the last address. Add another first. {HINT}")
    removed = matches[0]
    if removed != SLOT_KEYS[0]:
        return {removed: ""}
    last = [key for key, value in slots.items() if value and key != removed][-1]
    return {removed: slots[last], last: ""}


def build_change_set(cfn, stack: str, params: dict[str, str], change: dict[str, str]):
    parameters = [
        {"ParameterKey": name, "ParameterValue": change[name]}
        if name in change
        else {"ParameterKey": name, "UsePreviousValue": True}
        for name in params
    ]
    name = f"allowlist-{int(time.time())}"
    try:
        cfn.create_change_set(
            StackName=stack,
            ChangeSetName=name,
            ChangeSetType="UPDATE",
            UsePreviousTemplate=True,
            Parameters=parameters,
            Capabilities=["CAPABILITY_IAM"],
        )
    except ClientError as exc:
        message = exc.response["Error"]["Message"]
        sys.exit(f"CloudFormation rejected the change: {message}")
    try:
        cfn.get_waiter("change_set_create_complete").wait(
            StackName=stack, ChangeSetName=name
        )
    except WaiterError:
        detail = cfn.describe_change_set(StackName=stack, ChangeSetName=name)
        cfn.delete_change_set(StackName=stack, ChangeSetName=name)
        sys.exit(f"Change set failed: {detail.get('StatusReason')}")
    return name


def check_only_rules_change(cfn, stack: str, change_set: str) -> None:
    """Print the plan. Delete the change set and exit if more than rules change."""
    changes = cfn.describe_change_set(StackName=stack, ChangeSetName=change_set)[
        "Changes"
    ]
    others = []
    for item in changes:
        resource = item["ResourceChange"]
        print(
            f"  {resource['Action']:7} {resource['LogicalResourceId']} "
            f"({resource['ResourceType']}, replacement: "
            f"{resource.get('Replacement', 'n/a')})"
        )
        if resource["ResourceType"] != INGRESS_TYPE:
            others.append(resource["LogicalResourceId"])
    if others:
        cfn.delete_change_set(StackName=stack, ChangeSetName=change_set)
        sys.exit(f"Refusing to run. These resources would also change: {others}")


def apply_change(cfn, stack: str, change_set: str) -> bool:
    cfn.execute_change_set(StackName=stack, ChangeSetName=change_set)
    try:
        cfn.get_waiter("stack_update_complete").wait(
            StackName=stack, WaiterConfig={"Delay": 10, "MaxAttempts": 60}
        )
    except WaiterError:
        print(f"Update of {stack} failed. Failed resources:", file=sys.stderr)
        deploy.print_failed_events(cfn, stack)
        return False
    return True


def write_back(stack: str, cidrs: list[str], env_file: Path = deploy.ENV_FILE) -> None:
    """Record the new list in the stack record and in .env, without printing it."""
    record = deploy.read_record(stack)
    record["AllowedCidrs"] = ",".join(cidrs)
    deploy.write_record(stack, record)
    if not env_file.exists():
        print(
            f"Warning: {env_file} does not exist, so {deploy.CIDRS_VAR} was not "
            "saved. A later deploy.py up would use its own list.",
            file=sys.stderr,
        )
        return
    set_key(env_file, deploy.CIDRS_VAR, ",".join(cidrs), quote_mode="never")
    print(f"Saved the new list to {deploy.CIDRS_VAR} in {env_file}")


def main() -> int:
    args = parse_args()
    stack = args.stack_name or deploy.newest_stack()
    record = deploy.read_record(stack)
    session = deploy.make_session(args, record)
    cfn = session.client("cloudformation")
    params = stack_parameters(cfn, stack)
    slots = current_slots(params)

    if args.command == "list":
        for key, value in slots.items():
            print(f"{key}: {value or '(empty)'}")
        print(HINT)
        return 0

    address = deploy.normalise_cidr(args.address)
    if args.command == "add":
        added = plan_add(slots, address)
        if added is None:
            print(f"{address} is already allowed. Nothing to do.")
            return 0
        change = dict([added])
    else:
        change = plan_remove(slots, address)

    print(f"{args.command} {address} in {', '.join(change)} of stack {stack}")
    change_set = build_change_set(cfn, stack, params, change)
    check_only_rules_change(cfn, stack, change_set)
    if not apply_change(cfn, stack, change_set):
        return 1

    slots.update(change)
    write_back(stack, allowed(slots))
    print(f"Allowed now: {', '.join(allowed(slots))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
