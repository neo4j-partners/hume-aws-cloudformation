"""Shared helpers that read hume-ecs.template.yaml and resolve it offline.

The template is read as YAML, its conditions are evaluated for one set of
parameter values, and the result is a plain dict of the resources, outputs and
parameters that CloudFormation would create. No AWS call is made.
"""

import re
import subprocess
import unittest
from pathlib import Path

import yaml

TEMPLATE = Path(__file__).resolve().parents[2] / "stack/hume-ecs.template.yaml"

NOVALUE = object()

ARN_PREFIX = "arn:aws:secretsmanager:us-east-1:123456789012:secret:"
PASSWORD_ARN = ARN_PREFIX + "app-pw-AbCdEf"
TRUSTSTORE_ARN = ARN_PREFIX + "ts-AbCdEf"
KMS_KEY_ARN = (
    "arn:aws:kms:us-east-1:123456789012:key/1234abcd-12ab-34cd-56ef-1234567890ab"
)
SPINE_IMPORT_ARN = ARN_PREFIX + "hume/stack-x/spine-config-AbCdEf"
EE_STACK = "neo4j-ee-1"
NEO4J_INPUTS = {
    "EeStackName": EE_STACK,
    "Neo4jTruststoreSecretArn": TRUSTSTORE_ARN,
}
NEO4J_PARAMETERS = list(NEO4J_INPUTS)
# The resource that reads the Neo4j EE values from Parameter Store.
LOOKUP = "Neo4jEeLookup"
# The attributes that LOOKUP returns, which resolve() puts in place of each
# GetAtt and Sub reference to it.
NEO4J_VALUES = {
    "Neo4jHost": "neo4j.example.test",
    "VpcId": "vpc-0123456789abcdef0",
    "PublicSubnet1Id": "subnet-0123456789abcdef0",
    "PublicSubnet2Id": "subnet-0123456789abcdef1",
    "PublicSubnet3Id": "subnet-0123456789abcdef4",
    "PrivateSubnet1Id": "subnet-0123456789abcdef2",
    "PrivateSubnet2Id": "subnet-0123456789abcdef3",
    "Neo4jNlbSecurityGroupId": "sg-0123456789abcdef0",
    "VpcEndpointSecurityGroupId": "sg-0123456789abcdef1",
    "Neo4jUsername": "neo4j_app",
    "Neo4jAppPasswordSecretArn": PASSWORD_ARN,
    "Neo4jSecretsKmsKeyArn": KMS_KEY_ARN,
}
BASE_INPUTS = {
    "HumeAmiId": "ami-0123456789abcdef0",
    "AllowedCidr1": "203.0.113.10/32",
    "AllowedCidr2": "203.0.113.11/32",
    "AllowedCidr3": "203.0.113.12/32",
    "AllowedCidr4": "203.0.113.13/32",
    "AllowedCidr5": "203.0.113.14/32",
    "HumeLicenceSecretArn": ARN_PREFIX + "lic-AbCdEf",
    "HostedZoneId": "Z0123456789ABC",
    "BaseDomainName": "example.com",
    "OidcIssuerUri": "https://idp.example.test/pool",
    "OidcClientId": "client-0123456789",
    **NEO4J_INPUTS,
}

