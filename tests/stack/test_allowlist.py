"""Tests for the allowlist logic. Every .env is a temp file."""

import unittest
from typing import ClassVar
from unittest import mock

from botocore.exceptions import ClientError

import allowlist
import deploy
from tests.helpers.env import TempEnvCase


class CidrTest(unittest.TestCase):
    def test_accepts_bare_address_and_slash_32(self) -> None:
        self.assertEqual(deploy.normalise_cidr("203.0.113.9"), "203.0.113.9/32")
        self.assertEqual(deploy.normalise_cidr("203.0.113.9/32"), "203.0.113.9/32")

    def test_accepts_ranges_and_the_open_world(self) -> None:
        for good in ("0.0.0.0/0", "10.0.0.0/24", "203.0.113.8/31"):
            with self.subTest(good=good):
                self.assertEqual(deploy.normalise_cidr(good), good)

    def test_rejects_host_bits_bad_prefixes_and_junk(self) -> None:
        bad_values = ["10.0.0.5/24", "203.0.113.9/33", "nope", "::1", "1.2.3"]
        for bad in bad_values:
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                deploy.normalise_cidr(bad)

    def test_list_needs_one_and_at_most_five(self) -> None:
        with self.assertRaises(SystemExit):
            deploy.parse_cidrs(" , ")
        with self.assertRaises(SystemExit):
            deploy.parse_cidrs(",".join(f"203.0.113.{n}" for n in range(1, 7)))

    def test_repeats_are_dropped_in_order(self) -> None:
        self.assertEqual(
            deploy.parse_cidrs("203.0.113.2,203.0.113.1/32,203.0.113.2"),
            ["203.0.113.2/32", "203.0.113.1/32"],
        )

    def test_slots_are_padded_with_empty_values(self) -> None:
        params = deploy.cidr_parameters(["203.0.113.1/32"])
        self.assertEqual(params["AllowedCidr1"], "203.0.113.1/32")
        self.assertEqual([params[f"AllowedCidr{n}"] for n in range(2, 6)], [""] * 4)


class PlanTest(TempEnvCase):
    SLOTS: ClassVar[dict[str, str]] = {
        "AllowedCidr1": "203.0.113.1/32",
        "AllowedCidr2": "",
        "AllowedCidr3": "203.0.113.3/32",
        "AllowedCidr4": "",
        "AllowedCidr5": "",
    }

    def test_add_fills_first_empty_slot_and_never_moves_others(self) -> None:
        self.assertEqual(
            allowlist.plan_add(self.SLOTS, "203.0.113.9/32"),
            ("AllowedCidr2", "203.0.113.9/32"),
        )

    def test_add_of_present_address_is_a_no_op(self) -> None:
        self.assertIsNone(allowlist.plan_add(self.SLOTS, "203.0.113.3/32"))

    def test_add_to_full_list_exits(self) -> None:
        full = {k: f"203.0.113.{n}/32" for n, k in enumerate(self.SLOTS, start=1)}
        with self.assertRaises(SystemExit):
            allowlist.plan_add(full, "203.0.113.99/32")

    def test_remove_changes_only_the_slots_it_must(self) -> None:
        one, two, three = (f"203.0.113.{n}/32" for n in (1, 2, 3))
        three_set = {**self.SLOTS, "AllowedCidr2": two}
        two_set = {**three_set, "AllowedCidr3": ""}
        for slots, address, expected in (
            (self.SLOTS, three, {"AllowedCidr3": ""}),
            (two_set, one, {"AllowedCidr1": two, "AllowedCidr2": ""}),
            (three_set, one, {"AllowedCidr1": three, "AllowedCidr3": ""}),
            (three_set, two, {"AllowedCidr2": ""}),
            (three_set, three, {"AllowedCidr3": ""}),
            (two_set, two, {"AllowedCidr2": ""}),
        ):
            with self.subTest(slots=slots, address=address):
                self.assertEqual(allowlist.plan_remove(slots, address), expected)

    def test_remove_never_leaves_slot_1_empty(self) -> None:
        for count in (2, 3):
            filled = allowlist.SLOT_KEYS[:count]
            slots = {k: "" for k in allowlist.SLOT_KEYS}
            slots.update({k: f"203.0.113.{n}/32" for n, k in enumerate(filled, 1)})
            for address in [v for v in slots.values() if v]:
                after = {**slots, **allowlist.plan_remove(slots, address)}
                self.assertTrue(after["AllowedCidr1"])
                self.assertNotIn(address, after.values())
                kept = sorted(v for v in slots.values() if v and v != address)
                self.assertEqual(sorted(v for v in after.values() if v), kept)

    def test_change_set_sets_every_changed_slot_and_keeps_the_rest(self) -> None:
        cfn = mock.MagicMock()
        params = {"Other": "x", **{k: "" for k in allowlist.SLOT_KEYS}}
        change = {"AllowedCidr1": "203.0.113.2/32", "AllowedCidr2": ""}
        allowlist.build_change_set(cfn, "stack-a", params, change)
        sent = cfn.create_change_set.call_args.kwargs["Parameters"]
        self.assertIn(
            {"ParameterKey": "AllowedCidr1", "ParameterValue": "203.0.113.2/32"}, sent
        )
        self.assertIn({"ParameterKey": "AllowedCidr2", "ParameterValue": ""}, sent)
        self.assertIn({"ParameterKey": "Other", "UsePreviousValue": True}, sent)
        self.assertIn({"ParameterKey": "AllowedCidr3", "UsePreviousValue": True}, sent)

    def test_rejected_change_set_exits_with_a_message(self) -> None:
        cfn = mock.MagicMock()
        cfn.create_change_set.side_effect = ClientError(
            {"Error": {"Code": "ValidationError", "Message": "pattern mismatch"}},
            "CreateChangeSet",
        )
        with self.assertRaises(SystemExit) as raised:
            allowlist.build_change_set(
                cfn, "stack-a", {"AllowedCidr1": ""}, {"AllowedCidr1": ""}
            )
        self.assertIn("pattern mismatch", str(raised.exception))

    def test_remove_refuses_unknown_and_last_address(self) -> None:
        with self.assertRaises(SystemExit):
            allowlist.plan_remove(self.SLOTS, "203.0.113.50/32")
        last = {**self.SLOTS, "AllowedCidr3": ""}
        with self.assertRaises(SystemExit) as raised:
            allowlist.plan_remove(last, "203.0.113.1/32")
        self.assertIn("last address", str(raised.exception))

    def test_write_back_keeps_other_lines(self) -> None:
        self.env_file.write_text(
            "REGISTRY_USER=someone\nALLOWED_CIDRS=203.0.113.1/32\nOTHER=1\n"
        )
        deploy.write_record("stack-a", {"StackName": "stack-a"})
        new = ["203.0.113.1/32", "203.0.113.9/32"]
        allowlist.write_back("stack-a", new, self.env_file)
        self.assertEqual(
            self.env_file.read_text(),
            "REGISTRY_USER=someone\n"
            "ALLOWED_CIDRS=203.0.113.1/32,203.0.113.9/32\n"
            "OTHER=1\n",
        )
        self.assertIn("203.0.113.9/32", deploy.read_record("stack-a")["AllowedCidrs"])

    def test_write_back_warns_when_env_is_missing(self) -> None:
        deploy.write_record("stack-b", {"StackName": "stack-b"})
        allowlist.write_back("stack-b", ["203.0.113.1/32"], self.env_file)
        self.assertFalse(self.env_file.exists())


if __name__ == "__main__":
    unittest.main()
