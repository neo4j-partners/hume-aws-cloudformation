"""Offline tests for EFS persistence and the restore from an earlier stack.

The template is resolved with tests/helpers/template.py, so no AWS call is
made. These tests check what the template says. A deployed stack must still
show that a restore brings Hume back with its data.

Run from the repository root:
    uv run --with boto3 --with python-dotenv --with pyyaml \
        python -m unittest tests.template.test_persistence_restore
"""

import re
import unittest

from tests.helpers import template as tm

SNAPSHOT = "hume-old-final-snapshot"
FILE_SYSTEM = "fs-0123456789abcdef0"
KEY_ARN = "arn:aws:kms:us-east-1:123456789012:key/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
RESTORE_ON = {
    "DbSnapshotIdentifier": SNAPSHOT,
    "ExistingFileSystemId": FILE_SYSTEM,
    "ExistingDataKeyArn": KEY_ARN,
}
NATIVE_ON = {"AuthProvider": "Native", "OidcIssuerUri": "", "OidcClientId": ""}
NEW_KEY = {"Ref": "DataKey"}
NEW_KEY_ARN = {"Fn::GetAtt": ["DataKey", "Arn"]}
NEW_FILE_SYSTEM = {"Ref": "DataFileSystem"}
RESET_RESOURCES = {
    "DbMasterResetLogGroup": "AWS::Logs::LogGroup",
    "DbMasterResetRole": "AWS::IAM::Role",
    "DbMasterResetFunction": "AWS::Lambda::Function",
    "DbMasterReset": "Custom::DbMasterPasswordReset",
}
# Each EFS volume: task definition, volume name, access point, root path.
EFS_VOLUMES = {
    "OrchestraTaskDefinition": ("data", "OrchestraAccessPoint", "/orchestra"),
    "ApiTaskDefinition": ("audit", "AuditLogsAccessPoint", "/audit-logs"),
    "MediaTaskDefinition": ("files", "MediaAccessPoint", "/media"),
}
EFS_CHILDREN = [
    "DataMountTarget1",
    "DataMountTarget2",
    "OrchestraAccessPoint",
    "AuditLogsAccessPoint",
    "MediaAccessPoint",
]
EFS_SERVICES = ["OrchestraService", "ApiService", "MediaService"]
# The rules the ECS and SSM agents and the load balancer health checks use.
# Every service depends on them, so a delete removes the services first.
AGENT_AND_HEALTH_RULES = {
    "InstanceEgressHttps",
    "VpcEndpointIngressFromHume",
    "InstanceEgressSelf",
    "InstanceEgressDatabase",
    "InstanceIngressFromAlbManagementHealth",
    "AlbEgressManagementHealth",
    "InstanceIngressFromAlbWebAndApi",
    "AlbEgressWebAndApi",
}


def resources_of(resolved: dict) -> dict[str, str]:
    return {name: spec["Type"] for name, spec in resolved["resources"].items()}


def efs_volume(resources: dict, task: str) -> dict:
    name = EFS_VOLUMES[task][0]
    (volume,) = [
        v for v in resources[task]["Properties"]["Volumes"] if v["Name"] == name
    ]
    return volume


class RestoreCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = tm.load()
        cls.raw = cls.template["Resources"]
        cls.base = tm.resolve_template(cls.template, tm.BASE_INPUTS)
        cls.restore = tm.resolve_template(cls.template, tm.BASE_INPUTS | RESTORE_ON)