# The resource list, copied by hand. This is the gate: the stack must keep
# creating exactly these resources of exactly these types. The base deploy signs
# in through OIDC and runs no configuration import.
BASE_RESOURCES = {
    "Neo4jEeLookupLogGroup": "AWS::Logs::LogGroup",
    "Neo4jEeLookupRole": "AWS::IAM::Role",
    "Neo4jEeLookupFunction": "AWS::Lambda::Function",
    "Neo4jEeLookup": "Custom::Neo4jEeLookup",
    "AlbSecurityGroup": "AWS::EC2::SecurityGroup",
    "InstanceSecurityGroup": "AWS::EC2::SecurityGroup",
    "DatabaseSecurityGroup": "AWS::EC2::SecurityGroup",
    "EfsSecurityGroup": "AWS::EC2::SecurityGroup",
    "AlbIngressHttps": "AWS::EC2::SecurityGroupIngress",
    "AlbIngressHttps2": "AWS::EC2::SecurityGroupIngress",
    "AlbIngressHttps3": "AWS::EC2::SecurityGroupIngress",
    "AlbIngressHttps4": "AWS::EC2::SecurityGroupIngress",
    "AlbIngressHttps5": "AWS::EC2::SecurityGroupIngress",
    "AlbIngressHttp": "AWS::EC2::SecurityGroupIngress",
    "AlbIngressHttp2": "AWS::EC2::SecurityGroupIngress",
    "AlbIngressHttp3": "AWS::EC2::SecurityGroupIngress",
    "AlbIngressHttp4": "AWS::EC2::SecurityGroupIngress",
    "AlbIngressHttp5": "AWS::EC2::SecurityGroupIngress",
    "AlbEgressWebAndApi": "AWS::EC2::SecurityGroupEgress",
    "AlbEgressMedia": "AWS::EC2::SecurityGroupEgress",
    "AlbEgressManagementHealth": "AWS::EC2::SecurityGroupEgress",
    "InstanceIngressFromAlbWebAndApi": "AWS::EC2::SecurityGroupIngress",
    "InstanceIngressFromAlbMedia": "AWS::EC2::SecurityGroupIngress",
    "InstanceIngressFromAlbManagementHealth": "AWS::EC2::SecurityGroupIngress",
    "InstanceIngressSelfMedia": "AWS::EC2::SecurityGroupIngress",
    "InstanceIngressSelfWebAndApi": "AWS::EC2::SecurityGroupIngress",
    "InstanceIngressSelfOrchestra": "AWS::EC2::SecurityGroupIngress",
    "InstanceIngressSelfWebhooks": "AWS::EC2::SecurityGroupIngress",
    "InstanceIngressSelfSecurity": "AWS::EC2::SecurityGroupIngress",
    "InstanceIngressSelfEventstore": "AWS::EC2::SecurityGroupIngress",
    "InstanceEgressSelf": "AWS::EC2::SecurityGroupEgress",
    "InstanceEgressHttps": "AWS::EC2::SecurityGroupEgress",
    "InstanceEgressDatabase": "AWS::EC2::SecurityGroupEgress",
    "InstanceEgressEfs": "AWS::EC2::SecurityGroupEgress",
    "InstanceEgressNeo4jBolt": "AWS::EC2::SecurityGroupEgress",
    "Neo4jNlbIngressFromHume": "AWS::EC2::SecurityGroupIngress",
    "VpcEndpointIngressFromHume": "AWS::EC2::SecurityGroupIngress",
    "InstanceRole": "AWS::IAM::Role",
    "InstanceProfile": "AWS::IAM::InstanceProfile",
    "TaskExecutionRole": "AWS::IAM::Role",
    "VaultTaskRole": "AWS::IAM::Role",
    "Cluster": "AWS::ECS::Cluster",
    "InstanceLaunchTemplate": "AWS::EC2::LaunchTemplate",
    "AutoScalingGroup": "AWS::AutoScaling::AutoScalingGroup",
    "CapacityProvider": "AWS::ECS::CapacityProvider",
    "ClusterCapacityProviderAssociation": (
        "AWS::ECS::ClusterCapacityProviderAssociations"
    ),
    "DataKey": "AWS::KMS::Key",
    "DataKeyAlias": "AWS::KMS::Alias",
    "DbMasterSecret": "AWS::SecretsManager::Secret",
    "DbMasterSecretAttachment": "AWS::SecretsManager::SecretTargetAttachment",
    "ApiDbSecret": "AWS::SecretsManager::Secret",
    "OrchestraDbSecret": "AWS::SecretsManager::Secret",
    "EventstoreDbSecret": "AWS::SecretsManager::Secret",
    "MediaDbSecret": "AWS::SecretsManager::Secret",
    "EventstorePasswordSecret": "AWS::SecretsManager::Secret",
    "MediaDownloadKeySecret": "AWS::SecretsManager::Secret",
    "ApiToMediaKeySecret": "AWS::SecretsManager::Secret",
    "MediaToApiKeySecret": "AWS::SecretsManager::Secret",
    "SymmetricKeySecret": "AWS::SecretsManager::Secret",
    "MaintenanceKeySecret": "AWS::SecretsManager::Secret",
    "DatabaseSubnetGroup": "AWS::RDS::DBSubnetGroup",
    "DatabaseParameterGroup": "AWS::RDS::DBParameterGroup",
    "Database": "AWS::RDS::DBInstance",
    "DataFileSystem": "AWS::EFS::FileSystem",
    "DataMountTarget1": "AWS::EFS::MountTarget",
    "DataMountTarget2": "AWS::EFS::MountTarget",
    "OrchestraAccessPoint": "AWS::EFS::AccessPoint",
    "AuditLogsAccessPoint": "AWS::EFS::AccessPoint",
    "MediaAccessPoint": "AWS::EFS::AccessPoint",
    "EventstoreLogGroup": "AWS::Logs::LogGroup",
    "OrchestraLogGroup": "AWS::Logs::LogGroup",
    "ApiLogGroup": "AWS::Logs::LogGroup",
    "MediaLogGroup": "AWS::Logs::LogGroup",
    "WebLogGroup": "AWS::Logs::LogGroup",
    "DbInitLogGroup": "AWS::Logs::LogGroup",
    "SmokeTestLogGroup": "AWS::Logs::LogGroup",
    "SmokeTestTaskDefinition": "AWS::ECS::TaskDefinition",
    "InstanceBootPolicy": "AWS::IAM::Policy",
    "InstanceBootComplete": "AWS::CloudFormation::WaitCondition",
    "RunTaskLogGroup": "AWS::Logs::LogGroup",
    "RunTaskRole": "AWS::IAM::Role",
    "RunTaskFunction": "AWS::Lambda::Function",
    "DbInitTaskDefinition": "AWS::ECS::TaskDefinition",
    "DbInitRun": "Custom::EcsRunTask",
    "TaskExecutionSecretsPolicy": "AWS::IAM::RolePolicy",
    "ServiceNamespace": "AWS::ServiceDiscovery::PrivateDnsNamespace",
    "EventstoreDiscoveryService": "AWS::ServiceDiscovery::Service",
    "OrchestraDiscoveryService": "AWS::ServiceDiscovery::Service",
    "ApiDiscoveryService": "AWS::ServiceDiscovery::Service",
    "MediaDiscoveryService": "AWS::ServiceDiscovery::Service",
    "Certificate": "AWS::CertificateManager::Certificate",
    "AlbAliasRecord": "AWS::Route53::RecordSet",
    "AlbAliasRecordApi": "AWS::Route53::RecordSet",
    "AlbAliasRecordMedia": "AWS::Route53::RecordSet",
    "AccessLogBucket": "AWS::S3::Bucket",
    "AccessLogBucketPolicy": "AWS::S3::BucketPolicy",
    "LoadBalancer": "AWS::ElasticLoadBalancingV2::LoadBalancer",
    "WebTargetGroup": "AWS::ElasticLoadBalancingV2::TargetGroup",
    "ApiTargetGroup": "AWS::ElasticLoadBalancingV2::TargetGroup",
    "MediaTargetGroup": "AWS::ElasticLoadBalancingV2::TargetGroup",
    "HttpsListener": "AWS::ElasticLoadBalancingV2::Listener",
    "HttpRedirectListener": "AWS::ElasticLoadBalancingV2::Listener",
    "ApiPrometheusBlockRule": "AWS::ElasticLoadBalancingV2::ListenerRule",
    "ApiForwardRule": "AWS::ElasticLoadBalancingV2::ListenerRule",
    "MediaForwardRule": "AWS::ElasticLoadBalancingV2::ListenerRule",
    "WebForwardRule": "AWS::ElasticLoadBalancingV2::ListenerRule",
    "WebAcl": "AWS::WAFv2::WebACL",
    "WebAclAssociation": "AWS::WAFv2::WebACLAssociation",
    "WebAclLogGroup": "AWS::Logs::LogGroup",
    "WebAclLogging": "AWS::WAFv2::LoggingConfiguration",
    "AlarmTopic": "AWS::SNS::Topic",
    "WebUnhealthyAlarm": "AWS::CloudWatch::Alarm",
    "ApiUnhealthyAlarm": "AWS::CloudWatch::Alarm",
    "MediaUnhealthyAlarm": "AWS::CloudWatch::Alarm",
    "AlbTarget5xxAlarm": "AWS::CloudWatch::Alarm",
    "AlbElb5xxAlarm": "AWS::CloudWatch::Alarm",
    "DatabaseCpuAlarm": "AWS::CloudWatch::Alarm",
    "DatabaseFreeStorageAlarm": "AWS::CloudWatch::Alarm",
    "InstanceStatusAlarm": "AWS::CloudWatch::Alarm",
    "EventstoreTaskDefinition": "AWS::ECS::TaskDefinition",
    "OrchestraTaskDefinition": "AWS::ECS::TaskDefinition",
    "ApiTaskDefinition": "AWS::ECS::TaskDefinition",
    "MediaTaskDefinition": "AWS::ECS::TaskDefinition",
    "WebTaskDefinition": "AWS::ECS::TaskDefinition",
    "EventstoreService": "AWS::ECS::Service",
    "OrchestraService": "AWS::ECS::Service",
    "ApiService": "AWS::ECS::Service",
    "MediaService": "AWS::ECS::Service",
    "WebService": "AWS::ECS::Service",
}

