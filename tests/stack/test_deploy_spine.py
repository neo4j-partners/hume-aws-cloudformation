"""Offline tests for the Spine demo in deploy.py: the config files and secret.

The deploy helpers run against temp folders and a mocked AWS session. No AWS
call is made.

Run from the repository root:
    uv run --with boto3 --with python-dotenv --with pyyaml \
        python -m unittest tests.stack.test_deploy_spine
"""

import argparse
import base64
import contextlib
import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import deploy
from tests.helpers.env import TempEnvCase

SPINE_ARN = (
    "arn:aws:secretsmanager:us-east-1:123456789012:secret:"
    "hume/stack-x/spine-config-AbCdEf"
)
REPO_SPINE = Path(__file__).resolve().parents[2] / "demo/spine/hume-config"


class SpineConfigRepoTest(TempEnvCase):
    def test_a_relative_path_is_read_from_the_repository_root(self) -> None:
        repo = Path(deploy.__file__).resolve().parent.parent
        with (
            mock.patch.dict(
                os.environ, {deploy.SPINE_CONFIG_DIR_VAR: "demo/spine/hume-config"}
            ),
            mock.patch.object(deploy, "SCRIPT_DIR", repo / "stack"),
            contextlib.chdir(self.dir),
        ):
            got = deploy.resolve_spine_config_dir()
        self.assertEqual(got, repo / "demo/spine/hume-config")

    def test_the_error_names_the_copy_in_the_repository(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            deploy.resolve_spine_config_dir()
        self.assertIn("demo/spine/hume-config", str(raised.exception))
        self.assertNotIn("quickstart", str(raised.exception))

    def test_the_service_account_resource_holds_the_vault_placeholder(self) -> None:
        text = (REPO_SPINE / "resources/spine-neo4j-sa.yml").read_text()
        self.assertIn("    value: vault:aws:NEO4J_APP_SECRET_NAME\n", text)
        self.assertNotIn("SPINE_NEO4J_SA_PASSWORD", text)

    def test_the_copy_in_the_repository_fits_in_the_secret(self) -> None:
        root = Path(deploy.__file__).resolve().parent.parent / "demo/spine/hume-config"
        self.assertTrue((root / deploy.SPINE_REQUIRED_FILE).is_file())
        text = deploy.build_spine_config(root)
        self.assertLessEqual(len(text), deploy.SPINE_SECRET_LIMIT)
        for relative in deploy.SPINE_SKIPPED_FILES:
            self.assertNotIn(Path(relative), deploy.spine_config_files(root))


SPINE_FILES = {
    "spine-kg/knowledge-graph.yml": "kg: spine\n",
    "spine-kg/actions/keep.yml": "name: keep\n",
    "spine-kg/actions/get-ccr-records-graph.yml": "skip\n",
    "spine-kg/workflows/ccr-records-import.yml": "skip\n",
    "resources/spine-neo4j-sa.yml": "resource: sa\n",
    "resources/webhook-server.yml": "skip\n",
    "resources/spine-http-api.yml": "skip\n",
    "resources/spine-analytic-neo4j-sa.yml": "skip\n",
    ".DS_Store": "hidden\n",
    "spine-kg/.hidden/file.yml": "hidden\n",
}


class SpineConfigFilesTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for relative, content in SPINE_FILES.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def unpack(self, text: str) -> dict[str, str]:
        raw = base64.b64decode(text)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:bz2") as archive:
            return {
                m.name: archive.extractfile(m).read().decode()
                for m in archive.getmembers()
                if m.isfile()
            }

    def test_skipped_and_hidden_files_are_left_out(self) -> None:
        got = [p.as_posix() for p in deploy.spine_config_files(self.root)]
        self.assertEqual(
            got,
            [
                "resources/spine-neo4j-sa.yml",
                "spine-kg/actions/keep.yml",
                "spine-kg/knowledge-graph.yml",
            ],
        )

    def test_the_archive_holds_the_files_with_their_content(self) -> None:
        got = self.unpack(deploy.build_spine_config(self.root))
        self.assertEqual(
            got,
            {
                "resources/spine-neo4j-sa.yml": "resource: sa\n",
                "spine-kg/actions/keep.yml": "name: keep\n",
                "spine-kg/knowledge-graph.yml": "kg: spine\n",
            },
        )

    def test_the_archive_has_no_owner_or_time(self) -> None:
        raw = base64.b64decode(deploy.build_spine_config(self.root))
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:bz2") as archive:
            for member in archive.getmembers():
                self.assertEqual(
                    (member.uid, member.gid, member.mtime, member.uname, member.gname),
                    (0, 0, 0, "", ""),
                )

    def test_the_same_files_give_the_same_text(self) -> None:
        first = deploy.build_spine_config(self.root)
        for path in self.root.rglob("*.yml"):
            path.touch()
        self.assertEqual(deploy.build_spine_config(self.root), first)

    def test_a_text_over_the_limit_exits_with_the_size(self) -> None:
        with (
            mock.patch.object(deploy, "SPINE_SECRET_LIMIT", 10),
            self.assertRaises(SystemExit) as raised,
        ):
            deploy.build_spine_config(self.root)
        self.assertIn("The limit is 10", str(raised.exception))


class SpineConfigDirTest(TempEnvCase):
    def test_unset_exits_naming_the_setting(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            deploy.resolve_spine_config_dir()
        self.assertIn(deploy.SPINE_CONFIG_DIR_VAR, str(raised.exception))

    def test_a_directory_without_the_knowledge_graph_exits(self) -> None:
        with (
            mock.patch.dict(os.environ, {deploy.SPINE_CONFIG_DIR_VAR: str(self.dir)}),
            self.assertRaises(SystemExit) as raised,
        ):
            deploy.resolve_spine_config_dir()
        self.assertIn("knowledge-graph.yml", str(raised.exception))

    def test_a_good_directory_is_returned(self) -> None:
        kg = self.dir / deploy.SPINE_REQUIRED_FILE
        kg.parent.mkdir(parents=True)
        kg.write_text("kg\n")
        with mock.patch.dict(os.environ, {deploy.SPINE_CONFIG_DIR_VAR: str(self.dir)}):
            self.assertEqual(deploy.resolve_spine_config_dir(), self.dir)

    def test_the_dotenv_file_is_read(self) -> None:
        kg = self.dir / deploy.SPINE_REQUIRED_FILE
        kg.parent.mkdir(parents=True)
        kg.write_text("kg\n")
        self.env_file.write_text(f"{deploy.SPINE_CONFIG_DIR_VAR}={self.dir}\n")
        self.assertEqual(deploy.resolve_spine_config_dir(), self.dir)


class SpineConfigSecretTest(TempEnvCase):
    def setUp(self) -> None:
        super().setUp()
        kg = self.dir / "spine" / deploy.SPINE_REQUIRED_FILE
        kg.parent.mkdir(parents=True)
        kg.write_text("kg\n")
        patch = mock.patch.dict(
            os.environ, {deploy.SPINE_CONFIG_DIR_VAR: str(self.dir / "spine")}
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.sm = mock.MagicMock()
        self.sm.exceptions.ResourceNotFoundException = KeyError
        self.sm.describe_secret.return_value = {"ARN": SPINE_ARN}
        self.session = mock.MagicMock()
        self.session.client.side_effect = {"secretsmanager": self.sm}.__getitem__

    def test_put_stores_the_text_under_the_stack_name_without_printing_it(
        self,
    ) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            arn = deploy.put_spine_config(self.session, "stack-s")
        self.assertEqual(arn, SPINE_ARN)
        self.sm.describe_secret.assert_called_once_with(
            SecretId="hume/stack-s/spine-config"
        )
        kwargs = self.sm.put_secret_value.call_args.kwargs
        self.assertEqual(kwargs["SecretId"], "hume/stack-s/spine-config")
        value = kwargs["SecretString"]
        self.assertEqual(value, deploy.build_spine_config(self.dir / "spine"))
        self.assertNotIn(value, out.getvalue())
        self.assertIn(f"{len(value)} bytes", out.getvalue())

    def test_put_creates_the_secret_when_it_is_new(self) -> None:
        self.sm.put_secret_value.side_effect = KeyError
        with contextlib.redirect_stdout(io.StringIO()):
            deploy.put_spine_config(self.session, "stack-s")
        kwargs = self.sm.create_secret.call_args.kwargs
        self.assertEqual(kwargs["Name"], "hume/stack-s/spine-config")

    def test_the_command_reads_the_record_and_stores_the_secret(self) -> None:
        deploy.write_record("stack-s", {"Region": "us-east-1", "Profile": "p"})
        args = argparse.Namespace(stack_name="stack-s", profile=None, region=None)
        out = io.StringIO()
        with (
            mock.patch.object(deploy, "make_session", return_value=self.session),
            contextlib.redirect_stdout(out),
        ):
            self.assertEqual(deploy.cmd_spine_config(args), 0)
        self.assertIn(f"SpineConfigSecretArn={SPINE_ARN}", out.getvalue())
        self.assertEqual(
            self.sm.put_secret_value.call_args.kwargs["SecretId"],
            "hume/stack-s/spine-config",
        )

    def test_the_command_exits_when_the_stack_has_no_record(self) -> None:
        args = argparse.Namespace(stack_name="stack-none", profile=None, region=None)
        with self.assertRaises(SystemExit):
            deploy.cmd_spine_config(args)
        self.sm.put_secret_value.assert_not_called()


class DeployStackSpineTest(TempEnvCase):
    def run_deploy(self) -> mock.MagicMock:
        cfn = mock.MagicMock()
        cfn.create_stack.return_value = {"StackId": "id-1"}
        cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}
        session = mock.MagicMock()
        session.client.side_effect = {"cloudformation": cfn}.__getitem__
        settings = deploy.Settings(
            "marketplace", "us-east-1", "ami-1", ["203.0.113.1/32"], "m6i.xlarge",
            "hume.example.com", "Z0123456789ABC",
        )  # fmt: skip
        put = mock.MagicMock(return_value=SPINE_ARN)
        with (
            mock.patch.object(deploy.boto3, "Session", return_value=session),
            mock.patch.object(deploy, "licence_secret_arn", return_value="arn:l"),
            mock.patch.object(deploy, "template_arguments", return_value={}),
            mock.patch.object(deploy, "put_spine_config", put),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(deploy.deploy_stack(settings, "stack-d"), 0)
        self.cfn = cfn
        return put

    def test_no_secret_is_stored_at_create(self) -> None:
        put = self.run_deploy()
        put.assert_not_called()
        self.cfn.create_stack.assert_called_once()

    def test_the_arn_is_never_passed_at_create(self) -> None:
        self.run_deploy()
        keys = [p["ParameterKey"] for p in self.parameters()]
        self.assertNotIn("SpineConfigSecretArn", keys)

    def parameters(self) -> list[dict]:
        return self.cfn.create_stack.call_args.kwargs["Parameters"]


if __name__ == "__main__":
    unittest.main()
