"""Tests for the parts of cognito.py that run without AWS."""

import argparse
import base64
import json
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
from botocore.validate import validate_parameters
from dotenv import dotenv_values

import cognito
import deploy

NAME = "hume-test-idp"
POOL_ID = "us-east-1_abc"
POOL_ARN = f"arn:aws:cognito-idp:us-east-1:123456789012:userpool/{POOL_ID}"
ISSUER = f"https://cognito-idp.us-east-1.amazonaws.com/{POOL_ID}"
FUNCTION_ARN = "arn:aws:lambda:us-east-1:123456789012:function:hume-test-idp-pre-token"
ROLE_ARN = "arn:aws:iam::123456789012:role/hume-test-idp-pre-token"
TRIGGER = {"LambdaArn": FUNCTION_ARN, "LambdaVersion": "V2_0"}
# The shape of a live describe_user_pool result on the Essentials plan.
POOL = {
    "Id": POOL_ID,
    "Name": NAME,
    "Arn": POOL_ARN,
    "CreationDate": "2026-11-04",
    "Domain": NAME,
    "EstimatedNumberOfUsers": 3,
    "SchemaAttributes": [],
    "UsernameConfiguration": {"CaseSensitive": False},
    "LambdaConfig": {},
    "DeletionProtection": "INACTIVE",
    "MfaConfiguration": "OFF",
    "UserPoolTier": "ESSENTIALS",
    "UserPoolTags": {"hume-idp": NAME},
    "IssuerConfiguration": {"Type": "ORIGINAL"},
    "KeyConfiguration": {"KeyType": "AWS_OWNED_KEY"},
    "EmailConfiguration": {"EmailSendingAccount": "COGNITO_DEFAULT"},
    "VerificationMessageTemplate": {"DefaultEmailOption": "CONFIRM_WITH_CODE"},
    "UserAttributeUpdateSettings": {"AttributesRequireVerificationBeforeUpdate": []},
    "AccountRecoverySetting": {
        "RecoveryMechanisms": [{"Priority": 1, "Name": "verified_email"}]
    },
    "AdminCreateUserConfig": {
        "AllowAdminCreateUserOnly": True,
        "UnusedAccountValidityDays": 7,
    },
    "Policies": {
        "PasswordPolicy": {"MinimumLength": 8, "TemporaryPasswordValidityDays": 7},
        "SignInPolicy": {"AllowedFirstAuthFactors": ["PASSWORD"]},
    },
}


def token(claims: dict) -> str:
    def part(value: dict) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
        return raw.rstrip("=")

    return f"{part({'alg': 'RS256'})}.{part(claims)}.signature"


def settings(values: dict[str, str]):
    """A stand-in for deploy.env_setting that reads only the given values."""
    return patch.object(deploy, "env_setting", side_effect=values.get)


class MissingError(Exception):
    """Stands in for a modeled AWS error that a mocked client cannot raise."""


def aws_session(**clients: MagicMock) -> MagicMock:
    """A session whose client() returns the given mocks, plus trigger clients."""
    named = {
        "cognito-idp": clients.get("idp", MagicMock()),
        "secretsmanager": clients.get("sm", MagicMock()),
        "iam": clients.get("iam", MagicMock()),
        "lambda": clients.get("lam", MagicMock()),
        "logs": clients.get("logs", MagicMock()),
    }
    named["iam"].create_role.return_value = {"Role": {"Arn": ROLE_ARN}}
    named["lambda"].exceptions.ResourceNotFoundException = MissingError
    named["lambda"].get_function.side_effect = MissingError
    named["lambda"].create_function.return_value = {"FunctionArn": FUNCTION_ARN}
    named["cognito-idp"].describe_user_pool.return_value = {"UserPool": POOL}
    session = MagicMock(profile_name="p", region_name="us-east-1")
    session.client.side_effect = named.__getitem__
    return session


class TokenTest(unittest.TestCase):
    def test_claims_are_decoded_without_padding(self) -> None:
        claims = {"username": "hume-admin", "cognito:groups": ["hume-admins"]}
        self.assertEqual(cognito.decode_claims(token(claims)), claims)

    def test_only_listed_claims_are_shown(self) -> None:
        shown = cognito.shown_claims({"username": "u", "jti": "x", "auth_time": 1})
        self.assertEqual(shown, {"username": "u"})


class PasswordTest(unittest.TestCase):
    def test_password_meets_the_default_policy(self) -> None:
        password = cognito.make_password()
        self.assertGreaterEqual(len(password), 8)
        self.assertTrue(any(c.isupper() for c in password))
        self.assertTrue(any(c.islower() for c in password))
        self.assertTrue(any(c.isdigit() for c in password))
        self.assertTrue(any(not c.isalnum() for c in password))


class ClientUpdateInputTest(unittest.TestCase):
    def test_read_only_fields_are_dropped_and_urls_replaced(self) -> None:
        client = {
            "UserPoolId": "pool",
            "ClientId": "client",
            "ClientName": "hume-web",
            "AllowedOAuthFlows": ["code"],
            "CreationDate": "2026-10-27",
            "LastModifiedDate": "2026-10-27",
            "CallbackURLs": ["https://a"],
        }
        kwargs = cognito.client_update_input(client, ["https://a", "https://b"])
        self.assertNotIn("CreationDate", kwargs)
        self.assertNotIn("LastModifiedDate", kwargs)
        self.assertEqual(kwargs["AllowedOAuthFlows"], ["code"])
        self.assertEqual(kwargs["CallbackURLs"], ["https://a", "https://b"])
        self.assertEqual(kwargs["LogoutURLs"], ["https://a", "https://b"])

    def test_flows_alone_leave_the_urls_as_they_are(self) -> None:
        client = {
            "UserPoolId": "pool",
            "ClientId": "client",
            "CallbackURLs": ["https://a"],
            "LogoutURLs": ["https://b"],
            "ExplicitAuthFlows": ["ALLOW_USER_PASSWORD_AUTH"],
        }
        kwargs = cognito.client_update_input(client, flows=["ALLOW_REFRESH_TOKEN_AUTH"])
        self.assertEqual(kwargs["ExplicitAuthFlows"], ["ALLOW_REFRESH_TOKEN_AUTH"])
        self.assertEqual(kwargs["CallbackURLs"], ["https://a"])
        self.assertEqual(kwargs["LogoutURLs"], ["https://b"])