# The parameters with a fixed type and default, copied by hand: name, type,
# default. None means no default.
BASE_PARAMETERS = {
    "HumeAmiId": ("AWS::EC2::Image::Id", None),
    "InstanceType": ("String", "m6i.xlarge"),
    "RootVolumeSize": ("Number", 40),
    "AllowedCidr1": ("String", None),
    "AllowedCidr2": ("String", ""),
    "AllowedCidr3": ("String", ""),
    "AllowedCidr4": ("String", ""),
    "AllowedCidr5": ("String", ""),
    "HumeLicenceSecretArn": ("String", None),
    "DbInstanceClass": ("String", "db.t4g.medium"),
    "DbEngineVersion": ("String", "17"),
    "LogRetentionDays": ("Number", 7),
    "HumeVersion": ("String", "3.2.1"),
    "BaseDomainName": ("String", None),
    "HostedZoneId": ("String", None),
}

BASE_OUTPUTS = {
    "ClusterName",
    "EeStackName",
    "Neo4jHost",
    "VpcId",
    "InstanceSubnetId",
    "AlbSecurityGroupId",
    "InstanceSecurityGroupId",
    "DatabaseSecurityGroupId",
    "InstanceRoleArn",
    "TaskExecutionRoleArn",
    "AutoScalingGroupName",
    "CapacityProviderName",
    "DatabaseEndpoint",
    "DatabaseMasterSecretArn",
    "SmokeTestTaskDefinitionArn",
    "SecretNamePrefix",
    "LicenceSecretArn",
    "LogGroupPrefix",
    "DbInitTaskDefinitionArn",
    "AlbDnsName",
    "WebUrl",
    "ApiUrl",
    "MediaUrl",
    "CertificateArn",
    "WebAclName",
    "WebAclLogGroupName",
    "AccessLogBucketName",
    "FileSystemId",
    "AlarmTopicArn",
    "DataKeyArn",
}

