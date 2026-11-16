"""Offline tests for the Neo4j EE settings and the network check of deploy.py.

The settings, the read of the Neo4j EE stack and the network check are tested
with faked describe responses. No AWS call is made.

Run from the repository root:
    uv run --with boto3 --with python-dotenv --with pyyaml \
        python -m unittest tests.stack.test_neo4j_settings
"""

import argparse
import contextlib
import io
import unittest
from typing import ClassVar
from unittest import mock

from botocore.exceptions import ClientError

import deploy
from tests.helpers.env import (
    BASE_ENV,
    NEO4J_ENV,
    PARAMETER_FOR_VARIABLE,
    TRUSTSTORE,
    ZONE_ID,
    Neo4jCase,
    TempEnvCase,
)
from tests.helpers.template import (
    EE_STACK,
    NEO4J_SSM,
    NO_STACK_MESSAGE,
    NOT_PUBLISHED_MESSAGE,
    OLD_STACK_SSM,
    ONE_SERVER_MESSAGE,
    ONE_SERVER_SSM,
    OTHER_VPC,
    VPC,
)

BAD_VALUES = {
    deploy.EE_STACK_VAR: "neo4j_ee",
    deploy.TRUSTSTORE_ARN_VAR: TRUSTSTORE.rsplit("-", 1)[0],
}


def settings_args(**values) -> argparse.Namespace:
    base = {
        "ami_id": None,
        "allowed_cidr": None,
        "profile": None,
        "region": None,
        "instance_type": "m6i.xlarge",
        "base_domain": None,
        "hosted_zone_id": None,
        "db_snapshot_identifier": None,
        "existing_file_system_id": None,
        "existing_data_key_arn": None,
    }
    return argparse.Namespace(**{**base, **values})


class Neo4jSettingsTest(Neo4jCase):
    def test_passes_both_values(self) -> None:
        self.write_env()
        settings = deploy.resolve_settings(settings_args())
        params = deploy.neo4j_parameters(settings)
        expected = {
            PARAMETER_FOR_VARIABLE[name]: value for name, value in NEO4J_ENV.items()
        }
        self.assertEqual(params, expected)
        self.assertEqual(len(expected), 2)

    def test_each_missing_or_empty_value_exits_naming_it(self) -> None:
        for name in NEO4J_ENV:
            with self.subTest(name=name):
                values = {k: v for k, v in NEO4J_ENV.items() if k != name}
                self.env_file.write_text(
                    "".join(f"{k}={v}\n" for k, v in {**BASE_ENV, **values}.items())
                )
                with self.assertRaises(SystemExit) as raised:
                    deploy.resolve_settings(settings_args())
                message = str(raised.exception)
                self.assertIn(name, message)
                others = [n for n in NEO4J_ENV if n != name]
                self.assertFalse(any(other in message for other in others))

        for name in NEO4J_ENV:
            with self.subTest(name=name):
                self.write_env(**{name: ""})
                with self.assertRaises(SystemExit) as raised:
                    deploy.resolve_settings(settings_args())
                self.assertIn(name, str(raised.exception))

    def test_all_missing_are_listed_together(self) -> None:
        self.env_file.write_text("".join(f"{k}={v}\n" for k, v in BASE_ENV.items()))
        with self.assertRaises(SystemExit) as raised:
            deploy.resolve_settings(settings_args())
        for name in NEO4J_ENV:
            self.assertIn(name, str(raised.exception))

    def test_each_malformed_value_exits(self) -> None:
        for name, bad in BAD_VALUES.items():
            with self.subTest(name=name):
                self.write_env(**{name: bad})
                with self.assertRaises(SystemExit) as raised:
                    deploy.resolve_settings(settings_args())
                self.assertIn(name, str(raised.exception))
                self.assertIn("not valid", str(raised.exception))

    def test_the_stack_name_drops_spaces(self) -> None:
        self.write_env(**{deploy.EE_STACK_VAR: f" {EE_STACK} "})
        settings = deploy.resolve_settings(settings_args())
        self.assertEqual(settings.neo4j["EeStackName"], EE_STACK)