class ParameterTest(RestoreCase):
    def test_each_restore_parameter_is_an_optional_string(self) -> None:
        for name in tm.RESTORE_PARAMETERS:
            spec = self.template["Parameters"][name]
            self.assertEqual(spec["Type"], "String", name)
            self.assertEqual(spec["Default"], "", name)
            self.assertIn("on every update", spec["Description"], name)

    def test_the_restore_parameters_have_their_own_group(self) -> None:
        groups = self.template["Metadata"]["AWS::CloudFormation::Interface"][
            "ParameterGroups"
        ]
        (group,) = [
            g
            for g in groups
            if g["Label"]["default"] == "Restore from an earlier stack"
        ]
        self.assertEqual(group["Parameters"], tm.RESTORE_PARAMETERS)

    def test_the_snapshot_description_says_to_keep_the_snapshot(self) -> None:
        text = self.template["Parameters"]["DbSnapshotIdentifier"]["Description"]
        self.assertIn("Keep the snapshot until the restored stack is proven", text)

    def test_the_patterns_accept_empty_and_good_values(self) -> None:
        good = {
            "DbSnapshotIdentifier": [
                SNAPSHOT,
                "rds:hume-old-2026-11-11-03-10",
                "arn:aws-us-gov:rds:us-gov-west-1:123456789012:snapshot:hume-old",
            ],
            "ExistingFileSystemId": [FILE_SYSTEM, "fs-01234567"],
            "ExistingDataKeyArn": [
                KEY_ARN,
                KEY_ARN.replace("arn:aws:", "arn:aws-us-gov:").replace(
                    "us-east-1", "us-gov-west-1"
                ),
            ],
        }
        for name, values in good.items():
            pattern = self.template["Parameters"][name]["AllowedPattern"]
            for value in ["", *values]:
                self.assertRegex(value, pattern, name)

    def test_the_patterns_reject_bad_values(self) -> None:
        bad = {
            "DbSnapshotIdentifier": ["hume old", "hume/old", "x;y"],
            "ExistingFileSystemId": ["fs-xyz", "0123456789abcdef0", " fs-01234567"],
            "ExistingDataKeyArn": [
                "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b",
                "alias/hume/old",
                "arn:aws:kms:us-east-1:123456789012:alias/hume-old",
            ],
        }
        for name, values in bad.items():
            pattern = self.template["Parameters"][name]["AllowedPattern"]
            for value in values:
                self.assertNotRegex(value, pattern, name)


class RulesTest(RestoreCase):
    def test_a_snapshot_or_a_kept_file_system_needs_the_kept_key(self) -> None:
        cases = {
            "DbSnapshotIdentifier": "DbSnapshotIdentifier is set",
            "ExistingFileSystemId": "ExistingFileSystemId is set",
        }
        for name, phrase in cases.items():
            with self.subTest(name=name):
                (failed,) = tm.failed_rules(self.template, {name: RESTORE_ON[name]})
                self.assertIn("ExistingDataKeyArn is required", failed)
                self.assertIn(phrase, failed)
                self.assertEqual(
                    tm.failed_rules(
                        self.template, {name: RESTORE_ON[name]} | _key_only()
                    ),
                    [],
                )

    def test_both_without_the_key_fail_both_rules(self) -> None:
        inputs = {
            "DbSnapshotIdentifier": SNAPSHOT,
            "ExistingFileSystemId": FILE_SYSTEM,
        }
        self.assertEqual(len(tm.failed_rules(self.template, inputs)), 2)

    def test_the_key_alone_and_all_three_pass(self) -> None:
        self.assertEqual(tm.failed_rules(self.template, _key_only()), [])
        self.assertEqual(tm.failed_rules(self.template, RESTORE_ON), [])


def _key_only() -> dict[str, str]:
    return {"ExistingDataKeyArn": KEY_ARN}


class ResourceSetTest(RestoreCase):
    def test_a_restore_swaps_the_key_and_file_system_for_the_reset(self) -> None:
        expected = {
            name: kind
            for name, kind in tm.BASE_RESOURCES.items()
            if name not in {"DataKey", "DataFileSystem"}
        }
        self.assertEqual(resources_of(self.restore), expected | RESET_RESOURCES)
        self.assertEqual(set(self.restore["outputs"]), tm.BASE_OUTPUTS)

    def test_each_parameter_on_its_own_changes_only_its_resources(self) -> None:
        cases = {
            "ExistingDataKeyArn": {"DataKey"},
            "ExistingFileSystemId": {"DataFileSystem"},
        }
        for name, dropped in cases.items():
            with self.subTest(name=name):
                got = resources_of(
                    tm.resolve_template(
                        self.template, tm.BASE_INPUTS | {name: RESTORE_ON[name]}
                    )
                )
                self.assertEqual(set(tm.BASE_RESOURCES) - set(got), dropped)
                self.assertEqual(set(got) - set(tm.BASE_RESOURCES), set())
        got = resources_of(
            tm.resolve_template(
                self.template,
                tm.BASE_INPUTS | {"DbSnapshotIdentifier": SNAPSHOT} | _key_only(),
            )
        )
        self.assertEqual(set(got) - set(tm.BASE_RESOURCES), set(RESET_RESOURCES))
        self.assertEqual(set(tm.BASE_RESOURCES) - set(got), {"DataKey"})

    def test_the_kept_resources_are_retained_and_conditional(self) -> None:
        conditions = {
            "DataKey": "CreateDataKey",
            "DataFileSystem": "CreateFileSystem",
        }
        for name, condition in conditions.items():
            spec = self.raw[name]
            self.assertEqual(spec["Condition"], condition, name)
            self.assertEqual(spec["DeletionPolicy"], "Retain", name)
            self.assertEqual(spec["UpdateReplacePolicy"], "Retain", name)
        for name in RESET_RESOURCES:
            self.assertEqual(self.raw[name]["Condition"], "RestoreDatabase", name)


