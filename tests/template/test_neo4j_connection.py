"""Offline tests for the Neo4j EE connection in hume-ecs.template.yaml.

The template is read and resolved with the helpers of
tests/helpers/template.py, and the inline code of the lookup function runs
with a fake boto3 and a fake cfnresponse. No AWS call is made.

Run from the repository root:
    uv run --with boto3 --with python-dotenv --with pyyaml \
        python -m unittest tests.template.test_neo4j_connection
"""

import contextlib
import io
import json
import re
import subprocess
import sys
import types
import unittest
from unittest import mock

from botocore.exceptions import ClientError

import deploy
from tests.helpers.env import PARAMETER_FOR_VARIABLE
from tests.helpers.template import (
    AUTH_PARAMETERS,
    BASE_INPUTS,
    BASE_OUTPUTS,
    BASE_PARAMETERS,
    BASE_RESOURCES,
    EE_STACK,
    KMS_KEY_ARN,
    LOOKUP,
    NEO4J_INPUTS,
    NEO4J_PARAMETERS,
    NEO4J_RULES,
    NEO4J_SSM,
    NEO4J_VALUES,
    NO_STACK_MESSAGE,
    NOT_PUBLISHED_MESSAGE,
    OLD_STACK_SSM,
    ONE_SERVER_MESSAGE,
    ONE_SERVER_SSM,
    PASSWORD_ARN,
    PHASE1_PARAMETERS,
    RESTORE_PARAMETERS,
    SPINE_IMPORT_ARN,
    TEMPLATE,
    TRUSTSTORE_ARN,
    TemplateTestCase,
    as_list,
    failed_rules,
    load,
    references,
    rendered_script,
    resolve_template,
    run_bash_syntax_check,
    user_data,
    walk,
)