class RedirectUrlsTest(unittest.TestCase):
    def test_with_and_without_the_slash(self) -> None:
        urls = cognito.redirect_urls("https://h/")
        self.assertEqual(urls, ["https://h", "https://h/"])

    def test_web_url_comes_from_the_base_domain_setting(self) -> None:
        with settings({deploy.BASE_DOMAIN_VAR: " Example.com. "}):
            self.assertEqual(cognito.hume_web_url(), "https://hume.example.com")

    def test_web_url_exits_without_the_base_domain_setting(self) -> None:
        with settings({}):
            with self.assertRaises(SystemExit) as raised:
                cognito.hume_web_url()
        self.assertIn(deploy.BASE_DOMAIN_VAR, str(raised.exception))

    def test_the_old_domain_setting_exits(self) -> None:
        with (
            settings({deploy.OLD_DOMAIN_VAR: "hume.example.com"}),
            self.assertRaises(SystemExit),
        ):
            cognito.hume_web_url()


class NamesTest(unittest.TestCase):
    def test_secret_names_carry_the_pool_name(self) -> None:
        self.assertEqual(
            cognito.secret_name("idp-2", "neo4j-admin"),
            "hume/idp-2/cognito/neo4j-admin",
        )


class SessionTest(unittest.TestCase):
    def args(self, **values: str) -> argparse.Namespace:
        return argparse.Namespace(profile=values.get("profile"), region=None)

    def test_flag_wins_over_the_record(self) -> None:
        with patch("boto3.Session") as session, settings({}):
            cognito.make_session(self.args(profile="flag"), {"Profile": "record"})
        self.assertEqual(session.call_args.kwargs["profile_name"], "flag")

    def test_record_wins_over_the_environment(self) -> None:
        with (
            patch("boto3.Session") as session,
            settings({deploy.PROFILE_VAR: "env"}),
        ):
            cognito.make_session(self.args(), {"Profile": "record", "Region": "r"})
        self.assertEqual(session.call_args.kwargs["profile_name"], "record")
        self.assertEqual(session.call_args.kwargs["region_name"], "r")

    def test_defaults_without_a_record(self) -> None:
        with patch("boto3.Session") as session, settings({}):
            cognito.make_session(self.args(), {})
        self.assertEqual(
            session.call_args.kwargs,
            {
                "profile_name": deploy.DEFAULT_PROFILE,
                "region_name": deploy.DEFAULT_REGION,
            },
        )


class TempDeployDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        patcher = patch.object(deploy, "DEPLOY_DIR", Path(self.directory.name))
        patcher.start()
        self.addCleanup(patcher.stop)


class RecordFileTest(TempDeployDirCase):
    def test_record_is_named_after_the_pool_and_is_not_a_stack_record(self) -> None:
        cognito.write_cognito_record(NAME, {"UserPoolId": "pool"})
        self.assertEqual(list(deploy.DEPLOY_DIR.glob("*.txt")), [])
        self.assertTrue((deploy.DEPLOY_DIR / f"{NAME}.cognito").exists())
        self.assertEqual(cognito.read_cognito_record(NAME), {"UserPoolId": "pool"})

    def test_missing_record_reads_empty(self) -> None:
        self.assertEqual(cognito.read_cognito_record(NAME), {})

    def test_up_stops_when_a_record_exists(self) -> None:
        cognito.write_cognito_record(NAME, {"UserPoolId": "pool"})
        session = MagicMock()
        with self.assertRaises(SystemExit):
            cognito.command_up(argparse.Namespace(), session, NAME)
        session.client.assert_not_called()

    def test_teardown_deletes_the_pool_the_secrets_and_the_record(self) -> None:
        cognito.write_cognito_record(NAME, {"UserPoolId": "pool", "Domain": "d"})
        session = aws_session()
        with patch("builtins.print"):
            cognito.command_teardown(argparse.Namespace(), session, NAME)
        idp = session.client("cognito-idp")
        idp.delete_user_pool_domain.assert_called_once_with(
            Domain="d", UserPoolId="pool"
        )
        idp.delete_user_pool.assert_called_once_with(UserPoolId="pool")
        deleted = [
            call.kwargs["SecretId"]
            for call in session.client("secretsmanager").delete_secret.call_args_list
        ]
        self.assertEqual(deleted, [cognito.secret_name(NAME, u) for u in cognito.USERS])
        self.assertEqual(cognito.read_cognito_record(NAME), {})

    def test_teardown_deletes_the_trigger_function_logs_and_role(self) -> None:
        cognito.write_cognito_record(NAME, {"UserPoolId": "pool"})
        session = aws_session()
        with patch("builtins.print"):
            cognito.command_teardown(argparse.Namespace(), session, NAME)
        resource = "hume-test-idp-pre-token"
        session.client("lambda").delete_function.assert_called_once_with(
            FunctionName=resource
        )
        session.client("logs").delete_log_group.assert_called_once_with(
            logGroupName=f"/aws/lambda/{resource}"
        )
        iam = session.client("iam")
        iam.detach_role_policy.assert_called_once_with(
            RoleName=resource, PolicyArn=cognito.LAMBDA_BASIC_POLICY
        )
        iam.delete_role.assert_called_once_with(RoleName=resource)

    def test_teardown_finishes_when_the_trigger_is_already_gone(self) -> None:
        cognito.write_cognito_record(NAME, {"UserPoolId": "pool"})
        session = aws_session()
        lam = session.client("lambda")
        iam = session.client("iam")
        lam.delete_function.side_effect = MissingError
        iam.exceptions.NoSuchEntityException = MissingError
        iam.detach_role_policy.side_effect = MissingError
        with patch("builtins.print"):
            self.assertEqual(
                cognito.command_teardown(argparse.Namespace(), session, NAME), 0
            )
        self.assertEqual(cognito.read_cognito_record(NAME), {})