class KeyTest(RestoreCase):
    def key_uses(self, resolved: dict) -> list:
        """Return every place the stack names its data key."""
        resources = resolved["resources"]
        uses = [
            spec["Properties"]["KmsKeyId"]
            for spec in resources.values()
            if spec["Type"] == "AWS::SecretsManager::Secret"
        ]
        uses.append(resources["DataKeyAlias"]["Properties"]["TargetKeyId"])
        for name in ("TaskExecutionSecretsPolicy", "InstanceBootPolicy"):
            uses.extend(
                s["Resource"]
                for s in resources[name]["Properties"]["PolicyDocument"]["Statement"]
                if "kms:Decrypt" in tm.as_list(s["Action"])
            )
        if "DataFileSystem" in resources:
            uses.append(resources["DataFileSystem"]["Properties"]["KmsKeyId"])
        uses.append(resolved["outputs"]["DataKeyArn"]["Value"])
        return uses

    def test_a_new_stack_names_its_own_key_everywhere(self) -> None:
        resolved = tm.resolve_template(self.template, tm.BASE_INPUTS | NATIVE_ON)
        uses = self.key_uses(resolved)
        self.assertGreaterEqual(len(uses), 17)
        for use in uses:
            self.assertIn(use, (NEW_KEY, NEW_KEY_ARN))

    def test_an_adopted_key_is_named_by_its_arn_everywhere(self) -> None:
        resolved = tm.resolve_template(
            self.template, tm.BASE_INPUTS | NATIVE_ON | _key_only()
        )
        uses = self.key_uses(resolved)
        self.assertGreaterEqual(len(uses), 17)
        self.assertEqual(set(uses), {KEY_ARN})

    def test_nothing_names_the_key_resource_when_it_is_adopted(self) -> None:
        for resolved in (
            self.restore,
            tm.resolve_template(self.template, tm.BASE_INPUTS | _key_only()),
        ):
            self.assertNotIn("DataKey", tm.references(resolved))

    def test_the_reset_role_may_decrypt_with_the_adopted_key(self) -> None:
        statements = self.restore["resources"]["DbMasterResetRole"]["Properties"][
            "Policies"
        ][0]["PolicyDocument"]["Statement"]
        (decrypt,) = [s for s in statements if s["Action"] == "kms:Decrypt"]
        self.assertEqual(decrypt["Resource"], KEY_ARN)


class DatabaseTest(RestoreCase):
    def test_a_new_database_is_created_with_the_stack_key_and_secret(self) -> None:
        props = self.base["resources"]["Database"]["Properties"]
        self.assertNotIn("DBSnapshotIdentifier", props)
        self.assertEqual(props["KmsKeyId"], NEW_KEY_ARN)
        self.assertIn("MasterUsername", props)
        self.assertIn("MasterUserPassword", props)
        self.assertIs(props["StorageEncrypted"], True)

    def test_a_restored_database_takes_its_key_and_user_from_the_snapshot(
        self,
    ) -> None:
        props = self.restore["resources"]["Database"]["Properties"]
        self.assertEqual(props["DBSnapshotIdentifier"], SNAPSHOT)
        for name in ("KmsKeyId", "MasterUsername", "MasterUserPassword"):
            self.assertNotIn(name, props)
        self.assertIs(props["StorageEncrypted"], True)

    def test_the_rest_of_the_database_does_not_change_on_restore(self) -> None:
        changed = {"DBSnapshotIdentifier", "KmsKeyId"}
        changed |= {"MasterUsername", "MasterUserPassword"}
        base = self.base["resources"]["Database"]
        restore = self.restore["resources"]["Database"]
        for key in ("DeletionPolicy", "UpdateReplacePolicy"):
            self.assertEqual(base[key], restore[key], key)
        for key, value in base["Properties"].items():
            if key not in changed:
                self.assertEqual(restore["Properties"][key], value, key)