class FakeEc2:
    """Answers the describe calls from tables, with the not found errors of EC2."""

    def __init__(
        self, vpcs=(), subnets=None, groups=None, routes=None, main_routes=()
    ) -> None:
        self.vpcs = set(vpcs)
        self.subnets = subnets or {}
        self.groups = groups or {}
        # Subnet id to the routes of its explicitly associated route table.
        self.routes = routes or {}
        # The routes of the main route table of the VPC.
        self.main_routes = list(main_routes)
        self.route_error: ClientError | None = None

    @staticmethod
    def missing(code: str) -> ClientError:
        return ClientError({"Error": {"Code": code, "Message": "x"}}, "Describe")

    def describe_vpcs(self, VpcIds):
        if not set(VpcIds) <= self.vpcs:
            raise self.missing("InvalidVpcID.NotFound")
        return {"Vpcs": [{"VpcId": v} for v in VpcIds]}

    def describe_subnets(self, SubnetIds):
        if not set(SubnetIds) <= set(self.subnets):
            raise self.missing("InvalidSubnetID.NotFound")
        return {
            "Subnets": [
                {
                    "SubnetId": s,
                    "VpcId": self.subnets[s][0],
                    "AvailabilityZone": self.subnets[s][1],
                }
                for s in SubnetIds
            ]
        }

    def describe_route_tables(self, Filters):
        if self.route_error:
            raise self.route_error
        names = [f["Name"] for f in Filters]
        if names == ["association.subnet-id"]:
            wanted = Filters[0]["Values"]
            return {
                "RouteTables": [
                    {
                        "RouteTableId": f"rtb-{subnet}",
                        "Associations": [{"SubnetId": subnet, "Main": False}],
                        "Routes": self.routes[subnet],
                    }
                    for subnet in wanted
                    if subnet in self.routes
                ]
            }
        if names == ["vpc-id", "association.main"]:
            if Filters[0]["Values"] != [VPC] or Filters[1]["Values"] != ["true"]:
                return {"RouteTables": []}
            return {
                "RouteTables": [
                    {
                        "RouteTableId": "rtb-main",
                        "Associations": [{"Main": True}],
                        "Routes": self.main_routes,
                    }
                ]
            }
        raise AssertionError(f"Unexpected route table filters {Filters}")

    def describe_security_groups(self, GroupIds):
        if not set(GroupIds) <= set(self.groups):
            raise self.missing("InvalidGroup.NotFound")
        return {
            "SecurityGroups": [
                {"GroupId": g, "VpcId": self.groups[g]} for g in GroupIds
            ]
        }


# The template parameters that deploy sends.
NEO4J = {PARAMETER_FOR_VARIABLE[name]: value for name, value in NEO4J_ENV.items()}


LOCAL_ROUTE = {
    "DestinationCidrBlock": "10.0.0.0/16",
    "GatewayId": "local",
    "State": "active",
}


def nat_route(nat: str, state: str = "active") -> dict[str, str]:
    return {"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": nat, "State": state}


def good_ec2(**changes) -> FakeEc2:
    tables = {
        "vpcs": {VPC},
        "subnets": {
            "subnet-0aaaaaaa1": (VPC, "us-east-1a"),
            "subnet-0aaaaaaa2": (VPC, "us-east-1b"),
            "subnet-0aaaaaaa3": (VPC, "us-east-1c"),
            "subnet-0bbbbbbb1": (VPC, "us-east-1a"),
            "subnet-0bbbbbbb2": (VPC, "us-east-1b"),
        },
        "groups": {"sg-0cccccccc1": VPC, "sg-0cccccccc2": VPC},
        "routes": {
            "subnet-0bbbbbbb1": [LOCAL_ROUTE, nat_route("nat-0ddddddd1")],
            "subnet-0bbbbbbb2": [LOCAL_ROUTE, nat_route("nat-0ddddddd2")],
        },
        "main_routes": [LOCAL_ROUTE],
    }
    return FakeEc2(**{**tables, **changes})


