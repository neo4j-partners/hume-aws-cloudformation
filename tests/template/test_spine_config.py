"""Offline tests for the Spine demo in the template: SpineConfigSecretArn.

The template is resolved for one set of parameter values with the helpers of
tests/helpers/template.py. No AWS call is made.

Run from the repository root:
    uv run --with boto3 --with python-dotenv --with pyyaml \
        python -m unittest tests.template.test_spine_config
"""

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import deploy
import spine_demo
from tests.helpers import template as tm

SPINE_ARN = (
    "arn:aws:secretsmanager:us-east-1:123456789012:secret:"
    "hume/stack-x/spine-config-AbCdEf"
)
DEMO_ON = {"SpineConfigSecretArn": SPINE_ARN}
REPO_SPINE = Path(__file__).resolve().parents[2] / "demo/spine/hume-config"


class TemplateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = tm.load()
        cls.off = tm.resolve_template(cls.template, tm.BASE_INPUTS)
        cls.on = tm.resolve_template(cls.template, tm.BASE_INPUTS | DEMO_ON)

    def test_off_creates_the_base_resources_and_outputs_only(self) -> None:
        got = {n: s["Type"] for n, s in self.off["resources"].items()}
        self.assertEqual(got, tm.BASE_RESOURCES)
        self.assertEqual(set(self.off["outputs"]), tm.BASE_OUTPUTS)

    def test_the_arn_adds_the_import_and_the_config_cli_output(self) -> None:
        got = {n: s["Type"] for n, s in self.on["resources"].items()}
        self.assertEqual(got, tm.BASE_RESOURCES | tm.DEMO_IMPORT_RESOURCES)
        self.assertEqual(
            set(self.on["outputs"]),
            set(self.off["outputs"]) | {"ConfigCliTaskDefinitionArn"},
        )

    def test_spine_demo_expects_every_resource_the_import_adds(self) -> None:
        added = {
            ("Add", name)
            for name, spec in self.template["Resources"].items()
            if spec.get("Condition") == "UseSpineImport"
        }
        self.assertEqual(spine_demo.ADD_IMPORT_CHANGES, added)

    def test_the_arn_changes_no_resource_of_off_but_the_api_task(self) -> None:
        for name, spec in self.off["resources"].items():
            if name != "ApiTaskDefinition":
                self.assertEqual(self.on["resources"][name], spec, name)

    def api_settings(self, resolved: dict) -> tuple[dict, set[str]]:
        [container] = resolved["resources"]["ApiTaskDefinition"]["Properties"][
            "ContainerDefinitions"
        ]
        env = {e["Name"]: e["Value"] for e in container["Environment"]}
        keys = {name: value for name, value in env.items() if "hume.api." in name}
        secrets = {s["Name"] for s in container["Secrets"]}
        return keys, secrets

    def test_off_turns_api_keys_off_and_has_no_initial_key(self) -> None:
        keys, secrets = self.api_settings(self.off)
        self.assertEqual(
            keys,
            {"hume.api.enabled": "false", "hume.api.initial-key.create": "false"},
        )
        self.assertNotIn("hume.api.initial-key.token", secrets)
        self.assertNotIn("ApiKeySecret", self.off["resources"])

    def test_the_arn_turns_on_api_keys_and_the_initial_admin_key(self) -> None:
        keys, secrets = self.api_settings(self.on)
        self.assertEqual(
            keys,
            {
                "hume.api.enabled": "true",
                "hume.api.initial-key.create": "true",
                "hume.api.initial-key.name": "admin-api-key",
                "hume.api.initial-key.roles": (
                    "ROLE_ADMINISTRATOR,ROLE_WORKSPACES_MANAGER"
                ),
            },
        )
        self.assertIn("hume.api.initial-key.token", secrets)
        self.assertIn("ApiKeySecret", self.on["resources"])

    def test_media_accepts_api_keys_with_or_without_the_arn(self) -> None:
        for resolved in (self.off, self.on):
            [container] = resolved["resources"]["MediaTaskDefinition"]["Properties"][
                "ContainerDefinitions"
            ]
            env = {e["Name"]: e["Value"] for e in container["Environment"]}
            self.assertEqual(env["hume.security.api-key.enabled"], "true")

    def test_the_arn_passes_every_rule(self) -> None:
        self.assertEqual(tm.failed_rules(self.template, DEMO_ON), [])

    def test_the_secret_arn_is_empty_by_default_and_must_be_a_full_arn(self) -> None:
        spec = self.template["Parameters"]["SpineConfigSecretArn"]
        self.assertEqual(spec["Default"], "")
        pattern = re.compile(spec["AllowedPattern"])
        self.assertTrue(pattern.match(""))
        self.assertTrue(pattern.match(SPINE_ARN))
        self.assertFalse(pattern.match("hume/stack-x/spine-config"))

    def test_the_secret_arn_is_not_read_when_off(self) -> None:
        self.assertNotIn(SPINE_ARN, json.dumps(self.off))