class MasterResetTest(RestoreCase):
    def resource(self, name: str) -> dict:
        return self.restore["resources"][name]

    def test_db_init_waits_for_the_reset_only_on_restore(self) -> None:
        self.assertNotIn(
            "MasterReset", self.base["resources"]["DbInitRun"]["Properties"]
        )
        self.assertEqual(
            self.resource("DbInitRun")["Properties"]["MasterReset"],
            {"Fn::GetAtt": ["DbMasterReset", "Done"]},
        )

    def test_the_reset_sets_the_secret_on_the_database(self) -> None:
        reset = self.resource("DbMasterReset")
        self.assertEqual(reset["DependsOn"], "DbMasterSecretAttachment")
        props = reset["Properties"]
        self.assertEqual(
            props["ServiceToken"], {"Fn::GetAtt": ["DbMasterResetFunction", "Arn"]}
        )
        self.assertEqual(props["DBInstanceIdentifier"], {"Ref": "Database"})
        self.assertEqual(props["SecretArn"], {"Ref": "DbMasterSecret"})
        self.assertEqual(props["ServiceTimeout"], 900)

    def test_the_role_is_limited_to_this_database_and_secret(self) -> None:
        statements = self.resource("DbMasterResetRole")["Properties"]["Policies"][0][
            "PolicyDocument"
        ]["Statement"]
        by_action = {tuple(tm.as_list(s["Action"])): s["Resource"] for s in statements}
        self.assertEqual(
            by_action[("rds:ModifyDBInstance", "rds:DescribeDBInstances")],
            {
                "Fn::Sub": "arn:${AWS::Partition}:rds:${AWS::Region}:"
                "${AWS::AccountId}:db:${Database}"
            },
        )
        self.assertEqual(
            by_action[("secretsmanager:GetSecretValue",)], {"Ref": "DbMasterSecret"}
        )
        for statement in statements:
            self.assertNotEqual(statement["Resource"], "*")

    def test_the_function_never_prints_the_password(self) -> None:
        code = self.resource("DbMasterResetFunction")["Properties"]["Code"]["ZipFile"]
        compile(code, "index.py", "exec")
        for line in code.splitlines():
            if "print(" in line:
                self.assertNotRegex(line, r"\{[^}]*\bpassword\b", line)
        self.assertIn("ApplyImmediately=True", code)
        self.assertIn('"MasterUserPassword" not in pending', code)

    def test_the_function_log_group_is_the_lambda_default(self) -> None:
        name = self.resource("DbMasterResetLogGroup")["Properties"]["LogGroupName"]
        function = self.resource("DbMasterResetFunction")["Properties"]
        self.assertEqual(
            name,
            {"Fn::Sub": "/aws/lambda/" + function["FunctionName"]["Fn::Sub"]},
        )


