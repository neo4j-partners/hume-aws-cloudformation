"""Tests for the parts of prove.py that run without AWS."""

import unittest
from datetime import UTC, datetime
from unittest.mock import MagicMock

import prove


class RecordSecretTest(unittest.TestCase):
    def found(self, *names: str) -> prove.Findings:
        found = prove.Findings()
        for name in names:
            prove.record_secret(found, {"Name": name})
        return found

    def test_the_secrets_deploy_keeps_are_not_left(self) -> None:
        found = self.found("hume/licence-key")
        self.assertEqual(found.left, {})
        self.assertEqual(sorted(found.kept), ["secret: hume/licence-key"])

    def test_any_other_hume_secret_is_left(self) -> None:
        found = self.found("hume/licence-key-test", "hume/stack-a/db", "hume/other")
        self.assertEqual(found.kept, {})
        self.assertEqual(len(found.left), 3)

    def test_a_kept_secret_scheduled_for_deletion_says_so(self) -> None:
        found = prove.Findings()
        prove.record_secret(
            found, {"Name": "hume/licence-key", "DeletedDate": datetime.now(UTC)}
        )
        self.assertIn("scheduled for deletion", found.kept["secret: hume/licence-key"])


def database(identifier: str, project: str = prove.PROJECT_TAG) -> dict:
    return {
        "DBInstanceIdentifier": identifier,
        "DBInstanceStatus": "available",
        "InstanceCreateTime": datetime(2026, 10, 21, 21, 15, 0, tzinfo=UTC),
        "TagList": [{"Key": "Project", "Value": project}],
    }


class DatabaseLinesTest(unittest.TestCase):
    def test_only_the_named_stack_is_listed(self) -> None:
        databases = [
            database("hume-ecs-azz-1-database-abc"),
            database("hume-ecs-wooz-2-database-def"),
            database("hume-ecs-azz-10-database-ghi"),
        ]
        lines = prove.database_lines(databases, "hume-ecs-azz-1")
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("hume-ecs-azz-1-database-abc available"))

    def test_another_project_with_the_same_prefix_is_ignored(self) -> None:
        databases = [database("hume-ecs-azz-1-database-abc", project="other")]
        self.assertEqual(prove.database_lines(databases, "hume-ecs-azz-1"), [])

    def test_a_replaced_database_shows_a_different_line(self) -> None:
        before = prove.database_lines([database("s-database-a")], "s")
        after = prove.database_lines([database("s-database-b")], "s")
        self.assertNotEqual(before, after)


COUNTERS = f"""    pkts      bytes target     prot opt in     out     source               destination
       {{a}}      {{b}} REJECT     tcp  --  *      *       0.0.0.0/0            {{ip1}}  tcp dpt:443 /* {prove.RULE_COMMENT} */ reject-with icmp-port-unreachable
       {{c}}      {{d}} REJECT     tcp  --  *      *       0.0.0.0/0            {{ip2}}  tcp dpt:443 /* {prove.RULE_COMMENT} */ reject-with icmp-port-unreachable
"""


def counters(a: int, c: int) -> str:
    return COUNTERS.format(
        a=a, b=a * 60, c=c, d=c * 60, ip1="198.51.100.1", ip2="198.51.100.2"
    )


class CounterTest(unittest.TestCase):
    def test_parse_reads_packets_per_address(self) -> None:
        self.assertEqual(
            prove.parse_counters(counters(2, 5)),
            {"198.51.100.1": 2, "198.51.100.2": 5},
        )

    def test_parse_skips_the_header_and_other_rules(self) -> None:
        other = "      9      540 ACCEPT     tcp  --  *      *   0.0.0.0/0  10.0.0.1\n"
        self.assertEqual(len(prove.parse_counters(other + counters(0, 0))), 2)

    def test_delta_is_zero_when_nothing_was_counted_in_between(self) -> None:
        deltas = prove.counter_deltas(
            prove.parse_counters(counters(2, 2)), prove.parse_counters(counters(2, 2))
        )
        self.assertEqual(deltas, {"198.51.100.1": 0, "198.51.100.2": 0})

    def test_delta_ignores_the_packets_from_the_probes(self) -> None:
        deltas = prove.counter_deltas(
            prove.parse_counters(counters(2, 2)), prove.parse_counters(counters(2, 4))
        )
        self.assertEqual(deltas, {"198.51.100.1": 0, "198.51.100.2": 2})

    def test_missing_rules_exit(self) -> None:
        with self.assertRaises(SystemExit):
            prove.counter_deltas({}, {})
        with self.assertRaises(SystemExit):
            prove.counter_deltas({"198.51.100.1": 2}, {})


class StaleReasonTest(unittest.TestCase):
    def test_an_instance_the_api_no_longer_returns_is_stale(self) -> None:
        session = MagicMock()
        session.client("ec2").describe_instances.return_value = {"Reservations": []}
        arn = "arn:aws:ec2:us-east-1:123456789012:instance/i-0123456789abcdef0"
        self.assertEqual(prove.stale_reason(session, arn), "instance no longer exists")

    def test_an_inactive_ecs_service_is_stale(self) -> None:
        session = MagicMock()
        session.client("ecs").describe_services.return_value = {
            "services": [{"status": "INACTIVE"}],
            "failures": [],
        }
        arn = "arn:aws:ecs:us-east-1:123456789012:service/some-stack/media"
        self.assertEqual(prove.stale_reason(session, arn), "ECS service is INACTIVE")


if __name__ == "__main__":
    unittest.main()