class NetworkCheckTest(unittest.TestCase):
    def problems(self, ec2: FakeEc2, **changes) -> list[str]:
        return deploy.check_neo4j_network(ec2, {**NEO4J_SSM, **changes})

    def test_a_fitting_network_has_no_problems(self) -> None:
        self.assertEqual(self.problems(good_ec2()), [])

    def test_a_subnet_from_another_vpc_is_reported(self) -> None:
        subnets = dict(good_ec2().subnets)
        subnets["subnet-0aaaaaaa2"] = (OTHER_VPC, "us-east-1b")
        found = self.problems(good_ec2(subnets=subnets))
        self.assertTrue(any("subnet-0aaaaaaa2" in p and OTHER_VPC in p for p in found))

        subnets = dict(good_ec2().subnets)
        subnets["subnet-0bbbbbbb1"] = (OTHER_VPC, "us-east-1a")
        found = self.problems(good_ec2(subnets=subnets))
        self.assertEqual(len(found), 1)
        self.assertIn("subnet-0bbbbbbb1", found[0])

    def test_subnets_in_one_zone_are_reported(self) -> None:
        subnets = dict(good_ec2().subnets)
        subnets["subnet-0aaaaaaa2"] = (VPC, "us-east-1a")
        subnets["subnet-0aaaaaaa3"] = (VPC, "us-east-1a")
        found = self.problems(good_ec2(subnets=subnets))
        self.assertEqual(len(found), 1)
        self.assertIn("two different availability zones", found[0])

        subnets = dict(good_ec2().subnets)
        subnets["subnet-0bbbbbbb2"] = (VPC, "us-east-1a")
        found = self.problems(good_ec2(subnets=subnets))
        self.assertEqual(len(found), 1)
        self.assertIn("The private subnets must be in two different", found[0])

    def test_the_private_subnet_zone_does_not_count_for_the_public_pair(self) -> None:
        subnets = dict(good_ec2().subnets)
        subnets["subnet-0aaaaaaa2"] = (VPC, "us-east-1a")
        subnets["subnet-0aaaaaaa3"] = (VPC, "us-east-1a")
        subnets["subnet-0bbbbbbb1"] = (VPC, "us-east-1c")
        found = self.problems(good_ec2(subnets=subnets))
        self.assertTrue(any("two different" in p for p in found))

    def test_a_missing_subnet_vpc_or_security_group_is_reported(self) -> None:
        subnets = dict(good_ec2().subnets)
        del subnets["subnet-0bbbbbbb1"]
        found = self.problems(good_ec2(subnets=subnets))
        self.assertTrue(any("InvalidSubnetID.NotFound" in p for p in found))

        found = self.problems(good_ec2(vpcs=set()))
        self.assertTrue(any("InvalidVpcID.NotFound" in p for p in found))

        groups = {"sg-0cccccccc1": VPC}
        found = self.problems(good_ec2(groups=groups))
        self.assertTrue(any("InvalidGroup.NotFound" in p for p in found))

    def test_each_security_group_from_another_vpc_is_reported(self) -> None:
        for group in ["sg-0cccccccc1", "sg-0cccccccc2"]:
            with self.subTest(group=group):
                groups = {**good_ec2().groups, group: OTHER_VPC}
                found = self.problems(good_ec2(groups=groups))
                self.assertEqual(len(found), 1)
                self.assertIn(group, found[0])
                self.assertIn(OTHER_VPC, found[0])

    def test_one_group_for_both_roles_is_described_once(self) -> None:
        ec2 = good_ec2()
        found = self.problems(ec2, **{"vpc-endpoint-sg-id": "sg-0cccccccc1"})
        self.assertEqual(found, [])

    def test_a_private_subnet_without_a_default_route_is_reported(self) -> None:
        routes = {**good_ec2().routes, "subnet-0bbbbbbb2": [LOCAL_ROUTE]}
        found = self.problems(good_ec2(routes=routes))
        expected = (
            "Private subnet subnet-0bbbbbbb2 has no active default route. "
            "Hume's instance needs one for the ECS agent and the identity "
            "provider. Build the Neo4j stack with EnableInternetEgress true."
        )
        self.assertEqual(found, [expected])

    def test_a_blackhole_default_route_is_reported(self) -> None:
        routes = {
            **good_ec2().routes,
            "subnet-0bbbbbbb1": [LOCAL_ROUTE, nat_route("nat-0ddddddd1", "blackhole")],
        }
        found = self.problems(good_ec2(routes=routes))
        self.assertEqual(len(found), 1)
        self.assertIn("subnet-0bbbbbbb1", found[0])
        self.assertIn("EnableInternetEgress", found[0])

    def test_a_subnet_on_the_main_table_uses_its_routes(self) -> None:
        routes = {"subnet-0bbbbbbb1": good_ec2().routes["subnet-0bbbbbbb1"]}
        for main_routes, expected in (
            ([LOCAL_ROUTE, nat_route("nat-0ddddddd2")], []),
            ([LOCAL_ROUTE], ["subnet-0bbbbbbb2"]),
        ):
            with self.subTest(main_routes=main_routes):
                found = self.problems(good_ec2(routes=routes, main_routes=main_routes))
                self.assertEqual(
                    [p.split()[2] for p in found if p.startswith("Private")],
                    expected,
                )
                self.assertEqual(len(found), len(expected))

    def test_a_route_table_error_is_one_problem(self) -> None:
        ec2 = good_ec2()
        ec2.route_error = FakeEc2.missing("UnauthorizedOperation")
        found = self.problems(ec2)
        self.assertEqual(
            found,
            [
                "Route tables of subnet-0bbbbbbb1, subnet-0bbbbbbb2: UnauthorizedOperation"
            ],
        )

    def test_the_public_subnets_are_not_checked_for_a_route(self) -> None:
        ec2 = good_ec2()
        calls: list[list[dict]] = []
        describe = ec2.describe_route_tables

        def record(Filters):
            calls.append(Filters)
            return describe(Filters=Filters)

        ec2.describe_route_tables = record
        self.assertEqual(self.problems(ec2), [])
        checked = [value for filters in calls for f in filters for value in f["Values"]]
        self.assertNotIn("subnet-0aaaaaaa1", checked)
        self.assertNotIn("subnet-0aaaaaaa2", checked)
        self.assertNotIn("subnet-0aaaaaaa3", checked)
        self.assertEqual(
            calls[0],
            [
                {
                    "Name": "association.subnet-id",
                    "Values": ["subnet-0bbbbbbb1", "subnet-0bbbbbbb2"],
                }
            ],
        )

    def test_all_problems_come_back_together(self) -> None:
        subnets = dict(good_ec2().subnets)
        subnets["subnet-0aaaaaaa2"] = (OTHER_VPC, "us-east-1a")
        subnets["subnet-0aaaaaaa3"] = (VPC, "us-east-1a")
        groups = {**good_ec2().groups, "sg-0cccccccc1": OTHER_VPC}
        found = self.problems(good_ec2(subnets=subnets, groups=groups))
        self.assertGreaterEqual(len(found), 3)


