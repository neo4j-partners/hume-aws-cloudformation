"""Offline tests for spine_demo.py, the script that sets up the Spine demo.

Every AWS client is a mock, so no AWS call is made and nothing is staged,
stored or changed. The node check, copy and load of seed_nodes are mocks too.
The mocks record the order of the writes.

Run from the repository root:
    uv run --with boto3 --with python-dotenv --with pyyaml \
        python -m unittest tests.demo.test_spine_demo
"""

import contextlib
import io
import json
import os
import time
import unittest
from unittest import mock

from botocore.exceptions import ClientError, TokenRetrievalError, WaiterError

import cognito
import deploy
import seed_nodes
import spine_demo
from tests.helpers.env import TempEnvCase

STACK = "stack-s"
NEO4J_STACK = "ee-stack"
INSTANCE = "i-aaa"
ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:hume/stack-s/spine-config-AbCdEf"
PASSWORD_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:neo4j/ee-stack/password-AbCdEf"
SERVER_ID = "8f3a1c52-6b7d-4e90-a1b2-c3d4e5f60718"
SEEDS = "/var/lib/neo4j/seeds"
WANT = 614_894_162
PRESIGNED = "https://tmpl.s3.us-east-1.amazonaws.com/spine/spine-latest.backup?X-Amz-Signature=abc"
LOAD_LINES = (
    "spine-status=online",
    f"spine-nodes={spine_demo.EXPECTED_NODES}",
    f"spine-persons={spine_demo.EXPECTED_PERSONS}",
    f"spine-relationships={spine_demo.EXPECTED_RELATIONSHIPS}",
)
NODE = ("--neo4j-stack", NEO4J_STACK, "--instance-id", INSTANCE)
ADD = sorted(spine_demo.ADD_IMPORT_CHANGES)
MODIFY = sorted(spine_demo.IMPORT_CHANGES)
ROLES = sorted(spine_demo.ROLES_CHANGES)
DEFAULT_MAPPINGS = "hume-admins=ADMINISTRATOR,hume-users=USER"
SPINE_PAIRS = "spine-analysts=SPINE_ANALYST,spine-officers=SPINE_OFFICER"
FULL_MAPPINGS = f"{DEFAULT_MAPPINGS},{SPINE_PAIRS}"
ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc"
BASE_DOMAIN = "example.test"
COGNITO_RECORD = {
    "UserPoolId": "us-east-1_abc",
    "HumeClientId": "hume-id",
    "Neo4jClientId": "neo4j-id",
    "Issuer": ISSUER,
}
PASSWORD = "pw-never-printed"
ME_URL = f"https://hume-api.{BASE_DOMAIN}{cognito.USERS_ME_PATH}"


def me(*roles: str, status: int = 200) -> tuple[int, bytes]:
    body = {"username": "spine-analyst@hume.external", "roles": list(roles)}
    return status, (b"nope" if status != 200 else json.dumps(body).encode())


ANALYST_REPLIES = (me("USER", "SPINE_ANALYST"),)


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "Operation")


def probe(have: int = 0, server: str = SERVER_ID, rc: str = "0") -> dict[str, str]:
    return {
        "neo4j_conf": "yes",
        "seeds_dir": SEEDS,
        "have": str(have),
        "server_id": server,
        "free": "35000000000",
        "https_rc": rc,
        "https_status": "200" if rc == "0" else "",
        "want": str(WANT) if rc == "0" else "",
    }


