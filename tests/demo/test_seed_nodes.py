"""Offline tests for seed_nodes.py, the finder, copier and loader for the nodes.

Every AWS client is a mock, so no AWS call is made. The probe and copy scripts
run in a local bash against a temporary directory, with an unreachable address
standing in for the missing route. The load script runs in a local bash with
stand-ins for aws and cypher-shell at paths given to load_script.

Run from the repository root:
    uv run --with boto3 --with python-dotenv --with pyyaml \
        python -m unittest tests.demo.test_seed_nodes
"""

import argparse
import base64
import contextlib
import io
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from boto3.exceptions import Boto3Error
from botocore.exceptions import ClientError

import seed_nodes
from seed_nodes import StepFailed

STACK = "ee-stack"
WANT = 614_894_162
BUCKET = "hume-templates-123456789012-us-east-1"
PRESIGNED = f"https://{BUCKET}.s3.us-east-1.amazonaws.com/spine/spine-latest.backup?X-Amz-Signature=abc"
FREE = 35_000_000_000
SERVER_ID = "8f3a1c52-6b7d-4e90-a1b2-c3d4e5f60718"
SEEDS = "/var/lib/neo4j/seeds"
PASSWORD_ARN = "arn:aws:secretsmanager:eu-west-2:123456789012:secret:neo4j/ee-stack/password-AbCdEf"
INSTANCES = [
    {"id": "i-bbb", "name": "node-2", "zone": "us-east-1b", "ip": "10.0.2.5"},
    {"id": "i-aaa", "name": "node-1", "zone": "us-east-1a", "ip": "10.0.1.5"},
]


def probe_text(
    have: int = 0,
    want: int = WANT,
    free: int = FREE,
    rc: int = 0,
    status: str = "200",
    node: bool = True,
) -> str:
    lines = [f"neo4j_conf={'yes' if node else 'no'}"]
    if node:
        lines += [
            "home=/var/lib/neo4j",
            f"seeds_dir={SEEDS}",
            "seeds_exists=yes",
            "providers=ServerSeedProvider",
            f"have={have}",
            f"server_id={SERVER_ID}",
            f"free={free}",
            "neo4j_active=active",
            f"https_rc={rc}",
            f"https_status={status if rc == 0 else ''}",
            f"want={want if rc == 0 else ''}",
        ]
    return "\n".join(lines) + "\n"


def args(command: str = "discover", **extra) -> argparse.Namespace:
    values = {
        "profile": None,
        "region": None,
        "command": command,
        "neo4j_stack": STACK,
        "url": seed_nodes.BACKUP_URL,
        "instance_id": None,
        "seeds_dir": None,
        "password_secret_arn": None,
        "yes": True,
    }
    values.update(extra)
    return argparse.Namespace(**values)


class FakeSsm:
    def __init__(self, online: list[str]):
        self.online = online
        self.parameters = {f"/neo4j-ee/{STACK}/password-secret-arn": PASSWORD_ARN}
        self.parameter_names: list[str] = []

    def get_parameter(self, Name):
        self.parameter_names.append(Name)
        if Name not in self.parameters:
            raise ClientError(
                {"Error": {"Code": "ParameterNotFound", "Message": "x"}},
                "GetParameter",
            )
        return {"Parameter": {"Value": self.parameters[Name]}}

    def describe_instance_information(self, Filters):
        wanted = Filters[0]["Values"]
        return {
            "InstanceInformationList": [
                {"InstanceId": i, "PingStatus": "Online"}
                for i in self.online
                if i in wanted
            ]
        }


