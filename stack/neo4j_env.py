#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3[crt]", "python-dotenv"]
# ///
"""Check a Neo4j EE stack, save its trust anchor and write .env for Hume.

This automates the step Read the Neo4j values in docs/guides/run-book.md:

    uv run stack/neo4j_env.py <neo4j stack name>            # write .env and PEM
    uv run stack/neo4j_env.py <neo4j stack name> --dry-run  # print only

It reads the SSM parameters under /neo4j-ee/<stack>/ and checks that the stack
publishes every value that the template reads. The trust anchor in ca-pem goes
into .deploy/neo4j-ca.pem. .env gets the Neo4j EE stack name, which is the one
Neo4j value the template takes. Each .env line that already exists is replaced
in place, and a missing one is appended. None of the values is a secret.

HUME_BASE_DOMAIN is not a Neo4j value. It is written so that .env shows the
name deploy.py will use. The script exits when no name is set.

The truststore secret is not built here. Run `deploy.py truststore` next.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

from dotenv import set_key

import deploy

CA_PARAMETER = "ca-pem"
CA_FILE = deploy.DEPLOY_DIR / "neo4j-ca.pem"
# The value written to .env, relative to the repository root.
CA_FILE_SETTING = "stack/.deploy/neo4j-ca.pem"
CA_FILE_VAR = "HUME_NEO4J_CA_FILE"
PATTERNS = {s.variable: s.pattern for s in deploy.NEO4J_SETTINGS}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("stack", help="name of the Neo4j EE stack")
    parser.add_argument("--profile", help="AWS profile")
    parser.add_argument("--region", help=f"AWS region, default {deploy.DEFAULT_REGION}")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the values and write neither .env nor the PEM file",
    )
    return parser.parse_args()


def read_ssm(ssm, stack: str) -> dict[str, str]:
    """The SSM parameters of the stack. Exit when no stack of that name exists."""
    params = deploy.read_neo4j_stack(ssm, stack)
    if not params:
        sys.exit(deploy.neo4j_stack_problem(stack, params))
    return params


def ssm_settings(stack: str, params: dict[str, str]) -> dict[str, str]:
    """Check that the stack publishes what Hume reads. Return the .env settings."""
    needed = [*deploy.NEO4J_LOOKUP_NAMES, CA_PARAMETER]
    if missing := [name for name in needed if name not in params]:
        sys.exit(
            f"Missing SSM parameters: {', '.join(missing)}. Hume needs a Neo4j EE "
            "Private stack with three servers that publishes the app user, the "
            "secrets key, the public subnets and the CA. See "
            "docs/reference/infrastructure-setup-details.md."
        )
    return {deploy.EE_STACK_VAR: stack}


def check_ca(pem: str) -> None:
    """Exit unless pem holds one or more certificates and no key. Print each."""
    if "PRIVATE KEY" in pem:
        sys.exit(f"The {CA_PARAMETER} parameter holds a private key")
    certificates = deploy.PEM_CERTIFICATE.findall(pem)
    if not certificates:
        sys.exit(f"The {CA_PARAMETER} parameter holds no certificate")
    for certificate in certificates:
        shown = subprocess.run(
            ["openssl", "x509", "-noout", "-subject", "-issuer", "-enddate"],
            input=certificate,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        print(shown.strip())


def check_values(values: dict[str, str]) -> None:
    """Exit when a value does not match the pattern that deploy.py checks."""
    bad = [
        var
        for var, value in values.items()
        if var in PATTERNS and not re.fullmatch(PATTERNS[var], value)
    ]
    if bad:
        sys.exit(f"Unexpected values for {', '.join(bad)}")


def write_env(values: dict[str, str], env_file: Path = deploy.ENV_FILE) -> None:
    """Replace each line in place, or append it when missing."""
    if not env_file.exists():
        sys.exit(f"{env_file} does not exist. Copy .env.example to it first.")
    for var, value in values.items():
        set_key(env_file, var, value, quote_mode="never")
    print(f"Saved {len(values)} settings to {env_file}")


def main() -> int:
    args = parse_args()
    session = deploy.cli_session(args)
    params = read_ssm(session.client("ssm"), args.stack)
    values = ssm_settings(args.stack, params)
    pem = params[CA_PARAMETER]
    check_ca(pem)
    values[CA_FILE_VAR] = CA_FILE_SETTING
    base_domain = deploy.resolve_base_domain()
    values[deploy.BASE_DOMAIN_VAR] = base_domain
    check_values(values)
    for var, value in values.items():
        print(f"{var}={value}")
    if args.dry_run:
        print("Dry run. Nothing written.")
        return 0
    deploy.DEPLOY_DIR.mkdir(exist_ok=True)
    CA_FILE.write_text(pem if pem.endswith("\n") else pem + "\n")
    print(f"Saved the CA to {CA_FILE}")
    write_env(values)
    print(
        f"{deploy.hume_hosts(base_domain).web} must match a hume-web redirect "
        "address. "
        "List them with: uv run verify/cognito.py callbacks"
    )
    print("Next: cd stack && ./deploy.py truststore --ca-file .deploy/neo4j-ca.pem")
    return 0


if __name__ == "__main__":
    sys.exit(main())
