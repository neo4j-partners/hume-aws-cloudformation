"""Tests for the settings logic. Every .env is a temp file."""

import argparse
import contextlib
import io
import os
import re
import shlex
import time
import unittest
from typing import ClassVar
from unittest import mock

from botocore.exceptions import ClientError, WaiterError

import deploy
from tests.helpers.env import OIDC_ENV, ZONE_ID, TempEnvCase


class SettingsTest(TempEnvCase):
    def test_precedence_flag_then_environment_then_dotenv(self) -> None:
        self.env_file.write_text("HUME_AMI_ID=ami-dotenv\n")
        self.assertEqual(deploy.resolve_ami_id(self.args()), "ami-dotenv")
        os.environ["HUME_AMI_ID"] = "ami-environ"
        self.assertEqual(deploy.resolve_ami_id(self.args()), "ami-environ")
        flag = self.args(ami_id="ami-flag")
        self.assertEqual(deploy.resolve_ami_id(flag), "ami-flag")

    def test_ami_falls_back_to_file_then_exits_naming_both(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            deploy.resolve_ami_id(self.args())
        self.assertIn("HUME_AMI_ID", str(raised.exception))
        self.assertIn(str(self.ami_file), str(raised.exception))
        self.ami_file.write_text("ami-file\n")
        self.assertEqual(deploy.resolve_ami_id(self.args()), "ami-file")

    def test_settings_come_from_dotenv_alone(self) -> None:
        self.env_file.write_text(
            "HUME_AMI_ID=ami-1\nALLOWED_CIDRS=203.0.113.1,203.0.113.2/32\n"
            f"HUME_HOSTED_ZONE_ID={ZONE_ID}\n"
            "HUME_BASE_DOMAIN=dotenv.example.com\n"
        )
        settings = deploy.resolve_settings(self.args())
        self.assertEqual(settings.cidrs, ["203.0.113.1/32", "203.0.113.2/32"])
        self.assertEqual(settings.profile, deploy.DEFAULT_PROFILE)
        self.assertEqual(settings.base_domain, "dotenv.example.com")
        self.assertEqual(settings.hosted_zone_id, ZONE_ID)

    def test_missing_base_domain_exits_naming_the_flag_and_setting(self) -> None:
        self.env_file.write_text(
            "HUME_AMI_ID=ami-1\nALLOWED_CIDRS=203.0.113.1\n"
            f"HUME_HOSTED_ZONE_ID={ZONE_ID}\n"
        )
        with self.assertRaises(SystemExit) as raised:
            deploy.resolve_settings(self.args())
        self.assertIn("--base-domain", str(raised.exception))
        self.assertIn(deploy.BASE_DOMAIN_VAR, str(raised.exception))

    def test_missing_addresses_exit_with_hint(self) -> None:
        self.env_file.write_text(f"HUME_AMI_ID=ami-1\nHUME_HOSTED_ZONE_ID={ZONE_ID}\n")
        with self.assertRaises(SystemExit) as raised:
            deploy.resolve_settings(self.args())
        self.assertIn("checkip.amazonaws.com", str(raised.exception))

    def test_base_and_zone_follow_flag_then_environment_then_dotenv(self) -> None:
        self.env_file.write_text(
            "HUME_AMI_ID=ami-1\nALLOWED_CIDRS=203.0.113.1\n"
            f"HUME_HOSTED_ZONE_ID={ZONE_ID}\n"
            "HUME_BASE_DOMAIN=From.Dotenv.Example.COM.\n"
        )
        settings = deploy.resolve_settings(self.args())
        self.assertEqual(settings.base_domain, "from.dotenv.example.com")
        os.environ["HUME_BASE_DOMAIN"] = "environ.example.com"
        self.assertEqual(
            deploy.resolve_settings(self.args()).base_domain, "environ.example.com"
        )
        flags = self.args(base_domain="flag.example.com", hosted_zone_id="ZFLAG12345")
        settings = deploy.resolve_settings(flags)
        self.assertEqual(settings.base_domain, "flag.example.com")
        self.assertEqual(settings.hosted_zone_id, "ZFLAG12345")

    def test_the_old_domain_setting_exits_and_names_the_new_one(self) -> None:
        self.env_file.write_text(
            "HUME_AMI_ID=ami-1\nALLOWED_CIDRS=203.0.113.1\n"
            f"HUME_HOSTED_ZONE_ID={ZONE_ID}\n"
            "HUME_DOMAIN_NAME=hume.example.com\n"
        )
        with self.assertRaises(SystemExit) as raised:
            deploy.resolve_settings(self.args(base_domain="flag.example.com"))
        self.assertIn(deploy.OLD_DOMAIN_VAR, str(raised.exception))
        self.assertIn(deploy.BASE_DOMAIN_VAR, str(raised.exception))

    def test_missing_or_malformed_zone_exits(self) -> None:
        self.env_file.write_text("HUME_AMI_ID=ami-1\nALLOWED_CIDRS=203.0.113.1\n")
        with self.assertRaises(SystemExit) as raised:
            deploy.resolve_settings(self.args())
        self.assertIn("HUME_HOSTED_ZONE_ID", str(raised.exception))
        with self.assertRaises(SystemExit) as raised:
            deploy.resolve_settings(self.args(hosted_zone_id="/hostedzone/Z0123456789"))
        self.assertIn("/hostedzone/", str(raised.exception))


class NoSuchHostedZone(Exception):
    pass


def route53_session(zone: dict) -> mock.MagicMock:
    route53 = mock.MagicMock()
    route53.exceptions.NoSuchHostedZone = NoSuchHostedZone
    route53.get_hosted_zone.return_value = {"HostedZone": zone}
    session = mock.MagicMock()
    session.client.return_value = route53
    return session


class DomainTest(TempEnvCase):
    ZONE: ClassVar[dict] = {"Name": "example.com.", "Config": {"PrivateZone": False}}

    def settings(self, domain: str) -> deploy.Settings:
        return deploy.Settings(
            "marketplace", "us-east-1", "ami-1", ["203.0.113.1/32"], "m6i.xlarge",
            domain, ZONE_ID,
        )  # fmt: skip

    def test_base_must_be_the_zone_or_sit_below_it(self) -> None:
        self.assertTrue(deploy.domain_in_zone("example.com", "example.com."))
        self.assertTrue(deploy.domain_in_zone("team.example.com", "example.com."))
        self.assertTrue(deploy.domain_in_zone("a.b.example.com", "example.com"))
        self.assertFalse(deploy.domain_in_zone("badexample.com", "example.com."))
        self.assertFalse(deploy.domain_in_zone("hume.example.org", "example.com."))

    def test_hosts_sit_one_label_below_the_base(self) -> None:
        self.assertEqual(
            deploy.hume_hosts("example.com"),
            ("hume.example.com", "hume-api.example.com", "hume-media.example.com"),
        )

    def test_check_domain_returns_the_zone_name(self) -> None:
        for base in ["example.com", "team.example.com"]:
            with self.subTest(base=base):
                session = route53_session(self.ZONE)
                name = deploy.check_domain(session, self.settings(base))
                self.assertEqual(name, "example.com")

    def test_check_domain_rejects_an_outside_name_and_a_private_zone(self) -> None:
        with self.assertRaises(SystemExit):
            deploy.check_domain(
                route53_session(self.ZONE), self.settings("hume.example.org")
            )
        private = {"Name": "example.com.", "Config": {"PrivateZone": True}}
        session = route53_session(private)
        with self.assertRaises(SystemExit) as raised:
            deploy.check_domain(session, self.settings("a.example.com"))
        self.assertIn("private", str(raised.exception))

    def test_check_domain_reports_a_missing_zone(self) -> None:
        session = route53_session(self.ZONE)
        session.client.return_value.get_hosted_zone.side_effect = NoSuchHostedZone()
        with self.assertRaises(SystemExit) as raised:
            deploy.check_domain(session, self.settings("hume.example.com"))
        self.assertIn(ZONE_ID, str(raised.exception))


class NewestStackTest(TempEnvCase):
    def make_record(self, stack: str, age_seconds: float = 0) -> None:
        deploy.write_record(stack, {"StackName": stack})
        stamp = time.time() - age_seconds
        os.utime(deploy.record_path(stack), (stamp, stamp))

    def test_no_records_or_an_empty_deploy_dir_exits(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            deploy.newest_stack()
        self.assertIn("No record files", str(raised.exception))

        (self.dir / ".deploy").mkdir()
        with self.assertRaises(SystemExit) as raised:
            deploy.newest_stack()
        self.assertIn("No record files", str(raised.exception))

    def test_one_record_is_used(self) -> None:
        self.make_record("hume-ecs-1", age_seconds=7200)
        self.assertEqual(deploy.newest_stack(), "hume-ecs-1")

    def test_several_records_exit_and_list_each_with_its_age(self) -> None:
        self.make_record("hume-ecs-old", age_seconds=3 * 86400 + 5 * 3600)
        self.make_record("hume-ecs-mid", age_seconds=2 * 3600 + 120)
        self.make_record("hume-ecs-new", age_seconds=90)
        with self.assertRaises(SystemExit) as raised:
            deploy.newest_stack()
        message = str(raised.exception)
        self.assertIn("3 record files", message)
        self.assertIn("Pass the stack name", message)
        self.assertIn("hume-ecs-old  (record 3 d 5 h old)", message)
        self.assertIn("hume-ecs-mid  (record 2 h 2 min old)", message)
        self.assertIn("hume-ecs-new  (record 1 min old)", message)
        self.assertLess(message.index("hume-ecs-new"), message.index("hume-ecs-old"))


class TeardownRecordTest(TempEnvCase):
    BASE = "example.com"
    NAME = "_abc123.hume.example.com."
    VALUE = "_def456.xyz.acm-validations.aws."
    PAIRS: ClassVar[list[tuple[str, str]]] = [
        (NAME, VALUE),
        ("_abc124.hume-api.example.com.", "_def457.xyz.acm-validations.aws."),
        ("_abc125.hume-media.example.com.", "_def458.xyz.acm-validations.aws."),
    ]

    def record_set(self, **changes) -> dict:
        base = {
            "Name": self.NAME,
            "Type": "CNAME",
            "TTL": 300,
            "ResourceRecords": [{"Value": self.VALUE}],
        }
        return {**base, **changes}

    def record_sets(self) -> list[dict]:
        return [
            self.record_set(Name=name, ResourceRecords=[{"Value": value}])
            for name, value in self.PAIRS
        ]

    def record(self, **extra) -> dict[str, str]:
        base = {
            "StackName": "stack-c",
            "HostedZoneId": ZONE_ID,
            "BaseDomainName": self.BASE,
            "CertificateArn": "arn:cert",
            deploy.VALIDATION_NAMES_KEY: ",".join(n for n, _ in self.PAIRS),
            deploy.VALIDATION_VALUES_KEY: ",".join(v for _, v in self.PAIRS),
        }
        return {**base, **extra}

    def test_only_acm_records_one_label_below_a_host_are_safe(self) -> None:
        safe = deploy.is_acm_validation_record
        hosts = deploy.hume_hosts(self.BASE)
        for name, value in self.PAIRS:
            self.assertTrue(safe(name, value, hosts), name)
        self.assertFalse(safe("hume.example.com.", self.VALUE, hosts))
        self.assertFalse(safe(self.NAME, "target.example.net.", hosts))

    def test_a_base_at_the_zone_apex_leaves_other_sites_alone(self) -> None:
        safe = deploy.is_acm_validation_record
        hosts = deploy.hume_hosts(self.BASE)
        for other in (
            "_x.other.example.com.",
            "_x.example.com.",
            "_x.wooz.hume.example.com.",
            "_x.hume.example.com.evil.net.",
        ):
            self.assertFalse(safe(other, self.VALUE, hosts), other)

    def test_find_matches_name_type_and_value_exactly(self) -> None:
        wanted = self.record_set()
        others = [
            self.record_set(Type="A"),
            self.record_set(Name="_zzz.hume.example.com."),
            self.record_set(ResourceRecords=[{"Value": "_other.acm-validations.aws."}]),
        ]
        found = deploy.find_validation_record(others + [wanted], self.NAME, self.VALUE)
        self.assertIs(found, wanted)
        self.assertIsNone(deploy.find_validation_record(others, self.NAME, self.VALUE))

    def delete_with(self, record_sets: list[dict], record: dict) -> mock.MagicMock:
        route53 = mock.MagicMock()
        route53.list_resource_record_sets.return_value = {
            "ResourceRecordSets": record_sets
        }
        session = mock.MagicMock()
        session.client.return_value = route53
        with contextlib.redirect_stdout(io.StringIO()):
            deploy.delete_validation_records(session, record)
        return route53

    def test_delete_sends_each_exact_record_set(self) -> None:
        record_sets = self.record_sets()
        route53 = self.delete_with(record_sets, self.record())
        calls = route53.change_resource_record_sets.call_args_list
        self.assertEqual(len(calls), 3)
        for call, wanted in zip(calls, record_sets, strict=True):
            self.assertEqual(call.kwargs["HostedZoneId"], ZONE_ID)
            self.assertEqual(
                call.kwargs["ChangeBatch"],
                {"Changes": [{"Action": "DELETE", "ResourceRecordSet": wanted}]},
            )

    def test_delete_does_nothing_when_the_records_are_gone_or_unsafe(self) -> None:
        gone = self.delete_with([], self.record())
        gone.change_resource_record_sets.assert_not_called()
        unsafe = self.record(
            **{
                deploy.VALIDATION_NAMES_KEY: "hume.example.com.",
                deploy.VALIDATION_VALUES_KEY: self.VALUE,
            }
        )
        route53 = self.delete_with([self.record_set()], unsafe)
        route53.list_resource_record_sets.assert_not_called()
        route53.change_resource_record_sets.assert_not_called()

    def test_an_unsafe_record_does_not_stop_the_others(self) -> None:
        names = f"_x.other.example.com.,{self.NAME}"
        values = f"{self.VALUE},{self.VALUE}"
        record = self.record(
            **{deploy.VALIDATION_NAMES_KEY: names, deploy.VALIDATION_VALUES_KEY: values}
        )
        route53 = self.delete_with([self.record_set()], record)
        route53.change_resource_record_sets.assert_called_once()

    def test_delete_does_nothing_for_a_record_with_no_validation_data(self) -> None:
        bare = {"StackName": "stack-c", "HostedZoneId": ZONE_ID}
        route53 = self.delete_with([self.record_set()], bare)
        route53.change_resource_record_sets.assert_not_called()

    def test_capture_saves_every_record_before_the_certificate_goes(self) -> None:
        acm = mock.MagicMock()
        acm.describe_certificate.return_value = {
            "Certificate": {
                "DomainValidationOptions": [
                    {"ResourceRecord": {"Name": n, "Type": "CNAME", "Value": v}}
                    for n, v in self.PAIRS
                ]
            }
        }
        session = mock.MagicMock()
        session.client.return_value = acm
        record = {"StackName": "stack-c", "CertificateArn": "arn:cert"}
        deploy.write_record("stack-c", record)
        deploy.capture_validation_records(session, record)
        saved = deploy.read_record("stack-c")
        self.assertEqual(
            saved[deploy.VALIDATION_NAMES_KEY], ",".join(n for n, _ in self.PAIRS)
        )
        self.assertEqual(
            saved[deploy.VALIDATION_VALUES_KEY], ",".join(v for _, v in self.PAIRS)
        )

    def test_capture_skips_a_stack_without_a_certificate(self) -> None:
        session = mock.MagicMock()
        deploy.capture_validation_records(session, {"StackName": "stack-d"})
        session.client.assert_not_called()

    def test_run_task_log_group_is_cleaned_up(self) -> None:
        names = deploy.leftover_log_group_names("stack-e")
        self.assertIn("/aws/lambda/stack-e-run-task", names)
        self.assertIn("/hume/stack-e/", names)


class DeployParametersTest(TempEnvCase):
    def test_create_stack_passes_the_licence_domain_and_zone(self) -> None:
        sm = mock.MagicMock()
        sm.describe_secret.side_effect = lambda SecretId: {"ARN": f"arn:{SecretId}"}
        cfn = mock.MagicMock()
        cfn.create_stack.return_value = {"StackId": "id-1"}
        cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}
        clients = {"secretsmanager": sm, "cloudformation": cfn}
        session = mock.MagicMock()
        session.client.side_effect = clients.__getitem__
        settings = deploy.Settings(
            "marketplace", "us-east-1", "ami-1", ["203.0.113.1/32"], "m6i.xlarge",
            "example.com", ZONE_ID,
        )  # fmt: skip
        small = self.dir / "small.yaml"
        small.write_text("Resources: {}\n")
        with (
            mock.patch.object(deploy, "TEMPLATE", small),
            mock.patch.object(deploy.boto3, "Session", return_value=session),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(deploy.deploy_stack(settings, "stack-f"), 0)
        sent = {
            p["ParameterKey"]: p["ParameterValue"]
            for p in cfn.create_stack.call_args.kwargs["Parameters"]
        }
        self.assertNotIn("HumeKeystoreSecretArn", sent)
        self.assertEqual(sent["HumeLicenceSecretArn"], "arn:hume/licence-key")
        self.assertEqual(sent["BaseDomainName"], "example.com")
        self.assertEqual(sent["HostedZoneId"], ZONE_ID)
        record = deploy.read_record("stack-f")
        self.assertEqual(record["BaseDomainName"], "example.com")
        self.assertEqual(record["HostedZoneId"], ZONE_ID)


class AuthSettingsTest(TempEnvCase):
    def settings_args(self) -> argparse.Namespace:
        return self.args(
            ami_id="ami-1",
            allowed_cidr="203.0.113.1",
            base_domain="example.com",
            hosted_zone_id=ZONE_ID,
        )

    def test_oidc_is_the_default_and_sends_the_issuer_and_client_id(self) -> None:
        settings = deploy.resolve_settings(self.settings_args())
        self.assertEqual(settings.auth_provider, "Oidc")
        self.assertEqual(
            deploy.auth_parameters(settings),
            {
                "OidcIssuerUri": "https://idp.example.test/pool",
                "OidcClientId": "client-1",
            },
        )

    def test_the_optional_settings_are_sent_only_when_set(self) -> None:
        self.env_file.write_text(
            "HUME_OIDC_ROLES_MAPPINGS=admins=ADMINISTRATOR\n"
            "HUME_OIDC_ROLES_CLAIM_PATH=$.groups\n"
            "HUME_OIDC_PRINCIPAL_CLAIM=email\n"
            "HUME_OIDC_JWK_SET_URI=https://idp.example.test/keys\n"
            "HUME_OIDC_SCOPE=openid profile email api://client-1/access\n"
        )
        params = deploy.auth_parameters(deploy.resolve_settings(self.settings_args()))
        self.assertEqual(params["OidcRolesMappings"], "admins=ADMINISTRATOR")
        self.assertEqual(params["OidcRolesClaimPath"], "$.groups")
        self.assertEqual(params["OidcPrincipalClaim"], "email")
        self.assertEqual(params["OidcJwkSetUri"], "https://idp.example.test/keys")
        self.assertEqual(
            params["OidcScope"], "openid profile email api://client-1/access"
        )

    def test_oidc_without_the_issuer_or_client_id_exits_naming_them(self) -> None:
        for name in OIDC_ENV:
            with self.subTest(name=name):
                os.environ.update(OIDC_ENV)
                del os.environ[name]
                with self.assertRaises(SystemExit) as raised:
                    deploy.resolve_settings(self.settings_args())
                self.assertIn(name, str(raised.exception))
                self.assertIn("HUME_AUTH_PROVIDER=Native", str(raised.exception))

    def test_native_needs_no_oidc_setting_and_sends_only_the_provider(self) -> None:
        for name in OIDC_ENV:
            del os.environ[name]
        self.env_file.write_text("HUME_AUTH_PROVIDER=Native\n")
        settings = deploy.resolve_settings(self.settings_args())
        self.assertEqual(settings.oidc, {})
        self.assertEqual(deploy.auth_parameters(settings), {"AuthProvider": "Native"})

    def test_an_unknown_provider_exits_naming_the_setting(self) -> None:
        os.environ["HUME_AUTH_PROVIDER"] = "oidc"
        with self.assertRaises(SystemExit) as raised:
            deploy.resolve_settings(self.settings_args())
        self.assertIn("HUME_AUTH_PROVIDER", str(raised.exception))

    def test_create_stack_sends_the_auth_parameters_and_records_the_provider(
        self,
    ) -> None:
        sm = mock.MagicMock()
        sm.describe_secret.side_effect = lambda SecretId: {"ARN": f"arn:{SecretId}"}
        cfn = mock.MagicMock()
        cfn.create_stack.return_value = {"StackId": "id-1"}
        cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}
        clients = {"secretsmanager": sm, "cloudformation": cfn}
        session = mock.MagicMock()
        session.client.side_effect = clients.__getitem__
        settings = deploy.resolve_settings(self.settings_args())
        small = self.dir / "small.yaml"
        small.write_text("Resources: {}\n")
        with (
            mock.patch.object(deploy, "TEMPLATE", small),
            mock.patch.object(deploy.boto3, "Session", return_value=session),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(deploy.deploy_stack(settings, "stack-o"), 0)
        sent = {
            p["ParameterKey"]: p["ParameterValue"]
            for p in cfn.create_stack.call_args.kwargs["Parameters"]
        }
        self.assertEqual(sent["OidcIssuerUri"], "https://idp.example.test/pool")
        self.assertEqual(sent["OidcClientId"], "client-1")
        self.assertNotIn("AuthProvider", sent)
        self.assertEqual(deploy.read_record("stack-o")["AuthProvider"], "Oidc")


class FailedCreateTest(TempEnvCase):
    ARN = "arn:aws:acm:us-east-1:111111111111:certificate/abc"

    def run_failed_create(self, cfn: mock.MagicMock, acm: mock.MagicMock) -> int:
        sm = mock.MagicMock()
        sm.describe_secret.side_effect = lambda SecretId: {"ARN": f"arn:{SecretId}"}
        cfn.create_stack.return_value = {"StackId": "id-1"}
        cfn.get_waiter.return_value.wait.side_effect = deploy.WaiterError(
            "StackCreateComplete", "failed", {}
        )
        cfn.describe_stack_events.return_value = {"StackEvents": []}
        clients = {"secretsmanager": sm, "cloudformation": cfn, "acm": acm}
        session = mock.MagicMock()
        session.client.side_effect = clients.__getitem__
        settings = deploy.Settings(
            "marketplace", "us-east-1", "ami-1", ["203.0.113.1/32"], "m6i.xlarge",
            "example.com", ZONE_ID,
        )  # fmt: skip
        small = self.dir / "small.yaml"
        small.write_text("Resources: {}\n")
        with (
            mock.patch.object(deploy, "TEMPLATE", small),
            mock.patch.object(deploy.boto3, "Session", return_value=session),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return deploy.deploy_stack(settings, "stack-g")

    def test_a_failed_create_records_the_certificate_and_its_validation(self) -> None:
        cfn = mock.MagicMock()
        cfn.describe_stack_resource.return_value = {
            "StackResourceDetail": {"PhysicalResourceId": self.ARN}
        }
        acm = mock.MagicMock()
        acm.describe_certificate.return_value = {
            "Certificate": {
                "DomainValidationOptions": [
                    {
                        "ResourceRecord": {
                            "Name": "_a.hume.example.com.",
                            "Type": "CNAME",
                            "Value": "_b.x.acm-validations.aws.",
                        }
                    }
                ]
            }
        }
        self.assertEqual(self.run_failed_create(cfn, acm), 1)
        cfn.describe_stack_resource.assert_called_once_with(
            StackName="stack-g", LogicalResourceId="Certificate"
        )
        record = deploy.read_record("stack-g")
        self.assertEqual(record["CertificateArn"], self.ARN)
        self.assertEqual(record[deploy.VALIDATION_NAMES_KEY], "_a.hume.example.com.")
        self.assertEqual(record["StackId"], "id-1")

    def test_a_failed_create_without_a_certificate_keeps_the_record(self) -> None:
        cfn = mock.MagicMock()
        cfn.describe_stack_resource.side_effect = ClientError(
            {"Error": {"Code": "ValidationError", "Message": "does not exist"}},
            "DescribeStackResource",
        )
        acm = mock.MagicMock()
        self.assertEqual(self.run_failed_create(cfn, acm), 1)
        record = deploy.read_record("stack-g")
        self.assertNotIn("CertificateArn", record)
        self.assertEqual(record["StackName"], "stack-g")
        acm.describe_certificate.assert_not_called()

    def test_smoke_says_when_the_create_did_not_finish(self) -> None:
        deploy.write_record("stack-h", {"StackName": "stack-h", "Region": "us-east-1"})
        args = argparse.Namespace(stack_name="stack-h", expect_attribute=None)
        with self.assertRaises(SystemExit) as raised:
            deploy.cmd_smoke(args)
        self.assertIn("did not finish", str(raised.exception))


class StagingTest(TempEnvCase):
    BUCKET = "hume-ecs-templates-123456789012-us-east-1"

    def setUp(self) -> None:
        super().setUp()
        self.s3 = mock.MagicMock()
        sts = mock.MagicMock()
        sts.get_caller_identity.return_value = {"Account": "123456789012"}
        clients = {"s3": self.s3, "sts": sts}
        self.session = mock.MagicMock()
        self.session.region_name = "us-east-1"
        self.session.client.side_effect = clients.__getitem__

    def template(self, size: int) -> mock._patch:
        path = self.dir / "template.yaml"
        path.write_text("a" * size)
        return mock.patch.object(deploy, "TEMPLATE", path)

    def missing_bucket(self) -> ClientError:
        return ClientError({"Error": {"Code": "404", "Message": "no"}}, "HeadBucket")

    def stage(self, size: int) -> tuple[dict, dict]:
        record: dict[str, str] = {}
        with self.template(size), contextlib.redirect_stdout(io.StringIO()):
            args = deploy.template_arguments(self.session, "stack-g", record)
        return args, record

    def test_a_template_within_the_limit_goes_in_the_body(self) -> None:
        args, record = self.stage(deploy.TEMPLATE_DIRECT_LIMIT)
        self.assertEqual(list(args), ["TemplateBody"])
        self.assertEqual(record, {})
        self.s3.put_object.assert_not_called()

    def test_a_larger_template_is_staged_and_passed_by_url(self) -> None:
        args, record = self.stage(deploy.TEMPLATE_DIRECT_LIMIT + 1)
        key = "hume-ecs/stack-g.yaml"
        self.assertEqual(
            args,
            {"TemplateURL": f"https://{self.BUCKET}.s3.us-east-1.amazonaws.com/{key}"},
        )
        put = self.s3.put_object.call_args.kwargs
        self.assertEqual((put["Bucket"], put["Key"]), (self.BUCKET, key))
        self.assertEqual(record[deploy.BUCKET_RECORD_KEY], self.BUCKET)
        self.assertEqual(record[deploy.OBJECT_RECORD_KEY], key)

    def test_the_limit_counts_bytes_not_characters(self) -> None:
        path = self.dir / "template.yaml"
        path.write_text("\u00e9" * (deploy.TEMPLATE_DIRECT_LIMIT // 2 + 1))
        with (
            mock.patch.object(deploy, "TEMPLATE", path),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            args = deploy.template_arguments(self.session, "stack-g", {})
        self.assertIn("TemplateURL", args)

    def test_a_missing_bucket_is_created_private_in_its_region(self) -> None:
        self.s3.head_bucket.side_effect = self.missing_bucket()
        self.stage(deploy.TEMPLATE_DIRECT_LIMIT + 1)
        self.s3.create_bucket.assert_called_once_with(Bucket=self.BUCKET)
        block = self.s3.put_public_access_block.call_args.kwargs
        self.assertEqual(block["Bucket"], self.BUCKET)
        self.assertTrue(all(block["PublicAccessBlockConfiguration"].values()))

        self.session.region_name = "eu-west-1"
        self.s3.head_bucket.side_effect = self.missing_bucket()
        with contextlib.redirect_stdout(io.StringIO()):
            deploy.ensure_template_bucket(self.session)
        call = self.s3.create_bucket.call_args.kwargs
        self.assertEqual(
            call["CreateBucketConfiguration"], {"LocationConstraint": "eu-west-1"}
        )

    def test_an_existing_bucket_is_not_created_again(self) -> None:
        self.stage(deploy.TEMPLATE_DIRECT_LIMIT + 1)
        self.s3.create_bucket.assert_not_called()
        self.s3.put_public_access_block.assert_called_once()

    def test_a_bucket_created_by_a_parallel_deploy_is_accepted(self) -> None:
        self.s3.head_bucket.side_effect = self.missing_bucket()
        self.s3.create_bucket.side_effect = ClientError(
            {"Error": {"Code": "BucketAlreadyOwnedByYou", "Message": "x"}},
            "CreateBucket",
        )
        args, _ = self.stage(deploy.TEMPLATE_DIRECT_LIMIT + 1)
        self.assertIn("TemplateURL", args)

    def test_a_bucket_we_cannot_reach_exits(self) -> None:
        self.s3.head_bucket.side_effect = ClientError(
            {"Error": {"Code": "403", "Message": "denied"}}, "HeadBucket"
        )
        with self.assertRaises(SystemExit):
            deploy.ensure_template_bucket(self.session)
        self.s3.create_bucket.assert_not_called()

    def test_teardown_deletes_only_the_staged_object(self) -> None:
        record = {
            deploy.BUCKET_RECORD_KEY: self.BUCKET,
            deploy.OBJECT_RECORD_KEY: "hume-ecs/stack-g.yaml",
        }
        with contextlib.redirect_stdout(io.StringIO()):
            deploy.delete_template_object(self.session, record)
        self.s3.delete_object.assert_called_once_with(
            Bucket=self.BUCKET, Key="hume-ecs/stack-g.yaml"
        )
        self.s3.delete_bucket.assert_not_called()

    def test_teardown_ignores_a_stack_that_was_never_staged(self) -> None:
        deploy.delete_template_object(self.session, {"StackName": "stack-g"})
        self.s3.delete_object.assert_not_called()

    def delete_bucket(self) -> int:
        args = argparse.Namespace(profile=None, region=None)
        with (
            mock.patch.object(deploy, "cli_session", return_value=self.session),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return deploy.cmd_delete_template_bucket(args)

    def test_bucket_delete_refuses_while_a_template_remains(self) -> None:
        self.s3.list_objects_v2.return_value = {
            "KeyCount": 1,
            "Contents": [{"Key": "hume-ecs/stack-g.yaml"}],
        }
        self.assertEqual(self.delete_bucket(), 1)
        self.s3.delete_bucket.assert_not_called()

    def test_bucket_delete_removes_an_empty_bucket_and_accepts_a_gone_one(self) -> None:
        self.s3.list_objects_v2.return_value = {"KeyCount": 0}
        self.assertEqual(self.delete_bucket(), 0)
        self.s3.delete_bucket.assert_called_once_with(Bucket=self.BUCKET)
        self.s3.delete_objects.assert_not_called()

        self.s3.reset_mock()
        self.s3.list_objects_v2.side_effect = ClientError(
            {"Error": {"Code": "NoSuchBucket", "Message": "x"}}, "ListObjectsV2"
        )
        self.assertEqual(self.delete_bucket(), 0)
        self.s3.delete_bucket.assert_not_called()

    def test_create_stack_uses_the_url_for_a_large_template(self) -> None:
        sm = mock.MagicMock()
        sm.describe_secret.side_effect = lambda SecretId: {"ARN": f"arn:{SecretId}"}
        cfn = mock.MagicMock()
        cfn.create_stack.return_value = {"StackId": "id-2"}
        cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}
        clients = {
            "secretsmanager": sm,
            "cloudformation": cfn,
            "s3": self.s3,
            "sts": self.session.client("sts"),
        }
        session = mock.MagicMock()
        session.region_name = "us-east-1"
        session.client.side_effect = clients.__getitem__
        settings = deploy.Settings(
            "marketplace", "us-east-1", "ami-1", ["203.0.113.1/32"], "m6i.xlarge",
            "hume.example.com", ZONE_ID,
        )  # fmt: skip
        with (
            self.template(deploy.TEMPLATE_DIRECT_LIMIT + 1),
            mock.patch.object(deploy.boto3, "Session", return_value=session),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(deploy.deploy_stack(settings, "stack-h"), 0)
        sent = cfn.create_stack.call_args.kwargs
        self.assertIn("TemplateURL", sent)
        self.assertNotIn("TemplateBody", sent)
        record = deploy.read_record("stack-h")
        self.assertEqual(record[deploy.OBJECT_RECORD_KEY], "hume-ecs/stack-h.yaml")


def client_error(code: str, operation: str = "Describe") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}}, operation)


class ClusterNotFoundException(Exception):
    pass


class SkipReasonTest(TempEnvCase):
    PREFIX = "arn:aws:{service}:us-east-1:123456789012:{resource}"

    def arn(self, service: str, resource: str) -> str:
        return self.PREFIX.format(service=service, resource=resource)

    def session_with(self, **clients: mock.MagicMock) -> mock.MagicMock:
        session = mock.MagicMock()
        session.client.side_effect = clients.__getitem__
        return session

    def reason(self, service: str, resource: str, **clients) -> str | None:
        session = self.session_with(**clients)
        return deploy.skip_reason(session, self.arn(service, resource))

    def ecs(self) -> mock.MagicMock:
        ecs = mock.MagicMock()
        ecs.exceptions.ClusterNotFoundException = ClusterNotFoundException
        return ecs

    def client_answering(self, method: str, answer: object) -> mock.MagicMock:
        client = self.ecs()
        if isinstance(answer, Exception):
            getattr(client, method).side_effect = answer
        else:
            getattr(client, method).return_value = answer
        return client

    def test_each_kind_is_skipped_when_it_is_gone(self) -> None:
        cluster = "cluster/c"
        service = "service/c/s"
        group = "security-group/sg-1"
        balancer = "elasticloadbalancing"
        no_cluster = {"clusters": [], "failures": [{"reason": "MISSING"}]}
        old_cluster = {"clusters": [{"status": "INACTIVE"}], "failures": []}
        no_service = {"services": [], "failures": [{"reason": "MISSING"}]}
        old_service = {"services": [{"status": "INACTIVE"}], "failures": []}
        no_groups = {
            "SecurityGroups": [],
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }
        terminated = {
            "Reservations": [{"Instances": [{"State": {"Name": "terminated"}}]}]
        }
        old_task = {"taskDefinition": {"status": "INACTIVE"}}
        for kind, resource, name, method, answer, text in (
            ("ecs", cluster, "ecs", "describe_clusters", old_cluster, "INACTIVE"),
            (
                "ecs",
                cluster,
                "ecs",
                "describe_clusters",
                no_cluster,
                "no longer exists",
            ),
            (
                "ecs",
                "task-definition/t:1",
                "ecs",
                "describe_task_definition",
                old_task,
                "",
            ),
            ("ecs", service, "ecs", "describe_services", old_service, "INACTIVE"),
            (
                "ecs",
                service,
                "ecs",
                "describe_services",
                no_service,
                "no longer exists",
            ),
            (
                "ecs",
                service,
                "ecs",
                "describe_services",
                ClusterNotFoundException(),
                "cluster",
            ),
            (
                "ec2",
                group,
                "ec2",
                "describe_security_groups",
                client_error("InvalidGroup.NotFound"),
                "security-group",
            ),
            (
                "ec2",
                group,
                "ec2",
                "describe_security_groups",
                no_groups,
                "no longer exists",
            ),
            ("ec2", "instance/i-1", "ec2", "describe_instances", terminated, ""),
            (
                balancer,
                "loadbalancer/app/a/1",
                "elbv2",
                "describe_load_balancers",
                client_error("LoadBalancerNotFound"),
                "",
            ),
            (
                balancer,
                "targetgroup/t/1",
                "elbv2",
                "describe_target_groups",
                client_error("TargetGroupNotFound"),
                "",
            ),
            (
                "rds",
                "db:hume",
                "rds",
                "describe_db_instances",
                client_error("DBInstanceNotFound"),
                "",
            ),
        ):
            with self.subTest(resource=resource, answer=answer):
                client = self.client_answering(method, answer)
                reason = self.reason(kind, resource, **{name: client})
                self.assertIsNotNone(reason)
                self.assertIn(text, reason)

    def test_each_kind_counts_when_it_is_present_or_unknown(self) -> None:
        group = "security-group/sg-1"
        describe_groups = "describe_security_groups"
        groups = {"SecurityGroups": [{"GroupId": "sg-1"}]}
        with_metadata = {**groups, "ResponseMetadata": {"HTTPStatusCode": 200}}
        running = {"Reservations": [{"Instances": [{"State": {"Name": "running"}}]}]}
        for kind, resource, name, method, answer in (
            (
                "ecs",
                "cluster/c",
                "ecs",
                "describe_clusters",
                {"clusters": [{"status": "ACTIVE"}], "failures": []},
            ),
            (
                "ecs",
                "task-definition/t:1",
                "ecs",
                "describe_task_definition",
                {"taskDefinition": {"status": "ACTIVE"}},
            ),
            (
                "ecs",
                "service/c/s",
                "ecs",
                "describe_services",
                {"services": [{"status": "ACTIVE"}], "failures": []},
            ),
            ("ec2", group, "ec2", describe_groups, groups),
            ("ec2", group, "ec2", describe_groups, with_metadata),
            ("ec2", "instance/i-1", "ec2", "describe_instances", running),
            ("rds", "db:hume", "rds", "describe_db_instances", {"DBInstances": [{}]}),
            (
                "ec2",
                group,
                "ec2",
                describe_groups,
                client_error("UnauthorizedOperation"),
            ),
            (
                "elasticloadbalancing",
                "loadbalancer/app/a/1",
                "elbv2",
                "describe_load_balancers",
                client_error("AccessDenied"),
            ),
        ):
            with self.subTest(resource=resource, answer=answer):
                client = self.client_answering(method, answer)
                self.assertIsNone(self.reason(kind, resource, **{name: client}))
        session = self.session_with()
        self.assertIsNone(deploy.skip_reason(session, self.arn("sns", "topic")))
        self.assertIsNone(deploy.skip_reason(session, "not-an-arn"))

    def test_split_separates_live_from_skipped_with_reasons(self) -> None:
        gone = self.arn("ec2", "security-group/sg-gone")
        live = self.arn("ec2", "security-group/sg-live")
        ec2 = mock.MagicMock()

        def describe(GroupIds: list[str]) -> dict:
            if GroupIds == ["sg-gone"]:
                raise client_error("InvalidGroup.NotFound")
            return {"SecurityGroups": [{"GroupId": GroupIds[0]}]}

        ec2.describe_security_groups.side_effect = describe
        session = self.session_with(ec2=ec2)
        kept, skipped = deploy.split_remaining(session, [gone, live])
        self.assertEqual(kept, [live])
        self.assertEqual([arn for arn, _ in skipped], [gone])
        self.assertTrue(skipped[0][1])


class ScaleGroupToZeroTest(TempEnvCase):
    GROUP = "stack-s-AutoScalingGroup-abc"

    @staticmethod
    def group_entry(instances: list[dict] | None) -> list[dict]:
        """None means the group no longer exists."""
        return [] if instances is None else [{"Instances": instances}]

    def session(
        self,
        group_states: list[list[dict]],
        task_counts: list[int],
        resources: dict[str, str] | None = None,
    ) -> tuple[mock.MagicMock, dict[str, mock.MagicMock]]:
        """A session whose group and cluster answer with the given sequences."""
        ids = {"AutoScalingGroup": self.GROUP, "Cluster": "stack-s"}
        ids.update(resources or {})
        cfn = mock.MagicMock()
        cfn.describe_stack_resource.side_effect = lambda **kw: {
            "StackResourceDetail": {"PhysicalResourceId": ids[kw["LogicalResourceId"]]}
        }
        autoscaling = mock.MagicMock()
        autoscaling.describe_auto_scaling_groups.side_effect = [
            {"AutoScalingGroups": self.group_entry(instances)}
            for instances in group_states
        ]
        ecs = mock.MagicMock()
        ecs.describe_clusters.side_effect = [
            {"clusters": [{"runningTasksCount": count}]} for count in task_counts
        ]
        clients = {"cloudformation": cfn, "autoscaling": autoscaling, "ecs": ecs}
        session = mock.MagicMock()
        session.client.side_effect = lambda name: clients[name]
        return session, clients

    def scale(self, session: mock.MagicMock) -> tuple[str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(deploy.time, "sleep"),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            deploy.scale_group_to_zero(session, "stack-s")
        return out.getvalue(), err.getvalue()

    def test_group_is_set_to_zero_and_waits_for_instance_and_tasks(self) -> None:
        up = [{"InstanceId": "i-1"}]
        # existence check, then three polls: instance up, instance gone with a
        # task still running, everything stopped.
        session, clients = self.session([up, up, [], []], [1, 1, 0])
        out, _ = self.scale(session)
        clients["autoscaling"].update_auto_scaling_group.assert_called_once_with(
            AutoScalingGroupName=self.GROUP, MinSize=0, DesiredCapacity=0
        )
        self.assertEqual(clients["ecs"].describe_clusters.call_count, 3)
        self.assertIn("no tasks are running", out)

    def test_a_group_that_is_already_gone_is_left_alone(self) -> None:
        session, clients = self.session([None], [])
        self.scale(session)
        clients["autoscaling"].update_auto_scaling_group.assert_not_called()

    def test_a_stack_that_is_gone_is_left_alone(self) -> None:
        session, clients = self.session([], [])
        clients["cloudformation"].describe_stack_resource.side_effect = ClientError(
            {"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}},
            "DescribeStackResource",
        )
        self.scale(session)
        clients["autoscaling"].describe_auto_scaling_groups.assert_not_called()
        clients["autoscaling"].update_auto_scaling_group.assert_not_called()

    def test_another_cloudformation_error_is_raised(self) -> None:
        session, clients = self.session([], [])
        clients["cloudformation"].describe_stack_resource.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "no"}},
            "DescribeStackResource",
        )
        with self.assertRaises(ClientError):
            self.scale(session)

    def test_a_cluster_that_is_gone_counts_as_no_tasks(self) -> None:
        ecs = mock.MagicMock()
        ecs.describe_clusters.return_value = {"clusters": [], "failures": [{}]}
        self.assertEqual(deploy.running_task_count(ecs, "stack-s"), 0)
        self.assertEqual(deploy.running_task_count(ecs, None), 0)

    def test_it_gives_up_waiting_but_does_not_raise(self) -> None:
        up = [{"InstanceId": "i-1"}]
        session, _ = self.session([up] * 4, [1, 1, 1])
        clock = iter([0, 0, deploy.GROUP_DRAIN_TIMEOUT + 1])
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(deploy.time, "sleep"),
            mock.patch.object(deploy.time, "monotonic", side_effect=clock),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            deploy.scale_group_to_zero(session, "stack-s")
        self.assertIn("Still 1 instance(s) and 1 task(s)", err.getvalue())

    def test_scale_in_runs_before_every_delete_attempt(self) -> None:
        calls: list[str] = []
        cfn = mock.MagicMock()
        cfn.delete_stack.side_effect = lambda **kw: calls.append("delete")
        waiter = cfn.get_waiter.return_value
        waiter.wait.side_effect = [
            deploy.WaiterError("StackDeleteComplete", "failed", {}),
            None,
        ]
        with (
            mock.patch.object(deploy.time, "sleep"),
            mock.patch.object(deploy, "print_failed_events"),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            done = deploy.delete_stack_with_retry(
                cfn, "stack-s", scale_in=lambda: calls.append("scale")
            )
        self.assertTrue(done)
        self.assertEqual(calls, ["scale", "delete", "scale", "delete"])


class TeardownLeftoverTest(TempEnvCase):
    def run_teardown(
        self, remaining: list[str], skipped: list, record_removed: bool = False
    ) -> tuple[int, str, str]:
        deploy.write_record("stack-i", {"StackName": "stack-i"})
        removal = (
            (lambda *_: deploy.record_path("stack-i").unlink())
            if record_removed
            else None
        )
        out, err = io.StringIO(), io.StringIO()
        args = argparse.Namespace(
            stack_name="stack-i", profile=None, region=None, yes=True
        )
        with (
            mock.patch.object(deploy, "make_session"),
            mock.patch.object(deploy, "capture_validation_records"),
            mock.patch.object(
                deploy,
                "retained_resources",
                return_value=deploy.Retained("key-1", None, None),
            ),
            mock.patch.object(deploy, "disable_database_protection", return_value=True),
            mock.patch.object(deploy, "delete_stack_with_retry", return_value=True),
            mock.patch.object(deploy, "delete_leftover_secrets"),
            mock.patch.object(deploy, "delete_leftover_log_groups"),
            mock.patch.object(deploy, "delete_validation_records"),
            mock.patch.object(deploy, "delete_template_object", side_effect=removal),
            mock.patch.object(deploy, "list_remaining", return_value=["x"]),
            mock.patch.object(
                deploy, "split_remaining", return_value=(remaining, skipped)
            ),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = deploy.cmd_teardown(args)
        return code, out.getvalue(), err.getvalue()

    def test_skipped_leftovers_are_printed_and_do_not_fail_teardown(self) -> None:
        code, out, _ = self.run_teardown([], [("arn:subnet", "the subnet is gone")])
        self.assertEqual(code, 0)
        self.assertIn("Skipped arn:subnet: the subnet is gone", out)
        self.assertFalse(deploy.record_path("stack-i").exists())

    def test_a_record_that_is_already_gone_does_not_stop_the_teardown(self) -> None:
        code, _, err = self.run_teardown([], [], record_removed=True)
        self.assertEqual(code, 0)
        self.assertNotIn("Traceback", err)
        self.assertFalse(deploy.record_path("stack-i").exists())

    def test_teardown_scales_the_group_in_before_each_delete(self) -> None:
        with (
            mock.patch.object(deploy, "scale_group_to_zero") as scale,
            mock.patch.object(deploy, "empty_access_log_bucket") as empty,
        ):
            captured: dict = {}

            def fake_delete(cfn, stack, scale_in=None):
                captured["scale_in"] = scale_in
                return True

            with mock.patch.object(
                deploy, "delete_stack_with_retry", side_effect=fake_delete
            ):
                deploy.write_record("stack-i", {"StackName": "stack-i"})
                args = argparse.Namespace(
                    stack_name="stack-i", profile=None, region=None, yes=True
                )
                with (
                    mock.patch.object(deploy, "make_session") as make_session,
                    mock.patch.object(deploy, "capture_validation_records"),
                    mock.patch.object(
                        deploy,
                        "retained_resources",
                        return_value=deploy.Retained(None, None, None),
                    ),
                    mock.patch.object(
                        deploy, "disable_database_protection", return_value=True
                    ),
                    mock.patch.object(deploy, "delete_leftover_secrets"),
                    mock.patch.object(deploy, "delete_leftover_log_groups"),
                    mock.patch.object(deploy, "delete_validation_records"),
                    mock.patch.object(deploy, "delete_template_object"),
                    mock.patch.object(deploy, "list_remaining", return_value=[]),
                    mock.patch.object(deploy, "split_remaining", return_value=([], [])),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    deploy.cmd_teardown(args)
                captured["scale_in"]()
                scale.assert_called_once_with(make_session.return_value, "stack-i")
                empty.assert_called_once_with(make_session.return_value, "stack-i")

    def test_a_live_leftover_still_fails_and_keeps_the_record(self) -> None:
        code, out, err = self.run_teardown(
            ["arn:live"], [("arn:subnet", "the subnet is gone")]
        )
        self.assertEqual(code, 1)
        self.assertIn("arn:live", err)
        self.assertIn("Skipped arn:subnet", out)
        self.assertTrue(deploy.record_path("stack-i").exists())


class TeardownConfirmTest(TempEnvCase):
    def teardown_args(self, **changes) -> argparse.Namespace:
        values = {
            "stack_name": "stack-t",
            "profile": None,
            "region": None,
            "yes": False,
        }
        return argparse.Namespace(**{**values, **changes})

    def run_teardown(self, answer: str | None, **changes) -> tuple[int, dict, str]:
        """Run cmd_teardown with the AWS steps mocked. answer None means no prompt."""
        deploy.write_record("stack-t", {"StackName": "stack-t"})
        calls: list[str] = []
        disable = mock.MagicMock(side_effect=lambda *a: calls.append("disable") or True)
        delete = mock.MagicMock(
            side_effect=lambda *a, **k: calls.append("delete") or True
        )
        ask = mock.MagicMock(return_value=answer)
        err = io.StringIO()
        with (
            mock.patch.object(deploy, "make_session"),
            mock.patch.object(deploy, "capture_validation_records"),
            mock.patch.object(
                deploy,
                "retained_resources",
                return_value=deploy.Retained("key-9", None, None),
            ),
            mock.patch.object(deploy, "disable_database_protection", disable),
            mock.patch.object(deploy, "delete_stack_with_retry", delete),
            mock.patch.object(deploy, "delete_leftover_secrets"),
            mock.patch.object(deploy, "delete_leftover_log_groups"),
            mock.patch.object(deploy, "delete_validation_records"),
            mock.patch.object(deploy, "delete_template_object"),
            mock.patch.object(deploy, "list_remaining", return_value=[]),
            mock.patch.object(deploy, "split_remaining", return_value=([], [])),
            mock.patch("builtins.input", ask),
            contextlib.redirect_stdout(io.StringIO()) as out,
            contextlib.redirect_stderr(err),
        ):
            code = deploy.cmd_teardown(self.teardown_args(**changes))
        return code, {"calls": calls, "ask": ask, "out": out.getvalue()}, err.getvalue()

    def test_the_stack_name_is_required(self) -> None:
        with (
            mock.patch.object(deploy.sys, "argv", ["deploy.py", "teardown"]),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            deploy.parse_args()

    def test_the_yes_flag_is_read_from_the_command_line(self) -> None:
        for command in ("teardown", "reimport", "refresh-truststore"):
            for flags, expected in (([], False), (["--yes"], True)):
                argv = ["deploy.py", command, "stack-t", *flags]
                with (
                    self.subTest(command=command, flags=flags),
                    mock.patch.object(deploy.sys, "argv", argv),
                ):
                    args = deploy.parse_args()
                    self.assertEqual(args.stack_name, "stack-t")
                    self.assertEqual(args.yes, expected)

    def test_typing_the_stack_name_confirms(self) -> None:
        code, seen, _ = self.run_teardown("stack-t")
        self.assertEqual(code, 0)
        self.assertEqual(seen["calls"], ["disable", "delete"])

    def test_a_wrong_name_deletes_nothing(self) -> None:
        code, seen, err = self.run_teardown("y")
        self.assertEqual(code, 1)
        self.assertEqual(seen["calls"], [])
        self.assertIn("Nothing was deleted", err)
        self.assertTrue(deploy.record_path("stack-t").exists())

    def test_no_input_deletes_nothing(self) -> None:
        deploy.write_record("stack-t", {"StackName": "stack-t"})
        with (
            mock.patch("builtins.input", side_effect=EOFError),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertFalse(deploy.confirm_teardown("stack-t", False))

    def test_yes_skips_the_prompt(self) -> None:
        code, seen, _ = self.run_teardown(None, yes=True)
        self.assertEqual(code, 0)
        seen["ask"].assert_not_called()
        self.assertEqual(seen["calls"], ["disable", "delete"])

    def test_a_database_that_stays_protected_stops_before_the_delete(self) -> None:
        deploy.write_record("stack-t", {"StackName": "stack-t"})
        with (
            mock.patch.object(deploy, "make_session"),
            mock.patch.object(deploy, "capture_validation_records"),
            mock.patch.object(
                deploy,
                "retained_resources",
                return_value=deploy.Retained("key-9", None, None),
            ),
            mock.patch.object(
                deploy, "disable_database_protection", return_value=False
            ),
            mock.patch.object(deploy, "delete_stack_with_retry") as delete,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = deploy.cmd_teardown(self.teardown_args(yes=True))
        self.assertEqual(code, 1)
        delete.assert_not_called()

    def test_the_kept_key_and_snapshot_do_not_fail_the_leftover_check(self) -> None:
        key = "arn:aws:kms:us-east-1:111122223333:key/key-9"
        snapshot = "arn:aws:rds:us-east-1:111122223333:snapshot:stack-t-final"
        other_key = "arn:aws:kms:us-east-1:111122223333:key/other"
        self.assertTrue(deploy.is_kept_on_purpose(key, "key-9"))
        self.assertTrue(deploy.is_kept_on_purpose(snapshot, "key-9"))
        self.assertFalse(deploy.is_kept_on_purpose(other_key, "key-9"))
        self.assertFalse(deploy.is_kept_on_purpose(key, None))
        self.assertFalse(deploy.is_kept_on_purpose("arn:aws:rds:r:1:db:x", "key-9"))
        self.assertFalse(deploy.is_kept_on_purpose("x", "key-9"))

    def test_a_tagged_key_is_reported_as_kept_and_not_as_a_leftover(self) -> None:
        key = "arn:aws-us-gov:kms:us-gov-west-1:111122223333:key/key-9"
        deploy.write_record("stack-t", {"StackName": "stack-t"})
        with (
            mock.patch.object(deploy, "make_session"),
            mock.patch.object(deploy, "capture_validation_records"),
            mock.patch.object(
                deploy,
                "retained_resources",
                return_value=deploy.Retained("key-9", None, None),
            ),
            mock.patch.object(deploy, "disable_database_protection", return_value=True),
            mock.patch.object(deploy, "delete_stack_with_retry", return_value=True),
            mock.patch.object(deploy, "delete_leftover_secrets"),
            mock.patch.object(deploy, "delete_leftover_log_groups"),
            mock.patch.object(deploy, "delete_validation_records"),
            mock.patch.object(deploy, "delete_template_object"),
            mock.patch.object(deploy, "list_remaining", return_value=[key]),
            mock.patch.object(
                deploy, "split_remaining", return_value=([], [])
            ) as split,
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            code = deploy.cmd_teardown(self.teardown_args(yes=True))
        self.assertEqual(code, 0)
        split.assert_called_once()
        self.assertEqual(split.call_args.args[1], [])
        self.assertIn(f"Kept on purpose: {key}", out.getvalue())

    def test_the_retained_key_is_reported_with_the_command_to_remove_it(self) -> None:
        _, seen, _ = self.run_teardown(None, yes=True)
        self.assertIn("The encryption key key-9 is retained", seen["out"])
        self.assertIn("aws kms schedule-key-deletion --key-id key-9", seen["out"])


class TeardownResourcesTest(TempEnvCase):
    def make_session(self, database: str | None, bucket: str | None):
        ids = {"Database": database, "AccessLogBucket": bucket}
        cfn, rds, s3 = mock.MagicMock(), mock.MagicMock(), mock.MagicMock()
        cfn.describe_stack_resource.side_effect = lambda StackName, LogicalResourceId: {
            "StackResourceDetail": {"PhysicalResourceId": ids[LogicalResourceId]}
        }
        clients = {"cloudformation": cfn, "rds": rds, "s3": s3}
        session = mock.MagicMock()
        session.client.side_effect = clients.__getitem__
        return session, rds, s3

    def client_error(self, code: str, operation: str) -> ClientError:
        return ClientError({"Error": {"Code": code, "Message": "x"}}, operation)

    def run_quiet(self, function, *args):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            result = function(*args)
        return result, err.getvalue()

    def test_deletion_protection_is_switched_off_for_the_stack_database(self) -> None:
        session, rds, _ = self.make_session("hume-db", None)
        done, _ = self.run_quiet(deploy.disable_database_protection, session, "s")
        self.assertTrue(done)
        rds.modify_db_instance.assert_called_once_with(
            DBInstanceIdentifier="hume-db",
            DeletionProtection=False,
            ApplyImmediately=True,
        )

    def test_a_stack_without_a_database_needs_no_change(self) -> None:
        session, rds, _ = self.make_session(None, None)
        done, _ = self.run_quiet(deploy.disable_database_protection, session, "s")
        self.assertTrue(done)
        rds.modify_db_instance.assert_not_called()

    def test_what_is_already_gone_or_empty_is_fine(self) -> None:
        session, rds, _ = self.make_session("hume-db", None)
        rds.modify_db_instance.side_effect = self.client_error(
            "DBInstanceNotFound", "ModifyDBInstance"
        )
        done, _ = self.run_quiet(deploy.disable_database_protection, session, "s")
        self.assertTrue(done)

        session, _, s3 = self.make_session(None, "logs-bucket")
        s3.get_paginator.return_value.paginate.return_value = [{}]
        self.run_quiet(deploy.empty_access_log_bucket, session, "s")
        s3.delete_objects.assert_not_called()
        session, _, s3 = self.make_session(None, None)
        self.run_quiet(deploy.empty_access_log_bucket, session, "s")
        s3.get_paginator.assert_not_called()

        session, _, s3 = self.make_session(None, "logs-bucket")
        s3.get_paginator.return_value.paginate.side_effect = self.client_error(
            "NoSuchBucket", "ListObjectsV2"
        )
        self.run_quiet(deploy.empty_access_log_bucket, session, "s")
        s3.delete_objects.assert_not_called()

    def test_any_other_rds_error_fails_with_a_clear_message(self) -> None:
        session, rds, _ = self.make_session("hume-db", None)
        rds.modify_db_instance.side_effect = self.client_error(
            "InvalidDBInstanceState", "ModifyDBInstance"
        )
        done, err = self.run_quiet(deploy.disable_database_protection, session, "s")
        self.assertFalse(done)
        self.assertIn("InvalidDBInstanceState", err)
        self.assertIn("switch the protection off by hand", err)

    def test_the_log_bucket_is_emptied_in_batches(self) -> None:
        session, _, s3 = self.make_session(None, "logs-bucket")
        s3.get_paginator.return_value.paginate.return_value = [
            {"Contents": [{"Key": "a"}, {"Key": "b"}]},
            {},
            {"Contents": [{"Key": "c"}]},
        ]
        s3.delete_objects.side_effect = lambda Bucket, Delete: {
            "Deleted": Delete["Objects"]
        }
        self.run_quiet(deploy.empty_access_log_bucket, session, "s")
        sent = [c.kwargs["Delete"]["Objects"] for c in s3.delete_objects.mock_calls]
        self.assertEqual(sent, [[{"Key": "a"}, {"Key": "b"}], [{"Key": "c"}]])
        s3.delete_bucket.assert_not_called()

    def test_another_s3_error_is_raised(self) -> None:
        session, _, s3 = self.make_session(None, "logs-bucket")
        s3.get_paginator.return_value.paginate.side_effect = self.client_error(
            "AccessDenied", "ListObjectsV2"
        )
        with self.assertRaises(ClientError):
            self.run_quiet(deploy.empty_access_log_bucket, session, "s")

    def test_the_group_is_stopped_before_the_bucket_is_emptied(self) -> None:
        calls: list[str] = []
        with (
            mock.patch.object(
                deploy,
                "scale_group_to_zero",
                side_effect=lambda *a: calls.append("scale"),
            ),
            mock.patch.object(
                deploy,
                "empty_access_log_bucket",
                side_effect=lambda *a: calls.append("empty"),
            ),
        ):
            deploy.prepare_delete(mock.MagicMock(), "s")
        self.assertEqual(calls, ["scale", "empty"])


REIMPORT_STACK_OUTPUTS = {
    "ConfigCliTaskDefinitionArn": "arn:task-definition/config-cli:3",
    "ClusterName": "cluster-r",
    "CapacityProviderName": "cp-r",
    "InstanceSubnetId": "subnet-0aaaaaaa1",
    "InstanceSecurityGroupId": "sg-0aaaaaaa1",
}


class ReimportTest(TempEnvCase):
    def reimport_args(self, **changes) -> argparse.Namespace:
        values = {"stack_name": "stack-r", "profile": None, "region": None}
        return argparse.Namespace(**{**values, "yes": True, **changes})

    def make_ecs(self, exit_code: int | None = 0) -> mock.MagicMock:
        ecs = mock.MagicMock()
        ecs.run_task.return_value = {
            "failures": [],
            "tasks": [{"taskArn": "arn:task/t1"}],
        }
        ecs.describe_tasks.return_value = {
            "tasks": [
                {
                    "containers": [{"name": "config-cli", "exitCode": exit_code}],
                    "stoppedReason": "Essential container in task exited",
                }
            ]
        }
        return ecs

    def run_reimport(
        self,
        outputs: dict[str, str],
        ecs: mock.MagicMock,
        answer: str | None = None,
        **changes,
    ) -> tuple[int, str, str]:
        deploy.write_record("stack-r", {"StackName": "stack-r"})
        session = mock.MagicMock()
        session.client.return_value = ecs
        ask = mock.MagicMock(return_value=answer)
        with (
            mock.patch.object(deploy, "make_session", return_value=session),
            mock.patch.object(deploy, "stack_outputs", return_value=outputs),
            mock.patch("builtins.input", ask),
            contextlib.redirect_stdout(io.StringIO()) as out,
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            code = deploy.cmd_reimport(self.reimport_args(**changes))
        return code, out.getvalue(), err.getvalue()

    def test_a_stack_without_the_config_cli_output_stops(self) -> None:
        outputs = dict(REIMPORT_STACK_OUTPUTS)
        del outputs["ConfigCliTaskDefinitionArn"]
        ecs = self.make_ecs()
        with self.assertRaises(SystemExit) as stop:
            self.run_reimport(outputs, ecs)
        self.assertIn("SpineConfigSecretArn", str(stop.exception.code))
        ecs.run_task.assert_not_called()

    def test_a_missing_network_output_stops(self) -> None:
        outputs = dict(REIMPORT_STACK_OUTPUTS)
        del outputs["InstanceSubnetId"]
        ecs = self.make_ecs()
        with self.assertRaises(SystemExit) as stop:
            self.run_reimport(outputs, ecs)
        self.assertIn("InstanceSubnetId", str(stop.exception.code))
        ecs.run_task.assert_not_called()

    def test_the_task_runs_in_the_instance_network(self) -> None:
        ecs = self.make_ecs()
        code, _, _ = self.run_reimport(REIMPORT_STACK_OUTPUTS, ecs)
        self.assertEqual(code, 0)
        kwargs = ecs.run_task.call_args.kwargs
        self.assertEqual(kwargs["cluster"], "cluster-r")
        self.assertEqual(kwargs["taskDefinition"], "arn:task-definition/config-cli:3")
        self.assertEqual(
            kwargs["capacityProviderStrategy"],
            [{"capacityProvider": "cp-r", "weight": 1}],
        )
        self.assertEqual(
            kwargs["networkConfiguration"]["awsvpcConfiguration"],
            {
                "subnets": ["subnet-0aaaaaaa1"],
                "securityGroups": ["sg-0aaaaaaa1"],
                "assignPublicIp": "DISABLED",
            },
        )

    def test_a_clean_import_restarts_orchestra_and_waits(self) -> None:
        ecs = self.make_ecs()
        code, out, _ = self.run_reimport(REIMPORT_STACK_OUTPUTS, ecs)
        self.assertEqual(code, 0)
        ecs.update_service.assert_called_once_with(
            cluster="cluster-r", service="orchestra", forceNewDeployment=True
        )
        waiters = [c.args[0] for c in ecs.get_waiter.call_args_list]
        self.assertEqual(waiters, ["tasks_stopped", "services_stable"])
        self.assertIn("PASS", out)

    def test_a_failed_import_does_not_restart_orchestra(self) -> None:
        ecs = self.make_ecs(exit_code=1)
        code, _, err = self.run_reimport(REIMPORT_STACK_OUTPUTS, ecs)
        self.assertEqual(code, 1)
        self.assertIn("config-cli exited 1", err)
        self.assertIn("/hume/stack-r/config-cli", err)
        ecs.update_service.assert_not_called()

    def test_run_task_failures_stop_before_the_wait(self) -> None:
        ecs = self.make_ecs()
        ecs.run_task.return_value = {"failures": [{"reason": "RESOURCE:CPU"}]}
        code, _, err = self.run_reimport(REIMPORT_STACK_OUTPUTS, ecs)
        self.assertEqual(code, 1)
        self.assertIn("RESOURCE:CPU", err)
        ecs.get_waiter.assert_not_called()
        ecs.update_service.assert_not_called()

    def test_an_import_that_does_not_stop_fails_without_a_restart(self) -> None:
        ecs = self.make_ecs()
        ecs.get_waiter.return_value.wait.side_effect = WaiterError(
            "TasksStopped", "Max attempts exceeded", {}
        )
        code, _, err = self.run_reimport(REIMPORT_STACK_OUTPUTS, ecs)
        self.assertEqual(code, 1)
        self.assertIn("arn:task/t1 did not stop within 15 minutes", err)
        self.assertIn("/hume/stack-r/config-cli", err)
        ecs.describe_tasks.assert_not_called()
        ecs.update_service.assert_not_called()

    def test_an_orchestra_that_is_not_stable_fails(self) -> None:
        ecs = self.make_ecs()
        ecs.get_waiter.return_value.wait.side_effect = [
            None,
            WaiterError("ServicesStable", "Max attempts exceeded", {}),
        ]
        code, out, err = self.run_reimport(REIMPORT_STACK_OUTPUTS, ecs)
        self.assertEqual(code, 1)
        self.assertIn("orchestra was not stable within 15 minutes", err)
        self.assertIn("/hume/stack-r/orchestra", err)
        self.assertNotIn("PASS", out)

    def test_a_no_at_the_prompt_changes_nothing(self) -> None:
        ecs = self.make_ecs()
        code, _, err = self.run_reimport(
            REIMPORT_STACK_OUTPUTS, ecs, answer="n", yes=False
        )
        self.assertEqual(code, 1)
        self.assertIn("Nothing was changed", err)
        ecs.run_task.assert_not_called()

    def test_a_yes_at_the_prompt_runs_the_import(self) -> None:
        ecs = self.make_ecs()
        code, _, _ = self.run_reimport(
            REIMPORT_STACK_OUTPUTS, ecs, answer="y", yes=False
        )
        self.assertEqual(code, 0)
        ecs.run_task.assert_called_once()


TRUSTSTORE_ARN = (
    "arn:aws:secretsmanager:us-east-1:111122223333:secret:hume/neo4j-truststore-AbC"
)


class RefreshTruststoreTest(TempEnvCase):
    def refresh_args(self, **changes) -> argparse.Namespace:
        values = {"stack_name": "stack-r", "profile": None, "region": None}
        return argparse.Namespace(**{**values, "yes": True, **changes})

    def make_ssm(self, status: str = "Success") -> mock.MagicMock:
        ssm = mock.MagicMock()
        ssm.send_command.return_value = {"Command": {"CommandId": "cmd-1"}}
        ssm.get_command_invocation.return_value = {
            "Status": status,
            "StandardOutputContent": "",
            "StandardErrorContent": "The truststore secret has no truststore value",
        }
        return ssm

    def run_refresh(
        self,
        ssm: mock.MagicMock,
        ecs: mock.MagicMock,
        secret_arn: str = TRUSTSTORE_ARN,
        answer: str | None = None,
        instance: str | None = "i-0aaaaaaa1",
        **changes,
    ) -> tuple[int, str, str]:
        deploy.write_record(
            "stack-r", {"StackName": "stack-r", "AutoScalingGroupName": "asg-r"}
        )
        cloudformation = mock.MagicMock()
        cloudformation.describe_stacks.return_value = {
            "Stacks": [
                {
                    "Parameters": [
                        {
                            "ParameterKey": "Neo4jTruststoreSecretArn",
                            "ParameterValue": secret_arn,
                        }
                    ]
                }
            ]
        }
        clients = {"cloudformation": cloudformation, "ssm": ssm, "ecs": ecs}
        session = mock.MagicMock(region_name="us-east-1")
        session.client.side_effect = clients.__getitem__
        if instance is None:
            find_instance = mock.MagicMock(
                side_effect=SystemExit("stack-r has no in-service instance in asg-r")
            )
        else:
            find_instance = mock.MagicMock(return_value=instance)
        self.ask = mock.MagicMock(return_value=answer)
        with (
            mock.patch.object(deploy, "make_session", return_value=session),
            mock.patch.object(
                deploy, "stack_outputs", return_value=REIMPORT_STACK_OUTPUTS
            ),
            mock.patch.object(deploy, "hume_instance_id", find_instance),
            mock.patch("builtins.input", self.ask),
            contextlib.redirect_stdout(io.StringIO()) as out,
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            code = deploy.cmd_refresh_truststore(self.refresh_args(**changes))
        return code, out.getvalue(), err.getvalue()

    def test_the_commands_read_the_secret_and_move_the_file(self) -> None:
        commands = deploy.truststore_write_commands(TRUSTSTORE_ARN, "us-east-1")
        text = "\n".join(commands)
        tmp = f"{deploy.TRUSTSTORE_HOST_PATH}.tmp"
        self.assertIn(f"--secret-id {TRUSTSTORE_ARN}", text)
        self.assertIn("--region us-east-1", text)
        self.assertIn(f"> {tmp}", text)
        self.assertEqual(commands[-1], f"mv -f {tmp} {deploy.TRUSTSTORE_HOST_PATH}")
        self.assertIn(f"chown 1001:1001 {tmp}", commands)
        self.assertIn(f"chmod 0600 {tmp}", commands)
        for key in deploy.TRUSTSTORE_SECRET_KEYS:
            self.assertIn(key, text)
        self.assertNotIn("set -x", text)
        self.assertNotIn("put-secret-value", text)
        self.assertIsNone(re.search(r"[A-Za-z0-9+/]{64,}", text))

    def test_an_arn_with_shell_characters_is_quoted(self) -> None:
        arn = "arn:aws:secretsmanager:x:1:secret:a;rm -rf $HOME"
        text = "\n".join(deploy.truststore_write_commands(arn, "us-east-1"))
        self.assertIn(f"--secret-id {shlex.quote(arn)}", text)
        self.assertNotIn(f"--secret-id {arn}", text)

    def test_the_template_writes_the_same_path_and_keys(self) -> None:
        text = deploy.TEMPLATE.read_text()
        directory, name = deploy.TRUSTSTORE_HOST_PATH.rsplit("/", 1)
        self.assertIn(f"TRUSTSTORE_DIR={directory}", text)
        self.assertIn(f"$TRUSTSTORE_DIR/{name}", text)
        self.assertIn(deploy.TRUSTSTORE_HOST_PATH, text)
        for key in deploy.TRUSTSTORE_SECRET_KEYS:
            self.assertIn(f'"{key}"', text)

    def test_a_written_file_restarts_orchestra_and_api_and_waits(self) -> None:
        ssm = self.make_ssm()
        ecs = mock.MagicMock()
        code, out, _ = self.run_refresh(ssm, ecs)
        self.assertEqual(code, 0)
        kwargs = ssm.send_command.call_args.kwargs
        self.assertEqual(kwargs["InstanceIds"], ["i-0aaaaaaa1"])
        self.assertEqual(kwargs["DocumentName"], "AWS-RunShellScript")
        self.assertEqual(
            kwargs["Parameters"]["commands"],
            deploy.truststore_write_commands(TRUSTSTORE_ARN, "us-east-1"),
        )
        self.assertEqual(
            ecs.update_service.call_args_list,
            [
                mock.call(cluster="cluster-r", service=name, forceNewDeployment=True)
                for name in ("orchestra", "api")
            ],
        )
        self.assertEqual(ecs.get_waiter.call_args.args, ("services_stable",))
        self.assertEqual(
            ecs.get_waiter.return_value.wait.call_args.kwargs["services"],
            ["orchestra", "api"],
        )
        self.assertIn("PASS: the truststore was written", out)

    def test_a_failed_write_prints_stderr_and_does_not_restart(self) -> None:
        ssm = self.make_ssm(status="Failed")
        ecs = mock.MagicMock()
        code, out, err = self.run_refresh(ssm, ecs)
        self.assertEqual(code, 1)
        self.assertIn("The truststore secret has no truststore value", err)
        ecs.update_service.assert_not_called()
        self.assertNotIn("PASS", out)

    def test_a_service_that_is_not_stable_fails(self) -> None:
        ssm = self.make_ssm()
        ecs = mock.MagicMock()
        ecs.get_waiter.return_value.wait.side_effect = WaiterError(
            "ServicesStable", "Max attempts exceeded", {}
        )
        code, out, err = self.run_refresh(ssm, ecs)
        self.assertEqual(code, 1)
        self.assertIn("orchestra and api was not stable within 15 minutes", err)
        self.assertIn("/hume/stack-r/orchestra", err)
        self.assertIn("/hume/stack-r/api", err)
        self.assertNotIn("PASS", out)

    def test_a_stack_without_a_truststore_secret_stops(self) -> None:
        ssm = self.make_ssm()
        ecs = mock.MagicMock()
        with self.assertRaises(SystemExit) as stop:
            self.run_refresh(ssm, ecs, secret_arn="")
        self.assertIn("Neo4jTruststoreSecretArn", str(stop.exception.code))
        ssm.send_command.assert_not_called()
        ecs.update_service.assert_not_called()

    def test_a_stack_without_an_in_service_instance_stops(self) -> None:
        ssm = self.make_ssm()
        ecs = mock.MagicMock()
        with self.assertRaises(SystemExit) as stop:
            self.run_refresh(ssm, ecs, instance=None)
        self.assertIn("no in-service instance", str(stop.exception.code))
        ssm.send_command.assert_not_called()
        ecs.update_service.assert_not_called()

    def test_a_no_at_the_prompt_changes_nothing(self) -> None:
        ssm = self.make_ssm()
        ecs = mock.MagicMock()
        code, _, err = self.run_refresh(ssm, ecs, answer="n", yes=False)
        self.assertEqual(code, 1)
        self.assertIn("Nothing was changed", err)
        ssm.send_command.assert_not_called()
        ecs.update_service.assert_not_called()

    def test_a_yes_at_the_prompt_or_the_flag_writes_the_file(self) -> None:
        ssm = self.make_ssm()
        ecs = mock.MagicMock()
        code, _, _ = self.run_refresh(ssm, ecs, answer="y", yes=False)
        self.assertEqual(code, 0)
        ssm.send_command.assert_called_once()

        ssm = self.make_ssm()
        ecs = mock.MagicMock()
        code, _, _ = self.run_refresh(ssm, ecs, yes=True)
        self.assertEqual(code, 0)
        self.ask.assert_not_called()
        ssm.send_command.assert_called_once()


if __name__ == "__main__":
    unittest.main()
