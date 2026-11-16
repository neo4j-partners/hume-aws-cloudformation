"""Tests for the parts of neo4j_env.py that run without AWS."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from dotenv import dotenv_values

import deploy
import neo4j_env

STACK = "test-ee-1"
PREFIX = f"/neo4j-ee/{STACK}/"
HOST = "neo4j-oidc.neo4j.local"
KMS_KEY = "arn:aws:kms:us-east-1:123456789012:key/1234abcd-12ab-34cd-56ef-1234567890ab"
NLB = "test-ee-1-nlb-0123.elb.us-east-1.amazonaws.com"
ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:neo4j/app-AbC123"
CERT = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
PARAMS = {
    "advertised-dns": HOST,
    "vpc-id": "vpc-0123456789abcdef0",
    "external-sg-id": "sg-0123456789abcdef0",
    "vpc-endpoint-sg-id": "sg-0123456789abcdef1",
    "password-secret-arn": "arn:aws:secretsmanager:us-east-1:123456789012:"
    "secret:neo4j/pw-XyZ789",
    "app-username": "neo4j_app",
    "app-password-secret-arn": ARN,
    "kms-key-arn": KMS_KEY,
    "private-subnet-1-id": "subnet-0123456789abcdef1",
    "private-subnet-2-id": "subnet-0123456789abcdef2",
    "public-subnet-1-id": "subnet-000000000000000a1",
    "public-subnet-2-id": "subnet-000000000000000a2",
    "public-subnet-3-id": "subnet-000000000000000a3",
    "ca-pem": CERT,
    "nlb-dns": NLB,
}


class ReadSsmTest(unittest.TestCase):
    def test_names_lose_the_stack_prefix_across_pages(self) -> None:
        ssm = MagicMock()
        ssm.get_paginator.return_value.paginate.return_value = [
            {"Parameters": [{"Name": f"{PREFIX}vpc-id", "Value": "vpc-1"}]},
            {"Parameters": [{"Name": f"{PREFIX}nlb-dns", "Value": NLB}]},
        ]
        self.assertEqual(
            neo4j_env.read_ssm(ssm, STACK), {"vpc-id": "vpc-1", "nlb-dns": NLB}
        )
        ssm.get_paginator.return_value.paginate.assert_called_once_with(Path=PREFIX)

    def test_no_parameters_exits_naming_the_stack(self) -> None:
        ssm = MagicMock()
        ssm.get_paginator.return_value.paginate.return_value = [{"Parameters": []}]
        with self.assertRaises(SystemExit) as caught:
            neo4j_env.read_ssm(ssm, STACK)
        self.assertEqual(
            str(caught.exception),
            f"No Neo4j EE stack named {STACK} in this account and Region. "
            "Deploy Neo4j EE from AWS Marketplace first.",
        )


class SsmSettingsTest(unittest.TestCase):
    def test_only_the_stack_name_is_written(self) -> None:
        self.assertEqual(
            neo4j_env.ssm_settings(STACK, PARAMS), {deploy.EE_STACK_VAR: STACK}
        )

    def test_every_value_the_template_reads_is_required(self) -> None:
        for name in [*deploy.NEO4J_LOOKUP_NAMES, "ca-pem"]:
            params = {k: v for k, v in PARAMS.items() if k != name}
            with self.subTest(name=name), self.assertRaises(SystemExit) as caught:
                neo4j_env.ssm_settings(STACK, params)
            self.assertIn(name, str(caught.exception))
            self.assertIn("three servers", str(caught.exception))


class ValuesTest(unittest.TestCase):
    def test_good_values_pass(self) -> None:
        neo4j_env.check_values(neo4j_env.ssm_settings(STACK, PARAMS))

    def test_bad_value_exits(self) -> None:
        values = {deploy.EE_STACK_VAR: "neo4j_ee"}
        with self.assertRaises(SystemExit):
            neo4j_env.check_values(values)


class CaTest(unittest.TestCase):
    def test_private_key_exits(self) -> None:
        with self.assertRaises(SystemExit):
            neo4j_env.check_ca(CERT + "-----BEGIN PRIVATE KEY-----")

    def test_no_certificate_exits(self) -> None:
        with self.assertRaises(SystemExit):
            neo4j_env.check_ca("not a certificate")

    def test_each_certificate_of_the_chain_is_shown(self) -> None:
        shown = MagicMock(stdout="subject=CN=ca")
        with patch.object(neo4j_env.subprocess, "run", return_value=shown) as run:
            neo4j_env.check_ca(CERT + CERT)
        self.assertEqual(run.call_count, 2)


class DomainTest(unittest.TestCase):
    def test_a_set_name_is_kept(self) -> None:
        values = {deploy.BASE_DOMAIN_VAR: "ee.example.com"}
        with patch.object(deploy, "env_setting", values.get):
            self.assertEqual(deploy.resolve_base_domain(), "ee.example.com")

    def test_a_missing_name_exits_naming_the_setting(self) -> None:
        with patch.object(deploy, "env_setting", {}.get):
            with self.assertRaises(SystemExit) as raised:
                deploy.resolve_base_domain()
        self.assertIn(deploy.BASE_DOMAIN_VAR, str(raised.exception))


class WriteEnvTest(unittest.TestCase):
    def test_existing_lines_are_replaced_and_missing_ones_appended(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "HUME_LICENCE_KEY=keep\n"
                "HUME_NEO4J_STACK_NAME=ee-old\n"
                "HUME_BASE_DOMAIN=keep.example\n"
            )
            neo4j_env.write_env(
                {
                    "HUME_NEO4J_STACK_NAME": "ee-new",
                    "HUME_NEO4J_CA_FILE": "stack/.deploy/neo4j-ca.pem",
                },
                env_file,
            )
            lines = env_file.read_text().splitlines()
            values = dotenv_values(env_file)
        self.assertEqual(lines[1], "HUME_NEO4J_STACK_NAME=ee-new")
        self.assertEqual(lines[-1], "HUME_NEO4J_CA_FILE=stack/.deploy/neo4j-ca.pem")
        self.assertEqual(len(lines), 4)
        self.assertEqual(values["HUME_LICENCE_KEY"], "keep")
        self.assertEqual(values["HUME_BASE_DOMAIN"], "keep.example")

    def test_missing_file_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit):
            neo4j_env.write_env({"A": "b"}, Path(tmp) / ".env")


if __name__ == "__main__":
    unittest.main()