class StackContractTest(TemplateTestCase):
    def test_resource_names_and_types_are_the_listed_ones(self) -> None:
        got = {name: spec["Type"] for name, spec in self.base["resources"].items()}
        self.assertEqual(got, BASE_RESOURCES)

    def test_no_network_or_neo4j_resource_is_created(self) -> None:
        for name, spec in self.base["resources"].items():
            self.assertNotIn(
                spec["Type"],
                {
                    "AWS::EC2::VPC",
                    "AWS::EC2::Subnet",
                    "AWS::EC2::InternetGateway",
                    "AWS::EC2::VPCGatewayAttachment",
                    "AWS::EC2::RouteTable",
                    "AWS::EC2::Route",
                    "AWS::EC2::SubnetRouteTableAssociation",
                },
                name,
            )
            self.assertNotIn("Neo4jTask", name)

    def test_base_parameters_keep_their_type_and_default(self) -> None:
        for name, (kind, default) in BASE_PARAMETERS.items():
            spec = self.template["Parameters"][name]
            self.assertEqual(spec["Type"], kind, name)
            self.assertEqual(spec.get("Default"), default, name)

    def test_neo4j_parameters_are_required_strings(self) -> None:
        extra = set(self.template["Parameters"]) - set(BASE_PARAMETERS)
        self.assertEqual(
            extra,
            {
                "SpineConfigSecretArn",
                *NEO4J_PARAMETERS,
                *AUTH_PARAMETERS,
                *PHASE1_PARAMETERS,
                *RESTORE_PARAMETERS,
            },
        )
        for name in NEO4J_PARAMETERS:
            spec = self.template["Parameters"][name]
            self.assertEqual(spec["Type"], "String", name)
            self.assertNotIn("Default", spec, name)

    def test_every_reference_points_at_something_that_exists(self) -> None:
        known = set(self.base["resources"]) | set(self.base["parameters"])
        for name, spec in self.base["resources"].items():
            missing = {
                ref
                for ref in references(spec)
                if ref not in known and not ref.startswith("AWS::")
            }
            self.assertFalse(missing, f"{name} refers to {missing}")
        for name, spec in self.base["outputs"].items():
            missing = {
                ref
                for ref in references(spec)
                if ref not in known and not ref.startswith("AWS::")
            }
            self.assertFalse(missing, f"output {name} refers to {missing}")

    def test_resources_use_the_given_vpc(self) -> None:
        vpc = NEO4J_VALUES["VpcId"]
        for name in (
            "AlbSecurityGroup",
            "InstanceSecurityGroup",
            "DatabaseSecurityGroup",
            "WebTargetGroup",
            "ApiTargetGroup",
            "MediaTargetGroup",
        ):
            self.assertEqual(
                self.base["resources"][name]["Properties"]["VpcId"], vpc, name
            )
        self.assertEqual(
            self.base["resources"]["ServiceNamespace"]["Properties"]["Vpc"], vpc
        )

    def test_instance_and_tasks_use_the_first_private_subnet(self) -> None:
        private = NEO4J_VALUES["PrivateSubnet1Id"]
        resources = self.base["resources"]
        self.assertEqual(
            resources["AutoScalingGroup"]["Properties"]["VPCZoneIdentifier"], [private]
        )
        self.assertEqual(resources["DbInitRun"]["Properties"]["Subnets"], [private])
        demo = resolve_template(
            self.template, BASE_INPUTS | {"SpineConfigSecretArn": SPINE_IMPORT_ARN}
        )
        self.assertEqual(
            demo["resources"]["ConfigCliRun"]["Properties"]["Subnets"], [private]
        )
        for name in (
            "EventstoreService",
            "OrchestraService",
            "ApiService",
            "MediaService",
            "WebService",
        ):
            config = resources[name]["Properties"]["NetworkConfiguration"]
            self.assertEqual(config["AwsvpcConfiguration"]["Subnets"], [private], name)
        self.assertEqual(self.base["outputs"]["InstanceSubnetId"]["Value"], private)

    def test_load_balancer_uses_the_public_subnets(self) -> None:
        public = [
            NEO4J_VALUES["PublicSubnet1Id"],
            NEO4J_VALUES["PublicSubnet2Id"],
            NEO4J_VALUES["PublicSubnet3Id"],
        ]
        resources = self.base["resources"]
        self.assertEqual(resources["LoadBalancer"]["Properties"]["Subnets"], public)

    def test_database_uses_the_private_subnets(self) -> None:
        private = [NEO4J_VALUES["PrivateSubnet1Id"], NEO4J_VALUES["PrivateSubnet2Id"]]
        resources = self.base["resources"]
        self.assertEqual(
            resources["DatabaseSubnetGroup"]["Properties"]["SubnetIds"], private
        )

    def test_instance_gets_no_public_address(self) -> None:
        interface = self.base["resources"]["InstanceLaunchTemplate"]["Properties"][
            "LaunchTemplateData"
        ]["NetworkInterfaces"][0]
        self.assertIs(interface["AssociatePublicIpAddress"], False)

    def test_three_rules_name_exactly_the_given_groups(self) -> None:
        resources = self.base["resources"]
        nlb = NEO4J_VALUES["Neo4jNlbSecurityGroupId"]
        endpoint = NEO4J_VALUES["VpcEndpointSecurityGroupId"]
        egress = resources["InstanceEgressNeo4jBolt"]["Properties"]
        self.assertEqual(egress["GroupId"], {"Ref": "InstanceSecurityGroup"})
        self.assertEqual(egress["DestinationSecurityGroupId"], nlb)
        self.assertEqual((egress["FromPort"], egress["ToPort"]), (7687, 7687))
        bolt = resources["Neo4jNlbIngressFromHume"]["Properties"]
        self.assertEqual(bolt["GroupId"], nlb)
        self.assertEqual(
            bolt["SourceSecurityGroupId"], {"Ref": "InstanceSecurityGroup"}
        )
        self.assertEqual((bolt["FromPort"], bolt["ToPort"]), (7687, 7687))
        https = resources["VpcEndpointIngressFromHume"]["Properties"]
        self.assertEqual(https["GroupId"], endpoint)
        self.assertEqual(
            https["SourceSecurityGroupId"], {"Ref": "InstanceSecurityGroup"}
        )
        self.assertEqual((https["FromPort"], https["ToPort"]), (443, 443))

    def test_no_rule_opens_a_wide_source(self) -> None:
        for name in NEO4J_RULES:
            props = self.base["resources"][name]["Properties"]
            self.assertNotIn("CidrIp", props, name)
            self.assertNotIn("CidrIpv6", props, name)

    def test_execution_role_reads_the_truststore_but_not_the_app_password(
        self,
    ) -> None:
        policy = self.base["resources"]["TaskExecutionSecretsPolicy"]["Properties"]
        resources = [
            resource
            for statement in policy["PolicyDocument"]["Statement"]
            for resource in as_list(statement["Resource"])
        ]
        self.assertIn(TRUSTSTORE_ARN, resources)
        self.assertNotIn(PASSWORD_ARN, resources)
        self.assertFalse([item for item in resources if item == "*"])

    def test_execution_role_cannot_decrypt_with_the_neo4j_key(self) -> None:
        for demo in ({}, {"SpineConfigSecretArn": SPINE_IMPORT_ARN}):
            resolved = resolve_template(self.template, BASE_INPUTS | demo)
            policy = resolved["resources"]["TaskExecutionSecretsPolicy"]
            self.assertNotIn(KMS_KEY_ARN, list(walk(policy)), demo)
            self.assertNotIn(PASSWORD_ARN, list(walk(policy)), demo)

    def test_task_role_reads_only_the_app_password_secret(self) -> None:
        role = self.base["resources"]["VaultTaskRole"]["Properties"]
        [statement] = role["AssumeRolePolicyDocument"]["Statement"]
        self.assertEqual(statement["Principal"], {"Service": "ecs-tasks.amazonaws.com"})
        self.assertNotIn("ManagedPolicyArns", role)
        [policy] = role["Policies"]
        statements = policy["PolicyDocument"]["Statement"]
        [read] = [
            s for s in statements if s["Action"] == "secretsmanager:GetSecretValue"
        ]
        self.assertEqual(read["Resource"], PASSWORD_ARN)
        self.assertEqual(
            {s["Action"] for s in statements},
            {"secretsmanager:GetSecretValue", "kms:Decrypt"},
        )

    def test_task_role_decrypts_the_neo4j_key_for_that_secret_only(self) -> None:
        role = self.base["resources"]["VaultTaskRole"]["Properties"]
        [policy] = role["Policies"]
        [statement] = [
            s
            for s in policy["PolicyDocument"]["Statement"]
            if s["Action"] == "kms:Decrypt"
        ]
        self.assertEqual(statement["Resource"], KMS_KEY_ARN)
        via = {"Fn::Sub": "secretsmanager.${AWS::Region}.${AWS::URLSuffix}"}
        self.assertEqual(
            statement["Condition"],
            {
                "StringEquals": {
                    "kms:ViaService": via,
                    "kms:EncryptionContext:SecretARN": PASSWORD_ARN,
                }
            },
        )

    def test_orchestra_and_the_api_run_the_vault_under_the_task_role(self) -> None:
        for name in ("OrchestraTaskDefinition", "ApiTaskDefinition"):
            with self.subTest(task=name):
                props = self.base["resources"][name]["Properties"]
                self.assertEqual(
                    props["TaskRoleArn"], {"Fn::GetAtt": ["VaultTaskRole", "Arn"]}
                )
                container = props["ContainerDefinitions"][0]
                env = {e["Name"]: e["Value"] for e in container["Environment"]}
                self.assertEqual(env["hume.vault.aws.enabled"], "true")
                self.assertEqual(env["hume.vault.aws.region"], {"Ref": "AWS::Region"})
                names = set(env) | {s["Name"] for s in container["Secrets"]}
                for unset in (
                    "AWS_PROFILE",
                    "AWS_ACCESS_KEY_ID",
                    "AWS_SECRET_ACCESS_KEY",
                ):
                    self.assertNotIn(unset, names)

    def test_no_other_task_uses_the_task_role(self) -> None:
        demo = resolve_template(
            self.template, BASE_INPUTS | {"SpineConfigSecretArn": SPINE_IMPORT_ARN}
        )
        users = {
            name
            for name, spec in demo["resources"].items()
            if spec["Type"] == "AWS::ECS::TaskDefinition"
            and "TaskRoleArn" in spec["Properties"]
        }
        self.assertEqual(users, {"OrchestraTaskDefinition", "ApiTaskDefinition"})

    def test_boot_role_reads_the_truststore_secret_only(self) -> None:
        policy = self.base["resources"]["InstanceBootPolicy"]["Properties"]
        resources = [
            resource
            for statement in policy["PolicyDocument"]["Statement"]
            for resource in as_list(statement["Resource"])
        ]
        self.assertIn(TRUSTSTORE_ARN, resources)
        self.assertNotIn(PASSWORD_ARN, resources)

    def test_boot_script_writes_the_truststore_and_not_the_neo4j_data_dir(self) -> None:
        script = user_data(self.base["resources"])
        self.assertNotIn("/opt/hume/data", script)
        self.assertIn(f'--secret-id "{TRUSTSTORE_ARN}"', script)
        self.assertIn("TRUSTSTORE_DIR=/opt/hume/truststore", script)
        self.assertIn("$TRUSTSTORE_DIR/hume-truststore", script)
        self.assertIn('"truststore", "java_tool_options"', script)
        self.assertNotIn(PASSWORD_ARN, script)

    def test_boot_script_is_valid_bash(self) -> None:
        run_bash_syntax_check(rendered_script(self.base["resources"]))

    def test_embedded_python_decodes_the_secret(self) -> None:
        script = user_data(self.base["resources"])
        (code,) = [
            snippet
            for snippet in re.findall(r"python3 -c '\n(.*?)\n\s*' >", script, re.DOTALL)
            if "truststore" in snippet
        ]
        good = '{"truststore": "aGVs\\nbG8=", "java_tool_options": "-Dx=y"}'
        done = subprocess.run(
            [sys.executable, "-I", "-c", code],
            input=good,
            capture_output=True,
            text=True,
            check=False,
        ).returncode
        self.assertEqual(done, 0)
        bad_secrets = (
            '{"truststore": "aGVsbG8="}',
            '{"java_tool_options": "-Dx=y"}',
            "{}",
        )
        for bad in bad_secrets:
            proc = subprocess.run(
                [sys.executable, "-I", "-c", code],
                input=bad,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0, bad)

    def test_orchestra_and_the_api_get_the_truststore_file_and_options(self) -> None:
        for name in ("OrchestraTaskDefinition", "ApiTaskDefinition"):
            with self.subTest(task=name):
                props = self.base["resources"][name]["Properties"]
                volumes = {item["Name"]: item for item in props["Volumes"]}
                self.assertEqual(
                    volumes["truststore"]["Host"]["SourcePath"],
                    "/opt/hume/truststore/hume-truststore",
                )
                container = props["ContainerDefinitions"][0]
                mounts = {
                    item["SourceVolume"]: item for item in container["MountPoints"]
                }
                self.assertEqual(
                    mounts["truststore"]["ContainerPath"], "/opt/hume-truststore"
                )
                self.assertIs(mounts["truststore"]["ReadOnly"], True)
                secrets = {item["Name"]: item for item in container["Secrets"]}
                self.assertEqual(
                    secrets["JAVA_TOOL_OPTIONS"]["ValueFrom"],
                    {"Fn::Sub": f"{TRUSTSTORE_ARN}:java_tool_options::"},
                )

    def test_truststore_options_are_never_a_plain_environment_value(self) -> None:
        for name, spec in self.base["resources"].items():
            if spec["Type"] != "AWS::ECS::TaskDefinition":
                continue
            for container in spec["Properties"]["ContainerDefinitions"]:
                names = [item["Name"] for item in container.get("Environment", [])]
                self.assertNotIn("JAVA_TOOL_OPTIONS", names, name)

    def test_the_import_receives_no_credential_but_its_own_inputs(self) -> None:
        demo = resolve_template(
            self.template, BASE_INPUTS | {"SpineConfigSecretArn": SPINE_IMPORT_ARN}
        )
        containers = demo["resources"]["ConfigCliTaskDefinition"]["Properties"][
            "ContainerDefinitions"
        ]
        secrets = {
            container["Name"]: {item["Name"] for item in container.get("Secrets", [])}
            for container in containers
        }
        self.assertEqual(
            secrets,
            {
                "render": set(),
                "render-spine": {"SPINE_CONFIG"},
                "config-cli": {"hume.admin.api.key"},
            },
        )
        self.assertNotIn(PASSWORD_ARN, json.dumps(containers[0]))

    def test_render_spine_gets_the_app_username(self) -> None:
        demo = resolve_template(
            self.template, BASE_INPUTS | {"SpineConfigSecretArn": SPINE_IMPORT_ARN}
        )
        containers = {
            item["Name"]: item
            for item in demo["resources"]["ConfigCliTaskDefinition"]["Properties"][
                "ContainerDefinitions"
            ]
        }
        values = {
            item["Name"]: item for item in containers["render-spine"]["Environment"]
        }
        self.assertEqual(
            values["NEO4J_USERNAME"]["Value"], NEO4J_VALUES["Neo4jUsername"]
        )
        render = {item["Name"] for item in containers["render"]["Environment"]}
        self.assertFalse({"NEO4J_HOST", "NEO4J_USERNAME"} & render)

    def test_outputs_are_the_listed_ones(self) -> None:
        outputs = self.base["outputs"]
        self.assertEqual(set(outputs), BASE_OUTPUTS)
        self.assertEqual(outputs["VpcId"]["Value"], NEO4J_VALUES["VpcId"])
        self.assertEqual(outputs["EeStackName"]["Value"], EE_STACK)
        self.assertEqual(outputs["Neo4jHost"]["Value"], NEO4J_VALUES["Neo4jHost"])

    def test_no_secret_value_or_arn_is_printed_by_the_boot_script(self) -> None:
        script = user_data(self.base["resources"])
        self.assertNotIn('echo "$', script.replace('echo "Truststore', ""))
        self.assertNotIn("set -x", script)


