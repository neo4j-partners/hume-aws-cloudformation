"""Offline tests for the truststore of the Neo4j EE connection in deploy.py.

The truststore is built with a faked keytool run, and one test runs a real
local keytool when there is one. No AWS call is made.

Run from the repository root:
    uv run --with boto3 --with python-dotenv --with pyyaml \
        python -m unittest tests.stack.test_truststore
"""

import argparse
import base64
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import deploy
from tests.helpers.env import TRUSTSTORE, Neo4jCase, TempEnvCase
from tests.helpers.template import TEMPLATE

CERT_A = "-----BEGIN CERTIFICATE-----\nQUFB\n-----END CERTIFICATE-----"
CERT_B = "-----BEGIN CERTIFICATE-----\nQkJC\n-----END CERTIFICATE-----"


class CaFileTest(TempEnvCase):
    def read(self, content: bytes) -> list[str]:
        path = self.dir / "ca.crt"
        path.write_bytes(content)
        return deploy.read_ca_certificates(path)

    def test_one_certificate(self) -> None:
        self.assertEqual(self.read(CERT_A.encode()), [CERT_A])

    def test_a_bundle_gives_every_certificate_in_order(self) -> None:
        text = f"junk\n{CERT_A}\n\n{CERT_B}\n"
        self.assertEqual(self.read(text.encode()), [CERT_A, CERT_B])

    def test_a_private_key_is_refused(self) -> None:
        key = "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"
        for text in [key, f"{CERT_A}\n{key}", key.replace("PRIVATE", "RSA PRIVATE")]:
            with self.subTest(text=text[:30]), self.assertRaises(SystemExit) as raised:
                self.read(text.encode())
            self.assertIn("private key", str(raised.exception))
            self.assertNotIn("abc", str(raised.exception))

    def test_a_file_without_a_certificate_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            self.read(b"hello")

    def test_a_binary_file_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            self.read(b"\x30\x82\xff\xfe\x00")


class KeytoolChoiceTest(unittest.TestCase):
    def which(self, found: set[str]):
        return mock.patch.object(
            deploy.shutil, "which", side_effect=lambda n: n if n in found else None
        )

    def test_keytool_is_local_then_docker_then_exits_naming_both(self) -> None:
        with self.which({"keytool", "docker"}):
            self.assertTrue(deploy.keytool_runs_locally())

        with self.which({"docker"}):
            self.assertFalse(deploy.keytool_runs_locally())

        with self.which(set()), self.assertRaises(SystemExit) as raised:
            deploy.keytool_runs_locally()
        message = str(raised.exception)
        self.assertIn("keytool was not found", message)
        self.assertIn("JDK", message)
        self.assertIn("docker", message)

    def test_commands_keep_the_password_out_of_argv(self) -> None:
        work = deploy.Path("/tmp/work")
        for local in (True, False):
            with self.subTest(local=local):
                argv = deploy.keytool_import_command(work, 1, local)
                self.assertIn("-storepass:env", argv)
                self.assertIn(deploy.KEYTOOL_PASSWORD_VAR, argv)
                self.assertEqual(argv[0], "keytool" if local else "docker")
                self.assertIn("PKCS12", argv)

    def test_docker_mounts_the_work_directory_and_runs_as_the_caller(self) -> None:
        argv = deploy.keytool_import_command(deploy.Path("/tmp/work"), 2, False)
        self.assertIn("/tmp/work:/work", argv)
        self.assertIn(f"{os.getuid()}:{os.getgid()}", argv)
        self.assertIn("/work/ca2.crt", argv)
        self.assertIn("/work/truststore.p12", argv)
        self.assertEqual(argv[argv.index("--entrypoint") + 1], "keytool")
        self.assertEqual(argv[argv.index("-e") + 1], deploy.KEYTOOL_PASSWORD_VAR)


