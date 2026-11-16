"""Offline tests for the restore options of the deploy helper and its teardown.

The helper runs against mocked AWS clients. No AWS call is made.

Run from the repository root:
    uv run --with boto3 --with python-dotenv --with pyyaml \
        python -m unittest tests.stack.test_deploy_restore
"""

import argparse
import contextlib
import io
import unittest
from unittest import mock

from botocore.exceptions import ClientError

import deploy
from tests.helpers import template as tm
from tests.helpers.env import ZONE_ID, TempEnvCase

SNAPSHOT = "hume-old-final-snapshot"
FILE_SYSTEM = "fs-0123456789abcdef0"
KEY_ID = "1234abcd-12ab-34cd-56ef-1234567890ab"
KEY_ARN = f"arn:aws:kms:us-east-1:111122223333:key/{KEY_ID}"
FLAGS = {
    "db_snapshot_identifier": SNAPSHOT,
    "existing_file_system_id": FILE_SYSTEM,
    "existing_data_key_arn": KEY_ARN,
}
RESTORE = {
    "DbSnapshotIdentifier": SNAPSHOT,
    "ExistingFileSystemId": FILE_SYSTEM,
    "ExistingDataKeyArn": KEY_ARN,
}


def restore_args(**values) -> argparse.Namespace:
    base = dict.fromkeys(FLAGS)
    return argparse.Namespace(**{**base, **values})


def stack_gone() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ValidationError", "Message": "Stack x does not exist"}},
        "DescribeStacks",
    )


class RestoreFlagsTest(unittest.TestCase):
    def test_the_flags_are_the_template_restore_parameters(self) -> None:
        self.assertEqual(list(deploy.RESTORE_FLAGS), tm.RESTORE_PARAMETERS)
        parameters = tm.load()["Parameters"]
        for name in deploy.RESTORE_FLAGS:
            self.assertIn(name, parameters)

    def test_deploy_and_up_read_the_flags(self) -> None:
        for command in ("deploy", "up"):
            argv = [
                "deploy.py",
                command,
                "--db-snapshot-identifier",
                SNAPSHOT,
                "--existing-file-system-id",
                FILE_SYSTEM,
                "--existing-data-key-arn",
                KEY_ARN,
            ]
            with (
                self.subTest(command=command),
                mock.patch.object(deploy.sys, "argv", argv),
            ):
                args = deploy.parse_args()
                self.assertEqual(deploy.resolve_restore(args), RESTORE)

    def test_no_flag_means_no_restore(self) -> None:
        with mock.patch.object(deploy.sys, "argv", ["deploy.py", "deploy"]):
            self.assertEqual(deploy.resolve_restore(deploy.parse_args()), {})

    def test_kept_data_without_the_key_exits_naming_the_flag(self) -> None:
        for dest in ("db_snapshot_identifier", "existing_file_system_id"):
            with self.subTest(flag=dest), self.assertRaises(SystemExit) as raised:
                deploy.resolve_restore(restore_args(**{dest: FLAGS[dest]}))
            self.assertIn("--existing-data-key-arn is required", str(raised.exception))

    def test_the_key_alone_or_with_each_kept_item_is_accepted(self) -> None:
        key = {"existing_data_key_arn": KEY_ARN}
        self.assertEqual(
            deploy.resolve_restore(restore_args(**key)), {"ExistingDataKeyArn": KEY_ARN}
        )
        for dest in ("db_snapshot_identifier", "existing_file_system_id"):
            with self.subTest(flag=dest):
                got = deploy.resolve_restore(restore_args(**key, **{dest: FLAGS[dest]}))
                self.assertEqual(len(got), 2)