class SeedNodesCase(unittest.TestCase):
    def setUp(self) -> None:
        self.ssm = FakeSsm(["i-aaa", "i-bbb"])
        self.session = mock.Mock()
        self.session.client.return_value = self.ssm
        self.scripts: list[tuple[str, str]] = []
        self.outputs: list[str] = []
        patches = [
            mock.patch.object(
                seed_nodes.deploy, "cli_session", return_value=self.session
            ),
            mock.patch.object(seed_nodes, "check_sso"),
            mock.patch.object(
                seed_nodes, "list_instances", side_effect=lambda s, stack: INSTANCES
            ),
            mock.patch.object(seed_nodes, "run_script", side_effect=self.fake_run),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def fake_run(self, ssm, instance, script, timeout, step):
        self.scripts.append((instance, script))
        return self.outputs.pop(0)

    def run_command(self, function, namespace) -> tuple[int | None, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = function(namespace)
        return code, out.getvalue()


class ParseTest(unittest.TestCase):
    def test_parse_probe_reads_pairs_and_ignores_other_lines(self) -> None:
        probe = seed_nodes.parse_probe("home=/x\nnoise\nserver_id=\nk=a=b\n")
        self.assertEqual(probe, {"home": "/x", "server_id": "", "k": "a=b"})

    def test_reaches_backup_needs_curl_success_and_a_status(self) -> None:
        good = seed_nodes.parse_probe(probe_text())
        self.assertTrue(seed_nodes.reaches_backup(good))
        partial = seed_nodes.parse_probe(probe_text(status="206"))
        self.assertTrue(seed_nodes.reaches_backup(partial))
        forbidden = seed_nodes.parse_probe(probe_text(status="403"))
        self.assertTrue(seed_nodes.reaches_backup(forbidden))
        missing = seed_nodes.parse_probe(probe_text(status="404"))
        self.assertFalse(seed_nodes.reaches_backup(missing))
        timeout = seed_nodes.parse_probe(probe_text(rc=28))
        self.assertFalse(seed_nodes.reaches_backup(timeout))


class ScriptTest(unittest.TestCase):
    WRITING = re.compile(
        r"(^|[\s;|&])(rm|mv|cp|chmod|chown|install|tee|systemctl\s+(start|stop|restart)|"
        r"cypher-shell|neo4j-admin|dd|mkdir|touch)\b|[^>]>[^>&]|>>"
    )

    def test_probe_script_has_no_writing_command(self) -> None:
        script = seed_nodes.probe_script()
        stripped = re.sub(r"\d?>\s*/dev/null|2>&1|<\s*\"?\$\w+/?\S*", "", script)
        self.assertIsNone(self.WRITING.search(stripped), self.WRITING.search(stripped))
        self.assertIn('curl -s -m 15 -r 0-0 -D - -o /dev/null "$url"', script)

    def test_probe_script_quotes_the_url(self) -> None:
        script = seed_nodes.probe_script("https://x.example/a b?c=1&d=2")
        self.assertIn("url='https://x.example/a b?c=1&d=2'", script)

    def test_copy_script_downloads_to_part_then_moves(self) -> None:
        script = seed_nodes.copy_script(SEEDS)
        lines = script.splitlines()
        part = next(
            i
            for i, l in enumerate(lines)
            if "spine-latest.backup.part" in l and "curl" in l
        )
        move = next(i for i, l in enumerate(lines) if l.startswith("mv "))
        self.assertLess(part, move)
        self.assertIn("set -eu", script)
        self.assertNotIn("rm ", script)

    def test_probe_runs_locally_and_reports_the_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "neo4j"
            (home / "data").mkdir(parents=True)
            (home / "seeds").mkdir()
            id_bytes = bytes([1]) + bytes.fromhex(SERVER_ID.replace("-", ""))
            (home / "data" / "server_id").write_bytes(id_bytes)
            (home / "seeds" / seed_nodes.BACKUP_NAME).write_bytes(b"x" * 10)
            conf = Path(tmp) / "neo4j.conf"
            conf.write_text(
                f"server.directories.neo4j_home={home}\n"
                "dbms.databases.seed_from_uri_providers=ServerSeedProvider\n"
            )
            script = seed_nodes.probe_script("http://127.0.0.1:1/none", str(conf))
            done = subprocess.run(
                ["bash", "-c", script],
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        probe = seed_nodes.parse_probe(done.stdout)
        self.assertEqual(probe["neo4j_conf"], "yes")
        self.assertEqual(probe["seeds_dir"], f"{home}/seeds")
        self.assertEqual(probe["seeds_exists"], "yes")
        self.assertEqual(probe["have"], "10")
        self.assertEqual(probe["server_id"], SERVER_ID)
        self.assertEqual(probe["providers"], "ServerSeedProvider")
        self.assertGreater(int(probe["free"]), 0)
        self.assertNotEqual(probe["https_rc"], "0")
        self.assertFalse(seed_nodes.reaches_backup(probe))

    def test_probe_reads_the_size_from_content_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "curl"
            stub.write_text(
                "#!/bin/sh\n"
                "printf 'HTTP/1.1 206 Partial Content\\r\\n"
                f"Content-Range: bytes 0-0/{WANT}\\r\\n"
                "Content-Length: 1\\r\\n\\r\\n'\n"
            )
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
            script = seed_nodes.probe_script(PRESIGNED, "/nonexistent.conf")
            env = os.environ | {"PATH": f"{tmp}:{os.environ['PATH']}"}
            done = subprocess.run(
                ["bash", "-c", script],
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )
        probe = seed_nodes.parse_probe(done.stdout)
        self.assertEqual(probe.get("https_rc"), "0", done.stdout + done.stderr)
        self.assertEqual(probe.get("https_status"), "206")
        self.assertEqual(probe.get("want"), str(WANT))
        self.assertTrue(seed_nodes.reaches_backup(probe))

    def test_probe_runs_locally_on_a_host_without_neo4j(self) -> None:
        script = seed_nodes.probe_script("http://127.0.0.1:1/none", "/nonexistent.conf")
        done = subprocess.run(
            ["bash", "-c", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        probe = seed_nodes.parse_probe(done.stdout)
        self.assertFalse(seed_nodes.is_node(probe))

    def test_copy_runs_locally_and_leaves_no_part_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.backup"
            source.write_bytes(b"backup bytes")
            seeds = Path(tmp) / "neo4j" / "seeds"
            seeds.parent.mkdir()
            script = seed_nodes.copy_script(str(seeds), source.as_uri())
            done = subprocess.run(
                ["bash", "-c", script],
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(
                (seeds / seed_nodes.BACKUP_NAME).read_bytes(), b"backup bytes"
            )
            self.assertEqual(
                [p.name for p in seeds.iterdir()], [seed_nodes.BACKUP_NAME]
            )

    def test_copy_failure_leaves_no_final_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seeds = Path(tmp) / "neo4j" / "seeds"
            seeds.parent.mkdir()
            script = seed_nodes.copy_script(str(seeds), "http://127.0.0.1:1/none")
            done = subprocess.run(
                ["bash", "-c", script],
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertNotEqual(done.returncode, 0)
            self.assertFalse((seeds / seed_nodes.BACKUP_NAME).exists())


class DiscoverTest(SeedNodesCase):
    def test_discover_probes_each_online_instance_and_prints_next_steps(self) -> None:
        self.outputs = [probe_text(), probe_text(node=False)]
        code, out = self.run_command(seed_nodes.discover, args())
        self.assertEqual(code, 0)
        self.assertEqual([i for i, _ in self.scripts], ["i-bbb", "i-aaa"])
        self.assertIn(SERVER_ID, out)
        self.assertIn("not a Neo4j node", out)
        self.assertIn("outbound https:  yes", out)
        self.assertIn(
            f"./demo/spine/seed_nodes.py copy {STACK} --instance-id i-bbb", out
        )
        self.assertIn(
            f"./demo/spine/seed_nodes.py load {STACK} --instance-id i-bbb", out
        )
        self.assertIn(
            f"./demo/spine/spine_demo.py <hume stack> --neo4j-stack {STACK} "
            "--instance-id i-bbb",
            out,
        )
        self.assertNotIn("--seed-server-id", out)

    def test_an_invalid_server_id_is_neither_accepted_nor_printed(self) -> None:
        bad = probe_text().replace(SERVER_ID, "not-a-server-id")
        self.outputs = [bad, bad]
        code, out = self.run_command(seed_nodes.discover, args())
        self.assertEqual(code, 0)
        self.assertIn("server id:       not found or not valid", out)
        self.assertNotIn("not-a-server-id", out)

        for good in (SERVER_ID,):
            self.assertEqual(seed_nodes.server_id({"server_id": good}), good)
        for bad in ("", "abc", SERVER_ID.upper(), SERVER_ID + "0", "x" * 36):
            with self.subTest(bad=bad):
                self.assertEqual(seed_nodes.server_id({"server_id": bad}), "")
        self.assertEqual(seed_nodes.server_id({}), "")

    def test_discover_reports_what_copy_needs_when_https_fails(self) -> None:
        self.outputs = [probe_text(rc=28), probe_text(rc=28)]
        code, out = self.run_command(seed_nodes.discover, args())
        self.assertEqual(code, 0)
        self.assertIn("outbound https:  NO", out)
        self.assertIn("NAT gateway", out)
        self.assertIn("presigned", out)

    def test_discover_skips_an_instance_that_is_not_online_in_ssm(self) -> None:
        self.ssm.online = ["i-aaa"]
        self.outputs = [probe_text()]
        code, out = self.run_command(seed_nodes.discover, args())
        self.assertEqual(code, 0)
        self.assertEqual([i for i, _ in self.scripts], ["i-aaa"])
        self.assertIn("not online in SSM", out)

    def test_discover_fails_when_no_instance_is_a_node(self) -> None:
        self.outputs = [probe_text(node=False), probe_text(node=False)]
        with self.assertRaises(StepFailed):
            self.run_command(seed_nodes.discover, args())

    def test_discover_filters_by_instance_id(self) -> None:
        self.outputs = [probe_text()]
        self.run_command(seed_nodes.discover, args(instance_id="i-aaa"))
        self.assertEqual([i for i, _ in self.scripts], ["i-aaa"])


class FakeS3:
    def __init__(self, head_code: str | None = None, copy_error=None):
        self.head_code = head_code
        self.copy_error = copy_error
        self.heads: list[tuple[str, str]] = []
        self.copies: list[dict] = []
        self.presigns: list[tuple[str, dict, int]] = []

    def head_object(self, Bucket, Key):
        self.heads.append((Bucket, Key))
        if self.head_code:
            raise ClientError(
                {"Error": {"Code": self.head_code, "Message": "x"}}, "HeadObject"
            )
        return {"ContentLength": WANT}

    def copy(self, **kwargs):
        self.copies.append(kwargs)
        if self.copy_error:
            raise self.copy_error

    def generate_presigned_url(self, method, Params, ExpiresIn):
        self.presigns.append((method, Params, ExpiresIn))
        return PRESIGNED


class StageTest(unittest.TestCase):
    STAGED = f"s3://{BUCKET}/{seed_nodes.STAGED_KEY}"

    def stage(self, s3: FakeS3) -> tuple[str, str]:
        self.source = mock.Mock(name="source")
        self.client_calls: list[tuple[str, dict]] = []

        def client(name, **kwargs):
            self.client_calls.append((name, kwargs))
            if kwargs.get("region_name") == seed_nodes.BACKUP_SOURCE_REGION:
                return self.source
            return s3

        session = mock.Mock()
        session.client.side_effect = client
        out = io.StringIO()
        with (
            mock.patch.object(
                seed_nodes.deploy, "ensure_template_bucket", return_value=BUCKET
            ),
            contextlib.redirect_stdout(out),
        ):
            url = seed_nodes.stage_backup(session)
        return url, out.getvalue()

    def test_a_staged_backup_is_presigned_for_an_hour_without_a_copy(self) -> None:
        s3 = FakeS3()
        url, out = self.stage(s3)
        self.assertEqual(url, PRESIGNED)
        self.assertEqual(s3.heads, [(BUCKET, seed_nodes.STAGED_KEY)])
        self.assertEqual(s3.copies, [])
        self.assertEqual(
            s3.presigns,
            [("get_object", {"Bucket": BUCKET, "Key": seed_nodes.STAGED_KEY}, 3600)],
        )
        self.assertEqual(out, f"Using the staged Spine backup {self.STAGED}\n")

    def test_the_client_signs_with_v4_on_the_regional_host(self) -> None:
        self.stage(FakeS3())
        name, kwargs = self.client_calls[0]
        self.assertEqual(name, "s3")
        config = kwargs["config"]
        self.assertEqual(config.signature_version, "s3v4")
        self.assertEqual(config.s3, {"us_east_1_regional_endpoint": "regional"})

    def test_a_missing_backup_is_copied_from_the_source_then_presigned(self) -> None:
        for code in ("404", "NoSuchKey", "NotFound"):
            with self.subTest(code=code):
                s3 = FakeS3(head_code=code)
                url, out = self.stage(s3)
                self.assertEqual(url, PRESIGNED)
                self.assertEqual(
                    s3.copies,
                    [
                        {
                            "CopySource": {
                                "Bucket": "spine.dev.graphaware.io",
                                "Key": "spine-neo4j-backups/spine-latest.backup",
                            },
                            "Bucket": BUCKET,
                            "Key": "spine/spine-latest.backup",
                            "SourceClient": self.source,
                        }
                    ],
                )
                self.assertIn(("s3", {"region_name": "eu-west-1"}), self.client_calls)
                self.assertEqual(
                    out.splitlines(),
                    [
                        (
                            f"Staging the Spine backup to {self.STAGED}, about "
                            "600 MB, once per account and region"
                        ),
                        f"Staged the Spine backup to {self.STAGED}",
                    ],
                )
                self.assertEqual(len(s3.presigns), 1)

    def test_another_head_error_stops_the_stage_step(self) -> None:
        s3 = FakeS3(head_code="AccessDenied")
        with self.assertRaises(StepFailed) as raised:
            self.stage(s3)
        self.assertEqual(raised.exception.step, "stage")
        self.assertIn(BUCKET, str(raised.exception))
        self.assertIn("AccessDenied", str(raised.exception))
        self.assertEqual(s3.copies, [])
        self.assertEqual(s3.presigns, [])

    def test_a_failed_copy_explains_the_partition_limit(self) -> None:
        error = ClientError({"Error": {"Code": "AccessDenied", "Message": "x"}}, "Copy")
        for copy_error, code in ((error, "AccessDenied"), (Boto3Error(), "Boto3Error")):
            with self.subTest(code=code):
                s3 = FakeS3(head_code="404", copy_error=copy_error)
                with self.assertRaises(StepFailed) as raised:
                    self.stage(s3)
                self.assertEqual(raised.exception.step, "stage")
                self.assertEqual(
                    str(raised.exception),
                    "Could not copy the Spine backup from s3://spine.dev.graphaware.io/"
                    "spine-neo4j-backups/spine-latest.backup to "
                    f"s3://{BUCKET}/spine/spine-latest.backup: {code}. A server-side "
                    "copy cannot cross partitions, so in GovCloud upload the file to "
                    "that key yourself and run the command again, or pass --url.",
                )
                self.assertEqual(s3.presigns, [])

    def test_the_presigned_address_is_never_printed(self) -> None:
        for s3 in (FakeS3(), FakeS3(head_code="404")):
            _, out = self.stage(s3)
            self.assertNotIn(PRESIGNED, out)
            self.assertNotIn("X-Amz-Signature", out)


class DefaultUrlTest(SeedNodesCase):
    def setUp(self) -> None:
        super().setUp()
        patch = mock.patch.object(seed_nodes, "stage_backup", return_value=PRESIGNED)
        self.stage_backup = patch.start()
        self.addCleanup(patch.stop)

    def test_discover_probes_the_staged_backup(self) -> None:
        self.outputs = [probe_text(status="206"), probe_text(status="206")]
        code, out = self.run_command(seed_nodes.discover, args(url=None))
        self.assertEqual(code, 0)
        self.stage_backup.assert_called_once_with(self.session)
        for _, script in self.scripts:
            self.assertEqual(script, seed_nodes.probe_script(PRESIGNED))
        self.assertIn("outbound https:  yes", out)
        self.assertNotIn(PRESIGNED, out)

    def test_copy_downloads_the_staged_backup(self) -> None:
        self.outputs = [probe_text(status="206"), "", probe_text(have=WANT)]
        code, out = self.run_command(
            seed_nodes.copy, args("copy", instance_id="i-aaa", url=None)
        )
        self.assertEqual(code, 0)
        self.stage_backup.assert_called_once_with(self.session)
        self.assertEqual(self.scripts[0][1], seed_nodes.probe_script(PRESIGNED))
        self.assertEqual(self.scripts[1][1], seed_nodes.copy_script(SEEDS, PRESIGNED))
        self.assertEqual(self.scripts[2][1], seed_nodes.probe_script(PRESIGNED))
        self.assertNotIn(PRESIGNED, out)

    def test_load_probes_the_staged_backup(self) -> None:
        self.outputs = [probe_text(have=WANT, status="206"), load_output()]
        code, out = self.run_command(
            seed_nodes.load,
            args(
                "load",
                instance_id="i-aaa",
                password_secret_arn=PASSWORD_ARN,
                url=None,
            ),
        )
        self.assertEqual(code, 0)
        self.stage_backup.assert_called_once_with(self.session)
        self.assertEqual(self.scripts[0][1], seed_nodes.probe_script(PRESIGNED))
        self.assertNotIn(PRESIGNED, out)

    def test_an_explicit_url_skips_staging(self) -> None:
        url = "https://bucket.example/spine.backup"
        self.outputs = [probe_text(), probe_text()]
        self.run_command(seed_nodes.discover, args(url=url))
        self.stage_backup.assert_not_called()
        for _, script in self.scripts:
            self.assertEqual(script, seed_nodes.probe_script(url))


class CopyTest(SeedNodesCase):
    def copy(self, **extra) -> tuple[int | None, str]:
        return self.run_command(
            seed_nodes.copy, args("copy", instance_id="i-aaa", **extra)
        )

    def test_copy_downloads_after_the_confirm_and_checks_the_size(self) -> None:
        self.outputs = [probe_text(), "ls output", probe_text(have=WANT)]
        code, out = self.copy()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.scripts), 3)
        self.assertEqual(self.scripts[1][1], seed_nodes.copy_script(SEEDS))
        self.assertIn(SERVER_ID, out)
        self.assertIn(f"Copied {WANT} bytes. Server id: {SERVER_ID}", out)
        self.assertNotIn("demo-spine", out)

    def test_copy_asks_before_it_downloads_unless_given_yes(self) -> None:
        self.outputs = [probe_text()]
        with (
            mock.patch("builtins.input", return_value="n"),
            self.assertRaises(SystemExit),
        ):
            self.copy(yes=False)
        self.assertEqual(len(self.scripts), 1)

        self.outputs = [probe_text(), "", probe_text(have=WANT)]
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")):
            self.copy(yes=True)

    def test_copy_stops_when_the_node_has_no_route(self) -> None:
        self.outputs = [probe_text(rc=28)]
        with self.assertRaises(StepFailed) as raised:
            self.copy()
        self.assertIn("NAT gateway", str(raised.exception))
        self.assertEqual(len(self.scripts), 1)

    def test_copy_skips_when_the_file_is_already_complete(self) -> None:
        self.outputs = [probe_text(have=WANT)]
        code, out = self.copy()
        self.assertEqual(code, 0)
        self.assertIn("Skip", out)
        self.assertEqual(len(self.scripts), 1)

    def test_copy_stops_when_the_disk_is_too_small(self) -> None:
        self.outputs = [probe_text(free=WANT)]
        with self.assertRaises(StepFailed) as raised:
            self.copy()
        self.assertIn("free", str(raised.exception))
        self.assertEqual(len(self.scripts), 1)

    def test_copy_stops_when_the_size_is_wrong_afterwards(self) -> None:
        self.outputs = [probe_text(), "", probe_text(have=WANT - 1)]
        with self.assertRaises(StepFailed):
            self.copy()

    def test_copy_refuses_an_instance_outside_the_stack(self) -> None:
        with self.assertRaises(StepFailed):
            self.run_command(seed_nodes.copy, args("copy", instance_id="i-other"))
        self.assertEqual(self.scripts, [])

    def test_copy_refuses_a_host_that_is_not_a_neo4j_node(self) -> None:
        self.outputs = [probe_text(node=False)]
        with self.assertRaises(StepFailed):
            self.copy()
        self.assertEqual(len(self.scripts), 1)

    def test_copy_uses_the_seeds_dir_override(self) -> None:
        self.outputs = [probe_text(), "", probe_text(have=WANT)]
        self.copy(seeds_dir="/data/seeds")
        self.assertEqual(self.scripts[1][1], seed_nodes.copy_script("/data/seeds"))


def load_output(status: str = "online", nodes: str = "2596708") -> str:
    return (
        f"spine-status={status}\n"
        f"spine-nodes={nodes}\n"
        "spine-persons=193038\n"
        "spine-relationships=1552236\n"
    )


class ParseLoadTest(unittest.TestCase):
    def test_parse_load_reads_each_marker(self) -> None:
        for text, expected in (
            ("x\n" + load_output(nodes="1234"), ("online", 1234, 193_038, 1_552_236)),
            ("error\n", (None, None, None, None)),
            (
                (
                    "spine-status=online\nspine-nodes=count(n)\nspine-persons=\n"
                    "spine-relationships=7\n"
                ),
                ("online", None, None, 7),
            ),
        ):
            with self.subTest(text=text):
                self.assertEqual(seed_nodes.parse_load(text), expected)


# Stand-ins for the node: aws prints a password, cypher-shell logs each call
# and answers the queries the load script sends.
FAKE_AWS = """#!/bin/bash
echo "$@" >> "$LOG/aws.args"
echo s3cret-pw
"""
FAKE_CYPHER_SHELL = """#!/bin/bash
echo "ARGS $*" >> "$LOG/cypher.log"
echo "USER=$NEO4J_USERNAME PASSWORD=$NEO4J_PASSWORD" >> "$LOG/cypher.log"
for arg in "$@"; do last=$arg; done
case "$*" in
  *"-f "*) cp "$last" "$LOG/roles.cypher" ;;
  *"WHERE serverId"*) echo "count(*)"; echo "${KNOWN:-1}" ;;
  *"YIELD name"*) echo "count(*)"; echo 3 ;;
  *"CREATE DATABASE"*) ;;
  *"currentStatus"*) echo currentStatus; echo '"online"' ;;
  *"MATCH (n)"*) echo "count(n)"; echo 2596708 ;;
  *"MATCH (p:Person)"*) echo "count(p)"; echo 193038 ;;
  *"MATCH ()-[r]->()"*) echo "count(r)"; echo 1552236 ;;
esac
"""


class LoadScriptTest(unittest.TestCase):
    def test_a_bad_server_id_or_secret_arn_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            seed_nodes.load_script("x'; DROP", PASSWORD_ARN)
        with self.assertRaises(ValueError):
            seed_nodes.load_script(SERVER_ID, "neo4j/ee-stack/password")
        with self.assertRaises(ValueError):
            seed_nodes.load_script(SERVER_ID, PASSWORD_ARN + "; id")

    def test_the_password_stays_off_the_script(self) -> None:
        script = seed_nodes.load_script(SERVER_ID, PASSWORD_ARN)
        self.assertNotRegex(script, r"\s-p\b|--password")
        self.assertNotIn("echo $NEO4J_PASSWORD", script)
        self.assertNotIn("set -x", script)
        self.assertIn("--region eu-west-2", script)

    def test_the_tools_are_named_by_their_full_paths(self) -> None:
        script = seed_nodes.load_script(SERVER_ID, PASSWORD_ARN)
        self.assertIn("cypher_shell=/usr/bin/cypher-shell\n", script)
        self.assertIn("aws=/usr/local/bin/aws\n", script)
        self.assertNotIn("command -v", script)

    def test_the_server_is_checked_before_the_database_is_created(self) -> None:
        script = seed_nodes.load_script(SERVER_ID, PASSWORD_ARN)
        self.assertLess(
            script.index("SHOW SERVERS YIELD serverId"),
            script.index("CREATE DATABASE spine"),
        )
        self.assertIn("neo4j+ssc://localhost:7687", script)
        self.assertIn(f"server://$id/{seed_nodes.BACKUP_NAME}", script)
        self.assertIn("PRIMARIES 0 SECONDARIES", script)

    def test_the_roles_in_the_script_equal_the_file(self) -> None:
        script = seed_nodes.load_script(SERVER_ID, PASSWORD_ARN)
        (encoded,) = re.findall(r"echo (\S+) \| base64 -d > \"\$roles\"", script)
        self.assertEqual(base64.b64decode(encoded), seed_nodes.ROLES_FILE.read_bytes())

    def run_locally(
        self, known: str = "1", missing: str | None = None
    ) -> tuple[subprocess.CompletedProcess, Path]:
        """Run the script with the fake tools. missing names one left out."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bin_dir = Path(tmp.name) / "bin"
        bin_dir.mkdir()
        for name, text in (("aws", FAKE_AWS), ("cypher-shell", FAKE_CYPHER_SHELL)):
            if name == missing:
                continue
            path = bin_dir / name
            path.write_text(text)
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        env = {**os.environ, "LOG": tmp.name, "KNOWN": known}
        script = seed_nodes.load_script(
            SERVER_ID,
            PASSWORD_ARN,
            cypher_shell=str(bin_dir / "cypher-shell"),
            aws_cli=str(bin_dir / "aws"),
        )
        done = subprocess.run(
            ["bash", "-c", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        return done, Path(tmp.name)

    def test_the_script_runs_locally_and_prints_the_markers(self) -> None:
        done, log = self.run_locally()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(
            seed_nodes.parse_load(done.stdout),
            ("online", 2_596_708, 193_038, 1_552_236),
        )
        calls = (log / "cypher.log").read_text()
        self.assertIn("USER=neo4j PASSWORD=s3cret-pw", calls)
        self.assertNotIn("s3cret-pw", "".join(calls.splitlines(True)[0::2]))
        self.assertIn("TOPOLOGY 3 PRIMARIES 0 SECONDARIES", calls)
        self.assertIn(f"seedURI: 'server://{SERVER_ID}/spine-latest.backup'", calls)
        self.assertEqual(
            (log / "roles.cypher").read_bytes(), seed_nodes.ROLES_FILE.read_bytes()
        )
        self.assertIn(PASSWORD_ARN, (log / "aws.args").read_text())

    def test_the_script_stops_when_a_tool_is_missing(self) -> None:
        for name in ("cypher-shell", "aws"):
            with self.subTest(name=name):
                done, log = self.run_locally(missing=name)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn(f"bin/{name} is missing or not executable", done.stderr)
                self.assertFalse((log / "aws.args").exists())
                self.assertFalse((log / "cypher.log").exists())

    def test_the_script_stops_when_show_servers_does_not_list_the_id(self) -> None:
        done, log = self.run_locally(known="0")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("does not list the server id", done.stderr)
        self.assertNotIn("CREATE DATABASE", (log / "cypher.log").read_text())


class RunOnHostTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ssm = mock.Mock()
        self.ssm.send_command.return_value = {"Command": {"CommandId": "c-1"}}
        patch = mock.patch.object(seed_nodes.time, "sleep")
        patch.start()
        self.addCleanup(patch.stop)

    def test_it_waits_for_the_command_and_returns_its_output(self) -> None:
        missing = ClientError(
            {"Error": {"Code": "InvocationDoesNotExist", "Message": "x"}},
            "GetCommandInvocation",
        )
        self.ssm.get_command_invocation.side_effect = [
            missing,
            {"Status": "InProgress"},
            {"Status": "Success", "StandardOutputContent": "out\n"},
        ]
        got = seed_nodes.run_on_host(self.ssm, "i-aaa", ["echo"], 60, "copy")
        self.assertEqual(got, "out\n")
        sent = self.ssm.send_command.call_args.kwargs
        self.assertEqual(sent["InstanceIds"], ["i-aaa"])
        self.assertEqual(sent["DocumentName"], "AWS-RunShellScript")
        self.assertEqual(
            sent["Parameters"], {"commands": ["echo"], "executionTimeout": ["60"]}
        )
        self.assertEqual(sent["Comment"], "seed_nodes.py")

    def test_a_failed_command_stops_the_step_with_the_end_of_stderr(self) -> None:
        self.ssm.get_command_invocation.return_value = {
            "Status": "Failed",
            "StandardErrorContent": "x" * 600 + "the reason",
        }
        with self.assertRaises(StepFailed) as raised:
            seed_nodes.run_on_host(self.ssm, "i-aaa", ["false"], 60, "load")
        self.assertEqual(raised.exception.step, "load")
        self.assertIn("ended Failed", str(raised.exception))
        self.assertIn("the reason", str(raised.exception))

    def test_another_client_error_is_raised(self) -> None:
        self.ssm.get_command_invocation.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "x"}},
            "GetCommandInvocation",
        )
        with self.assertRaises(ClientError):
            seed_nodes.run_on_host(self.ssm, "i-aaa", ["echo"], 60, "copy")


class LoadTest(SeedNodesCase):
    def load(self, **extra) -> tuple[int | None, str]:
        values = {"instance_id": "i-aaa", "password_secret_arn": PASSWORD_ARN}
        return self.run_command(seed_nodes.load, args("load", **values | extra))

    def test_load_runs_the_script_after_the_confirm(self) -> None:
        self.outputs = [probe_text(have=WANT), load_output()]
        code, out = self.load()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.scripts), 2)
        self.assertEqual(
            self.scripts[1][1], seed_nodes.load_script(SERVER_ID, PASSWORD_ARN)
        )
        self.assertIn(f"server://{SERVER_ID}/spine-latest.backup", out)
        self.assertIn("PASS: the spine database is online with 2596708 nodes", out)

    def test_load_asks_before_it_runs(self) -> None:
        self.outputs = [probe_text(have=WANT)]
        with (
            mock.patch("builtins.input", return_value="n"),
            self.assertRaises(SystemExit),
        ):
            self.load(yes=False)
        self.assertEqual(len(self.scripts), 1)

    def test_load_reads_the_superuser_secret_arn_from_ssm(self) -> None:
        self.outputs = [probe_text(have=WANT), load_output()]
        self.load(password_secret_arn=None)
        self.assertEqual(
            self.ssm.parameter_names, [f"/neo4j-ee/{STACK}/password-secret-arn"]
        )
        self.assertIn(PASSWORD_ARN, self.scripts[1][1])

    def test_the_option_overrides_ssm(self) -> None:
        self.outputs = [probe_text(have=WANT), load_output()]
        self.load()
        self.assertEqual(self.ssm.parameter_names, [])

    def test_load_stops_when_the_ssm_parameter_is_missing(self) -> None:
        self.ssm.parameters = {}
        with self.assertRaises(StepFailed) as raised:
            self.load(password_secret_arn=None)
        self.assertIn(f"/neo4j-ee/{STACK}/password-secret-arn", str(raised.exception))
        self.assertEqual(self.scripts, [])

    def test_load_refuses_a_bad_secret_arn_before_any_script(self) -> None:
        with self.assertRaises(StepFailed) as raised:
            self.load(password_secret_arn="neo4j/ee-stack/password")
        self.assertIn("is not the full ARN", str(raised.exception))
        self.assertEqual(self.scripts, [])

    def test_load_stops_when_the_backup_is_missing(self) -> None:
        self.outputs = [probe_text(have=0)]
        with self.assertRaises(StepFailed) as raised:
            self.load()
        self.assertIn("Run the copy command first", str(raised.exception))
        self.assertEqual(len(self.scripts), 1)

    def test_load_stops_without_a_valid_server_id(self) -> None:
        self.outputs = [probe_text(have=WANT).replace(SERVER_ID, "bad")]
        with self.assertRaises(StepFailed):
            self.load()
        self.assertEqual(len(self.scripts), 1)

    def test_load_fails_when_offline_or_the_node_count_is_zero(self) -> None:
        self.outputs = [probe_text(have=WANT), load_output(status="offline")]
        with self.assertRaises(StepFailed) as raised:
            self.load()
        self.assertEqual(raised.exception.step, "load")

        self.outputs = [probe_text(have=WANT), load_output(nodes="0")]
        with self.assertRaises(StepFailed):
            self.load()


class ArgsTest(unittest.TestCase):
    def test_url_must_be_https(self) -> None:
        argv = ["seed_nodes.py", "discover", STACK, "--url", "http://x"]
        with (
            mock.patch("sys.argv", argv),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            seed_nodes.parse_args()

    def test_load_takes_the_secret_arn(self) -> None:
        argv = [
            "seed_nodes.py", "load", STACK, "--instance-id", "i-aaa",
            "--password-secret-arn", PASSWORD_ARN, "--yes",
        ]  # fmt: skip
        with mock.patch("sys.argv", argv):
            got = seed_nodes.parse_args()
        self.assertEqual(got.password_secret_arn, PASSWORD_ARN)
        self.assertTrue(got.yes)


if __name__ == "__main__":
    unittest.main()
