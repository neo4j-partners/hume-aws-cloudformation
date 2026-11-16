"""Offline tests for check_prereqs.py, the read-only check of an account.

Every AWS client is a fake, so no AWS call is made. The settings come from a
dict in place of the environment and the repo-root .env.

Run from the repository root:
    uv run --with boto3 --with python-dotenv --with pyyaml \
        python -m unittest tests.stack.test_check_prereqs
"""

import argparse
import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

from botocore.exceptions import ClientError, NoCredentialsError

import check_prereqs
import deploy

ACCOUNT = "123456789012"
AMI_ID = "ami-0123456789abcdef0"
ZONE_ID = "Z0123456789ABC"
POOL_ID = "us-west-2_AbCdEf123"
ISSUER = f"https://cognito-idp.us-west-2.amazonaws.com/{POOL_ID}"


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "Operation")


class FakeSts:
    def __init__(self, error: Exception | None = None):
        self.error = error

    def get_caller_identity(self):
        if self.error:
            raise self.error
        return {"Account": ACCOUNT}


class FakeEc2:
    def __init__(self, images=None, error: Exception | None = None):
        self.images = (
            [{"ImageId": AMI_ID, "Name": "hume-2.33", "State": "available"}]
            if images is None
            else images
        )
        self.error = error
        self.asked: list[list[str]] = []

    def describe_images(self, ImageIds):
        self.asked.append(ImageIds)
        if self.error:
            raise self.error
        return {"Images": self.images}


class FakeRoute53:
    def __init__(self, private: bool = False):
        self.private = private
        self.asked: list[str] = []

    def get_hosted_zone(self, Id):
        self.asked.append(Id)
        return {
            "HostedZone": {
                "Name": "example.com.",
                "Config": {"PrivateZone": self.private},
            }
        }


class FakeSecrets:
    """Only describe_secret exists, so a secret value cannot be read."""

    def __init__(self, errors: dict[str, str] | None = None, deleted=()):
        self.errors = errors or {}
        self.deleted = deleted
        self.asked: list[str] = []

    def describe_secret(self, SecretId):
        self.asked.append(SecretId)
        if SecretId in self.errors:
            raise client_error(self.errors[SecretId])
        secret = {"Name": SecretId, "ARN": f"arn:aws:secretsmanager:x:{SecretId}"}
        if SecretId in self.deleted:
            secret["DeletedDate"] = "2026-10-30"
        return secret


class FakeCognito:
    def __init__(self, error: str | None = None):
        self.error = error
        self.asked: list[str] = []

    def describe_user_pool(self, UserPoolId):
        self.asked.append(UserPoolId)
        if self.error:
            raise client_error(self.error)
        return {"UserPool": {"Id": UserPoolId, "Name": "hume-users"}}


class CheckCase(unittest.TestCase):
    def setUp(self) -> None:
        self.sts = FakeSts()
        self.ec2 = FakeEc2()
        self.route53 = FakeRoute53()
        self.secrets = FakeSecrets()
        self.cognito = FakeCognito()
        self.settings = {deploy.ZONE_VAR: ZONE_ID, check_prereqs.ISSUER_VAR: ISSUER}
        self.client_calls: list[tuple[str, dict]] = []

    def client(self, name, **kwargs):
        self.client_calls.append((name, kwargs))
        return {
            "sts": self.sts,
            "ec2": self.ec2,
            "route53": self.route53,
            "secretsmanager": self.secrets,
            "cognito-idp": self.cognito,
        }[name]

    def run_check(self, ami_id: str | None = AMI_ID) -> tuple[int, list[str]]:
        session = mock.Mock()
        session.client.side_effect = self.client
        namespace = argparse.Namespace(profile=None, region=None, ami_id=ami_id)
        out = io.StringIO()
        with (
            mock.patch.object(deploy, "cli_session", return_value=session),
            mock.patch.object(deploy, "env_setting", side_effect=self.settings.get),
            contextlib.redirect_stdout(out),
        ):
            code = check_prereqs.run(namespace)
        return code, out.getvalue().splitlines()


class PresentTest(CheckCase):
    def test_everything_present_passes_with_the_account_first(self) -> None:
        code, lines = self.run_check()
        self.assertEqual(code, 0)
        self.assertEqual(
            lines,
            [
                f"account       {ACCOUNT}",
                f"ami           {AMI_ID} hume-2.33 available",
                f"hosted zone   {ZONE_ID} example.com. public",
                "secret        hume/licence-key present",
                "secret        hume/neo4j-truststore present",
                f"cognito pool  {POOL_ID} hume-users",
            ],
        )
        self.assertEqual(self.ec2.asked, [[AMI_ID]])
        self.assertEqual(self.route53.asked, [ZONE_ID])
        self.assertEqual(self.cognito.asked, [POOL_ID])

    def test_each_secret_is_described_by_its_deploy_name(self) -> None:
        self.run_check()
        self.assertEqual(
            self.secrets.asked,
            [
                deploy.LICENCE_SECRET_NAME,
                deploy.TRUSTSTORE_SECRET_NAME,
            ],
        )

    def test_the_pool_is_read_in_the_region_of_the_issuer(self) -> None:
        self.run_check()
        self.assertIn(("cognito-idp", {"region_name": "us-west-2"}), self.client_calls)

    def test_the_ami_falls_back_to_the_setting(self) -> None:
        self.settings[deploy.AMI_ID_VAR] = "ami-0fedcba9876543210"
        self.run_check(ami_id=None)
        self.assertEqual(self.ec2.asked, [["ami-0fedcba9876543210"]])

    def test_a_private_zone_is_named_as_private(self) -> None:
        self.route53.private = True
        _, lines = self.run_check()
        self.assertIn(f"hosted zone   {ZONE_ID} example.com. private", lines)