# Resources and outputs that exist only with AuthProvider=Native.
NATIVE_AUTH_RESOURCES = {
    "JwtKeySecret": "AWS::SecretsManager::Secret",
    "AdminPasswordSecret": "AWS::SecretsManager::Secret",
}
NATIVE_AUTH_OUTPUTS = {"AdminUsername", "AdminPasswordSecretArn"}
# The email subscription, which exists only when AlarmEmail is set.
ALARM_EMAIL_RESOURCES = {"AlarmEmailSubscription": "AWS::SNS::Subscription"}
# The configuration import and the API key it signs in with, which exist only
# when SpineConfigSecretArn is set.
DEMO_IMPORT_RESOURCES = {
    "ApiKeySecret": "AWS::SecretsManager::Secret",
    "ConfigCliLogGroup": "AWS::Logs::LogGroup",
    "ConfigCliTaskDefinition": "AWS::ECS::TaskDefinition",
    "ConfigCliRun": "Custom::EcsRunTask",
}
# Parameters that Phase 1 added. Each has a default, so an old command line works.
PHASE1_PARAMETERS = [
    "DbBackupRetentionDays",
    "AccessLogExpirationDays",
    "AlarmEmail",
    "EventstoreCopies",
    "ApiCopies",
    "MediaCopies",
    "WebCopies",
]
# Parameters that restore a stack from an earlier one. Each defaults to empty,
# which creates a new database, file system and key.
RESTORE_PARAMETERS = [
    "DbSnapshotIdentifier",
    "ExistingFileSystemId",
    "ExistingDataKeyArn",
]
AUTH_PARAMETERS = [
    "AuthProvider",
    "OidcIssuerUri",
    "OidcClientId",
    "OidcRolesMappings",
    "OidcRolesClaimPath",
    "OidcPrincipalClaim",
    "OidcJwkSetUri",
    "OidcScope",
]
# The rules that let Hume reach the Neo4j cluster.
NEO4J_RULES = {
    "InstanceEgressNeo4jBolt",
    "Neo4jNlbIngressFromHume",
    "VpcEndpointIngressFromHume",
}
VPC = "vpc-0aaaaaaaaaaaaaaa1"
OTHER_VPC = "vpc-0aaaaaaaaaaaaaaa2"
# What a three-server Neo4j EE stack publishes under /neo4j-ee/<stack>/.
NEO4J_SSM = {
    "advertised-dns": "neo4j.example.internal",
    "vpc-id": VPC,
    "public-subnet-1-id": "subnet-0aaaaaaa1",
    "public-subnet-2-id": "subnet-0aaaaaaa2",
    "public-subnet-3-id": "subnet-0aaaaaaa3",
    "private-subnet-1-id": "subnet-0bbbbbbb1",
    "private-subnet-2-id": "subnet-0bbbbbbb2",
    "external-sg-id": "sg-0cccccccc1",
    "vpc-endpoint-sg-id": "sg-0cccccccc2",
    "app-username": "neo4j_app",
    "app-password-secret-arn": PASSWORD_ARN,
    "kms-key-arn": KMS_KEY_ARN,
}
NO_STACK_MESSAGE = (
    f"No Neo4j EE stack named {EE_STACK} in this account and Region. "
    "Deploy Neo4j EE from AWS Marketplace first."
)
ONE_SERVER_MESSAGE = (
    f"Neo4j EE stack {EE_STACK} has one server. Hume needs a three-server Neo4j "
    "EE stack. Deploy Neo4j EE with NumberOfServers 3."
)
NOT_PUBLISHED_MESSAGE = (
    f"Neo4j EE stack {EE_STACK} does not publish app-username, kms-key-arn "
    f"under /neo4j-ee/{EE_STACK}/."
)
# A one-server Neo4j EE stack publishes everything but two public subnets.
ONE_SERVER_SSM = {
    k: v
    for k, v in NEO4J_SSM.items()
    if k not in ("public-subnet-2-id", "public-subnet-3-id")
}
OLD_STACK_SSM = {
    k: v for k, v in NEO4J_SSM.items() if k not in ("app-username", "kms-key-arn")
}


