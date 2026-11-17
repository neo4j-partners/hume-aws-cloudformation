"""Offline tests for the Phase 1 hardening of hume-ecs.template.yaml.

The template is read and resolved with tests/helpers/template.py, so no AWS
call is made. These tests check what the template says. They cannot show that
AWS accepts it or that a service behaves as the template expects.
docs/guides/testing.md lists the checks that need a deployed stack.

Run from the repository root:
    uv run --with boto3 --with python-dotenv --with pyyaml \
        python -m unittest tests.template.test_hardening
"""

import re
import unittest

import deploy
from tests.helpers import template as tm

TARGET_GROUPS = ["WebTargetGroup", "ApiTargetGroup", "MediaTargetGroup"]
SERVICES = ["Eventstore", "Api", "Media", "Web"]
ALARMS = [
    "WebUnhealthyAlarm",
    "ApiUnhealthyAlarm",
    "MediaUnhealthyAlarm",
    "AlbTarget5xxAlarm",
    "AlbElb5xxAlarm",
    "DatabaseCpuAlarm",
    "DatabaseFreeStorageAlarm",
    "InstanceStatusAlarm",
]
# Service principals are the same in every AWS partition except China, so these
# hosts are allowed in the template. Any other amazonaws.com host fails the lint.
ALLOWED_SERVICE_HOSTS = {
    "ec2.amazonaws.com",
    "ecs-tasks.amazonaws.com",
    "lambda.amazonaws.com",
    "logdelivery.elasticloadbalancing.amazonaws.com",
}
RDS_CA_SOURCE = "/opt/hume/config/rds/global-bundle.pem"
RDS_CA_PATH = "/opt/hume-rds-ca.pem"
# Each Hume task that connects to RDS, with the database in its JDBC URL.
JDBC_TASKS = {
    "EventstoreTaskDefinition": "eventstore",
    "OrchestraTaskDefinition": "orchestra",
    "ApiTaskDefinition": "hume",
    "MediaTaskDefinition": "media",
}
RDS_TASKS = [*JDBC_TASKS, "DbInitTaskDefinition"]
# The five Hume service keys from finalize phase 4: each setting on each
# service, with the stack secret that feeds it. No other task gets a key.
SERVICE_KEYS = {
    "ApiTaskDefinition": {
        "internal.hume.media.api-key": "ApiToMediaKeySecret",
        "hume.security.api-key": "MediaToApiKeySecret",
        "hume.security.symmetric-key": "SymmetricKeySecret",
        "hume.security.api-key-download.value": "MediaDownloadKeySecret",
    },
    "MediaTaskDefinition": {
        "hume.security.api-key.value": "ApiToMediaKeySecret",
        "hume.security.api-key": "MediaToApiKeySecret",
        "hume.security.symmetric-key": "SymmetricKeySecret",
        "hume.security.api-key-download.value": "MediaDownloadKeySecret",
        "hume.security.api-key-maintenance.value": "MaintenanceKeySecret",
    },
}
KEY_SECRETS = {
    secret for settings in SERVICE_KEYS.values() for secret in settings.values()
}
KEY_SETTINGS = {setting for settings in SERVICE_KEYS.values() for setting in settings}
# The four new keys use the format of the local test: 64 lowercase hex
# characters. The download key keeps its format, because a change to
# GenerateSecretString could generate a new value on update.
HEX_64 = {
    "PasswordLength": 64,
    "ExcludePunctuation": True,
    "ExcludeUppercase": True,
    "ExcludeCharacters": "ghijklmnopqrstuvwxyz",
}
KEY_FORMATS = {
    "ApiToMediaKeySecret": HEX_64,
    "MediaToApiKeySecret": HEX_64,
    "SymmetricKeySecret": HEX_64,
    "MaintenanceKeySecret": HEX_64,
    "MediaDownloadKeySecret": {"PasswordLength": 32, "ExcludePunctuation": True},
}
SPINE_ON = {
    "SpineConfigSecretArn": (
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:"
        "hume/stack-x/spine-config-AbCdEf"
    )
}
NATIVE_ON = {"AuthProvider": "Native", "OidcIssuerUri": "", "OidcClientId": ""}


def statements(policy: dict) -> list[dict]:
    return policy["PolicyDocument"]["Statement"]


def actions(statement: dict) -> list[str]:
    action = statement["Action"]
    return [action] if isinstance(action, str) else list(action)


def strings(node):
    """Yield every string in a loaded tree, including Sub text."""
    for item in tm.walk(node):
        if isinstance(item, str):
            yield item