class MissingTest(CheckCase):
    def test_a_missing_secret_fails_and_the_others_still_run(self) -> None:
        self.secrets.errors = {deploy.LICENCE_SECRET_NAME: "ResourceNotFoundException"}
        code, lines = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("secret        hume/licence-key missing", lines)
        self.assertIn("secret        hume/neo4j-truststore present", lines)
        self.assertIn(f"cognito pool  {POOL_ID} hume-users", lines)

    def test_another_secret_error_prints_its_code(self) -> None:
        self.secrets.errors = {deploy.LICENCE_SECRET_NAME: "AccessDeniedException"}
        code, lines = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("secret        hume/licence-key AccessDeniedException", lines)

    def test_a_secret_scheduled_for_deletion_fails(self) -> None:
        self.secrets.deleted = (deploy.TRUSTSTORE_SECRET_NAME,)
        code, lines = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn(
            "secret        hume/neo4j-truststore scheduled for deletion", lines
        )

    def test_a_missing_ami_fails(self) -> None:
        self.ec2.error = client_error("InvalidAMIID.NotFound")
        code, lines = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("ami           FAILED InvalidAMIID.NotFound", lines)
        self.assertEqual(len(lines), 6)

    def test_an_ami_that_is_not_available_fails(self) -> None:
        self.ec2.images = [{"ImageId": AMI_ID, "Name": "hume-2.33", "State": "pending"}]
        code, lines = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn(f"ami           {AMI_ID} hume-2.33 pending", lines)

    def test_an_empty_image_list_fails(self) -> None:
        self.ec2.images = []
        code, lines = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn(f"ami           {AMI_ID} not found", lines)

    def test_a_missing_pool_fails(self) -> None:
        self.cognito.error = "ResourceNotFoundException"
        code, lines = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("cognito pool  FAILED ResourceNotFoundException", lines)

    def test_no_credentials_stops_after_the_account_line(self) -> None:
        self.sts.error = NoCredentialsError()
        code, lines = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(lines, ["account       FAILED Unable to locate credentials"])
        self.assertEqual(self.ec2.asked, [])
        self.assertEqual(self.secrets.asked, [])


class UnsetTest(CheckCase):
    def test_an_unset_issuer_with_native_sign_in_is_skipped(self) -> None:
        del self.settings[check_prereqs.ISSUER_VAR]
        self.settings[deploy.AUTH_PROVIDER_VAR] = deploy.NATIVE_PROVIDER
        code, lines = self.run_check()
        self.assertEqual(code, 0)
        self.assertEqual(lines[-1], "cognito pool  not set: HUME_OIDC_ISSUER_URI")
        self.assertEqual(self.cognito.asked, [])

    def test_an_unset_issuer_with_oidc_sign_in_fails(self) -> None:
        del self.settings[check_prereqs.ISSUER_VAR]
        code, lines = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1], "cognito pool  not set: HUME_OIDC_ISSUER_URI")
        self.assertEqual(self.cognito.asked, [])

    def test_an_issuer_that_is_not_cognito_is_skipped(self) -> None:
        self.settings[check_prereqs.ISSUER_VAR] = "https://login.example.com/realm"
        code, lines = self.run_check()
        self.assertEqual(code, 0)
        self.assertEqual(lines[-1], "cognito pool  skipped, the issuer is not Cognito")
        self.assertEqual(self.cognito.asked, [])

    def test_an_unset_zone_fails(self) -> None:
        del self.settings[deploy.ZONE_VAR]
        code, lines = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("hosted zone   not set: HUME_HOSTED_ZONE_ID", lines)
        self.assertEqual(self.route53.asked, [])

    def test_an_unset_ami_exits_with_the_deploy_message(self) -> None:
        with (
            mock.patch.object(deploy, "AMI_ID_FILE", Path("/nonexistent/ami-id.txt")),
            self.assertRaises(SystemExit) as stopped,
        ):
            self.run_check(ami_id=None)
        self.assertIn("No AMI id", str(stopped.exception))


class ArgsTest(unittest.TestCase):
    def test_the_flags(self) -> None:
        argv = [
            "check_prereqs.py", "--profile", "p", "--region", "eu-west-2",
            "--ami-id", AMI_ID,
        ]  # fmt: skip
        with mock.patch("sys.argv", argv):
            args = check_prereqs.parse_args()
        self.assertEqual(
            (args.profile, args.region, args.ami_id), ("p", "eu-west-2", AMI_ID)
        )

    def test_the_flags_are_optional(self) -> None:
        with mock.patch("sys.argv", ["check_prereqs.py"]):
            args = check_prereqs.parse_args()
        self.assertEqual((args.profile, args.region, args.ami_id), (None, None, None))


if __name__ == "__main__":
    unittest.main()