class FakeKeytool:
    """Stands in for subprocess.run. Writes the store that keytool would write."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.returncode, self.stderr = returncode, stderr

    def __call__(self, argv, env=None, **_):
        self.calls.append((argv, env))
        store = argv[argv.index("-keystore") + 1]
        with open(store, "ab") as handle:
            handle.write(b"STORE")
        return subprocess.CompletedProcess(argv, self.returncode, "", self.stderr)


class BuildTruststoreTest(unittest.TestCase):
    def build(self, runner: FakeKeytool, certificates: list[str]):
        with (
            mock.patch.object(deploy, "keytool_runs_locally", return_value=True),
            mock.patch.object(deploy.subprocess, "run", runner),
        ):
            return deploy.build_truststore(certificates)

    def test_one_import_per_certificate_with_its_own_alias(self) -> None:
        runner = FakeKeytool()
        store, _ = self.build(runner, [CERT_A, CERT_B])
        aliases = [c[0][c[0].index("-alias") + 1] for c in runner.calls]
        self.assertEqual(aliases, ["privca1", "privca2"])
        self.assertEqual(store, b"STORESTORE")

    def test_the_password_is_random_and_only_in_the_environment(self) -> None:
        first, second = FakeKeytool(), FakeKeytool()
        _, password_one = self.build(first, [CERT_A])
        _, password_two = self.build(second, [CERT_A])
        self.assertNotEqual(password_one, password_two)
        self.assertGreaterEqual(len(password_one), 32)
        argv, env = first.calls[0]
        self.assertEqual(env[deploy.KEYTOOL_PASSWORD_VAR], password_one)
        self.assertFalse(any(password_one in part for part in argv))

    def test_a_keytool_failure_exits_with_its_message(self) -> None:
        runner = FakeKeytool(returncode=1, stderr="keytool error: bad cert")
        with self.assertRaises(SystemExit) as raised:
            self.build(runner, [CERT_A])
        self.assertEqual(
            str(raised.exception), "keytool failed: keytool error: bad cert"
        )

    def test_the_work_directory_is_removed(self) -> None:
        runner = FakeKeytool()
        self.build(runner, [CERT_A])
        store = runner.calls[0][0][runner.calls[0][0].index("-keystore") + 1]
        self.assertFalse(os.path.exists(os.path.dirname(store)))


class TruststoreSecretTest(unittest.TestCase):
    def test_secret_holds_the_file_and_the_jvm_options(self) -> None:
        text = deploy.truststore_secret_string(b"\x00bin\xff", "pw1")
        secret = json.loads(text)
        self.assertEqual(set(secret), {"truststore", "java_tool_options"})
        self.assertEqual(base64.b64decode(secret["truststore"]), b"\x00bin\xff")
        options = secret["java_tool_options"]
        self.assertIn("-Djavax.net.ssl.trustStore=/opt/hume-truststore ", options)
        self.assertIn("-Djavax.net.ssl.trustStoreType=PKCS12", options)
        self.assertIn("-Djavax.net.ssl.trustStorePassword=pw1", options)
        self.assertIn(f"-Dsun.net.inetaddr.ttl={deploy.JVM_DNS_TTL_SECONDS}", options)

    def test_the_path_is_where_the_template_mounts_the_file(self) -> None:
        template = TEMPLATE.read_text()
        self.assertIn(f"ContainerPath: {deploy.TRUSTSTORE_CONTAINER_PATH}", template)

    def test_the_secret_name_sits_outside_the_stack_prefix(self) -> None:
        self.assertFalse(deploy.TRUSTSTORE_SECRET_NAME.startswith("hume/hume-ecs"))
        self.assertEqual(deploy.TRUSTSTORE_SECRET_NAME.count("/"), 1)


class TruststoreCommandTest(Neo4jCase):
    def args(self, **values) -> argparse.Namespace:
        base = {
            "ca_file": None,
            "secret_name": deploy.TRUSTSTORE_SECRET_NAME,
            "profile": None,
            "region": None,
        }
        return argparse.Namespace(**{**base, **values})

    def run_command(self, args: argparse.Namespace):
        sm = mock.MagicMock()
        sm.describe_secret.return_value = {"ARN": TRUSTSTORE}
        session = mock.MagicMock()
        session.client.return_value = sm
        out = io.StringIO()
        with (
            mock.patch.object(deploy, "cli_session", return_value=session),
            mock.patch.object(
                deploy, "build_truststore", return_value=(b"STOREBYTES", "pw-secret-1")
            ),
            contextlib.redirect_stdout(out),
        ):
            code = deploy.cmd_truststore(args)
        return code, sm, out.getvalue()

    def test_the_secret_is_stored_and_the_arn_printed(self) -> None:
        ca = self.dir / "ca.crt"
        ca.write_text(CERT_A)
        code, sm, out = self.run_command(self.args(ca_file=str(ca)))
        self.assertEqual(code, 0)
        put = sm.put_secret_value.call_args.kwargs
        self.assertEqual(put["SecretId"], deploy.TRUSTSTORE_SECRET_NAME)
        self.assertEqual(
            json.loads(put["SecretString"]),
            json.loads(deploy.truststore_secret_string(b"STOREBYTES", "pw-secret-1")),
        )
        self.assertIn(f"{deploy.TRUSTSTORE_ARN_VAR}={TRUSTSTORE}", out)

    def test_nothing_secret_is_printed(self) -> None:
        ca = self.dir / "ca.crt"
        ca.write_text(CERT_A)
        _, _, out = self.run_command(self.args(ca_file=str(ca)))
        encoded = base64.b64encode(b"STOREBYTES").decode()
        for text in ["pw-secret-1", encoded, "STOREBYTES"]:
            self.assertNotIn(text, out)

    def test_the_ca_file_can_come_from_the_environment(self) -> None:
        ca = self.dir / "ca.crt"
        ca.write_text(CERT_A)
        os.environ[deploy.CA_FILE_VAR] = str(ca)
        code, _, _ = self.run_command(self.args())
        self.assertEqual(code, 0)

    def test_no_ca_file_or_a_missing_one_exits(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            self.run_command(self.args())
        self.assertIn(deploy.CA_FILE_VAR, str(raised.exception))
        with self.assertRaises(SystemExit):
            self.run_command(self.args(ca_file=str(self.dir / "missing.crt")))

    def test_a_private_key_file_stops_before_any_build_or_secret(self) -> None:
        key = self.dir / "ca.key"
        key.write_text("-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----")
        with (
            mock.patch.object(deploy, "build_truststore") as build,
            mock.patch.object(deploy, "cli_session") as session,
            self.assertRaises(SystemExit),
        ):
            deploy.cmd_truststore(self.args(ca_file=str(key)))
        build.assert_not_called()
        session.assert_not_called()


class TruststoreOptionsCommandTest(unittest.TestCase):
    OLD_OPTIONS = (
        "-Djavax.net.ssl.trustStore=/opt/hume-truststore "
        "-Djavax.net.ssl.trustStoreType=PKCS12 "
        "-Djavax.net.ssl.trustStorePassword=pw-old-1"
    )

    def run_command(self, options: str | None):
        secret = {"truststore": "U1RPUkU="}
        if options is not None:
            secret["java_tool_options"] = options
        sm = mock.MagicMock()
        sm.get_secret_value.return_value = {"SecretString": json.dumps(secret)}
        session = mock.MagicMock()
        session.client.return_value = sm
        args = argparse.Namespace(
            secret_name=deploy.TRUSTSTORE_SECRET_NAME, profile=None, region=None
        )
        out = io.StringIO()
        with (
            mock.patch.object(deploy, "cli_session", return_value=session),
            contextlib.redirect_stdout(out),
        ):
            code = deploy.cmd_truststore_options(args)
        return code, sm, out.getvalue()

    def test_only_the_options_change_and_the_password_stays(self) -> None:
        code, sm, _ = self.run_command(self.OLD_OPTIONS)
        self.assertEqual(code, 0)
        put = sm.put_secret_value.call_args.kwargs
        self.assertEqual(put["SecretId"], deploy.TRUSTSTORE_SECRET_NAME)
        self.assertEqual(
            json.loads(put["SecretString"]),
            {
                "truststore": "U1RPUkU=",
                "java_tool_options": deploy.truststore_java_options("pw-old-1"),
            },
        )

    def test_current_options_write_nothing(self) -> None:
        code, sm, out = self.run_command(deploy.truststore_java_options("pw-old-1"))
        self.assertEqual(code, 0)
        sm.put_secret_value.assert_not_called()
        self.assertIn("already current", out)

    def test_options_without_a_password_exit_before_any_write(self) -> None:
        for options in (None, "-Dother=1"):
            with self.subTest(options=options), self.assertRaises(SystemExit):
                self.run_command(options)

    def test_nothing_secret_is_printed(self) -> None:
        _, _, out = self.run_command(self.OLD_OPTIONS)
        self.assertNotIn("pw-old-1", out)
        self.assertNotIn("U1RPUkU=", out)

    def test_the_command_is_on_the_parser(self) -> None:
        argv = ["deploy.py", "truststore-options"]
        with mock.patch.object(sys, "argv", argv):
            args = deploy.parse_args()
        self.assertEqual(args.command, "truststore-options")
        self.assertEqual(args.secret_name, deploy.TRUSTSTORE_SECRET_NAME)


@unittest.skipUnless(shutil.which("keytool"), "no local keytool")
class RealKeytoolTest(unittest.TestCase):
    """Run the real keytool on a throwaway CA. Local only, never docker."""

    def make_ca(self, work: deploy.Path, name: str) -> str:
        store = work / f"{name}.p12"
        env = {**os.environ, "TESTPW": "throwaway-pw"}
        subprocess.run(
            [
                "keytool",
                "-genkeypair",
                "-alias",
                name,
                "-keyalg",
                "RSA",
                "-keysize",
                "2048",
                "-dname",
                f"CN={name}",
                "-validity",
                "30",
                "-storetype",
                "PKCS12",
                "-keystore",
                str(store),
                "-storepass:env",
                "TESTPW",
                "-ext",
                "bc:c",
            ],
            env=env,
            check=True,
            capture_output=True,
        )
        result = subprocess.run(
            [
                "keytool",
                "-exportcert",
                "-rfc",
                "-alias",
                name,
                "-keystore",
                str(store),
                "-storepass:env",
                "TESTPW",
            ],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout

    def test_the_built_store_lists_every_certificate_under_the_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = deploy.Path(tmp)
            pem = self.make_ca(work, "one") + self.make_ca(work, "two")
            ca = work / "ca.crt"
            ca.write_text(pem)
            certificates = deploy.read_ca_certificates(ca)
            self.assertEqual(len(certificates), 2)
            store, password = deploy.build_truststore(certificates)
            path = work / "built.p12"
            path.write_bytes(store)
            env = {**os.environ, "BUILTPW": password}
            listing = subprocess.run(
                [
                    "keytool",
                    "-list",
                    "-keystore",
                    str(path),
                    "-storetype",
                    "PKCS12",
                    "-storepass:env",
                    "BUILTPW",
                ],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn("privca1", listing)
            self.assertIn("privca2", listing)
            self.assertEqual(len(re.findall("trustedCertEntry", listing)), 2)


if __name__ == "__main__":
    unittest.main()
