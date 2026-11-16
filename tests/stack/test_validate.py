"""Offline tests for the validate command, the TLS check from the Hume instance.

The command runs against a mocked AWS session. No AWS call is made.

Run from the repository root:
    uv run --with boto3 --with python-dotenv \
        python -m unittest tests.stack.test_validate
"""

import argparse
import base64
import contextlib
import io
import os
import unittest
from unittest import mock

from botocore.exceptions import WaiterError

import deploy
from tests.helpers.env import TempEnvCase

CERT = "-----BEGIN CERTIFICATE-----\nQUFB\n-----END CERTIFICATE-----"
HOST = "neo4j-oidc.neo4j.local"
PASS_OUTPUT = (
    "CONNECTED(00000003)\n"
    f"subject=CN={HOST}\n"
    "Verification: OK\n"
    "Verify return code: 0 (ok)\n"
)
FAIL_OUTPUT = "connect:errno=0\n"


class TlsCheckCommandsTest(unittest.TestCase):
    def test_it_writes_the_ca_checks_the_host_and_removes_the_ca(self) -> None:
        commands = deploy.tls_check_commands(HOST, CERT + "\n")
        self.assertEqual(len(commands), 3)
        encoded = base64.b64encode((CERT + "\n").encode()).decode()
        self.assertEqual(
            commands[0], f"echo {encoded} | base64 -d > {deploy.TLS_CHECK_CA_PATH}"
        )
        self.assertIn(f"-connect {HOST}:7687", commands[1])
        self.assertIn(f"-servername {HOST}", commands[1])
        self.assertIn(f"-verify_hostname {HOST}", commands[1])
        self.assertIn(f"-CAfile {deploy.TLS_CHECK_CA_PATH}", commands[1])
        self.assertEqual(commands[2], f"rm -f {deploy.TLS_CHECK_CA_PATH}")

    def test_it_quotes_a_host_with_shell_characters(self) -> None:
        commands = deploy.tls_check_commands("a;b", CERT)
        self.assertIn("-connect 'a;b':7687", commands[1])
        self.assertNotIn(" a;b", commands[1])


class ValidateCommandTest(TempEnvCase):
    def setUp(self) -> None:
        super().setUp()
        os.environ.pop(deploy.CA_FILE_VAR, None)
        self.ca_file = self.dir / "neo4j-ca.pem"
        self.ca_file.write_text(f"{CERT}\n")

    def run_validate(
        self,
        *,
        group: str | None = "asg-v",
        ca_file: str | None = "default",
        host: str = HOST,
        instances: tuple[dict, ...] = (
            {"InstanceId": "i-old", "LifecycleState": "Terminating"},
            {"InstanceId": "i-1", "LifecycleState": "InService"},
        ),
        output: str = PASS_OUTPUT,
        wait_error: bool = False,
    ):
        record = {"StackName": "stack-v"}
        if group:
            record["AutoScalingGroupName"] = group
        deploy.write_record("stack-v", record)
        cfn, autoscaling, ssm = mock.MagicMock(), mock.MagicMock(), mock.MagicMock()
        cfn.describe_stacks.return_value = {
            "Stacks": [
                {
                    "Outputs": [
                        {"OutputKey": "Neo4jHost", "OutputValue": host},
                    ]
                }
            ]
        }
        autoscaling.describe_auto_scaling_groups.return_value = {
            "AutoScalingGroups": [{"Instances": list(instances)}]
        }
        ssm.send_command.return_value = {"Command": {"CommandId": "c-1"}}
        if wait_error:
            ssm.get_waiter.return_value.wait.side_effect = WaiterError(
                "CommandExecuted", "failed", {}
            )
        ssm.get_command_invocation.return_value = {
            "StandardOutputContent": output,
            "StandardErrorContent": "",
        }
        session = mock.MagicMock()
        session.client.side_effect = {
            "cloudformation": cfn,
            "autoscaling": autoscaling,
            "ssm": ssm,
        }.__getitem__
        args = argparse.Namespace(
            stack_name="stack-v",
            profile=None,
            region=None,
            ca_file=str(self.ca_file) if ca_file == "default" else ca_file,
        )
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(deploy, "make_session", return_value=session),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = deploy.cmd_validate(args)
        return code, out.getvalue(), err.getvalue(), ssm

    def test_pass_when_the_output_has_verify_return_code_zero(self) -> None:
        code, out, err, ssm = self.run_validate()
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn(f"Checking {HOST}:7687 from i-1", out)
        self.assertIn("Verify return code: 0 (ok)", out)
        self.assertIn(f"PASS: Hume reaches {HOST} over TLS", out)
        kwargs = ssm.send_command.call_args.kwargs
        self.assertEqual(kwargs["InstanceIds"], ["i-1"])
        self.assertEqual(kwargs["DocumentName"], "AWS-RunShellScript")
        self.assertEqual(
            kwargs["Parameters"]["commands"],
            deploy.tls_check_commands(HOST, f"{CERT}\n"),
        )
        ssm.get_command_invocation.assert_called_once_with(
            CommandId="c-1", InstanceId="i-1"
        )

    def test_fail_when_the_output_has_no_verify_return_code_zero(self) -> None:
        code, out, err, _ = self.run_validate(output=FAIL_OUTPUT)
        self.assertEqual(code, 1)
        self.assertIn("connect:errno=0", out)
        self.assertIn("FAIL", err)
        self.assertNotIn("PASS", out)

    def test_a_failed_wait_still_reads_and_judges_the_output(self) -> None:
        code, out, err, ssm = self.run_validate(wait_error=True)
        self.assertEqual(code, 0)
        self.assertIn("The SSM command did not succeed", err)
        self.assertIn("PASS", out)
        ssm.get_command_invocation.assert_called_once()

    def test_the_ca_file_defaults_to_the_setting(self) -> None:
        os.environ[deploy.CA_FILE_VAR] = str(self.ca_file)
        code, _, _, _ = self.run_validate(ca_file=None)
        self.assertEqual(code, 0)

    def test_a_record_without_a_group_is_refused(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            self.run_validate(group=None)
        self.assertIn("AutoScalingGroupName", str(raised.exception))

    def test_no_ca_file_is_refused(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            self.run_validate(ca_file=None)
        self.assertIn(deploy.CA_FILE_VAR, str(raised.exception))

    def test_a_missing_ca_file_is_refused(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            self.run_validate(ca_file=str(self.dir / "missing.pem"))
        self.assertIn("does not exist", str(raised.exception))

    def test_a_ca_file_with_a_private_key_is_refused(self) -> None:
        self.ca_file.write_text(f"{CERT}\n-----BEGIN PRIVATE KEY-----\nx\n")
        with self.assertRaises(SystemExit) as raised:
            self.run_validate()
        self.assertIn("private key", str(raised.exception))

    def test_a_stack_without_the_host_output_is_refused(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            self.run_validate(host="")
        self.assertIn("Neo4jHost", str(raised.exception))

    def test_no_in_service_instance_is_refused(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            self.run_validate(
                instances=({"InstanceId": "i-old", "LifecycleState": "Pending"},)
            )
        self.assertIn("no in-service instance", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