class SpineDemoCase(TempEnvCase):
    def setUp(self) -> None:
        super().setUp()
        os.environ["HUME_SPINE_CONFIG_DIR"] = "demo/spine/hume-config"
        deploy.write_record(
            STACK,
            {
                "ClusterName": "cluster-s",
                "Profile": "marketplace",
                "Region": "us-east-1",
                "BaseDomainName": BASE_DOMAIN,
            },
        )

    def run_main(
        self,
        argv=(STACK, "--yes", *NODE),
        *,
        answer: str | type | None = None,
        secret_exists: bool = False,
        arn_parameter: str = "",
        superuser_arn: str = PASSWORD_ARN,
        status: str = "UPDATE_COMPLETE",
        changes=ADD,
        node_probe: dict[str, str] | None = None,
        node_error: Exception | None = None,
        load_lines=LOAD_LINES,
        load_error: Exception | None = None,
        sso_error: Exception | None = None,
        change_set_error: bool = False,
        update_error: bool = False,
        stage=None,
        stage_error: Exception | None = None,
        auth_provider: str = "Oidc",
        roles_mappings: str = FULL_MAPPINGS,
        cognito_record: dict[str, str] | None = None,
        replies=ANALYST_REPLIES,
    ):
        """Run main() against mocks. Returns (code, stdout, stderr)."""
        self.calls: list[str] = []
        calls = self.calls
        sts, cfn, sm, idp = (mock.MagicMock() for _ in range(4))
        if cognito_record is not None:
            cognito.write_cognito_record(cognito.DEFAULT_NAME, cognito_record)
        sm.get_secret_value.return_value = {"SecretString": PASSWORD}
        idp.admin_initiate_auth.return_value = {
            "AuthenticationResult": {"AccessToken": "tok-access", "IdToken": "tok-id"}
        }
        if sso_error:
            sts.get_caller_identity.side_effect = sso_error
        else:
            sts.get_caller_identity.return_value = {"Account": "123456789012"}
        parameters = [
            {"ParameterKey": "HumeAmiId", "ParameterValue": "ami-1"},
            {"ParameterKey": "InstanceType", "ParameterValue": "m6i.xlarge"},
            {"ParameterKey": "EeStackName", "ParameterValue": "neo4j-ee-1"},
            {"ParameterKey": "AuthProvider", "ParameterValue": auth_provider},
            {"ParameterKey": "OidcIssuerUri", "ParameterValue": ISSUER},
            {"ParameterKey": "OidcRolesMappings", "ParameterValue": roles_mappings},
            {"ParameterKey": "SpineConfigSecretArn", "ParameterValue": arn_parameter},
        ]  # fmt: skip
        cfn.describe_stacks.return_value = {
            "Stacks": [{"StackStatus": status, "Parameters": parameters}]
        }
        cfn.describe_change_set.return_value = {
            "Status": "CREATE_COMPLETE",
            "Changes": [
                {"ResourceChange": {"Action": a, "LogicalResourceId": i}}
                for a, i in changes
            ],
        }
        cfn.create_change_set.side_effect = lambda **kw: calls.append("change-set")
        cfn.execute_change_set.side_effect = lambda **kw: calls.append("execute")
        waiters = {"change_set_create_complete": change_set_error}
        waiters["stack_update_complete"] = update_error

        def waiter(name):
            result = mock.MagicMock()
            if waiters[name]:
                result.wait.side_effect = WaiterError(name, "failed", {})
            return result

        cfn.get_waiter.side_effect = waiter
        if secret_exists:
            sm.describe_secret.return_value = {"ARN": ARN}
        else:
            sm.describe_secret.side_effect = client_error("ResourceNotFoundException")
        session = mock.MagicMock()
        session.profile_name = "marketplace"
        session.region_name = "us-east-1"
        session.client.side_effect = {
            "sts": sts,
            "cloudformation": cfn,
            "secretsmanager": sm,
            "cognito-idp": idp,
        }.__getitem__
        self.sts, self.cfn, self.sm, self.ssm = sts, cfn, sm, mock.Mock()
        self.idp = idp
        self.fetch = mock.MagicMock(side_effect=list(replies))

        def check_node(session_, stack, instance, url, step):
            if node_error:
                raise node_error
            item = {"id": instance, "name": "node-1", "zone": "z", "ip": "10.0.0.1"}
            return self.ssm, item, node_probe or probe()

        def copy(ssm, instance, seeds_dir, url, want):
            calls.append("copy")
            return probe(have=want)

        def load(ssm, instance, server, secret_arn):
            calls.append("load")
            if load_error:
                raise load_error
            for line in load_lines:
                print(line)
            return seed_nodes.parse_load("\n".join(load_lines))

        def put(session_, stack):
            calls.append("secret")
            return ARN

        self.check_node = mock.MagicMock(side_effect=check_node)
        self.copy = mock.MagicMock(side_effect=copy)
        self.load = mock.MagicMock(side_effect=load)
        self.put = mock.MagicMock(side_effect=put)
        self.superuser = mock.MagicMock(return_value=superuser_arn)
        self.input = mock.MagicMock(return_value=answer)
        if answer is None:
            self.input.side_effect = AssertionError("input was called")
        elif answer is EOFError:
            self.input.side_effect = EOFError
        self.print_failed = mock.MagicMock()
        self.stage_backup = mock.MagicMock(return_value=PRESIGNED)
        if stage_error:
            self.stage_backup.side_effect = stage_error
        self.template = mock.MagicMock(return_value={"TemplateURL": "https://t/x.yaml"})
        if stage:
            self.template.side_effect = stage
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(deploy, "make_session", return_value=session),
            mock.patch.object(deploy, "put_spine_config", self.put),
            mock.patch.object(deploy, "template_arguments", self.template),
            mock.patch.object(deploy, "print_failed_events", self.print_failed),
            mock.patch.object(seed_nodes, "check_node", self.check_node),
            mock.patch.object(seed_nodes, "superuser_secret_arn", self.superuser),
            mock.patch.object(seed_nodes, "run_copy", self.copy),
            mock.patch.object(seed_nodes, "run_load", self.load),
            mock.patch.object(seed_nodes, "stage_backup", self.stage_backup),
            mock.patch.object(cognito, "fetch", self.fetch),
            mock.patch.object(time, "sleep"),
            mock.patch("builtins.input", self.input),
            mock.patch("sys.argv", ["spine_demo.py", *argv]),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            try:
                code = spine_demo.main()
            finally:
                self.out, self.err = out.getvalue(), err.getvalue()
        return code, self.out, self.err

    def change_set_sent(self) -> dict:
        (sent,) = (c.kwargs for c in self.cfn.create_change_set.call_args_list)
        return sent