class Loader(yaml.SafeLoader):
    """Reads CloudFormation short tags as their long form."""


def _tag(loader: Loader, suffix: str, node: yaml.Node) -> dict:
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    if suffix == "GetAtt" and isinstance(value, str):
        value = value.split(".", 1)
    name = suffix if suffix in ("Ref", "Condition") else f"Fn::{suffix}"
    return {name: value}


Loader.add_multi_constructor("!", _tag)


def evaluate(expr, params: dict, conditions: dict):
    """Evaluate a condition or rule expression to a bool, or a string for Ref."""
    if isinstance(expr, str):
        return expr
    ((key, value),) = expr.items()
    if key == "Ref":
        return params[value]
    if key == "Condition":
        return evaluate(conditions[value], params, conditions)
    if key == "Fn::Equals":
        return evaluate(value[0], params, conditions) == evaluate(
            value[1], params, conditions
        )
    if key == "Fn::Not":
        return not evaluate(value[0], params, conditions)
    if key == "Fn::And":
        return all(evaluate(item, params, conditions) for item in value)
    if key == "Fn::Or":
        return any(evaluate(item, params, conditions) for item in value)
    raise ValueError(f"unsupported expression {key}")


def resolve(node, ctx: dict):
    """Resolve parameters, conditions, Select, Split and Sub variable maps."""
    if isinstance(node, list):
        items = [resolve(item, ctx) for item in node]
        return [item for item in items if item is not NOVALUE]
    if not isinstance(node, dict):
        return node
    if len(node) == 1:
        ((key, value),) = node.items()
        if key == "Ref":
            if value == "AWS::NoValue":
                return NOVALUE
            return ctx["params"].get(value, node)
        if key == "Fn::If":
            name, chosen, other = value
            branch = chosen if evaluate({"Condition": name}, **ctx["eval"]) else other
            return resolve(branch, ctx)
        if key == "Fn::Split":
            delimiter, source = (resolve(item, ctx) for item in value)
            return source.split(delimiter) if isinstance(source, str) else node
        if key == "Fn::Select":
            index, source = (resolve(item, ctx) for item in value)
            if isinstance(source, list) and all(isinstance(x, str) for x in source):
                return source[int(index)]
            return {key: [index, source]}
        if key == "Fn::Sub":
            return resolve_sub(value, ctx)
        if key == "Fn::GetAtt" and value[0] == LOOKUP:
            return NEO4J_VALUES[value[1]]
    result = {}
    for key, value in node.items():
        resolved = resolve(value, ctx)
        if resolved is not NOVALUE:
            result[key] = resolved
    return result