def config_cli_containers(resolved: dict) -> dict[str, dict]:
    task = resolved["resources"]["ConfigCliTaskDefinition"]["Properties"]
    return {c["Name"]: c for c in task["ContainerDefinitions"]}


class SpineConfigTemplateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = tm.load()
        cls.off = tm.resolve_template(cls.template, tm.BASE_INPUTS)
        cls.on = tm.resolve_template(cls.template, tm.BASE_INPUTS | DEMO_ON)

    def test_the_import_runs_once_the_arn_is_set(self) -> None:
        wait = {"ContainerName": "render-spine", "Condition": "SUCCESS"}
        containers = config_cli_containers(self.on)
        self.assertIn("render-spine", containers)
        self.assertIn(wait, containers["config-cli"]["DependsOn"])

    def test_the_render_container_waits_for_the_render_container(self) -> None:
        container = config_cli_containers(self.on)["render-spine"]
        self.assertEqual(
            container["DependsOn"],
            [{"ContainerName": "render", "Condition": "SUCCESS"}],
        )
        self.assertFalse(container["Essential"])

    def test_the_only_secret_is_the_spine_config(self) -> None:
        container = config_cli_containers(self.on)["render-spine"]
        got = {s["Name"]: s["ValueFrom"] for s in container["Secrets"]}
        self.assertEqual(got, {"SPINE_CONFIG": SPINE_ARN})

    def test_the_neo4j_app_secret_arn_is_a_plain_value(self) -> None:
        container = config_cli_containers(self.on)["render-spine"]
        env = {e["Name"]: e["Value"] for e in container["Environment"]}
        self.assertEqual(env["NEO4J_APP_SECRET_ARN"], tm.PASSWORD_ARN)

    def test_the_render_container_points_at_the_cluster(self) -> None:
        container = config_cli_containers(self.on)["render-spine"]
        env = {e["Name"]: e["Value"] for e in container["Environment"]}
        self.assertEqual(
            env["SPINE_NEO4J_HOST"],
            {"Fn::Sub": "neo4j+s://neo4j.example.test"},
        )

    def test_the_render_script_writes_no_credential(self) -> None:
        container = config_cli_containers(self.on)["render"]
        (text,) = container["Command"]
        for word in (
            "neo4j-prototype",
            "NEO4J_PASSWORD",
            "ORCHESTRA_MEDIA_DOWNLOAD_KEY",
            "MEDIA_DOWNLOAD_KEY",
            "./resources",
            "/config/resources",
        ):
            self.assertNotIn(word, text)
        self.assertNotIn("Secrets", container)

    def test_the_secret_name_matches_the_deploy_helper(self) -> None:
        self.assertEqual(
            deploy.spine_secret_name("stack-x"), "hume/stack-x/spine-config"
        )

    def test_the_script_unpacks_the_files_and_writes_the_two_secrets(self) -> None:
        container = config_cli_containers(self.on)["render-spine"]
        (text,) = container["Command"]
        self.assertIn("base64 -d | tar -xj -C /config/spine", text)
        for name in ("spine-neo4j-host.yml", "spine-neo4j-sa-user.yml"):
            self.assertIn(f"/config/secrets/{name}", text)
        self.assertNotIn("spine-neo4j-sa-password", text)
        self.assertNotIn("NEO4J_PASSWORD", text)
        self.assertIn("SPINE_NEO4J_HOST", text)
        self.assertIn("SPINE_ANALYST", text)
        self.assertIn("SPINE_OFFICER", text)
        self.assertIn("/config/manifest.yml", text)

    def test_the_script_removes_the_skipped_actions_from_the_perspective(
        self,
    ) -> None:
        container = config_cli_containers(self.on)["render-spine"]
        (text,) = container["Command"]
        (line,) = [x for x in text.splitlines() if x.startswith("sed -i '/")]
        names = [
            Path(skipped).stem
            for skipped in deploy.SPINE_SKIPPED_FILES
            if skipped.startswith("spine-kg/actions/")
        ]
        for name in names:
            self.assertIn(f"/^    - {name}$/d", line)
        self.assertEqual(line.count("/d"), len(names))
        self.assertTrue(line.endswith("spine-kg/perspectives/analyst-perspective.yml"))
        self.assertLess(text.index("tar -xj"), text.index(line))

    def test_the_perspective_names_only_actions_that_are_packed(self) -> None:
        root = Path.home() / "projects/graphaware/hume-quickstart/config/hume/spine"
        if not root.is_dir():
            self.skipTest("The quickstart is not at the usual place")
        container = config_cli_containers(self.on)["render-spine"]
        (text,) = container["Command"]
        (line,) = [x for x in text.splitlines() if x.startswith("sed -i '/")]
        patterns = re.findall(r"/\^(.*?)\$/d", line)
        perspective = root / "spine-kg/perspectives/analyst-perspective.yml"
        lines = perspective.read_text().splitlines()
        kept = [x for x in lines if x not in patterns]
        self.assertEqual(len(lines) - len(kept), len(patterns))
        packed = {
            p.stem for p in deploy.spine_config_files(root) if "actions" in p.parts
        }
        listed = {x.removeprefix("    - ") for x in kept if x.startswith("    - ")}
        actions = {
            x for x in listed if (root / "spine-kg/actions" / f"{x}.yml").exists()
        }
        self.assertTrue(actions <= packed)
        self.assertEqual({x for x in patterns} & {f"    - {n}" for n in packed}, set())

    def run_vault_block(self, arn: str, content: str) -> tuple[int, str]:
        """Run the reference lines of the script on one resource file."""
        container = config_cli_containers(self.on)["render-spine"]
        lines = container["Command"][0].splitlines()
        start = next(i for i, x in enumerate(lines) if x.startswith("name=$("))
        check = next(
            i for i, x in enumerate(lines) if "grep -q NEO4J_APP_SECRET_NAME" in x
        )
        end = next(i for i in range(check, len(lines)) if lines[i] == "fi")
        block = "\n".join(lines[start : end + 1])
        with tempfile.TemporaryDirectory() as tmp:
            sa = Path(tmp) / "resources/spine-neo4j-sa.yml"
            sa.parent.mkdir()
            sa.write_text(content)
            script = "set -eu\n" + block.replace("/config/spine", tmp) + "\n"
            proc = subprocess.run(
                ["sh", "-c", script],
                env={"PATH": os.environ["PATH"], "NEO4J_APP_SECRET_ARN": arn},
                capture_output=True,
                text=True,
                check=False,
            )
            return proc.returncode, sa.read_text()

    def test_the_script_writes_the_vault_reference_into_the_resource(self) -> None:
        content = (REPO_SPINE / "resources/spine-neo4j-sa.yml").read_text()
        arn = tm.ARN_PREFIX + "neo4j/test-ee-1/app-password-AbCdEf"
        code, got = self.run_vault_block(arn, content)
        self.assertEqual(code, 0)
        self.assertEqual(
            got,
            content.replace(
                "vault:aws:NEO4J_APP_SECRET_NAME",
                "vault:aws:neo4j/test-ee-1/app-password",
            ),
        )

    def test_the_script_stops_when_the_placeholder_is_missing(self) -> None:
        content = "  - name: password\n    value: $$SPINE_NEO4J_SA_PASSWORD\n"
        code, got = self.run_vault_block(tm.PASSWORD_ARN, content)
        self.assertNotEqual(code, 0)
        self.assertEqual(got, content)

    def test_the_script_stops_when_the_secret_name_is_empty(self) -> None:
        content = (REPO_SPINE / "resources/spine-neo4j-sa.yml").read_text()
        code, got = self.run_vault_block("", content)
        self.assertNotEqual(code, 0)
        self.assertEqual(got, content)

    def test_the_script_has_no_cloudformation_substitution(self) -> None:
        container = config_cli_containers(self.on)["render-spine"]
        self.assertIsInstance(container["Command"][0], str)
        self.assertNotIn("${", container["Command"][0])

    def test_the_container_mounts_the_config_volume_and_logs(self) -> None:
        container = config_cli_containers(self.on)["render-spine"]
        render = config_cli_containers(self.on)["render"]
        self.assertEqual(container["MountPoints"], render["MountPoints"])
        options = container["LogConfiguration"]["Options"]
        self.assertEqual(options["awslogs-stream-prefix"], "render-spine")
        self.assertEqual(
            options["awslogs-group"],
            render["LogConfiguration"]["Options"]["awslogs-group"],
        )


if __name__ == "__main__":
    unittest.main()