class ConfirmationTest(SpineDemoCase):
    def test_the_plan_is_printed_before_any_write(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_main(argv=(STACK, *NODE), answer="n")
        for text in (
            "marketplace",
            "us-east-1",
            "123456789012",
            STACK,
            f"Neo4j:     {NEO4J_STACK}",
            f"Seed node: {INSTANCE} (node-1), server {SERVER_ID}",
            f"Download the backup to {SEEDS}/spine-latest.backup",
            "Create the spine database and its roles",
            "hume/stack-s/spine-config",
            "2,596,708",
            "193,038",
            "1,552,236",
        ):
            self.assertIn(text, self.out)

    def test_no_answer_or_a_closed_prompt_changes_nothing(self) -> None:
        for answer in ("n", "", "no", "maybe"):
            with self.subTest(answer=answer):
                with self.assertRaises(SystemExit) as cancelled:
                    self.run_main(argv=(STACK, *NODE), answer=answer)
                self.assertIn("Nothing was changed", str(cancelled.exception))
                self.assertEqual(self.calls, [])
                self.cfn.create_change_set.assert_not_called()
                self.copy.assert_not_called()
                self.load.assert_not_called()
                self.put.assert_not_called()

        with self.assertRaises(SystemExit):
            self.run_main(argv=(STACK, *NODE), answer=EOFError)
        self.assertEqual(self.calls, [])

    def test_yes_and_y_continue(self) -> None:
        for answer in ("y", "YES"):
            with self.subTest(answer=answer):
                code, _, _ = self.run_main(argv=(STACK, *NODE), answer=answer)
                self.assertEqual(code, 0)

    def test_the_flag_skips_the_prompt(self) -> None:
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.input.assert_not_called()
        self.assertIn("--yes was given", out)


class SettingsTest(SpineDemoCase):
    def test_the_neo4j_stack_and_instance_are_required(self) -> None:
        for argv, missing in (
            ((STACK, "--yes", "--instance-id", INSTANCE), "--neo4j-stack"),
            ((STACK, "--yes", "--neo4j-stack", NEO4J_STACK), "--instance-id"),
        ):
            with self.subTest(missing=missing):
                with self.assertRaises(SystemExit):
                    self.run_main(argv=argv)
                self.assertIn(missing, self.err)
                self.assertEqual(self.calls, [])

    def test_the_url_must_be_https(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_main(argv=(STACK, "--yes", *NODE, "--url", "http://x"))
        self.assertIn("https://", self.err)

    def test_the_node_is_checked_with_the_given_stack_instance_and_url(self) -> None:
        url = "https://bucket.example/spine.backup?sig=1"
        self.run_main(argv=(STACK, "--yes", *NODE, "--url", url))
        args = self.check_node.call_args.args
        self.assertEqual(args[1:], (NEO4J_STACK, INSTANCE, url, "node"))
        self.assertEqual(self.copy.call_args.args[3], url)
        self.stage_backup.assert_not_called()

    def test_without_url_the_staged_backup_is_used_and_not_printed(self) -> None:
        code, out, err = self.run_main()
        self.assertEqual(code, 0)
        self.stage_backup.assert_called_once()
        args = self.check_node.call_args.args
        self.assertEqual(args[1:], (NEO4J_STACK, INSTANCE, PRESIGNED, "node"))
        self.assertEqual(self.copy.call_args.args[3], PRESIGNED)
        self.assertNotIn(PRESIGNED, out + err)
        self.assertNotIn("X-Amz-Signature", out + err)

    def test_a_staging_failure_stops_the_run_before_any_write(self) -> None:
        failed = seed_nodes.StepFailed("stage", "Could not copy the Spine backup")
        code, _, err = self.run_main(stage_error=failed)
        self.assertEqual(code, 1)
        self.assertIn("FAIL at step stage: Could not copy the Spine backup", err)
        self.check_node.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_the_config_directory_must_be_set(self) -> None:
        os.environ.pop("HUME_SPINE_CONFIG_DIR")
        with self.assertRaises(SystemExit) as stopped:
            self.run_main()
        self.assertIn("HUME_SPINE_CONFIG_DIR", str(stopped.exception))

    def test_a_stack_without_a_record_exits(self) -> None:
        with self.assertRaises(SystemExit) as stopped:
            self.run_main(argv=("other", "--yes", *NODE))
        self.assertIn("No record file", str(stopped.exception))

    def test_the_load_secret_is_the_superuser_arn_of_the_neo4j_stack(self) -> None:
        self.run_main()
        self.assertEqual(self.superuser.call_args.args[1], NEO4J_STACK)
        self.assertEqual(self.load.call_args.args[3], PASSWORD_ARN)

    def test_a_bad_superuser_arn_stops_before_any_write(self) -> None:
        for value in ("", "neo4j/ee-stack/password"):
            with self.subTest(value=value):
                code, _, err = self.run_main(superuser_arn=value)
                self.assertEqual(code, 1)
                self.assertIn("step node", err)
                self.assertIn(f"/neo4j-ee/{NEO4J_STACK}/password-secret-arn", err)
                self.assertEqual(self.calls, [])


class NodeTest(SpineDemoCase):
    def test_each_node_failure_stops_before_the_plan(self) -> None:
        error = seed_nodes.StepFailed("node", "i-aaa is not online in SSM.")
        code, out, err = self.run_main(node_error=error)
        self.assertEqual(code, 1)
        self.assertIn("step node", err)
        self.assertNotIn("This will", out)
        self.assertEqual(self.calls, [])

        code, out, err = self.run_main(node_probe=probe(server="bad"))
        self.assertEqual(code, 1)
        self.assertIn("server id", err)
        self.assertNotIn("This will", out)
        self.assertEqual(self.calls, [])

        code, out, err = self.run_main(node_probe=probe(rc="28"))
        self.assertEqual(code, 1)
        self.assertIn("step copy", err)
        self.assertIn("NAT gateway", err)
        self.assertNotIn("This will", out)
        self.assertEqual(self.calls, [])

    def test_a_complete_backup_is_kept(self) -> None:
        code, out, _ = self.run_main(node_probe=probe(have=WANT))
        self.assertEqual(code, 0)
        self.copy.assert_not_called()
        self.assertIn("Keep the backup", out)
        self.assertIn("Skip: /var/lib/neo4j/seeds/spine-latest.backup is complete", out)

    def test_the_copy_and_load_go_to_the_node(self) -> None:
        self.run_main()
        self.assertEqual(
            self.copy.call_args.args,
            (self.ssm, INSTANCE, SEEDS, PRESIGNED, WANT),
        )
        self.assertEqual(
            self.load.call_args.args, (self.ssm, INSTANCE, SERVER_ID, PASSWORD_ARN)
        )


class SignInTest(SpineDemoCase):
    def test_an_expired_token_or_credential_stops_with_the_login_hint(self) -> None:
        error = TokenRetrievalError(provider="sso", error_msg="refresh failed")
        code, out, err = self.run_main(sso_error=error)
        self.assertEqual(code, 1)
        self.assertIn("aws sso login --profile marketplace", err)
        self.assertNotIn("Traceback", out + err)
        self.assertEqual(self.calls, [])

        code, _, err = self.run_main(sso_error=client_error("ExpiredToken"))
        self.assertEqual(code, 1)
        self.assertIn("aws sso login", err)

    def test_the_confirmation_line_comes_before_the_sign_in_error(self) -> None:
        error = TokenRetrievalError(provider="sso", error_msg="refresh failed")
        _, out, _ = self.run_main(sso_error=error)
        self.assertEqual(
            out.splitlines()[0], f"Confirm setting up demo in stack {STACK}"
        )

    def test_another_error_is_not_hidden(self) -> None:
        with self.assertRaises(ClientError):
            self.run_main(sso_error=client_error("AccessDenied"))


class OrderTest(SpineDemoCase):
    def test_the_steps_run_in_order(self) -> None:
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(
            self.calls, ["secret", "copy", "load", "change-set", "execute"]
        )
        self.assertIn("PASS", out)
        self.assertIn(f"The demo is set up in stack {STACK}", out)

    def test_the_secret_is_stored_with_the_stack_name(self) -> None:
        self.run_main()
        self.put.assert_called_once()
        self.assertEqual(self.put.call_args.args[1], STACK)

    def test_the_stack_name_defaults_to_the_only_record(self) -> None:
        code, out, _ = self.run_main(argv=("--yes", *NODE))
        self.assertEqual(code, 0)
        self.assertIn(f"in stack {STACK}", out.splitlines()[0])


class SkipTest(SpineDemoCase):
    def test_a_rerun_after_a_complete_setup_only_loads(self) -> None:
        code, out, _ = self.run_main(
            secret_exists=True, arn_parameter=ARN, node_probe=probe(have=WANT)
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, ["load"])
        self.put.assert_not_called()
        self.cfn.create_change_set.assert_not_called()
        self.template.assert_not_called()
        self.assertIn("Skip: hume/stack-s/spine-config exists", out)
        self.assertIn("Skip: SpineConfigSecretArn is already set", out)

    def test_an_existing_secret_is_kept_and_its_arn_is_used(self) -> None:
        self.run_main(secret_exists=True)
        self.put.assert_not_called()
        self.assertEqual(
            self.change_set_sent()["Parameters"][-1],
            {"ParameterKey": "SpineConfigSecretArn", "ParameterValue": ARN},
        )

    def test_another_arn_updates_and_modifies_the_import(self) -> None:
        code, _, _ = self.run_main(
            secret_exists=True, arn_parameter=ARN + "-old", changes=MODIFY
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, ["copy", "load", "change-set", "execute"])


class UpdateTest(SpineDemoCase):
    def test_the_update_changes_only_the_arn_parameter(self) -> None:
        self.run_main()
        parameters = self.change_set_sent()["Parameters"]
        self.assertEqual(
            [p for p in parameters if p.get("UsePreviousValue")],
            [
                {"ParameterKey": "HumeAmiId", "UsePreviousValue": True},
                {"ParameterKey": "InstanceType", "UsePreviousValue": True},
                {"ParameterKey": "EeStackName", "UsePreviousValue": True},
                {"ParameterKey": "AuthProvider", "UsePreviousValue": True},
                {"ParameterKey": "OidcIssuerUri", "UsePreviousValue": True},
                {"ParameterKey": "OidcRolesMappings", "UsePreviousValue": True},
            ],
        )
        self.assertEqual(
            [p for p in parameters if "ParameterValue" in p],
            [{"ParameterKey": "SpineConfigSecretArn", "ParameterValue": ARN}],
        )

    def test_the_change_set_is_an_update_with_the_staged_template(self) -> None:
        self.run_main()
        sent = self.change_set_sent()
        self.assertEqual(sent["StackName"], STACK)
        self.assertEqual(sent["ChangeSetType"], "UPDATE")
        self.assertEqual(sent["Capabilities"], ["CAPABILITY_IAM"])
        self.assertEqual(sent["TemplateURL"], "https://t/x.yaml")
        self.assertRegex(sent["ChangeSetName"], r"^spine-import-\d+$")

    def test_the_change_list_is_printed_and_the_change_set_executed(self) -> None:
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        for _, logical_id in ADD:
            self.assertIn(f"Add {logical_id}", out)
        sent = self.cfn.execute_change_set.call_args.kwargs
        self.assertEqual(sent["StackName"], STACK)

    def test_an_unexpected_change_stops_before_the_execute(self) -> None:
        code, out, err = self.run_main(changes=[*ADD, ("Remove", "Database")])
        self.assertEqual(code, 1)
        self.assertIn("step update", err)
        self.assertIn("Remove Database", err)
        self.assertIn("not executed", err)
        self.assertNotIn("execute", self.calls)
        self.assertIn("load", self.calls)
        self.assertNotIn("PASS", out)

    def test_an_orchestra_task_change_is_unexpected(self) -> None:
        code, _, err = self.run_main(
            changes=[*ADD, ("Modify", "OrchestraTaskDefinition")]
        )
        self.assertEqual(code, 1)
        self.assertIn("Modify OrchestraTaskDefinition", err)
        self.assertNotIn("execute", self.calls)

    def test_a_change_set_that_fails_to_create_is_not_executed(self) -> None:
        code, _, err = self.run_main(change_set_error=True)
        self.assertEqual(code, 1)
        self.assertIn("step update", err)
        self.assertIn("It was not executed", err)
        self.assertNotIn("execute", self.calls)

    def test_a_failed_update_prints_the_events_and_skips_the_counts(self) -> None:
        code, out, err = self.run_main(update_error=True)
        self.assertEqual(code, 1)
        self.assertIn("step update", err)
        self.print_failed.assert_called_once()
        self.assertNotIn("Step 5. Counts", out)

    def test_the_record_is_written_only_when_the_staging_changes_it(self) -> None:
        with mock.patch.object(deploy, "write_record") as write:
            self.run_main()
            write.assert_not_called()

        def stage(session, stack, record):
            record["TemplateObjectKey"] = "hume-ecs/stack-s.yaml"
            return {"TemplateURL": "https://t/x.yaml"}

        with mock.patch.object(deploy, "write_record") as write:
            self.run_main(stage=stage)
            write.assert_called_once()

    def test_a_stack_in_progress_is_not_updated(self) -> None:
        code, out, err = self.run_main(status="UPDATE_IN_PROGRESS")
        self.assertEqual(code, 1)
        self.assertIn("step stack", err)
        self.assertNotIn("This will", out)

    def test_a_stack_that_rolled_back_can_be_updated(self) -> None:
        code, _, _ = self.run_main(status="UPDATE_ROLLBACK_COMPLETE")
        self.assertEqual(code, 0)


class LoadTest(SpineDemoCase):
    def test_a_failed_load_stops_with_its_step(self) -> None:
        error = seed_nodes.StepFailed("load", "spine status 'offline', node count 0")
        code, out, err = self.run_main(load_error=error)
        self.assertEqual(code, 1)
        self.assertIn("step load", err)
        self.assertIn("run the same command again", err)
        self.assertNotIn("PASS", out)

    def test_a_failed_load_does_not_run_the_update(self) -> None:
        self.run_main(load_error=seed_nodes.StepFailed("load", "x"))
        self.cfn.create_change_set.assert_not_called()
        self.assertEqual(self.calls[-1], "load")

    def test_the_load_output_is_still_shown(self) -> None:
        _, out, _ = self.run_main()
        self.assertIn("spine-nodes=2596708", out)

    def test_counts_that_differ_warn_and_do_not_fail_the_run(self) -> None:
        lines = (*LOAD_LINES[:1], "spine-nodes=5", *LOAD_LINES[2:])
        code, out, _ = self.run_main(load_lines=lines)
        self.assertEqual(code, 0)
        self.assertIn("WARNING", out)
        self.assertIn("nodes: got 5, expected 2596708", out)

    def test_missing_counts_fail_the_counts(self) -> None:
        lines = (*LOAD_LINES[:3], "spine-relationships=")
        code, _, err = self.run_main(load_lines=lines)
        self.assertEqual(code, 1)
        self.assertIn("step counts", err)


class SecretSafetyTest(SpineDemoCase):
    def test_the_arn_is_not_a_secret_but_no_value_is_read(self) -> None:
        self.run_main(secret_exists=True)
        self.sm.get_secret_value.assert_not_called()
        self.sm.put_secret_value.assert_not_called()
        self.sm.create_secret.assert_not_called()

    def test_nothing_is_deleted(self) -> None:
        self.run_main()
        for client in (self.cfn, self.sm):
            names = [name for name in dir(client) if name.startswith("delete")]
            for name in names:
                getattr(client, name).assert_not_called()


if __name__ == "__main__":
    unittest.main()


class RolesMappingTest(SpineDemoCase):
    """Step 4 also maps the Spine groups to the Hume roles of the SPINE graph."""

    def sent_value(self, key: str) -> str | None:
        values = {
            p["ParameterKey"]: p["ParameterValue"]
            for p in self.change_set_sent()["Parameters"]
            if "ParameterValue" in p
        }
        return values.get(key)

    def test_the_spine_pairs_are_added_to_the_default_mapping(self) -> None:
        code, out, _ = self.run_main(
            roles_mappings=DEFAULT_MAPPINGS, changes=[*ADD, *ROLES]
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.sent_value("OidcRolesMappings"), FULL_MAPPINGS)
        self.assertEqual(self.sent_value("SpineConfigSecretArn"), ARN)
        self.assertIn(
            "4. Update the stack with a change set: SpineConfigSecretArn, OidcRolesMappings.",
            out,
        )
        self.assertIn(f"OidcRolesMappings becomes {FULL_MAPPINGS}", out)
        self.assertIn("api and media services restart", out)

    def test_a_mapping_that_grants_the_spine_roles_is_kept(self) -> None:
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIsNone(self.sent_value("OidcRolesMappings"))
        self.assertIn(
            "4. Update the stack with a change set: SpineConfigSecretArn.", out
        )

    def test_a_role_granted_by_another_group_is_not_mapped_again(self) -> None:
        self.run_main(
            roles_mappings="hume-admins=ADMINISTRATOR,analysts=SPINE_ANALYST",
            changes=[*ADD, *ROLES],
        )
        self.assertEqual(
            self.sent_value("OidcRolesMappings"),
            "hume-admins=ADMINISTRATOR,analysts=SPINE_ANALYST,"
            "spine-officers=SPINE_OFFICER",
        )

    def test_only_the_mapping_changes_when_the_import_is_already_set(self) -> None:
        code, out, _ = self.run_main(
            secret_exists=True,
            arn_parameter=ARN,
            roles_mappings=DEFAULT_MAPPINGS,
            changes=ROLES,
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, ["copy", "load", "change-set", "execute"])
        self.assertEqual(self.sent_value("OidcRolesMappings"), FULL_MAPPINGS)
        self.assertIsNone(self.sent_value("SpineConfigSecretArn"))
        self.assertIn("4. Update the stack with a change set: OidcRolesMappings.", out)

    def test_a_complete_setup_with_the_mapping_is_skipped(self) -> None:
        code, out, _ = self.run_main(secret_exists=True, arn_parameter=ARN)
        self.assertEqual(code, 0)
        self.cfn.create_change_set.assert_not_called()
        self.assertIn("4. Keep the stack as it is", out)
        self.assertIn("the Spine roles are mapped", out)

    def test_a_native_stack_keeps_its_mapping(self) -> None:
        code, out, _ = self.run_main(
            auth_provider="Native", roles_mappings=DEFAULT_MAPPINGS
        )
        self.assertEqual(code, 0)
        self.assertIsNone(self.sent_value("OidcRolesMappings"))
        self.assertIn("signs users in natively", out)
        self.assertIn("Skip: the stack signs users in natively", out)
        self.idp.admin_initiate_auth.assert_not_called()

    def test_a_task_change_is_unexpected_when_the_mapping_does_not_change(self) -> None:
        code, _, err = self.run_main(changes=[*ADD, *ROLES])
        self.assertEqual(code, 1)
        self.assertIn("Modify ApiTaskDefinition", err)
        self.assertNotIn("execute", self.calls)

    def test_spine_roles_mappings(self) -> None:
        cases = {
            DEFAULT_MAPPINGS: FULL_MAPPINGS,
            FULL_MAPPINGS: None,
            f"{SPINE_PAIRS},hume-users=USER": None,
            "g=SPINE_OFFICER": "g=SPINE_OFFICER,spine-analysts=SPINE_ANALYST",
            "": SPINE_PAIRS,
        }
        for current, wanted in cases.items():
            with self.subTest(current=current):
                self.assertEqual(spine_demo.spine_roles_mappings(current), wanted)


class DemoSignInTest(SpineDemoCase):
    """Step 6 signs in as spine-analyst through the Cognito test pool."""

    def test_without_a_cognito_record_the_check_is_skipped(self) -> None:
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("Step 6. Sign-in as spine-analyst", out)
        self.assertIn("Skip: no Cognito record for hume-test-idp", out)
        self.idp.admin_initiate_auth.assert_not_called()
        self.fetch.assert_not_called()

    def test_another_issuer_skips_the_check(self) -> None:
        record = {**COGNITO_RECORD, "Issuer": "https://other.example/realm"}
        code, out, _ = self.run_main(cognito_record=record)
        self.assertEqual(code, 0)
        self.assertIn("Skip: the stack uses another provider", out)
        self.idp.admin_initiate_auth.assert_not_called()

    def test_the_demo_user_signs_in_and_has_the_role(self) -> None:
        code, out, _ = self.run_main(cognito_record=COGNITO_RECORD)
        self.assertEqual(code, 0)
        self.sm.get_secret_value.assert_called_once_with(
            SecretId="hume/hume-test-idp/cognito/spine-analyst"
        )
        self.idp.admin_initiate_auth.assert_called_once_with(
            UserPoolId="us-east-1_abc",
            ClientId="hume-id",
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": "spine-analyst", "PASSWORD": PASSWORD},
        )
        self.fetch.assert_called_once_with(ME_URL, "tok-access")
        self.assertIn("PASS: spine-analyst has the roles USER, SPINE_ANALYST", out)
        self.assertIn(
            "Sign in to Hume as a user in a Spine group, such as spine-analyst", out
        )

    def test_the_role_may_appear_on_the_second_request(self) -> None:
        code, out, _ = self.run_main(
            cognito_record=COGNITO_RECORD,
            replies=(me("USER"), me("USER", "SPINE_ANALYST")),
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.fetch.call_count, 2)
        self.assertIn("PASS: spine-analyst", out)

    def test_a_missing_role_fails_the_run(self) -> None:
        code, out, err = self.run_main(
            cognito_record=COGNITO_RECORD, replies=(me("USER"), me("USER"))
        )
        self.assertEqual(code, 1)
        self.assertIn("FAIL at step sign-in", err)
        self.assertIn("has the roles USER and not SPINE_ANALYST", err)
        self.assertIn("OidcRolesMappings", err)
        self.assertIn("PASS: 2,596,708 nodes", out)

    def test_a_rejected_token_fails_the_run(self) -> None:
        code, _, err = self.run_main(
            cognito_record=COGNITO_RECORD, replies=(me(status=401), me(status=401))
        )
        self.assertEqual(code, 1)
        self.assertIn("FAIL at step sign-in", err)
        self.assertIn("/users/me as spine-analyst returned 401", err)

    def test_the_password_and_the_token_are_never_printed(self) -> None:
        for replies in (ANALYST_REPLIES, (me("USER"), me("USER"))):
            _, out, err = self.run_main(cognito_record=COGNITO_RECORD, replies=replies)
            for secret in (PASSWORD, "tok-access", "tok-id"):
                self.assertNotIn(secret, out + err)