class RulesTest(TemplateTestCase):
    def test_rules_use_only_equals_and_not(self) -> None:
        used = set()
        for node in walk(list(self.template["Rules"].values())):
            if isinstance(node, dict) and len(node) == 1:
                used.update(node)
        self.assertLessEqual(used, {"Fn::Equals", "Fn::Not", "Ref"})
        text = repr(self.template["Rules"])
        for banned in ("Fn::ValueOf", "Fn::Join", "Fn::Select", "Fn::Split", "Fn::If"):
            self.assertNotIn(banned, text)

    def test_base_inputs_pass(self) -> None:
        self.assertEqual(failed_rules(self.template, {}), [])


class ParameterPatternTest(TemplateTestCase):
    def pattern(self, name: str) -> re.Pattern:
        return re.compile(self.template["Parameters"][name]["AllowedPattern"])

    def test_each_neo4j_parameter_rejects_empty(self) -> None:
        for name in NEO4J_PARAMETERS:
            self.assertFalse(self.pattern(name).fullmatch(""), name)

    def test_each_neo4j_parameter_accepts_its_sample(self) -> None:
        for name, value in NEO4J_INPUTS.items():
            self.assertTrue(self.pattern(name).fullmatch(value), name)

    def test_the_ee_stack_name_is_a_stack_name(self) -> None:
        pattern = self.pattern("EeStackName")
        for value in (EE_STACK, "N", "a" * 128):
            self.assertTrue(pattern.fullmatch(value), value)
        for value in ("1neo4j", "neo4j_ee", "neo4j ee", "neo4j/ee", "a" * 129):
            self.assertFalse(pattern.fullmatch(value), value)

    def test_the_truststore_arn_needs_the_suffix(self) -> None:
        self.assertFalse(
            self.pattern("Neo4jTruststoreSecretArn").fullmatch(
                "arn:aws:secretsmanager:us-east-1:123456789012:secret:ts"
            )
        )

    def test_no_typed_list_parameters(self) -> None:
        for name, spec in self.template["Parameters"].items():
            self.assertFalse(spec["Type"].startswith("List<"), name)
            self.assertNotEqual(spec["Type"], "CommaDelimitedList", name)


