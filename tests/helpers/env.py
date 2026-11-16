"""Shared helpers that point deploy.py at temp files and a clean environment."""

import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import deploy

KEYS = [
    deploy.AMI_ID_VAR,
    deploy.CIDRS_VAR,
    deploy.PROFILE_VAR,
    deploy.LICENCE_VAR,
    deploy.BASE_DOMAIN_VAR,
    deploy.OLD_DOMAIN_VAR,
    deploy.ZONE_VAR,
    deploy.SPINE_CONFIG_DIR_VAR,
    deploy.AUTH_PROVIDER_VAR,
    *(setting.variable for setting in deploy.OIDC_SETTINGS),
    *(setting.variable for setting in deploy.NEO4J_SETTINGS),
]
ZONE_ID = "Z0123456789ABC"
# Oidc is the default sign-in, so every resolve_settings call needs these two.
OIDC_ENV = {
    "HUME_OIDC_ISSUER_URI": "https://idp.example.test/pool",
    "HUME_OIDC_CLIENT_ID": "client-1",
}
# The Neo4j EE stack and the truststore, which every resolve_settings call needs.
NEO4J_ENV = {
    deploy.EE_STACK_VAR: "neo4j-ee-1",
    deploy.TRUSTSTORE_ARN_VAR: (
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:ts-AbCdEf"
    ),
}
TRUSTSTORE = NEO4J_ENV[deploy.TRUSTSTORE_ARN_VAR]
BASE_ENV = {
    "HUME_AMI_ID": "ami-1",
    "ALLOWED_CIDRS": "203.0.113.1",
    "HUME_HOSTED_ZONE_ID": ZONE_ID,
    "HUME_BASE_DOMAIN": "example.com",
}
PARAMETER_FOR_VARIABLE = {s.variable: s.parameter for s in deploy.NEO4J_SETTINGS}


class TempEnvCase(unittest.TestCase):
    """Point deploy at temp files and clear the settings from the environment."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.env_file = self.dir / ".env"
        self.ami_file = self.dir / "ami-id.txt"
        patches = [
            mock.patch.object(deploy, "ENV_FILE", self.env_file),
            mock.patch.object(deploy, "AMI_ID_FILE", self.ami_file),
            mock.patch.object(deploy, "DEPLOY_DIR", self.dir / ".deploy"),
            mock.patch.dict(os.environ, clear=False),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        for key in KEYS:
            os.environ.pop(key, None)
        os.environ.update(OIDC_ENV)
        os.environ.update(NEO4J_ENV)

    def args(self, **values) -> argparse.Namespace:
        base = {
            "ami_id": None,
            "allowed_cidr": None,
            "profile": None,
            "region": None,
            "instance_type": "m6i.xlarge",
            "base_domain": None,
            "hosted_zone_id": None,
            "db_snapshot_identifier": None,
            "existing_file_system_id": None,
            "existing_data_key_arn": None,
        }
        return argparse.Namespace(**{**base, **values})


class Neo4jCase(TempEnvCase):
    def setUp(self) -> None:
        super().setUp()
        for name in [*NEO4J_ENV, deploy.CA_FILE_VAR]:
            os.environ.pop(name, None)

    def write_env(self, **overrides) -> None:
        values = {**BASE_ENV, **NEO4J_ENV, **overrides}
        self.env_file.write_text(
            "".join(f"{name}={value}\n" for name, value in values.items())
        )