class UpCase(TempDeployDirCase):
    neo4j_host: str | None = None
    no_bloom: str | None = None

    def setUp(self) -> None:
        super().setUp()
        self.idp = MagicMock()
        self.sm = MagicMock()
        self.idp.create_user_pool.return_value = {"UserPool": {"Id": POOL_ID}}
        self.idp.create_user_pool_client.side_effect = lambda **kwargs: {
            "UserPoolClient": {"ClientId": f"id-{kwargs['ClientName']}"}
        }
        self.session = aws_session(idp=self.idp, sm=self.sm)
        args = argparse.Namespace(domain_prefix=None, neo4j_host=self.neo4j_host)
        values = {deploy.BASE_DOMAIN_VAR: "example.com"}
        if self.no_bloom is not None:
            values["NO_BLOOM"] = self.no_bloom
        with settings(values), patch("builtins.print"):
            self.result = cognito.command_up(args, self.session, NAME)

    def clients(self) -> dict[str, dict]:
        calls = self.idp.create_user_pool_client.call_args_list
        return {call.kwargs["ClientName"]: call.kwargs for call in calls}


class UpTest(UpCase):
    def test_pool_and_domain_take_the_name(self) -> None:
        self.assertEqual(self.result, 0)
        self.assertEqual(self.idp.create_user_pool.call_args.kwargs["PoolName"], NAME)
        self.assertEqual(
            self.idp.create_user_pool_domain.call_args.kwargs["Domain"], NAME
        )

    def test_two_public_code_flow_clients(self) -> None:
        clients = self.clients()
        self.assertEqual(set(clients), {"hume-web", "neo4j"})
        for kwargs in clients.values():
            self.assertFalse(kwargs["GenerateSecret"])
            self.assertEqual(kwargs["AllowedOAuthFlows"], ["code"])
            self.assertEqual(
                kwargs["AllowedOAuthScopes"], ["openid", "profile", "email"]
            )

    def test_hume_web_signs_in_only_with_admin_credentials(self) -> None:
        clients = self.clients()
        self.assertEqual(
            clients["hume-web"]["ExplicitAuthFlows"],
            ["ALLOW_ADMIN_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
        )
        self.assertEqual(
            clients["neo4j"]["ExplicitAuthFlows"], ["ALLOW_REFRESH_TOKEN_AUTH"]
        )
        for kwargs in clients.values():
            self.assertNotIn("ALLOW_USER_PASSWORD_AUTH", kwargs["ExplicitAuthFlows"])

    def test_redirects_come_from_the_hume_domain_and_the_placeholder(self) -> None:
        clients = self.clients()
        self.assertEqual(
            clients["hume-web"]["CallbackURLs"],
            ["https://hume.example.com", "https://hume.example.com/"],
        )
        self.assertEqual(
            clients["neo4j"]["CallbackURLs"], ["https://localhost:7473/browser/"]
        )

    def test_each_user_joins_its_groups(self) -> None:
        groups = [c.kwargs["GroupName"] for c in self.idp.create_group.call_args_list]
        self.assertEqual(
            groups,
            [
                "hume-admins",
                "hume-users",
                "neo4j-admins",
                "spine-analysts",
                "spine-officers",
            ],
        )
        users = [
            c.kwargs["Username"] for c in self.idp.admin_create_user.call_args_list
        ]
        self.assertEqual(
            users,
            [
                "hume-admin",
                "hume-user",
                "neo4j-admin",
                "spine-analyst",
                "spine-officer",
            ],
        )
        joined = [
            (c.kwargs["Username"], c.kwargs["GroupName"])
            for c in self.idp.admin_add_user_to_group.call_args_list
        ]
        self.assertEqual(
            joined,
            [
                ("hume-admin", "hume-admins"),
                ("hume-admin", "spine-officers"),
                ("hume-user", "hume-users"),
                ("neo4j-admin", "neo4j-admins"),
                ("spine-analyst", "hume-users"),
                ("spine-analyst", "spine-analysts"),
                ("spine-officer", "hume-users"),
                ("spine-officer", "spine-officers"),
            ],
        )
        secrets = [c.kwargs["Name"] for c in self.sm.create_secret.call_args_list]
        self.assertEqual(secrets, [cognito.secret_name(NAME, u) for u in users])

    def test_the_new_pool_is_wired_to_the_trigger(self) -> None:
        kwargs = self.idp.update_user_pool.call_args.kwargs
        self.assertEqual(kwargs["UserPoolId"], POOL_ID)
        self.assertEqual(kwargs["LambdaConfig"], {"PreTokenGenerationConfig": TRIGGER})

    def test_record_holds_ids_and_no_password(self) -> None:
        self.assertEqual(
            cognito.read_cognito_record(NAME),
            {
                "Name": NAME,
                "Profile": "p",
                "Region": "us-east-1",
                "UserPoolId": POOL_ID,
                "Domain": NAME,
                "HumeClientId": "id-hume-web",
                "Neo4jClientId": "id-neo4j",
                "Issuer": ISSUER,
                "Users": (
                    "hume-admin,hume-user,neo4j-admin,spine-analyst,spine-officer"
                ),
            },
        )


class UpNeo4jHostTest(UpCase):
    neo4j_host = " Neo4j-OIDC.neo4j.local. "

    def test_redirects_come_from_the_hume_domain_and_the_placeholder(self) -> None:
        self.assertEqual(
            self.clients()["neo4j"]["CallbackURLs"],
            [BROWSER_QUERY_URL, BROWSER_URL, BLOOM_URL],
        )


class UpNeo4jHostNoBloomTest(UpCase):
    neo4j_host = "neo4j-oidc.neo4j.local"
    no_bloom = "true"

    def test_redirects_come_from_the_hume_domain_and_the_placeholder(self) -> None:
        urls = self.clients()["neo4j"]["CallbackURLs"]
        self.assertEqual(len(urls), 2)
        self.assertFalse(any("/bloom/" in url for url in urls))


class UpBadNeo4jHostTest(TempDeployDirCase):
    def test_a_bad_host_stops_before_any_call(self) -> None:
        session = MagicMock()
        args = argparse.Namespace(domain_prefix=None, neo4j_host="x; rm -rf /")
        with settings({}), self.assertRaises(SystemExit):
            cognito.command_up(args, session, NAME)
        session.client.assert_not_called()


ENV_RECORD = {
    "UserPoolId": POOL_ID,
    "HumeClientId": "hume-id",
    "Neo4jClientId": "n-id",
    "Issuer": ISSUER,
}
BROWSER_URL = "https://neo4j-oidc.neo4j.local:7473/browser/"
BROWSER_QUERY_URL = f"{BROWSER_URL}?idp_id=sso&auth_flow_step=redirect_uri"
BLOOM_URL = "https://neo4j-oidc.neo4j.local:7473/bloom/"
PLACEHOLDER_URL = "https://localhost:7473/browser/"


class EnvTest(TempDeployDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.env_file = deploy.DEPLOY_DIR / ".env"
        self.env_file.write_text("HUME_OIDC_ISSUER_URI=old\nHUME_BASE_DOMAIN=h\n")
        patcher = patch.object(deploy, "ENV_FILE", self.env_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.discovery = (200, json.dumps({"issuer": ISSUER}).encode())
        self.session = MagicMock()
        self.idp = self.session.client("cognito-idp")
        self.flows = {
            "hume-id": cognito.CLIENT_AUTH_FLOWS["hume"],
            "n-id": cognito.CLIENT_AUTH_FLOWS["neo4j"],
        }
        self.idp.describe_user_pool_client.side_effect = self.describe
        self.set_neo4j_urls([PLACEHOLDER_URL])

    def set_neo4j_urls(self, urls: list[str]) -> None:
        self.neo4j_urls = urls

    def describe(self, UserPoolId: str, ClientId: str) -> dict:
        names = {"hume-id": "hume-web", "n-id": "neo4j"}
        urls = self.neo4j_urls if ClientId == "n-id" else ["https://h"]
        return {
            "UserPoolClient": {
                "UserPoolId": UserPoolId,
                "ClientId": ClientId,
                "ClientName": names[ClientId],
                "AllowedOAuthFlows": ["code"],
                "ExplicitAuthFlows": self.flows[ClientId],
                "CallbackURLs": urls,
                "LogoutURLs": urls,
            }
        }

    def run_env(
        self,
        advertised_dns: str | None = "neo4j-oidc.neo4j.local",
        zone: str | None = None,
        env: dict[str, str] | None = None,
        no_bloom: bool = False,
    ) -> int:
        args = argparse.Namespace(
            advertised_dns=advertised_dns, private_dns_zone=zone, no_bloom=no_bloom
        )
        with (
            settings(env or {}),
            patch.object(cognito, "fetch", return_value=self.discovery) as fetch,
            patch("builtins.print"),
        ):
            result = cognito.command_env(args, self.session, NAME)
        self.fetch = fetch
        return result

    def assert_nothing_written(self) -> None:
        self.assertFalse((deploy.DEPLOY_DIR / ".env.neo4jee").exists())
        self.assertEqual(
            self.env_file.read_text(), "HUME_OIDC_ISSUER_URI=old\nHUME_BASE_DOMAIN=h\n"
        )

    def test_file_holds_the_ee_settings_in_order(self) -> None:
        cognito.write_cognito_record(NAME, ENV_RECORD)
        self.assertEqual(self.run_env(), 0)
        self.assertEqual(
            (deploy.DEPLOY_DIR / ".env.neo4jee").read_text(),
            f"OIDC_ISSUER_URI={ISSUER}\n"
            "OIDC_CLIENT_ID=hume-id,n-id\n"
            "OIDC_BROWSER_CLIENT_ID=n-id\n"
            "OIDC_PRINCIPAL_CLAIM=sub\n"
            "OIDC_GROUPS_CLAIM=cognito:groups\n"
            "OIDC_ADMIN_GROUP=neo4j-admins\n"
            "OIDC_GROUP_ROLE_MAPPING="
            "spine-analysts=spine_analyst;spine-officers=spine_officer\n"
            "ADVERTISED_DNS=neo4j-oidc.neo4j.local\n"
            "CREATE_PRIVATE_DNS=true\n"
            "PRIVATE_DNS_ZONE=neo4j.local\n",
        )

    def test_group_role_mapping_reads_back_whole_from_the_file(self) -> None:
        cognito.write_cognito_record(NAME, ENV_RECORD)
        self.run_env()
        values = dotenv_values(deploy.DEPLOY_DIR / ".env.neo4jee")
        self.assertEqual(
            values["OIDC_GROUP_ROLE_MAPPING"],
            "spine-analysts=spine_analyst;spine-officers=spine_officer",
        )

    def test_root_env_gets_the_issuer_and_the_hume_client_id(self) -> None:
        cognito.write_cognito_record(NAME, ENV_RECORD)
        self.run_env()
        self.assertEqual(
            self.env_file.read_text(),
            f"HUME_OIDC_ISSUER_URI={ISSUER}\n"
            "HUME_BASE_DOMAIN=h\n"
            "HUME_OIDC_CLIENT_ID=hume-id\n",
        )
        self.fetch.assert_called_once_with(f"{ISSUER}/.well-known/openid-configuration")

    def test_advertised_dns_comes_from_the_flag_then_the_setting(self) -> None:
        cognito.write_cognito_record(NAME, ENV_RECORD)
        self.run_env(None, env={"NEO4J_ADVERTISED_DNS": "neo4j.graph.example.com"})
        text = (deploy.DEPLOY_DIR / ".env.neo4jee").read_text()
        self.assertIn("ADVERTISED_DNS=neo4j.graph.example.com\n", text)

        cognito.write_cognito_record(NAME, ENV_RECORD)
        self.run_env(env={"NEO4J_ADVERTISED_DNS": "neo4j.graph.example.com"})
        text = (deploy.DEPLOY_DIR / ".env.neo4jee").read_text()
        self.assertIn("ADVERTISED_DNS=neo4j-oidc.neo4j.local\n", text)

    def test_missing_advertised_dns_is_refused(self) -> None:
        cognito.write_cognito_record(NAME, ENV_RECORD)
        with self.assertRaises(SystemExit):
            self.run_env(None)
        self.assert_nothing_written()

    def test_zone_flag_wins_and_the_name_is_normalized(self) -> None:
        cognito.write_cognito_record(NAME, ENV_RECORD)
        self.run_env(" Neo4j.Graph.Example.COM. ", zone="example.com")
        text = (deploy.DEPLOY_DIR / ".env.neo4jee").read_text()
        self.assertIn("ADVERTISED_DNS=neo4j.graph.example.com\n", text)
        self.assertIn("PRIVATE_DNS_ZONE=example.com\n", text)

    def test_other_issuer_in_discovery_writes_nothing(self) -> None:
        cognito.write_cognito_record(NAME, ENV_RECORD)
        self.discovery = (200, json.dumps({"issuer": f"{ISSUER}/"}).encode())
        with self.assertRaises(SystemExit):
            self.run_env()
        self.assert_nothing_written()

    def test_discovery_error_writes_nothing(self) -> None:
        cognito.write_cognito_record(NAME, ENV_RECORD)
        self.discovery = (404, b"")
        with self.assertRaises(SystemExit):
            self.run_env()
        self.assert_nothing_written()
        self.idp.update_user_pool_client.assert_not_called()

    def test_missing_root_env_writes_nothing(self) -> None:
        cognito.write_cognito_record(NAME, ENV_RECORD)
        self.env_file.unlink()
        with self.assertRaises(SystemExit):
            self.run_env()
        self.assertFalse((deploy.DEPLOY_DIR / ".env.neo4jee").exists())
        self.assertFalse(self.env_file.exists())

    def test_a_name_outside_the_zone_or_without_one_is_refused(self) -> None:
        cognito.write_cognito_record(NAME, ENV_RECORD)
        with self.assertRaises(SystemExit):
            self.run_env(zone="example.com")
        self.assert_nothing_written()

        cognito.write_cognito_record(NAME, ENV_RECORD)
        with self.assertRaises(SystemExit):
            self.run_env("neo4j")

    def test_incomplete_record_is_refused(self) -> None:
        cognito.write_cognito_record(NAME, {"UserPoolId": POOL_ID})
        with self.assertRaises(SystemExit):
            self.run_env()
        self.assert_nothing_written()

    def test_callback_urls_carry_the_browser_query_string(self) -> None:
        self.assertEqual(
            cognito.neo4j_callback_urls("neo4j-oidc.neo4j.local"),
            [BROWSER_QUERY_URL, BROWSER_URL, BLOOM_URL],
        )

    def test_neo4j_client_gets_exactly_the_derived_urls(self) -> None:
        cognito.write_cognito_record(NAME, ENV_RECORD)
        self.run_env()
        self.assertEqual(
            [c.kwargs for c in self.idp.describe_user_pool_client.call_args_list],
            [
                {"UserPoolId": POOL_ID, "ClientId": "hume-id"},
                {"UserPoolId": POOL_ID, "ClientId": "n-id"},
            ],
        )
        urls = [BROWSER_QUERY_URL, BROWSER_URL, BLOOM_URL]
        self.idp.update_user_pool_client.assert_called_once_with(
            UserPoolId=POOL_ID,
            ClientId="n-id",
            ClientName="neo4j",
            AllowedOAuthFlows=["code"],
            ExplicitAuthFlows=["ALLOW_REFRESH_TOKEN_AUTH"],
            CallbackURLs=urls,
            LogoutURLs=urls,
        )

    def test_old_password_flows_are_replaced_on_both_clients(self) -> None:
        cognito.write_cognito_record(NAME, ENV_RECORD)
        old = ["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
        self.flows = {"hume-id": old, "n-id": old}
        self.run_env()
        updates = [c.kwargs for c in self.idp.update_user_pool_client.call_args_list]
        self.assertEqual(
            [(u["ClientId"], u["ExplicitAuthFlows"]) for u in updates],
            [
                (
                    "hume-id",
                    ["ALLOW_ADMIN_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
                ),
                ("n-id", ["ALLOW_REFRESH_TOKEN_AUTH"]),
                ("n-id", ["ALLOW_REFRESH_TOKEN_AUTH"]),
            ],
        )
        self.assertEqual(updates[0]["CallbackURLs"], ["https://h"])
        self.assertEqual(updates[1]["CallbackURLs"], [PLACEHOLDER_URL])

    def test_flows_already_set_are_left_alone(self) -> None:
        cognito.write_cognito_record(NAME, ENV_RECORD)
        self.flows = {
            "hume-id": list(reversed(cognito.CLIENT_AUTH_FLOWS["hume"])),
            "n-id": cognito.CLIENT_AUTH_FLOWS["neo4j"],
        }
        self.set_neo4j_urls([BROWSER_QUERY_URL, BROWSER_URL, BLOOM_URL])
        self.run_env()
        self.idp.update_user_pool_client.assert_not_called()

    def test_old_and_hand_added_urls_are_dropped(self) -> None:
        cognito.write_cognito_record(NAME, ENV_RECORD)
        self.set_neo4j_urls(
            [PLACEHOLDER_URL, "https://old.neo4j.local:7473/browser/", BROWSER_URL]
        )
        self.run_env()
        kwargs = self.idp.update_user_pool_client.call_args.kwargs
        self.assertEqual(
            kwargs["CallbackURLs"], [BROWSER_QUERY_URL, BROWSER_URL, BLOOM_URL]
        )
        self.assertNotIn(PLACEHOLDER_URL, kwargs["LogoutURLs"])

    def test_no_update_when_the_urls_already_match_in_any_order(self) -> None:
        cognito.write_cognito_record(NAME, ENV_RECORD)
        for urls in (
            [BROWSER_QUERY_URL, BROWSER_URL, BLOOM_URL],
            [BLOOM_URL, BROWSER_URL, BROWSER_QUERY_URL],
        ):
            with self.subTest(urls=urls):
                self.set_neo4j_urls(urls)
                self.assertEqual(self.run_env(), 0)
                self.idp.update_user_pool_client.assert_not_called()

    def test_no_bloom_flag_skips_bloom_and_tells_the_ee_stack(self) -> None:
        cognito.write_cognito_record(NAME, ENV_RECORD)
        self.run_env(no_bloom=True)
        kwargs = self.idp.update_user_pool_client.call_args.kwargs
        self.assertEqual(kwargs["CallbackURLs"], [BROWSER_QUERY_URL, BROWSER_URL])
        text = (deploy.DEPLOY_DIR / ".env.neo4jee").read_text()
        self.assertTrue(text.endswith("NO_BLOOM=true\n"))

    def test_the_no_bloom_setting_decides_the_bloom_url(self) -> None:
        cognito.write_cognito_record(NAME, ENV_RECORD)
        self.run_env(env={"NO_BLOOM": "True"})
        kwargs = self.idp.update_user_pool_client.call_args.kwargs
        self.assertEqual(kwargs["CallbackURLs"], [BROWSER_QUERY_URL, BROWSER_URL])

        cognito.write_cognito_record(NAME, ENV_RECORD)
        self.run_env(env={"NO_BLOOM": "false"})
        kwargs = self.idp.update_user_pool_client.call_args.kwargs
        self.assertIn(BLOOM_URL, kwargs["CallbackURLs"])
        text = (deploy.DEPLOY_DIR / ".env.neo4jee").read_text()
        self.assertNotIn("NO_BLOOM", text)

    def test_invalid_no_bloom_writes_nothing(self) -> None:
        cognito.write_cognito_record(NAME, ENV_RECORD)
        with self.assertRaises(SystemExit):
            self.run_env(env={"NO_BLOOM": "yes"})
        self.assert_nothing_written()

    def test_client_read_failure_writes_nothing(self) -> None:
        cognito.write_cognito_record(NAME, ENV_RECORD)
        self.idp.describe_user_pool_client.side_effect = RuntimeError("no creds")
        with self.assertRaises(RuntimeError):
            self.run_env()
        self.assert_nothing_written()
        self.idp.update_user_pool_client.assert_not_called()


class TokenTriggerSourceTest(unittest.TestCase):
    def handler(self):
        namespace: dict = {}
        # The source is a constant from cognito.py, the text the Lambda zip holds.
        exec(cognito.TOKEN_TRIGGER_SOURCE, namespace)  # noqa: S102
        return namespace["handler"]

    def event(self, client_id: str) -> dict:
        return {
            "version": "2",
            "triggerSource": "TokenGeneration_HostedAuth",
            "callerContext": {"clientId": client_id},
            "request": {"userAttributes": {"email": "a@example.com"}},
            "response": {"claimsAndScopeOverrideDetails": None},
        }

    def test_aud_is_the_client_that_asked_for_the_token(self) -> None:
        for client_id in ("hume-id", "n-id"):
            with self.subTest(client_id=client_id):
                result = self.handler()(self.event(client_id), None)
                self.assertEqual(
                    result["response"]["claimsAndScopeOverrideDetails"],
                    {
                        "accessTokenGeneration": {
                            "claimsToAddOrOverride": {"aud": client_id}
                        }
                    },
                )

    def test_nothing_else_in_the_event_changes(self) -> None:
        result = self.handler()(self.event("n-id"), None)
        expected = self.event("n-id")
        del result["response"], expected["response"]
        self.assertEqual(result, expected)

    def test_the_package_holds_the_source_under_the_handler_module(self) -> None:
        module = cognito.TOKEN_TRIGGER_HANDLER.split(".")[0]
        with zipfile.ZipFile(BytesIO(cognito.token_trigger_zip())) as archive:
            self.assertEqual(archive.namelist(), [f"{module}.py"])
            source = archive.read(f"{module}.py").decode()
        self.assertEqual(source, cognito.TOKEN_TRIGGER_SOURCE)


class PoolUpdateInputTest(unittest.TestCase):
    def test_settings_survive_and_read_only_fields_go(self) -> None:
        kwargs = cognito.pool_update_input(POOL, FUNCTION_ARN)
        self.assertEqual(kwargs["UserPoolId"], POOL_ID)
        self.assertEqual(kwargs["PoolName"], NAME)
        self.assertEqual(kwargs["UserPoolTier"], "ESSENTIALS")
        self.assertEqual(kwargs["Policies"], POOL["Policies"])
        self.assertEqual(kwargs["DeletionProtection"], "INACTIVE")
        for field in ("Id", "Name", "Arn", "CreationDate", "SchemaAttributes"):
            self.assertNotIn(field, kwargs)

    def test_the_old_validity_field_is_dropped(self) -> None:
        kwargs = cognito.pool_update_input(POOL, FUNCTION_ARN)
        self.assertEqual(
            kwargs["AdminCreateUserConfig"], {"AllowAdminCreateUserOnly": True}
        )

    def test_other_triggers_stay_and_the_legacy_field_goes(self) -> None:
        pool = POOL | {
            "LambdaConfig": {"PreSignUp": "arn:x", "PreTokenGeneration": "arn:old"}
        }
        kwargs = cognito.pool_update_input(pool, FUNCTION_ARN)
        self.assertEqual(
            kwargs["LambdaConfig"],
            {"PreSignUp": "arn:x", "PreTokenGenerationConfig": TRIGGER},
        )

    def test_the_sdk_accepts_the_update(self) -> None:
        client = boto3.client(
            "cognito-idp",
            region_name="us-east-1",
            aws_access_key_id="unused",
            aws_secret_access_key="unused",
        )
        shape = client.meta.service_model.operation_model("UpdateUserPool").input_shape
        validate_parameters(cognito.pool_update_input(POOL, FUNCTION_ARN), shape)
        self.assertLessEqual(set(cognito.POOL_FIELDS), set(shape.members))


class InstallTokenTriggerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.session = aws_session()
        self.idp = self.session.client("cognito-idp")
        self.lam = self.session.client("lambda")
        self.iam = self.session.client("iam")

    def install(self) -> None:
        with patch("builtins.print"), patch("time.sleep"):
            cognito.install_token_trigger(self.session, NAME, POOL_ID)

    def test_function_runs_a_recent_python_under_its_role(self) -> None:
        self.install()
        kwargs = self.lam.create_function.call_args.kwargs
        self.assertEqual(kwargs["FunctionName"], "hume-test-idp-pre-token")
        self.assertEqual(kwargs["Role"], ROLE_ARN)
        self.assertEqual(kwargs["Handler"], "index.handler")
        minor = int(kwargs["Runtime"].removeprefix("python3."))
        self.assertGreaterEqual(minor, 12)

    def test_role_trusts_lambda_and_has_basic_execution_only(self) -> None:
        self.install()
        kwargs = self.iam.create_role.call_args.kwargs
        trust = json.loads(kwargs["AssumeRolePolicyDocument"])
        self.assertEqual(
            trust["Statement"][0]["Principal"], {"Service": "lambda.amazonaws.com"}
        )
        self.iam.attach_role_policy.assert_called_once_with(
            RoleName="hume-test-idp-pre-token",
            PolicyArn=(
                "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
            ),
        )

    def test_only_this_pool_may_invoke_the_function(self) -> None:
        self.install()
        self.lam.add_permission.assert_called_once_with(
            FunctionName="hume-test-idp-pre-token",
            StatementId="cognito-invoke",
            Action="lambda:InvokeFunction",
            Principal="cognito-idp.amazonaws.com",
            SourceArn=POOL_ARN,
        )

    def test_the_permission_comes_before_the_wiring(self) -> None:
        steps = MagicMock()
        steps.attach_mock(self.lam.add_permission, "permission")
        steps.attach_mock(self.idp.update_user_pool, "wiring")
        self.install()
        self.assertEqual(
            [step[0] for step in steps.mock_calls], ["permission", "wiring"]
        )

    def test_a_role_that_iam_has_not_spread_is_retried(self) -> None:
        self.lam.exceptions.InvalidParameterValueException = ValueError
        self.lam.create_function.side_effect = [
            ValueError("cannot be assumed"),
            {"FunctionArn": FUNCTION_ARN},
        ]
        self.install()
        self.assertEqual(self.lam.create_function.call_count, 2)
        self.assertEqual(
            self.idp.update_user_pool.call_args.kwargs["LambdaConfig"],
            {"PreTokenGenerationConfig": TRIGGER},
        )

    def test_a_role_that_never_spreads_stops_before_the_wiring(self) -> None:
        self.lam.exceptions.InvalidParameterValueException = ValueError
        self.lam.create_function.side_effect = ValueError("cannot be assumed")
        with self.assertRaises(ValueError):
            self.install()
        self.idp.update_user_pool.assert_not_called()

    def test_a_second_run_keeps_the_role_and_updates_the_code(self) -> None:
        self.iam.exceptions.EntityAlreadyExistsException = FileExistsError
        self.iam.create_role.side_effect = FileExistsError
        self.iam.get_role.return_value = {"Role": {"Arn": ROLE_ARN}}
        self.lam.get_function.side_effect = None
        self.lam.get_function.return_value = {
            "Configuration": {"FunctionArn": FUNCTION_ARN}
        }
        self.lam.exceptions.ResourceConflictException = FileExistsError
        self.lam.add_permission.side_effect = FileExistsError
        self.idp.describe_user_pool.return_value = {
            "UserPool": POOL | {"LambdaConfig": {"PreTokenGenerationConfig": TRIGGER}}
        }
        self.install()
        self.lam.create_function.assert_not_called()
        self.lam.update_function_code.assert_called_once()
        self.idp.update_user_pool.assert_not_called()


class SyncTest(TempDeployDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.idp = MagicMock()
        self.sm = MagicMock()
        self.session = aws_session(idp=self.idp, sm=self.sm)
        self.idp.list_groups.return_value = {
            "Groups": [
                {"GroupName": name}
                for name in ("hume-admins", "hume-users", "neo4j-admins")
            ]
        }
        self.idp.list_users.return_value = {
            "Users": [
                {"Username": name}
                for name in ("hume-admin", "hume-user", "neo4j-admin")
            ]
        }
        cognito.write_cognito_record(
            NAME, {"UserPoolId": POOL_ID, "Users": "hume-admin,hume-user,neo4j-admin"}
        )

    def sync(self) -> int:
        with patch("builtins.print"):
            return cognito.command_sync(argparse.Namespace(), self.session, NAME)

    def test_only_the_missing_groups_and_users_are_created(self) -> None:
        self.assertEqual(self.sync(), 0)
        self.assertEqual(
            [c.kwargs["GroupName"] for c in self.idp.create_group.call_args_list],
            ["spine-analysts", "spine-officers"],
        )
        self.assertEqual(
            [c.kwargs["Username"] for c in self.idp.admin_create_user.call_args_list],
            ["spine-analyst", "spine-officer"],
        )
        self.assertEqual(
            [c.kwargs["Name"] for c in self.sm.create_secret.call_args_list],
            [
                cognito.secret_name(NAME, "spine-analyst"),
                cognito.secret_name(NAME, "spine-officer"),
            ],
        )

    def test_the_existing_pool_is_wired_and_the_record_lists_every_user(self) -> None:
        self.sync()
        self.assertEqual(
            self.idp.update_user_pool.call_args.kwargs["LambdaConfig"],
            {"PreTokenGenerationConfig": TRIGGER},
        )
        self.assertEqual(
            cognito.read_cognito_record(NAME)["Users"], ",".join(cognito.USERS)
        )

    def test_a_pool_without_a_record_is_refused(self) -> None:
        (deploy.DEPLOY_DIR / f"{NAME}.cognito").unlink()
        with self.assertRaises(SystemExit):
            self.sync()
        self.session.client.assert_not_called()


class CheckTest(TempDeployDirCase):
    def test_check_signs_in_with_the_admin_flow(self) -> None:
        cognito.write_cognito_record(NAME, ENV_RECORD)
        session = MagicMock()
        clients = {"secretsmanager": MagicMock(), "cognito-idp": MagicMock()}
        session.client.side_effect = clients.__getitem__
        clients["secretsmanager"].get_secret_value.return_value = {"SecretString": "pw"}
        jwt = token({"token_use": "id"})
        clients["cognito-idp"].admin_initiate_auth.return_value = {
            "AuthenticationResult": {"AccessToken": jwt, "IdToken": jwt}
        }
        discovery = (200, json.dumps({"issuer": ISSUER}).encode())
        args = argparse.Namespace(stack_name="s", user="hume-admin")
        with (
            patch.object(deploy, "read_record", return_value={"BaseDomainName": "h"}),
            patch.object(cognito, "fetch", return_value=discovery),
            patch("builtins.print"),
        ):
            self.assertEqual(cognito.command_check(args, session, NAME), 0)
        clients["cognito-idp"].admin_initiate_auth.assert_called_once_with(
            UserPoolId=POOL_ID,
            ClientId="hume-id",
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": "hume-admin", "PASSWORD": "pw"},
        )
        clients["cognito-idp"].initiate_auth.assert_not_called()


class CallbacksTest(TempDeployDirCase):
    def setUp(self) -> None:
        super().setUp()
        cognito.write_cognito_record(
            NAME,
            {"UserPoolId": "pool", "HumeClientId": "hume-id", "Neo4jClientId": "n-id"},
        )
        self.session = MagicMock()
        self.callback_urls = ["https://a"]
        self.idp = self.session.client("cognito-idp")
        self.idp.describe_user_pool_client.side_effect = lambda **kwargs: {
            "UserPoolClient": {
                "UserPoolId": "pool",
                "ClientId": kwargs["ClientId"],
                "ClientName": "hume-web",
                "CallbackURLs": self.callback_urls,
                "LogoutURLs": self.callback_urls,
            }
        }

    def run_callbacks(
        self, client: str, add: str | None, reset_hume: bool = False
    ) -> None:
        args = argparse.Namespace(client=client, add=add, reset_hume=reset_hume)
        values = {deploy.BASE_DOMAIN_VAR: "example.com"}
        with settings(values), patch("builtins.print"):
            cognito.command_callbacks(args, self.session, NAME)

    def test_neo4j_client_is_chosen_with_the_flag(self) -> None:
        self.run_callbacks("neo4j", "https://b")
        update = self.idp.update_user_pool_client.call_args.kwargs
        self.assertEqual(update["ClientId"], "n-id")
        self.assertEqual(update["CallbackURLs"], ["https://a", "https://b"])

    def test_hume_client_is_the_default(self) -> None:
        self.assertEqual(cognito.HUME_CLIENT, "hume")
        self.run_callbacks("hume", None)
        self.assertEqual(
            self.idp.describe_user_pool_client.call_args.kwargs["ClientId"], "hume-id"
        )
        self.idp.update_user_pool_client.assert_not_called()

    def test_a_plain_http_address_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_callbacks("hume", "http://b")
        self.idp.update_user_pool_client.assert_not_called()

    def test_reset_hume_leaves_only_the_two_web_addresses(self) -> None:
        self.callback_urls = [
            "https://hume.example.com:8081",
            "https://hume.example.com:8081/",
            "https://localhost:3000",
        ]
        self.run_callbacks("hume", None, reset_hume=True)
        update = self.idp.update_user_pool_client.call_args.kwargs
        wanted = ["https://hume.example.com", "https://hume.example.com/"]
        self.assertEqual(update["ClientId"], "hume-id")
        self.assertEqual(update["CallbackURLs"], wanted)
        self.assertEqual(update["LogoutURLs"], wanted)

    def test_reset_hume_does_nothing_when_the_addresses_are_already_set(self) -> None:
        self.callback_urls = ["https://hume.example.com/", "https://hume.example.com"]
        self.run_callbacks("hume", None, reset_hume=True)
        self.idp.update_user_pool_client.assert_not_called()

    def test_reset_hume_refuses_the_neo4j_client(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_callbacks("neo4j", None, reset_hume=True)
        self.idp.update_user_pool_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
