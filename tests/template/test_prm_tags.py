"""Offline tests for the AWS Partner Revenue Measurement tag in the Hume template.

Every resource that CloudFormation can tag carries one tag. The key names the
Neo4j partner account and the value is the Hume Marketplace product code. Both
live once, in the Prm mapping, and each resource reads them with FindInMap.

The tests read the template as written, so resources behind a condition are
checked too. No AWS call is made.

Run from the repository root:
    uv run --with boto3 --with python-dotenv --with pyyaml \
        python -m unittest tests.template.test_prm_tags
"""

import importlib.util
import unittest

from tests.helpers import template as tm

PRM_KEY = "aws-apn-id-385155106615"
PRM_VALUE = "pc:7zd8meh8xo9g62zqwwvprdw7m"
PRM_ENTRY = {
    "Key": {"Fn::FindInMap": ["Prm", "Tag", "Key"]},
    "Value": {"Fn::FindInMap": ["Prm", "Tag", "Value"]},
}
# Each taggable type with the property that holds its tag list.
TAG_PROPERTY = {
    "AWS::AutoScaling::AutoScalingGroup": "Tags",
    "AWS::CertificateManager::Certificate": "Tags",
    "AWS::CloudWatch::Alarm": "Tags",
    "AWS::EC2::SecurityGroup": "Tags",
    "AWS::ECS::CapacityProvider": "Tags",
    "AWS::ECS::Cluster": "Tags",
    "AWS::ECS::Service": "Tags",
    "AWS::ECS::TaskDefinition": "Tags",
    "AWS::EFS::AccessPoint": "AccessPointTags",
    "AWS::EFS::FileSystem": "FileSystemTags",
    "AWS::ElasticLoadBalancingV2::Listener": "Tags",
    "AWS::ElasticLoadBalancingV2::ListenerRule": "Tags",
    "AWS::ElasticLoadBalancingV2::LoadBalancer": "Tags",
    "AWS::ElasticLoadBalancingV2::TargetGroup": "Tags",
    "AWS::IAM::Role": "Tags",
    "AWS::KMS::Key": "Tags",
    "AWS::Lambda::Function": "Tags",
    "AWS::Logs::LogGroup": "Tags",
    "AWS::RDS::DBInstance": "Tags",
    "AWS::RDS::DBParameterGroup": "Tags",
    "AWS::RDS::DBSubnetGroup": "Tags",
    "AWS::S3::Bucket": "Tags",
    "AWS::SNS::Topic": "Tags",
    "AWS::SecretsManager::Secret": "Tags",
    "AWS::ServiceDiscovery::PrivateDnsNamespace": "Tags",
    "AWS::ServiceDiscovery::Service": "Tags",
    "AWS::WAFv2::WebACL": "Tags",
}
# Types that CloudFormation cannot tag, each with its reason.
UNTAGGABLE = {
    "AWS::CloudFormation::WaitCondition": "CloudFormation bookkeeping, no tags",
    "AWS::EC2::LaunchTemplate": (
        "The template itself is not tagged. Its TagSpecifications tag what it "
        "launches, which LaunchTemplateTest checks."
    ),
    "AWS::EC2::SecurityGroupEgress": "A rule, not a resource with tags",
    "AWS::EC2::SecurityGroupIngress": "A rule, not a resource with tags",
    "AWS::ECS::ClusterCapacityProviderAssociations": "An association, no tags",
    "AWS::EFS::MountTarget": "No tag property",
    "AWS::IAM::InstanceProfile": "The schema marks it not taggable",
    "AWS::IAM::Policy": "An inline policy, no tags",
    "AWS::IAM::RolePolicy": "An inline policy, no tags",
    "AWS::KMS::Alias": "No tag property",
    "AWS::Route53::RecordSet": "Records have no tags",
    "AWS::S3::BucketPolicy": "A policy, no tags",
    "AWS::SNS::Subscription": "No tag property",
    "AWS::SecretsManager::SecretTargetAttachment": "An attachment, no tags",
    "AWS::WAFv2::LoggingConfiguration": "A configuration, no tags",
    "AWS::WAFv2::WebACLAssociation": "An association, no tags",
}
# Taggable resources that stay untagged on purpose, by logical ID, with the
# reason. Hume has none today.
EXCLUDED: dict[str, str] = {}
LAUNCH_TEMPLATE = "InstanceLaunchTemplate"
LAUNCH_TYPE = "AWS::EC2::LaunchTemplate"
LAUNCHED_TYPES = {"instance", "volume", "network-interface"}