class DeployRestoreTest(TempEnvCase):
    def deploy(self, restore: dict[str, str]) -> tuple[dict, dict]:
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
            "example.com", ZONE_ID, restore=restore,
        )  # fmt: skip
        small = self.dir / "small.yaml"
        small.write_text("Resources: {}\n")
        with (
            mock.patch.object(deploy, "TEMPLATE", small),
            mock.patch.object(deploy.boto3, "Session", return_value=session),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(deploy.deploy_stack(settings, "stack-r"), 0)
        sent = {
            p["ParameterKey"]: p["ParameterValue"]
            for p in cfn.create_stack.call_args.kwargs["Parameters"]
        }
        return sent, deploy.read_record("stack-r")

    def test_the_restore_values_are_sent_and_recorded(self) -> None:
        sent, record = self.deploy(RESTORE)
        for name, value in RESTORE.items():
            self.assertEqual(sent[name], value, name)
            self.assertEqual(record[name], value, name)

    def test_a_new_stack_sends_and_records_no_restore_value(self) -> None:
        sent, record = self.deploy({})
        for name in RESTORE:
            self.assertNotIn(name, sent)
            self.assertNotIn(name, record)


class RetainedResourcesTest(unittest.TestCase):
    def session(self, outputs: dict | None, ids: dict | None = None):
        cfn = mock.MagicMock()
        if outputs is None:
            cfn.describe_stacks.side_effect = stack_gone()
        else:
            cfn.describe_stacks.return_value = {
                "Stacks": [
                    {
                        "Outputs": [
                            {"OutputKey": k, "OutputValue": v}
                            for k, v in outputs.items()
                        ]
                    }
                ]
            }
        ids = ids or {}

        def describe(StackName, LogicalResourceId):
            if LogicalResourceId not in ids:
                raise ClientError(
                    {
                        "Error": {
                            "Code": "ValidationError",
                            "Message": f"Resource {LogicalResourceId} does not exist",
                        }
                    },
                    "DescribeStackResource",
                )
            return {
                "StackResourceDetail": {"PhysicalResourceId": ids[LogicalResourceId]}
            }

        cfn.describe_stack_resource.side_effect = describe
        session = mock.MagicMock()
        session.client.return_value = cfn
        return session

    def test_the_outputs_name_the_key_and_file_system(self) -> None:
        session = self.session({"DataKeyArn": KEY_ARN, "FileSystemId": FILE_SYSTEM})
        self.assertEqual(
            deploy.retained_resources(session, "stack-r"),
            deploy.Retained(KEY_ID, KEY_ARN, FILE_SYSTEM),
        )

    def test_a_failed_create_falls_back_to_the_resources(self) -> None:
        session = self.session({}, {"DataKey": KEY_ID, "DataFileSystem": FILE_SYSTEM})
        self.assertEqual(
            deploy.retained_resources(session, "stack-r"),
            deploy.Retained(KEY_ID, None, FILE_SYSTEM),
        )

    def test_a_failed_create_without_the_resources_has_nothing(self) -> None:
        session = self.session({})
        self.assertEqual(
            deploy.retained_resources(session, "stack-r"),
            deploy.Retained(None, None, None),
        )

    def test_a_deleted_stack_has_nothing(self) -> None:
        session = self.session(None)
        self.assertEqual(
            deploy.retained_resources(session, "stack-r"),
            deploy.Retained(None, None, None),
        )


class KeptOnPurposeTest(unittest.TestCase):
    def test_the_stack_file_system_is_kept_and_no_other(self) -> None:
        arn = f"arn:aws:elasticfilesystem:us-east-1:111122223333:file-system/{FILE_SYSTEM}"
        other = arn.replace(FILE_SYSTEM, "fs-0fedcba9876543210")
        gov = arn.replace("arn:aws:", "arn:aws-us-gov:")
        self.assertTrue(deploy.is_kept_on_purpose(arn, None, FILE_SYSTEM))
        self.assertTrue(deploy.is_kept_on_purpose(gov, None, FILE_SYSTEM))
        self.assertFalse(deploy.is_kept_on_purpose(other, None, FILE_SYSTEM))
        self.assertFalse(deploy.is_kept_on_purpose(arn, KEY_ID, None))
        access_point = arn.replace("file-system/", "access-point/fsap-")
        self.assertFalse(deploy.is_kept_on_purpose(access_point, None, FILE_SYSTEM))

    def test_the_reset_function_log_group_is_cleaned_up(self) -> None:
        names = deploy.leftover_log_group_names("stack-r")
        self.assertIn("/aws/lambda/stack-r-db-master-reset", names)


class TeardownRestoreTest(TempEnvCase):
    def run_teardown(self, retained: deploy.Retained, tagged: list[str]):
        deploy.write_record("stack-r", {"StackName": "stack-r"})
        args = argparse.Namespace(
            stack_name="stack-r", profile=None, region=None, yes=True
        )
        with (
            mock.patch.object(deploy, "make_session"),
            mock.patch.object(deploy, "capture_validation_records"),
            mock.patch.object(deploy, "retained_resources", return_value=retained),
            mock.patch.object(deploy, "disable_database_protection", return_value=True),
            mock.patch.object(deploy, "delete_stack_with_retry", return_value=True),
            mock.patch.object(deploy, "delete_leftover_secrets"),
            mock.patch.object(deploy, "delete_leftover_log_groups"),
            mock.patch.object(deploy, "delete_validation_records"),
            mock.patch.object(deploy, "delete_template_object"),
            mock.patch.object(deploy, "list_remaining", return_value=tagged),
            mock.patch.object(
                deploy, "split_remaining", return_value=([], [])
            ) as split,
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            code = deploy.cmd_teardown(args)
        return code, out.getvalue(), split.call_args.args[1]

    def test_the_kept_file_system_is_not_a_leftover(self) -> None:
        arn = f"arn:aws:elasticfilesystem:us-east-1:111122223333:file-system/{FILE_SYSTEM}"
        code, out, checked = self.run_teardown(
            deploy.Retained(KEY_ID, KEY_ARN, FILE_SYSTEM), [arn, "arn:other"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(checked, ["arn:other"])
        self.assertIn(f"Kept on purpose: {arn}", out)

    def test_teardown_prints_what_a_restore_needs(self) -> None:
        _, out, _ = self.run_teardown(deploy.Retained(KEY_ID, KEY_ARN, FILE_SYSTEM), [])
        self.assertIn(f"The file system {FILE_SYSTEM} is retained", out)
        self.assertIn(f"--existing-data-key-arn {KEY_ARN}", out)
        self.assertIn(f"--existing-file-system-id {FILE_SYSTEM}", out)
        self.assertIn("--db-snapshot-identifier", out)
        self.assertIn(f"aws kms schedule-key-deletion --key-id {KEY_ID}", out)

    def test_no_restore_hint_without_the_key_arn(self) -> None:
        _, out, _ = self.run_teardown(deploy.Retained(KEY_ID, None, FILE_SYSTEM), [])
        self.assertIn(f"The file system {FILE_SYSTEM} is retained", out)
        self.assertNotIn("To restore", out)

    def test_the_confirmation_says_the_file_system_is_kept(self) -> None:
        with (
            mock.patch("builtins.input", return_value="no"),
            contextlib.redirect_stdout(io.StringIO()) as out,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertFalse(deploy.confirm_teardown("stack-r", False))
        self.assertIn(
            "The EFS file system with the Hume data is retained.", out.getvalue()
        )


if __name__ == "__main__":
    unittest.main()