def fake_ssm(values: dict[str, str], stack: str = EE_STACK) -> mock.MagicMock:
    """An SSM client whose get_parameters_by_path pages hold the given values."""
    prefix = f"/neo4j-ee/{stack}/"
    ssm = mock.MagicMock()
    ssm.get_paginator.return_value.paginate.return_value = [
        {"Parameters": [{"Name": prefix + k, "Value": v} for k, v in values.items()]}
    ]
    return ssm


class ReadNeo4jStackTest(unittest.TestCase):
    def test_reads_the_values_under_the_stack_prefix(self) -> None:
        ssm = fake_ssm(NEO4J_SSM)
        self.assertEqual(deploy.read_neo4j_stack(ssm, EE_STACK), NEO4J_SSM)
        ssm.get_paginator.assert_called_once_with("get_parameters_by_path")
        ssm.get_paginator.return_value.paginate.assert_called_once_with(
            Path=f"/neo4j-ee/{EE_STACK}/"
        )

    def test_the_three_messages(self) -> None:
        for values, expected in (
            ({}, NO_STACK_MESSAGE),
            (ONE_SERVER_SSM, ONE_SERVER_MESSAGE),
            (OLD_STACK_SSM, NOT_PUBLISHED_MESSAGE),
            (NEO4J_SSM, None),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(deploy.neo4j_stack_problem(EE_STACK, values), expected)


class ConfirmSettingsTest(Neo4jCase):
    """confirm_settings stops before the prompt when the Neo4j EE stack misfits."""

    def session(self, ec2: FakeEc2, ssm: mock.MagicMock) -> mock.MagicMock:
        route53 = mock.MagicMock()
        route53.get_hosted_zone.return_value = {
            "HostedZone": {"Name": "example.com.", "Config": {"PrivateZone": False}}
        }
        sts = mock.MagicMock()
        sts.get_caller_identity.return_value = {"Account": "123456789012"}
        clients = {"route53": route53, "sts": sts, "ec2": ec2, "ssm": ssm}
        session = mock.MagicMock()
        session.client.side_effect = clients.__getitem__
        return session

    def settings(self) -> deploy.Settings:
        return deploy.Settings(
            "marketplace", "us-east-1", "ami-1", ["203.0.113.1/32"], "m6i.xlarge",
            "hume.example.com", ZONE_ID, dict(NEO4J),
        )  # fmt: skip

    def run_confirm(self, ec2: FakeEc2, values: dict[str, str] = NEO4J_SSM) -> str:
        out = io.StringIO()
        session = self.session(ec2, fake_ssm(values, NEO4J["EeStackName"]))
        with (
            mock.patch.object(deploy.boto3, "Session", return_value=session),
            contextlib.redirect_stdout(out),
        ):
            deploy.confirm_settings(self.settings(), assume_yes=True)
        return out.getvalue()

    def test_a_fitting_network_prints_the_stack_host_and_vpc(self) -> None:
        out = self.run_confirm(good_ec2())
        self.assertIn(NEO4J["EeStackName"], out)
        self.assertIn("neo4j.example.internal", out)
        self.assertIn(VPC, out)

    def test_a_misfit_exits_before_anything_is_printed(self) -> None:
        subnets = dict(good_ec2().subnets)
        subnets["subnet-0aaaaaaa2"] = (OTHER_VPC, "us-east-1b")
        with self.assertRaises(SystemExit) as raised:
            self.run_confirm(good_ec2(subnets=subnets))
        self.assertIn("subnet-0aaaaaaa2", str(raised.exception))

    def test_a_missing_or_one_server_stack_exits_before_the_network_check(
        self,
    ) -> None:
        stack = NEO4J["EeStackName"]
        for values, expected in (
            ({}, f"No Neo4j EE stack named {stack} in this account"),
            (ONE_SERVER_SSM, f"Neo4j EE stack {stack} has one server."),
        ):
            with self.subTest(expected=expected):
                ec2 = mock.MagicMock()
                with self.assertRaises(SystemExit) as raised:
                    self.run_confirm(ec2, values)
                self.assertIn(expected, str(raised.exception))
                ec2.describe_vpcs.assert_not_called()


class DeployParametersTest(Neo4jCase):
    def test_create_stack_sends_the_two_values_and_no_mode(self) -> None:
        sm = mock.MagicMock()
        sm.describe_secret.side_effect = lambda SecretId: {"ARN": f"arn:{SecretId}"}
        cfn = mock.MagicMock()
        cfn.create_stack.return_value = {"StackId": "id-1"}
        cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": []}]}
        clients = {"secretsmanager": sm, "cloudformation": cfn}
        session = mock.MagicMock()
        session.client.side_effect = clients.__getitem__
        settings = deploy.Settings(
            "marketplace", "us-east-1", "ami-1", ["203.0.113.1/32"], "m6i.xlarge",
            "hume.example.com", ZONE_ID, dict(NEO4J),
        )  # fmt: skip
        small = self.dir / "small.yaml"
        small.write_text("Resources: {}\n")
        with (
            mock.patch.object(deploy, "TEMPLATE", small),
            mock.patch.object(deploy.boto3, "Session", return_value=session),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(deploy.deploy_stack(settings, "stack-e"), 0)
        sent = {
            p["ParameterKey"]: p["ParameterValue"]
            for p in cfn.create_stack.call_args.kwargs["Parameters"]
        }
        for name, value in NEO4J.items():
            self.assertEqual(sent[name], value)
        self.assertNotIn("Neo4jMode", sent)
        self.assertNotIn("Neo4jMode", deploy.read_record("stack-e"))


class SmokeSubnetTest(TempEnvCase):
    def run_smoke(self, record: dict[str, str]):
        deploy.write_record("stack-s", record)
        ecs = mock.MagicMock()
        ecs.run_task.return_value = {"failures": [{"reason": "stop here"}]}
        session = mock.MagicMock()
        session.client.return_value = ecs
        instance = {
            "ec2InstanceId": "i-1",
            "status": "ACTIVE",
            "attributes": [{"name": deploy.TRUNK_ATTRIBUTE, "value": "t"}],
        }
        args = argparse.Namespace(
            stack_name="stack-s", profile=None, region=None, expect_attribute=None
        )
        with (
            mock.patch.object(deploy, "make_session", return_value=session),
            mock.patch.object(
                deploy, "wait_for_container_instance", return_value=instance
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            deploy.cmd_smoke(args)
        return ecs

    BASE: ClassVar[dict[str, str]] = {
        "ClusterName": "c",
        "SmokeTestTaskDefinitionArn": "arn:td",
        "InstanceSecurityGroupId": "sg-1",
    }

    def test_the_task_runs_in_the_instance_subnet(self) -> None:
        ecs = self.run_smoke({**self.BASE, "InstanceSubnetId": "subnet-0bbbbbbb1"})
        config = ecs.run_task.call_args.kwargs["networkConfiguration"]
        self.assertEqual(config["awsvpcConfiguration"]["subnets"], ["subnet-0bbbbbbb1"])

    def test_a_public_subnet_output_is_not_used(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            self.run_smoke({**self.BASE, "PublicSubnet1Id": "subnet-0aaaaaaa1"})
        self.assertIn("InstanceSubnetId", str(raised.exception))

    def test_an_old_record_with_a_mode_is_fine(self) -> None:
        record = {
            **self.BASE,
            "InstanceSubnetId": "subnet-0bbbbbbb1",
            "Neo4jMode": "ExistingEE",
        }
        ecs = self.run_smoke(record)
        config = ecs.run_task.call_args.kwargs["networkConfiguration"]
        self.assertEqual(config["awsvpcConfiguration"]["subnets"], ["subnet-0bbbbbbb1"])


if __name__ == "__main__":
    unittest.main()