def is_custom(resource_type: str) -> bool:
    return resource_type.startswith("Custom::")


class PrmCase(tm.TemplateTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.raw = cls.template["Resources"]


class MappingTest(PrmCase):
    def test_the_mapping_holds_the_partner_key_and_hume_product_code(self) -> None:
        self.assertEqual(
            self.template["Mappings"]["Prm"]["Tag"],
            {"Key": PRM_KEY, "Value": PRM_VALUE},
        )

    def test_the_key_and_code_appear_only_in_the_mapping(self) -> None:
        for name, spec in self.raw.items():
            for item in tm.walk(spec):
                if isinstance(item, str):
                    self.assertNotIn("aws-apn-id", item, name)
                    self.assertNotIn(PRM_VALUE, item, name)


class ResourceTagTest(PrmCase):
    def test_every_resource_type_is_classified(self) -> None:
        for name, spec in self.raw.items():
            kind = spec["Type"]
            if is_custom(kind):
                continue
            with self.subTest(resource=name):
                self.assertTrue(
                    kind in TAG_PROPERTY or kind in UNTAGGABLE,
                    f"{kind} is new. Add it to TAG_PROPERTY or UNTAGGABLE.",
                )

    def test_every_taggable_resource_carries_the_prm_tag(self) -> None:
        for name, spec in self.raw.items():
            prop = TAG_PROPERTY.get(spec["Type"])
            if prop is None or name in EXCLUDED:
                continue
            with self.subTest(resource=name):
                tags = spec["Properties"].get(prop, [])
                entries = [
                    {k: v for k, v in tag.items() if k != "PropagateAtLaunch"}
                    for tag in tags
                ]
                self.assertEqual(entries.count(PRM_ENTRY), 1)

    def test_the_auto_scaling_group_propagates_the_tag_at_launch(self) -> None:
        for name, spec in self.raw.items():
            if spec["Type"] != "AWS::AutoScaling::AutoScalingGroup":
                continue
            tag = next(
                tag
                for tag in spec["Properties"]["Tags"]
                if tag["Key"] == PRM_ENTRY["Key"]
            )
            self.assertIs(tag.get("PropagateAtLaunch"), True, name)

    def test_services_propagate_their_tags_to_tasks(self) -> None:
        for name, spec in self.raw.items():
            if spec["Type"] != "AWS::ECS::Service":
                continue
            with self.subTest(resource=name):
                props = spec["Properties"]
                self.assertEqual(props.get("PropagateTags"), "SERVICE")
                self.assertIs(props.get("EnableECSManagedTags"), True)

    def test_the_lists_match_the_cfn_lint_schemas(self) -> None:
        if importlib.util.find_spec("cfnlint") is None:
            self.skipTest("cfn-lint is not installed")
        from cfnlint.schema import PROVIDER_SCHEMA_MANAGER

        for kind in sorted((set(TAG_PROPERTY) | set(UNTAGGABLE)) - {LAUNCH_TYPE}):
            schema = PROVIDER_SCHEMA_MANAGER.get_resource_schema(
                "us-east-1", kind
            ).schema
            # Some schemas have no tagging block. A Tags property then decides.
            taggable = schema.get("tagging", {}).get("taggable")
            if taggable is None:
                taggable = "Tags" in schema.get("properties", {})
            with self.subTest(type=kind):
                self.assertEqual(taggable, kind in TAG_PROPERTY)


class LaunchTemplateTest(PrmCase):
    def test_the_instance_its_volumes_and_interfaces_get_the_tag(self) -> None:
        data = self.raw[LAUNCH_TEMPLATE]["Properties"]["LaunchTemplateData"]
        specs = {
            spec["ResourceType"]: spec["Tags"] for spec in data["TagSpecifications"]
        }
        self.assertEqual(set(specs), LAUNCHED_TYPES)
        for kind, tags in specs.items():
            with self.subTest(resource_type=kind):
                self.assertEqual(tags.count(PRM_ENTRY), 1)


if __name__ == "__main__":
    unittest.main()