class TemplateAgreementTest(unittest.TestCase):
    """The settings match the parameters of the template, so a rename shows here."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.parameters = load()["Parameters"]

    def test_every_setting_maps_to_a_template_parameter(self) -> None:
        for setting in deploy.NEO4J_SETTINGS:
            with self.subTest(parameter=setting.parameter):
                self.assertIn(setting.parameter, self.parameters)

    def test_the_settings_are_the_neo4j_parameters(self) -> None:
        self.assertEqual(set(PARAMETER_FOR_VARIABLE.values()), set(NEO4J_PARAMETERS))

    def test_patterns_are_the_template_patterns(self) -> None:
        for setting in deploy.NEO4J_SETTINGS:
            with self.subTest(parameter=setting.parameter):
                allowed = self.parameters[setting.parameter]["AllowedPattern"]
                self.assertEqual(allowed, f"^{setting.pattern}$")


class FakeLookupSsm:
    """Answers GetParameters from a table of full names, as SSM does."""

    def __init__(self, values: dict[str, str], error: Exception | None = None):
        self.values = values
        self.error = error
        self.calls: list[list[str]] = []

    def get_parameters(self, Names):
        self.calls.append(list(Names))
        if self.error:
            raise self.error
        if len(Names) > 10:
            raise AssertionError("GetParameters takes at most 10 names")
        return {
            "Parameters": [
                {"Name": name, "Value": self.values[name]}
                for name in Names
                if name in self.values
            ],
            "InvalidParameters": [name for name in Names if name not in self.values],
        }


class LookupFunctionTest(TemplateTestCase):
    """Runs the inline code of the lookup function with fake boto3 and cfnresponse."""

    def run_lookup(
        self,
        request_type: str,
        values: dict[str, str] = NEO4J_SSM,
        error: Exception | None = None,
        physical_id: str | None = None,
    ) -> tuple[FakeLookupSsm, list[dict]]:
        prefix = f"/neo4j-ee/{EE_STACK}/"
        ssm = FakeLookupSsm({prefix + k: v for k, v in values.items()}, error)
        sent: list[dict] = []
        boto3 = types.ModuleType("boto3")
        boto3.client = lambda name: ssm if name == "ssm" else None
        cfnresponse = types.ModuleType("cfnresponse")
        cfnresponse.SUCCESS = "SUCCESS"
        cfnresponse.FAILED = "FAILED"

        def send(event, context, status, data, physical, noEcho=False, reason=None):
            sent.append(
                {"status": status, "data": data, "id": physical, "reason": reason}
            )

        cfnresponse.send = send
        code = self.template["Resources"]["Neo4jEeLookupFunction"]["Properties"][
            "Code"
        ]["ZipFile"]
        namespace: dict = {}
        with (
            mock.patch.dict(sys.modules, {"boto3": boto3, "cfnresponse": cfnresponse}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            exec(compile(code, "index.py", "exec"), namespace)  # noqa: S102
            event = {
                "RequestType": request_type,
                "LogicalResourceId": LOOKUP,
                "ResourceProperties": {"EeStackName": EE_STACK},
            }
            if physical_id:
                event["PhysicalResourceId"] = physical_id
            namespace["handler"](event, None)
        self.attributes = namespace["ATTRIBUTES"]
        return ssm, sent

    def test_create_returns_every_attribute(self) -> None:
        _, sent = self.run_lookup("Create")
        expected = {
            attribute: NEO4J_SSM[name] for attribute, name in self.attributes.items()
        }
        self.assertEqual(
            sent,
            [{"status": "SUCCESS", "data": expected, "id": LOOKUP, "reason": None}],
        )

    def test_update_reads_again_and_keeps_the_physical_id(self) -> None:
        ssm, sent = self.run_lookup("Update", physical_id="lookup-1")
        self.assertTrue(ssm.calls)
        [response] = sent
        self.assertEqual(response["status"], "SUCCESS")
        self.assertEqual(response["id"], "lookup-1")
        self.assertEqual(set(response["data"]), set(self.attributes))

    def test_delete_reads_nothing_and_keeps_the_physical_id(self) -> None:
        ssm, sent = self.run_lookup("Delete", values={}, physical_id="lookup-1")
        self.assertEqual(ssm.calls, [])
        self.assertEqual(
            sent, [{"status": "SUCCESS", "data": {}, "id": "lookup-1", "reason": None}]
        )

    def test_the_three_messages(self) -> None:
        for values, expected in (
            ({}, NO_STACK_MESSAGE),
            (ONE_SERVER_SSM, ONE_SERVER_MESSAGE),
            (OLD_STACK_SSM, NOT_PUBLISHED_MESSAGE),
        ):
            for request_type in ("Create", "Update"):
                with self.subTest(request_type=request_type, expected=expected):
                    _, sent = self.run_lookup(request_type, values=values)
                    self.assertEqual(
                        sent,
                        [
                            {
                                "status": "FAILED",
                                "data": {},
                                "id": LOOKUP,
                                "reason": expected,
                            }
                        ],
                    )

    def test_an_ssm_error_fails_the_resource_instead_of_hanging(self) -> None:
        error = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
            "GetParameters",
        )
        _, sent = self.run_lookup("Create", error=error)
        [response] = sent
        self.assertEqual(response["status"], "FAILED")
        self.assertIn("AccessDeniedException", response["reason"])

    def test_names_are_read_in_batches_of_ten_under_the_prefix(self) -> None:
        ssm, _ = self.run_lookup("Create")
        self.assertEqual([len(call) for call in ssm.calls], [10, 2])
        prefix = f"/neo4j-ee/{EE_STACK}/"
        self.assertEqual(
            [name.removeprefix(prefix) for call in ssm.calls for name in call],
            list(deploy.NEO4J_LOOKUP_NAMES),
        )

    def test_the_names_are_the_ones_deploy_checks(self) -> None:
        self.run_lookup("Delete", values={})
        self.assertEqual(tuple(self.attributes.values()), deploy.NEO4J_LOOKUP_NAMES)

    def test_every_reference_to_the_lookup_names_an_attribute(self) -> None:
        self.run_lookup("Delete", values={})
        text = TEMPLATE.read_text()
        used = set(re.findall(rf"{LOOKUP}\.([A-Za-z0-9]+)", text))
        self.assertTrue(used)
        self.assertLessEqual(used, set(self.attributes))
        self.assertEqual(set(NEO4J_VALUES), set(self.attributes))

    def test_the_role_reads_only_the_stack_path_and_writes_its_own_logs(self) -> None:
        role = self.template["Resources"]["Neo4jEeLookupRole"]["Properties"]
        self.assertNotIn("ManagedPolicyArns", role)
        [policy] = role["Policies"]
        statements = policy["PolicyDocument"]["Statement"]
        self.assertEqual(
            statements,
            [
                {
                    "Effect": "Allow",
                    "Action": "ssm:GetParameters",
                    "Resource": {
                        "Fn::Sub": "arn:${AWS::Partition}:ssm:${AWS::Region}:"
                        "${AWS::AccountId}:parameter/neo4j-ee/${EeStackName}/*"
                    },
                },
                {
                    "Effect": "Allow",
                    "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                    "Resource": {"Fn::GetAtt": ["Neo4jEeLookupLogGroup", "Arn"]},
                },
            ],
        )

    def test_the_log_group_is_the_function_log_group(self) -> None:
        resources = self.template["Resources"]
        group = resources["Neo4jEeLookupLogGroup"]["Properties"]
        function = resources["Neo4jEeLookupFunction"]
        name = function["Properties"]["FunctionName"]["Fn::Sub"]
        self.assertEqual(group["LogGroupName"], {"Fn::Sub": f"/aws/lambda/{name}"})
        self.assertEqual(group["RetentionInDays"], {"Ref": "LogRetentionDays"})
        self.assertEqual(function["DependsOn"], "Neo4jEeLookupLogGroup")

    def test_kept_resources_wait_for_the_lookup(self) -> None:
        # A failed lookup rolls the create back, and rollback keeps a Retain
        # resource. So no kept resource may exist before the lookup passes.
        resources = self.template["Resources"]
        kept = {
            name: body
            for name, body in resources.items()
            if body.get("DeletionPolicy") == "Retain"
        }
        self.assertEqual(set(kept), {"DataKey", "DataFileSystem"})
        for name, body in kept.items():
            with self.subTest(resource=name):
                self.assertEqual(body["DependsOn"], LOOKUP)

    def test_the_lookup_takes_only_the_stack_name(self) -> None:
        props = self.template["Resources"][LOOKUP]["Properties"]
        self.assertEqual(
            props,
            {
                "ServiceToken": {"Fn::GetAtt": ["Neo4jEeLookupFunction", "Arn"]},
                "ServiceTimeout": 120,
                "EeStackName": {"Ref": "EeStackName"},
            },
        )


if __name__ == "__main__":
    unittest.main()