class Phase1Case(tm.TemplateTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.raw = cls.template["Resources"]
        cls.resources = cls.base["resources"]


class LoadBalancerTest(Phase1Case):
    def attributes(self) -> dict:
        pairs = self.raw["LoadBalancer"]["Properties"]["LoadBalancerAttributes"]
        return {pair["Key"]: pair["Value"] for pair in pairs}

    def test_invalid_header_fields_are_dropped(self) -> None:
        self.assertEqual(
            self.attributes()["routing.http.drop_invalid_header_fields.enabled"],
            "true",
        )

    def test_access_logs_go_to_the_bucket_under_a_prefix(self) -> None:
        attributes = self.attributes()
        self.assertEqual(attributes["access_logs.s3.enabled"], "true")
        self.assertEqual(
            attributes["access_logs.s3.bucket"], {"Ref": "AccessLogBucket"}
        )
        self.assertEqual(attributes["access_logs.s3.prefix"], "alb")

    def test_the_load_balancer_waits_for_the_bucket_policy(self) -> None:
        self.assertEqual(self.raw["LoadBalancer"]["DependsOn"], "AccessLogBucketPolicy")

    def test_only_a_200_makes_a_target_healthy(self) -> None:
        for name in TARGET_GROUPS:
            matcher = self.raw[name]["Properties"]["Matcher"]
            self.assertEqual(matcher, {"HttpCode": "200"}, name)

    def test_health_checks_and_listener_ports(self) -> None:
        paths = {
            name: self.raw[name]["Properties"]["HealthCheckPath"]
            for name in TARGET_GROUPS
        }
        self.assertEqual(
            paths,
            {
                "WebTargetGroup": "/",
                "ApiTargetGroup": "/actuator/health",
                "MediaTargetGroup": "/actuator/health",
            },
        )
        ports = {
            name: self.raw[name]["Properties"]["HealthCheckPort"]
            for name in TARGET_GROUPS
        }
        self.assertEqual(
            ports,
            {
                "WebTargetGroup": "traffic-port",
                "ApiTargetGroup": "7001",
                "MediaTargetGroup": "7001",
            },
        )
        listeners = {
            name: self.raw[name]["Properties"]["Port"]
            for name in ("HttpsListener", "HttpRedirectListener")
        }
        self.assertEqual(listeners, {"HttpsListener": 443, "HttpRedirectListener": 80})

    def test_every_service_except_web_moves_its_management_port(self) -> None:
        for service in ("Eventstore", "Orchestra", "Api", "Media"):
            task = self.resources[f"{service}TaskDefinition"]["Properties"]
            (definition,) = task["ContainerDefinitions"]
            env = {e["Name"]: e["Value"] for e in definition["Environment"]}
            self.assertEqual(env["management.server.port"], "7001", service)
        web = self.resources["WebTaskDefinition"]["Properties"]
        (definition,) = web["ContainerDefinitions"]
        names = {e["Name"] for e in definition["Environment"]}
        self.assertNotIn("management.server.port", names)

    def test_the_groups_allow_the_management_port_health_checks(self) -> None:
        for name in (
            "AlbEgressManagementHealth",
            "InstanceIngressFromAlbManagementHealth",
        ):
            props = self.raw[name]["Properties"]
            self.assertEqual((props["FromPort"], props["ToPort"]), (7001, 7001), name)


class AccessLogBucketTest(Phase1Case):
    def properties(self) -> dict:
        return self.raw["AccessLogBucket"]["Properties"]

    def policy(self) -> dict:
        return {
            s["Sid"]: s
            for s in self.raw["AccessLogBucketPolicy"]["Properties"]["PolicyDocument"][
                "Statement"
            ]
        }

    def test_the_bucket_is_encrypted_with_s3_managed_keys(self) -> None:
        (rule,) = self.properties()["BucketEncryption"][
            "ServerSideEncryptionConfiguration"
        ]
        self.assertEqual(
            rule["ServerSideEncryptionByDefault"], {"SSEAlgorithm": "AES256"}
        )

    def test_all_public_access_is_blocked(self) -> None:
        block = self.properties()["PublicAccessBlockConfiguration"]
        self.assertEqual(
            block,
            {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
        )

    def test_the_expiry_is_a_parameter(self) -> None:
        (rule,) = self.properties()["LifecycleConfiguration"]["Rules"]
        self.assertEqual(rule["Status"], "Enabled")
        self.assertEqual(rule["ExpirationInDays"], {"Ref": "AccessLogExpirationDays"})
        spec = self.template["Parameters"]["AccessLogExpirationDays"]
        self.assertEqual(spec["Default"], 90)
        self.assertGreaterEqual(spec["MinValue"], 1)

    def test_the_bucket_has_no_fixed_name(self) -> None:
        # A fixed name would clash between stacks and tie the template to one Region.
        self.assertNotIn("BucketName", self.properties())

    def test_plain_http_is_denied(self) -> None:
        deny = self.policy()["DenyInsecureTransport"]
        self.assertEqual(deny["Effect"], "Deny")
        self.assertEqual(deny["Principal"], "*")
        self.assertEqual(deny["Condition"], {"Bool": {"aws:SecureTransport": "false"}})
        self.assertEqual(len(deny["Resource"]), 2)

    def test_only_the_log_delivery_service_may_write(self) -> None:
        allow = self.policy()["AllowLoadBalancerLogDelivery"]
        self.assertEqual(allow["Effect"], "Allow")
        self.assertEqual(
            allow["Principal"],
            {"Service": "logdelivery.elasticloadbalancing.amazonaws.com"},
        )
        self.assertEqual(allow["Action"], "s3:PutObject")
        resource = allow["Resource"]["Fn::Sub"]
        self.assertIn("/alb/AWSLogs/${AWS::AccountId}/*", resource)

    def test_the_source_is_limited_to_load_balancers_of_this_account(self) -> None:
        allow = self.policy()["AllowLoadBalancerLogDelivery"]
        source = allow["Condition"]["ArnLike"]["aws:SourceArn"]["Fn::Sub"]
        self.assertEqual(
            source,
            "arn:${AWS::Partition}:elasticloadbalancing:${AWS::Region}:"
            "${AWS::AccountId}:loadbalancer/*",
        )

    def test_the_prefix_matches_the_policy(self) -> None:
        prefix = {
            pair["Key"]: pair["Value"]
            for pair in self.raw["LoadBalancer"]["Properties"]["LoadBalancerAttributes"]
        }["access_logs.s3.prefix"]
        resource = self.policy()["AllowLoadBalancerLogDelivery"]["Resource"]["Fn::Sub"]
        self.assertIn(f"/{prefix}/AWSLogs/", resource)
        self.assertNotIn("AWSLogs", prefix)


class WafTest(Phase1Case):
    def rules(self) -> dict:
        return {
            rule["Name"]: rule for rule in self.raw["WebAcl"]["Properties"]["Rules"]
        }

    def test_the_managed_rule_group_blocks(self) -> None:
        rule = self.rules()["aws-common-rules"]
        self.assertEqual(rule["OverrideAction"], {"None": {}})

    def test_only_the_body_size_rule_counts(self) -> None:
        for name, rule in self.rules().items():
            self.assertNotIn("Count", rule.get("OverrideAction", {}), name)
            self.assertNotIn("Count", rule.get("Action", {}), name)
        group = self.rules()["aws-common-rules"]["Statement"][
            "ManagedRuleGroupStatement"
        ]
        self.assertEqual(
            group["RuleActionOverrides"],
            [{"Name": "SizeRestrictions_BODY", "ActionToUse": {"Count": {}}}],
        )

    def test_the_rate_rule_still_blocks(self) -> None:
        self.assertEqual(self.rules()["rate-limit-per-ip"]["Action"], {"Block": {}})

    def test_the_log_group_name_starts_with_the_required_prefix(self) -> None:
        name = self.raw["WebAclLogGroup"]["Properties"]["LogGroupName"]
        self.assertTrue(name["Fn::Sub"].startswith("aws-waf-logs-"))
        self.assertEqual(
            self.raw["WebAclLogGroup"]["Properties"]["RetentionInDays"],
            {"Ref": "LogRetentionDays"},
        )

    def test_logging_targets_the_acl_and_the_group_without_a_wildcard(self) -> None:
        props = self.raw["WebAclLogging"]["Properties"]
        self.assertEqual(props["ResourceArn"], {"Fn::GetAtt": ["WebAcl", "Arn"]})
        (destination,) = props["LogDestinationConfigs"]
        text = destination["Fn::Sub"]
        self.assertTrue(text.startswith("arn:${AWS::Partition}:logs:"))
        self.assertTrue(text.endswith(":log-group:${WebAclLogGroup}"))
        self.assertFalse(text.endswith("*"))

    def test_tokens_are_redacted_from_the_log(self) -> None:
        redacted = {
            field["SingleHeader"]["Name"]
            for field in self.raw["WebAclLogging"]["Properties"]["RedactedFields"]
        }
        self.assertEqual(redacted, {"authorization", "cookie"})

    def test_the_acl_is_still_attached_to_the_load_balancer(self) -> None:
        props = self.raw["WebAclAssociation"]["Properties"]
        self.assertEqual(props["ResourceArn"], {"Ref": "LoadBalancer"})


class AlarmTest(Phase1Case):
    def test_every_alarm_notifies_the_topic(self) -> None:
        for name in ALARMS:
            actions_ = self.raw[name]["Properties"]["AlarmActions"]
            self.assertEqual(actions_, [{"Ref": "AlarmTopic"}], name)

    def test_the_required_signals_are_covered(self) -> None:
        got = {
            (
                self.raw[name]["Properties"]["Namespace"],
                self.raw[name]["Properties"]["MetricName"],
            )
            for name in ALARMS
        }
        self.assertEqual(
            got,
            {
                ("AWS/ApplicationELB", "UnHealthyHostCount"),
                ("AWS/ApplicationELB", "HTTPCode_Target_5XX_Count"),
                ("AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count"),
                ("AWS/RDS", "CPUUtilization"),
                ("AWS/RDS", "FreeStorageSpace"),
                ("AWS/EC2", "StatusCheckFailed"),
            },
        )

    def test_each_target_group_has_an_unhealthy_alarm(self) -> None:
        groups = set()
        for name in ALARMS[:3]:
            dimensions = {
                d["Name"]: d["Value"]
                for d in self.raw[name]["Properties"]["Dimensions"]
            }
            self.assertEqual(
                dimensions["LoadBalancer"],
                {"Fn::GetAtt": ["LoadBalancer", "LoadBalancerFullName"]},
            )
            groups.add(dimensions["TargetGroup"]["Fn::GetAtt"][0])
        self.assertEqual(groups, set(TARGET_GROUPS))

    def test_a_missing_metric_does_not_raise_an_alarm(self) -> None:
        for name in ALARMS:
            self.assertEqual(
                self.raw[name]["Properties"]["TreatMissingData"], "notBreaching", name
            )

    def test_free_storage_is_below_a_tenth_of_the_volume(self) -> None:
        props = self.raw["DatabaseFreeStorageAlarm"]["Properties"]
        self.assertEqual(props["ComparisonOperator"], "LessThanThreshold")
        self.assertEqual(props["Threshold"], 2 * 1024**3)

    def test_the_instance_alarm_watches_the_auto_scaling_group(self) -> None:
        (dimension,) = self.raw["InstanceStatusAlarm"]["Properties"]["Dimensions"]
        self.assertEqual(
            dimension,
            {"Name": "AutoScalingGroupName", "Value": {"Ref": "AutoScalingGroup"}},
        )

    def test_there_is_no_subscription_without_an_address(self) -> None:
        self.assertNotIn("AlarmEmailSubscription", self.resources)
        self.assertEqual(self.template["Parameters"]["AlarmEmail"]["Default"], "")

    def test_an_address_adds_one_email_subscription(self) -> None:
        resolved = tm.resolve_template(
            self.template, tm.BASE_INPUTS | {"AlarmEmail": "ops@example.test"}
        )
        props = resolved["resources"]["AlarmEmailSubscription"]["Properties"]
        self.assertEqual(props["Protocol"], "email")
        self.assertEqual(props["Endpoint"], "ops@example.test")
        self.assertEqual(props["TopicArn"], {"Ref": "AlarmTopic"})

    def test_the_address_pattern_accepts_empty_and_an_address_only(self) -> None:
        pattern = self.template["Parameters"]["AlarmEmail"]["AllowedPattern"]
        for good in ("", "ops@example.test", "a.b+c@sub.example.test"):
            self.assertRegex(good, pattern)
        for bad in ("ops", "ops@example", "ops @example.test", "a@b@c.test"):
            self.assertNotRegex(bad, pattern)


class DatabaseTest(Phase1Case):
    def test_backup_retention_is_a_parameter_that_defaults_to_seven(self) -> None:
        self.assertEqual(
            self.raw["Database"]["Properties"]["BackupRetentionPeriod"],
            {"Ref": "DbBackupRetentionDays"},
        )
        spec = self.template["Parameters"]["DbBackupRetentionDays"]
        self.assertEqual(spec["Default"], 7)
        self.assertEqual((spec["MinValue"], spec["MaxValue"]), (1, 35))

    def test_deletion_protection_is_on(self) -> None:
        self.assertIs(self.raw["Database"]["Properties"]["DeletionProtection"], True)

    def test_a_snapshot_is_kept_on_delete_and_on_replacement(self) -> None:
        self.assertEqual(self.raw["Database"]["DeletionPolicy"], "Snapshot")
        self.assertEqual(self.raw["Database"]["UpdateReplacePolicy"], "Snapshot")

    def test_the_database_uses_the_stack_key(self) -> None:
        props = self.resources["Database"]["Properties"]
        self.assertIs(props["StorageEncrypted"], True)
        self.assertEqual(props["KmsKeyId"], {"Fn::GetAtt": ["DataKey", "Arn"]})


class KeyTest(Phase1Case):
    def test_the_key_rotates_and_is_kept_when_the_stack_goes(self) -> None:
        key = self.raw["DataKey"]
        self.assertIs(key["Properties"]["EnableKeyRotation"], True)
        self.assertEqual(key["DeletionPolicy"], "Retain")
        self.assertEqual(key["UpdateReplacePolicy"], "Retain")

    def test_the_key_policy_delegates_to_the_account_in_its_partition(self) -> None:
        (statement,) = self.raw["DataKey"]["Properties"]["KeyPolicy"]["Statement"]
        self.assertEqual(
            statement["Principal"]["AWS"],
            {"Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:root"},
        )

    def test_the_alias_points_at_the_key(self) -> None:
        props = self.resources["DataKeyAlias"]["Properties"]
        self.assertEqual(props["TargetKeyId"], {"Ref": "DataKey"})
        self.assertTrue(props["AliasName"]["Fn::Sub"].startswith("alias/hume/"))

    def test_every_stack_secret_uses_the_key(self) -> None:
        # Native auth adds two secrets and the Spine import adds one. Check
        # them all.
        resources = tm.resolve_template(
            self.template, tm.BASE_INPUTS | NATIVE_ON | SPINE_ON
        )["resources"]
        names = [
            name
            for name, spec in resources.items()
            if spec["Type"] == "AWS::SecretsManager::Secret"
        ]
        self.assertEqual(len(names), 14)
        for name in names:
            self.assertEqual(
                resources[name]["Properties"]["KmsKeyId"], {"Ref": "DataKey"}, name
            )

    def test_the_roles_may_decrypt_with_the_key(self) -> None:
        key = {"Fn::GetAtt": ["DataKey", "Arn"]}
        for name in ("TaskExecutionSecretsPolicy", "InstanceBootPolicy"):
            props = self.resources[name]["Properties"]
            found = [
                s
                for s in statements(props)
                if "Action" in s
                and "kms:Decrypt" in actions(s)
                and s["Resource"] == key
            ]
            self.assertEqual(len(found), 1, name)
        self.assertEqual(
            self.raw["TaskExecutionSecretsPolicy"]["Properties"]["RoleName"],
            {"Ref": "TaskExecutionRole"},
        )
        self.assertEqual(
            self.raw["InstanceBootPolicy"]["Properties"]["Roles"],
            [{"Ref": "InstanceRole"}],
        )

    def test_the_key_is_not_open_to_a_wildcard_principal(self) -> None:
        for node in tm.walk(self.raw["DataKey"]):
            if isinstance(node, dict) and "Principal" in node:
                self.assertNotEqual(node["Principal"], "*")


class CopiesTest(Phase1Case):
    def test_copies_default_to_one_and_two_resolve_except_for_orchestra(self) -> None:
        for service in SERVICES:
            spec = self.template["Parameters"][f"{service}Copies"]
            self.assertEqual(spec["Type"], "Number", service)
            self.assertEqual(spec["Default"], 1, service)
            self.assertEqual(spec["MinValue"], 1, service)
        inputs = {f"{service}Copies": 2 for service in SERVICES}
        resolved = tm.resolve_template(self.template, tm.BASE_INPUTS | inputs)
        counts = {
            name: resolved["resources"][f"{name}Service"]["Properties"]["DesiredCount"]
            for name in SERVICES + ["Orchestra"]
        }
        self.assertEqual(
            counts, {"Eventstore": 2, "Api": 2, "Media": 2, "Web": 2, "Orchestra": 1}
        )

    def test_orchestra_stays_at_one_and_has_no_parameter(self) -> None:
        props = self.raw["OrchestraService"]["Properties"]
        self.assertEqual(props["DesiredCount"], 1)
        copies = [name for name in self.template["Parameters"] if "Orchestra" in name]
        self.assertEqual(copies, [])


class PartitionLintTest(Phase1Case):
    """Every ARN uses the partition pseudo parameter."""

    def checked_strings(self):
        for section in ("Resources", "Outputs", "Conditions"):
            yield from strings(self.template.get(section, {}))

    def test_no_arn_names_a_fixed_partition(self) -> None:
        for text in self.checked_strings():
            for match in re.finditer(r"arn:(\$\{AWS::Partition\}|[^:\s]*):", text):
                self.assertEqual(match.group(1), "${AWS::Partition}", text)

    def test_no_china_or_fixed_regional_host(self) -> None:
        for text in self.checked_strings():
            self.assertNotIn("amazonaws.com.cn", text)
            for host in re.findall(r"[A-Za-z0-9.${}:-]+\.amazonaws\.com", text):
                self.assertIn(host, ALLOWED_SERVICE_HOSTS, text)

    def test_the_input_arn_patterns_accept_any_partition_and_gov_cloud(self) -> None:
        for name, spec in self.template["Parameters"].items():
            pattern = spec.get("AllowedPattern", "")
            if "arn:" in pattern:
                self.assertIn("arn:aws[a-z-]*:", pattern, name)

        pattern = self.template["Parameters"]["HumeLicenceSecretArn"]["AllowedPattern"]
        arn = "arn:aws-us-gov:secretsmanager:us-gov-west-1:123456789012:secret:x-AbCdEf"
        self.assertRegex(arn, pattern)


class RdsCertificateTest(Phase1Case):
    def container(self, task: str) -> dict:
        return self.resources[task]["Properties"]["ContainerDefinitions"][0]

    def environment(self, task: str) -> dict:
        return {e["Name"]: e["Value"] for e in self.container(task)["Environment"]}

    def test_each_task_mounts_the_bundle_read_only(self) -> None:
        mount = {
            "SourceVolume": "rds-ca",
            "ContainerPath": RDS_CA_PATH,
            "ReadOnly": True,
        }
        for task in RDS_TASKS:
            with self.subTest(task=task):
                volumes = self.resources[task]["Properties"]["Volumes"]
                sources = {
                    v["Name"]: v["Host"]["SourcePath"] for v in volumes if "Host" in v
                }
                self.assertEqual(sources["rds-ca"], RDS_CA_SOURCE)
                self.assertIn(mount, self.container(task)["MountPoints"])

    def test_each_jdbc_url_verifies_the_certificate_with_the_bundle(self) -> None:
        for task, database in JDBC_TASKS.items():
            with self.subTest(task=task):
                self.assertEqual(
                    self.environment(task)["spring.datasource.url"],
                    {
                        "Fn::Sub": "jdbc:postgresql://${Database.Endpoint.Address}"
                        f":5432/{database}"
                        f"?sslmode=verify-full&sslrootcert={RDS_CA_PATH}"
                    },
                )

    def test_db_init_verifies_the_certificate_with_the_bundle(self) -> None:
        environment = self.environment("DbInitTaskDefinition")
        self.assertEqual(environment["PGSSLMODE"], "verify-full")
        self.assertEqual(environment["PGSSLROOTCERT"], RDS_CA_PATH)


class LiquibaseAnalyticsTest(Phase1Case):
    def test_the_services_turn_off_liquibase_analytics(self) -> None:
        for task in (
            "EventstoreTaskDefinition",
            "OrchestraTaskDefinition",
            "MediaTaskDefinition",
        ):
            with self.subTest(task):
                properties = self.resources[task]["Properties"]
                container = properties["ContainerDefinitions"][0]
                environment = {
                    e["Name"]: e["Value"]
                    for e in container["Environment"]
                    if "Name" in e
                }
                self.assertEqual(environment["LIQUIBASE_ANALYTICS_ENABLED"], "false")


class ServiceKeyTest(Phase1Case):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.variants = {
            "oidc": cls.resources,
            "spine": tm.resolve_template(cls.template, tm.BASE_INPUTS | SPINE_ON)[
                "resources"
            ],
            "native": tm.resolve_template(cls.template, tm.BASE_INPUTS | NATIVE_ON)[
                "resources"
            ],
        }

    @staticmethod
    def containers(resources: dict):
        for name, spec in resources.items():
            if spec["Type"] == "AWS::ECS::TaskDefinition":
                for definition in spec["Properties"]["ContainerDefinitions"]:
                    yield name, definition

    def test_the_five_keys_are_stack_secrets_under_the_stack_key(self) -> None:
        self.assertEqual(len(KEY_SECRETS), 5)
        for name in KEY_SECRETS:
            spec = self.raw[name]
            self.assertEqual(spec["Type"], "AWS::SecretsManager::Secret", name)
            self.assertNotIn("Condition", spec, name)
            props = self.resources[name]["Properties"]
            self.assertEqual(props["KmsKeyId"], {"Ref": "DataKey"}, name)
            self.assertTrue(
                props["Name"]["Fn::Sub"].startswith("hume/${AWS::StackName}/"), name
            )

    def test_each_key_is_generated_with_its_fixed_format(self) -> None:
        for name, generate in KEY_FORMATS.items():
            props = self.raw[name]["Properties"]
            self.assertNotIn("SecretString", props, name)
            self.assertEqual(props["GenerateSecretString"], generate, name)

    def key_secrets(self, resources: dict, task: str) -> dict[str, str]:
        """Return each key setting of a task with the secret that feeds it."""
        (definition,) = resources[task]["Properties"]["ContainerDefinitions"]
        return {
            secret["Name"]: secret["ValueFrom"]["Ref"]
            for secret in definition["Secrets"]
            if isinstance(secret["ValueFrom"], dict)
            and secret["ValueFrom"].get("Ref") in KEY_SECRETS
        }

    def test_no_two_keys_can_be_equal(self) -> None:
        # Each key is its own generated secret, and no service feeds two of
        # its key settings from the same secret.
        names = [
            self.raw[name]["Properties"]["Name"]["Fn::Sub"] for name in KEY_SECRETS
        ]
        self.assertEqual(len(set(names)), len(KEY_SECRETS))
        for task in SERVICE_KEYS:
            fed = self.key_secrets(self.resources, task)
            self.assertEqual(len(set(fed.values())), len(fed), task)

    def test_api_and_media_get_exactly_their_keys_as_secrets(self) -> None:
        for variant, resources in self.variants.items():
            for task, expected in SERVICE_KEYS.items():
                with self.subTest(variant=variant, task=task):
                    self.assertEqual(self.key_secrets(resources, task), expected)
                    (definition,) = resources[task]["Properties"][
                        "ContainerDefinitions"
                    ]
                    environment = {e["Name"] for e in definition["Environment"]}
                    self.assertFalse(environment & KEY_SETTINGS)

    def test_no_other_task_gets_a_key(self) -> None:
        for variant, resources in self.variants.items():
            for task, definition in self.containers(resources):
                if task in SERVICE_KEYS:
                    continue
                with self.subTest(variant=variant, task=task):
                    names = {e["Name"] for e in definition.get("Environment", [])}
                    names |= {s["Name"] for s in definition.get("Secrets", [])}
                    self.assertFalse(names & KEY_SETTINGS)
                    refs = {
                        item["Ref"]
                        for item in tm.walk(definition)
                        if isinstance(item, dict) and "Ref" in item
                    }
                    self.assertFalse(refs & KEY_SECRETS)
                    mentions = [
                        text
                        for text in strings(definition)
                        if any(name in text for name in KEY_SECRETS)
                    ]
                    self.assertFalse(mentions)

    def test_the_execution_role_reads_the_keys_by_the_stack_prefix(self) -> None:
        policy = self.resources["TaskExecutionSecretsPolicy"]["Properties"]
        (read, decrypt) = statements(policy)
        self.assertEqual(actions(read), ["secretsmanager:GetSecretValue"])
        self.assertIn(
            {
                "Fn::Sub": "arn:${AWS::Partition}:secretsmanager:${AWS::Region}:"
                "${AWS::AccountId}:secret:hume/${AWS::StackName}/*"
            },
            read["Resource"],
        )
        self.assertEqual(len(read["Resource"]), 3)
        self.assertEqual(decrypt["Resource"], {"Fn::GetAtt": ["DataKey", "Arn"]})
        for task in SERVICE_KEYS:
            self.assertEqual(
                self.raw[task]["Properties"]["ExecutionRoleArn"],
                {"Fn::GetAtt": ["TaskExecutionRole", "Arn"]},
                task,
            )


class UnchangedSettingsTest(Phase1Case):
    def test_no_task_definition_sets_a_jvm_option_in_the_environment(self) -> None:
        # Orchestra gets the JVM DNS cache TTL through the truststore secret. The
        # other tasks wait on GraphAware, because nothing shows that their images
        # read JAVA_TOOL_OPTIONS.
        for name, spec in self.resources.items():
            if spec["Type"] != "AWS::ECS::TaskDefinition":
                continue
            for definition in spec["Properties"]["ContainerDefinitions"]:
                names = {e["Name"] for e in definition.get("Environment", [])}
                self.assertNotIn("JAVA_TOOL_OPTIONS", names, name)


class TemplateSecretArnPatternTest(unittest.TestCase):
    PREFIX = "arn:aws:secretsmanager:us-east-1:123456789012:secret:"
    LICENCE = PREFIX + "hume/licence-key-AbC123"
    PARTIAL = PREFIX + "hume/licence-key"

    def pattern(self, parameter: str) -> re.Pattern[str]:
        text = deploy.TEMPLATE.read_text()
        block = re.search(
            rf"^  {parameter}:\n(?:    .*\n)*?    AllowedPattern: '(.*)'$",
            text,
            re.MULTILINE,
        )
        self.assertIsNotNone(block, parameter)
        return re.compile(block.group(1))

    def test_the_licence_parameter_accepts_a_full_arn_and_rejects_partial_ones(
        self,
    ) -> None:
        pattern = self.pattern("HumeLicenceSecretArn")
        self.assertTrue(pattern.match(self.LICENCE))
        self.assertFalse(pattern.match(self.PARTIAL))
        self.assertFalse(pattern.match("hume/licence-key"))


class ApiPrometheusBlockTest(unittest.TestCase):
    """The api listener refuses the metrics path and leaves everything else alone."""

    def setUp(self) -> None:
        text = deploy.TEMPLATE.read_text()
        starts = list(re.finditer(r"^  (\w+):\n    Type: (\S+)\n", text, re.MULTILINE))
        ends = [m.start() for m in starts[1:]] + [len(text)]
        self.blocks = {
            m.group(1): (m.group(2), text[m.start() : end])
            for m, end in zip(starts, ends)
        }

    def rules(self) -> dict[str, str]:
        return {
            name: block
            for name, (kind, block) in self.blocks.items()
            if kind == "AWS::ElasticLoadBalancingV2::ListenerRule"
        }

    def test_the_rule_answers_403_for_the_metrics_path_on_the_api_host(self) -> None:
        block = self.rules()["ApiPrometheusBlockRule"]
        self.assertIn("ListenerArn: !Ref HttpsListener\n", block)
        self.assertIn("Priority: 10\n", block)
        self.assertIn("hume-api.${BaseDomainName}", block)
        paths = re.findall(r"^\s+- (/\S+)$", block, re.MULTILINE)
        self.assertEqual(paths, ["/actuator/prometheus*"])
        self.assertIn("Type: fixed-response", block)
        self.assertIn("StatusCode: '403'", block)
        self.assertNotIn("forward", block)

    def test_the_path_pattern_does_not_match_the_health_path(self) -> None:
        block = self.rules()["ApiPrometheusBlockRule"]
        (pattern,) = re.findall(r"^\s+- (/\S+)$", block, re.MULTILINE)
        regex = re.compile(re.escape(pattern).replace(r"\*", ".*") + "$")
        for blocked in ("/actuator/prometheus", "/actuator/prometheus/x"):
            self.assertTrue(regex.match(blocked), blocked)
        for open_path in ("/actuator/health", "/actuator", "/", "/v1/upload"):
            self.assertFalse(regex.match(open_path), open_path)

    def test_each_host_has_one_ordered_rule_on_the_https_listener(self) -> None:
        rules = self.rules()
        self.assertEqual(
            set(rules),
            {
                "ApiPrometheusBlockRule",
                "ApiForwardRule",
                "MediaForwardRule",
                "WebForwardRule",
            },
        )
        for name, priority, host, target in (
            ("ApiForwardRule", 20, "hume-api", "ApiTargetGroup"),
            ("MediaForwardRule", 30, "hume-media", "MediaTargetGroup"),
            ("WebForwardRule", 40, "hume", "WebTargetGroup"),
        ):
            block = rules[name]
            self.assertIn("ListenerArn: !Ref HttpsListener\n", block)
            self.assertIn(f"Priority: {priority}\n", block)
            self.assertIn(f"{host}.${{BaseDomainName}}", block)
            self.assertIn(f"TargetGroupArn: !Ref {target}", block)
        self.assertIn("/v1/download*", rules["MediaForwardRule"])

    def test_the_https_default_is_a_fixed_403_and_port_80_only_redirects(self) -> None:
        https = self.blocks["HttpsListener"][1]
        self.assertIn("Port: 443", https)
        self.assertIn("Type: fixed-response", https)
        self.assertIn("StatusCode: '403'", https)
        self.assertNotIn("Type: forward", https)
        redirect = self.blocks["HttpRedirectListener"][1]
        self.assertIn("Port: 80", redirect)
        self.assertIn("Type: redirect", redirect)
        self.assertIn("StatusCode: HTTP_301", redirect)
        self.assertNotIn("Type: forward", redirect)

    def test_the_template_builds_the_hosts_the_scripts_expect(self) -> None:
        hosts = deploy.hume_hosts("${BaseDomainName}")
        for host, logical_id in zip(
            hosts,
            ("AlbAliasRecord", "AlbAliasRecordApi", "AlbAliasRecordMedia"),
            strict=True,
        ):
            self.assertIn(host, self.blocks[logical_id][1], logical_id)
        certificate = self.blocks["Certificate"][1]
        for host in hosts:
            self.assertIn(host, certificate)


if __name__ == "__main__":
    unittest.main()