def resolve_sub(value, ctx: dict) -> dict:
    """Return Fn::Sub as one string with map variables and parameters inlined."""
    text, variables = (value, {}) if isinstance(value, str) else value
    for name, raw in variables.items():
        inner = resolve(raw, ctx)
        if isinstance(inner, dict) and set(inner) == {"Fn::Sub"}:
            inner = inner["Fn::Sub"]
        if not isinstance(inner, str):
            raise TypeError(f"Sub variable {name} did not resolve to text")
        text = text.replace(f"${{{name}}}", inner)
    for name, param in ctx["params"].items():
        text = text.replace(f"${{{name}}}", str(param))
    for name, attribute in NEO4J_VALUES.items():
        text = text.replace(f"${{{LOOKUP}.{name}}}", attribute)
    return {"Fn::Sub": text}


def load(path: Path = TEMPLATE) -> dict:
    return yaml.load(path.read_text(), Loader=Loader)


def resolve_template(template: dict, inputs: dict) -> dict:
    """Resolve a loaded template for the given parameter values."""
    params = {
        name: spec["Default"]
        for name, spec in template["Parameters"].items()
        if "Default" in spec
    }
    params.update(inputs)
    missing = [name for name in template["Parameters"] if name not in params]
    if missing:
        raise ValueError(f"no value for parameters {missing}")
    conditions = template.get("Conditions", {})
    ctx = {"params": params, "eval": {"params": params, "conditions": conditions}}

    def present(spec: dict) -> bool:
        name = spec.get("Condition")
        return name is None or evaluate({"Condition": name}, params, conditions)

    def finish(spec: dict) -> dict:
        body = {key: value for key, value in spec.items() if key != "Condition"}
        return resolve(body, ctx)

    return {
        "parameters": params,
        "resources": {
            name: finish(spec)
            for name, spec in template["Resources"].items()
            if present(spec)
        },
        "outputs": {
            name: finish(spec)
            for name, spec in template["Outputs"].items()
            if present(spec)
        },
    }


def failed_rules(template: dict, inputs: dict) -> list[str]:
    """Return the assertion descriptions that fail for these parameter values."""
    params = {
        name: spec["Default"]
        for name, spec in template["Parameters"].items()
        if "Default" in spec
    }
    params.update(BASE_INPUTS)
    params.update(inputs)
    conditions = template.get("Conditions", {})
    failed = []
    for rule in template.get("Rules", {}).values():
        if not evaluate(rule["RuleCondition"], params, conditions):
            continue
        for assertion in rule["Assertions"]:
            if not evaluate(assertion["Assert"], params, conditions):
                failed.append(assertion["AssertDescription"])
    return failed


def walk(node):
    """Yield every node of a resolved tree."""
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def references(resolved: dict) -> set[str]:
    """Return every name that a resolved resource or output refers to."""
    names = set()
    for node in walk(resolved):
        if isinstance(node, dict) and len(node) == 1:
            ((key, value),) = node.items()
            if key == "Ref":
                names.add(value)
            elif key == "Fn::GetAtt":
                names.add(value[0])
            elif key == "Fn::Sub":
                names.update(
                    match.split(".")[0]
                    for match in re.findall(r"\$\{([^!}][^}]*)\}", value)
                )
    return names


def user_data(resources: dict) -> str:
    launch = resources["InstanceLaunchTemplate"]["Properties"]
    data = launch["LaunchTemplateData"]["UserData"]["Fn::Base64"]
    return data["Fn::Sub"]


def rendered_script(resources: dict) -> str:
    """Return the instance boot script with remaining variables filled in."""
    return re.sub(
        r"\$\{([A-Za-z][A-Za-z0-9:]*)\}",
        lambda match: "VALUE_" + match.group(1).replace(":", ""),
        user_data(resources),
    )


def as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def run_bash_syntax_check(script: str) -> None:
    proc = subprocess.run(
        ["bash", "-n"], input=script, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)


class TemplateTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = load()
        cls.base = resolve_template(cls.template, BASE_INPUTS)