class FileSystemTest(RestoreCase):
    def test_the_new_file_system_is_encrypted_and_backed_up(self) -> None:
        props = self.base["resources"]["DataFileSystem"]["Properties"]
        self.assertIs(props["Encrypted"], True)
        self.assertEqual(props["KmsKeyId"], NEW_KEY_ARN)
        self.assertEqual(props["BackupPolicy"], {"Status": "ENABLED"})

    def test_mount_targets_and_access_points_use_the_right_file_system(
        self,
    ) -> None:
        for resolved, expected in (
            (self.base, NEW_FILE_SYSTEM),
            (self.restore, FILE_SYSTEM),
        ):
            for name in EFS_CHILDREN:
                props = resolved["resources"][name]["Properties"]
                self.assertEqual(props["FileSystemId"], expected, name)
            self.assertEqual(resolved["outputs"]["FileSystemId"]["Value"], expected)

    def test_mount_targets_sit_in_the_first_two_private_subnets(self) -> None:
        subnets = [
            tm.NEO4J_VALUES["PrivateSubnet1Id"],
            tm.NEO4J_VALUES["PrivateSubnet2Id"],
        ]
        for index, name in enumerate(["DataMountTarget1", "DataMountTarget2"]):
            props = self.base["resources"][name]["Properties"]
            self.assertEqual(props["SubnetId"], subnets[index], name)
            self.assertEqual(props["SecurityGroups"], [{"Ref": "EfsSecurityGroup"}])

    def test_each_access_point_runs_as_hume_in_its_own_directory(self) -> None:
        for _, point, path in EFS_VOLUMES.values():
            props = self.base["resources"][point]["Properties"]
            self.assertEqual(props["PosixUser"], {"Uid": "1001", "Gid": "1001"})
            self.assertEqual(
                props["RootDirectory"],
                {
                    "Path": path,
                    "CreationInfo": {
                        "OwnerUid": "1001",
                        "OwnerGid": "1001",
                        "Permissions": "0700",
                    },
                },
            )

    def test_each_task_mounts_its_access_point_over_tls(self) -> None:
        for resolved, expected in (
            (self.base, NEW_FILE_SYSTEM),
            (self.restore, FILE_SYSTEM),
        ):
            for task, (_, point, _) in EFS_VOLUMES.items():
                with self.subTest(task=task):
                    volume = efs_volume(resolved["resources"], task)
                    self.assertNotIn("Host", volume)
                    self.assertEqual(
                        volume["EFSVolumeConfiguration"],
                        {
                            "FilesystemId": expected,
                            "TransitEncryption": "ENABLED",
                            "AuthorizationConfig": {
                                "AccessPointId": {"Ref": point},
                                "IAM": "DISABLED",
                            },
                        },
                    )

    def test_no_task_mounts_a_host_data_directory(self) -> None:
        for spec in self.base["resources"].values():
            if spec["Type"] != "AWS::ECS::TaskDefinition":
                continue
            for volume in spec["Properties"].get("Volumes", []):
                source = volume.get("Host", {}).get("SourcePath", "")
                self.assertFalse(source.startswith("/opt/hume/data"), source)

    def test_the_services_wait_for_the_mount_targets_and_the_rule(self) -> None:
        needed = {"DataMountTarget1", "DataMountTarget2", "InstanceEgressEfs"}
        for name in EFS_SERVICES:
            depends = set(tm.as_list(self.raw[name].get("DependsOn", [])))
            self.assertLessEqual(needed, depends, name)

    def test_media_mounts_its_volume_at_a_path_the_image_lacks(self) -> None:
        props = self.base["resources"]["MediaTaskDefinition"]["Properties"]
        (container,) = props["ContainerDefinitions"]
        (mount,) = [m for m in container["MountPoints"] if m["SourceVolume"] == "files"]
        self.assertEqual(mount["ContainerPath"], "/hume-media")
        env = {e["Name"]: e["Value"] for e in container["Environment"]}
        prefix = "hume.media.storage.providers.local.path."
        for name in ("uploaded", "ready"):
            self.assertEqual(env[prefix + name], "/hume-media/ready", name)


class NetworkTest(RestoreCase):
    def test_every_service_depends_on_the_agent_and_health_rules(self) -> None:
        services = [
            name
            for name, spec in self.raw.items()
            if spec["Type"] == "AWS::ECS::Service"
        ]
        self.assertEqual(len(services), 5)
        for name in services:
            depends = set(tm.as_list(self.raw[name].get("DependsOn", [])))
            self.assertLessEqual(AGENT_AND_HEALTH_RULES, depends, name)
            for rule in AGENT_AND_HEALTH_RULES:
                self.assertNotIn("Condition", self.raw[rule], rule)

    def test_only_the_instance_group_reaches_nfs(self) -> None:
        group = self.base["resources"]["EfsSecurityGroup"]["Properties"]
        (ingress,) = group["SecurityGroupIngress"]
        self.assertEqual(
            (ingress["IpProtocol"], ingress["FromPort"], ingress["ToPort"]),
            ("tcp", 2049, 2049),
        )
        self.assertEqual(
            ingress["SourceSecurityGroupId"], {"Ref": "InstanceSecurityGroup"}
        )
        (egress,) = group["SecurityGroupEgress"]
        self.assertEqual(egress["CidrIp"], "127.0.0.1/32")

    def test_the_instance_may_send_nfs_only_to_the_mount_targets(self) -> None:
        rule = self.base["resources"]["InstanceEgressEfs"]["Properties"]
        self.assertEqual(rule["GroupId"], {"Ref": "InstanceSecurityGroup"})
        self.assertEqual(
            (rule["IpProtocol"], rule["FromPort"], rule["ToPort"]),
            ("tcp", 2049, 2049),
        )
        self.assertEqual(
            rule["DestinationSecurityGroupId"], {"Ref": "EfsSecurityGroup"}
        )

    def test_no_nfs_rule_is_open_to_a_cidr(self) -> None:
        for spec in self.base["resources"].values():
            for node in tm.walk(spec):
                if isinstance(node, dict) and node.get("FromPort") == 2049:
                    self.assertFalse(re.search("Cidr", " ".join(node)), node)


if __name__ == "__main__":
    unittest.main()
