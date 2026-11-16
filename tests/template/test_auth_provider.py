"""Offline tests for the AuthProvider parameter of hume-ecs.template.yaml.

Oidc is the default. Native is a fallback for troubleshooting. Both are
resolved with the helpers of tests/helpers/template.py, so no AWS call is made.

Run from the repository root:
    uv run --with pyyaml python -m unittest tests.template.test_auth_provider
"""

import re
import unittest

from tests.helpers import template as tm

ISSUER = tm.BASE_INPUTS["OidcIssuerUri"]
CLIENT_ID = tm.BASE_INPUTS["OidcClientId"]
NATIVE = {"AuthProvider": "Native", "OidcIssuerUri": "", "OidcClientId": ""}
SERVICE_OIDC_SETTINGS = {
    "hume.security.provider": "oidc",
    "hume.security.role-management": "provider",
    "hume.security.oidc.issuer-uri": ISSUER,
    "hume.security.oidc.jwk-set-uri": {"Fn::Sub": f"{ISSUER}/.well-known/jwks.json"},
    "hume.security.oidc.client-id": CLIENT_ID,
    "hume.security.oidc.principal-claim": "username",
    "hume.security.oidc.roles-claim-paths": "$['cognito:groups']",
    "hume.security.oidc.roles-mappings": "hume-admins=ADMINISTRATOR,hume-users=USER",
}
WEB_OIDC_SETTINGS = {
    "OIDC_ENABLED": "true",
    "OIDC_ISSUER_URL": ISSUER,
    "OIDC_ISSUER_URI": ISSUER,
    "OIDC_CLIENT_ID": CLIENT_ID,
    "OIDC_SCOPE": "openid profile email",
    "OIDC_SESSION_RESTORATION_METHOD": "full-page-redirect",
}
ADMIN_CREATION = "com.hume.core.admin.setup.step.admin_creation"
NATIVE_SECRETS = {
    "hume.security.native.jwt.key",
    f"{ADMIN_CREATION}.password",
}


def container(resolved: dict, task: str) -> dict:
    (definition,) = resolved["resources"][task]["Properties"]["ContainerDefinitions"]
    return definition


def environment(resolved: dict, task: str) -> dict:
    return {e["Name"]: e["Value"] for e in container(resolved, task)["Environment"]}


def secret_names(resolved: dict, task: str) -> set[str]:
    return {s["Name"] for s in container(resolved, task).get("Secrets", [])}


class TemplateCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = tm.load()
        cls.oidc = tm.resolve_template(cls.template, tm.BASE_INPUTS)
        cls.native = tm.resolve_template(cls.template, tm.BASE_INPUTS | NATIVE)


class ParameterTest(TemplateCase):
    def test_oidc_is_the_default_and_native_the_only_other_value(self) -> None:
        spec = self.template["Parameters"]["AuthProvider"]
        self.assertEqual(spec["Default"], "Oidc")
        self.assertEqual(spec["AllowedValues"], ["Oidc", "Native"])

    def test_defaults_are_the_values_proven_against_cognito(self) -> None:
        params = self.template["Parameters"]
        self.assertEqual(params["OidcIssuerUri"]["Default"], "")
        self.assertEqual(params["OidcClientId"]["Default"], "")
        self.assertEqual(
            params["OidcRolesMappings"]["Default"],
            "hume-admins=ADMINISTRATOR,hume-users=USER",
        )
        self.assertEqual(params["OidcRolesClaimPath"]["Default"], "$['cognito:groups']")
        self.assertEqual(params["OidcPrincipalClaim"]["Default"], "username")
        self.assertEqual(params["OidcJwkSetUri"]["Default"], "")
        self.assertEqual(params["OidcScope"]["Default"], "openid profile email")

    def test_issuer_pattern(self) -> None:
        pattern = re.compile(
            self.template["Parameters"]["OidcIssuerUri"]["AllowedPattern"]
        )
        self.assertTrue(pattern.match(""))
        self.assertTrue(
            pattern.match("https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Ab")
        )
        self.assertFalse(pattern.match("https://idp.example.test/"))
        self.assertFalse(pattern.match("http://idp.example.test"))

    def test_jwk_set_uri_pattern(self) -> None:
        pattern = re.compile(
            self.template["Parameters"]["OidcJwkSetUri"]["AllowedPattern"]
        )
        self.assertTrue(pattern.match(""))
        self.assertTrue(pattern.match("https://idp.example.test/oauth2/a/v1/keys"))
        self.assertFalse(pattern.match("http://idp.example.test/keys"))
        self.assertFalse(pattern.match("https://idp.example.test/a b"))

    def test_scope_pattern_needs_openid(self) -> None:
        pattern = re.compile(self.template["Parameters"]["OidcScope"]["AllowedPattern"])
        for good in (
            "openid",
            "openid profile email",
            "profile openid",
            "openid profile email api://client-1/access",
        ):
            self.assertTrue(pattern.match(good), good)
        for bad in ("", "profile email", "openidx", "openid  email", " openid"):
            self.assertFalse(pattern.match(bad), bad)

    def test_roles_mappings_pattern_takes_only_whole_pairs(self) -> None:
        pattern = re.compile(
            self.template["Parameters"]["OidcRolesMappings"]["AllowedPattern"]
        )
        self.assertTrue(pattern.match("hume-admins=ADMINISTRATOR"))
        self.assertTrue(pattern.match("hume-admins=ADMINISTRATOR,hume-users=USER"))
        for bad in ("", "hume-admins", "a=b,", ",a=b", "a==b", "a b=c", "a=b,c"):
            self.assertFalse(pattern.match(bad), bad)


class RulesTest(TemplateCase):
    def test_oidc_needs_the_issuer_and_the_client_id(self) -> None:
        self.assertEqual(tm.failed_rules(self.template, {}), [])
        self.assertEqual(
            tm.failed_rules(self.template, {"OidcIssuerUri": ""}),
            ["OidcIssuerUri is required when AuthProvider is Oidc."],
        )
        self.assertEqual(
            tm.failed_rules(self.template, {"OidcClientId": ""}),
            ["OidcClientId is required when AuthProvider is Oidc."],
        )

    def test_native_needs_neither(self) -> None:
        self.assertEqual(tm.failed_rules(self.template, NATIVE), [])

    def test_native_is_refused_when_any_slot_is_open_to_the_internet(self) -> None:
        for n in range(1, 6):
            message = (
                f"AuthProvider must be Oidc when AllowedCidr{n} is 0.0.0.0/0. "
                "Native sign-in is not allowed on a load balancer open to the "
                "internet."
            )
            open_slot = {f"AllowedCidr{n}": "0.0.0.0/0"}
            with self.subTest(slot=n):
                self.assertEqual(
                    tm.failed_rules(self.template, NATIVE | open_slot), [message]
                )
                self.assertEqual(tm.failed_rules(self.template, open_slot), [])

    def test_native_accepts_a_range_that_is_not_the_internet(self) -> None:
        narrow = NATIVE | {"AllowedCidr1": "198.51.100.0/24"}
        self.assertEqual(tm.failed_rules(self.template, narrow), [])


class AllowedCidrPatternTest(TemplateCase):
    def test_slots_take_any_ipv4_range(self) -> None:
        for n in range(1, 6):
            pattern = re.compile(
                self.template["Parameters"][f"AllowedCidr{n}"]["AllowedPattern"]
            )
            for good in ("203.0.113.10/32", "198.51.100.0/24", "0.0.0.0/0"):
                self.assertTrue(pattern.match(good), (n, good))
            for bad in ("203.0.113.10", "203.0.113.10/33", "::/0", "a.b.c.d/8"):
                self.assertFalse(pattern.match(bad), (n, bad))
            self.assertEqual(bool(pattern.match("")), n > 1, n)


class OidcTest(TemplateCase):
    def test_api_and_media_get_the_same_oidc_settings(self) -> None:
        for task in ("ApiTaskDefinition", "MediaTaskDefinition"):
            env = environment(self.oidc, task)
            got = {name: env.get(name) for name in SERVICE_OIDC_SETTINGS}
            self.assertEqual(got, SERVICE_OIDC_SETTINGS, task)

    def test_web_gets_the_browser_settings(self) -> None:
        env = environment(self.oidc, "WebTaskDefinition")
        got = {name: env.get(name) for name in WEB_OIDC_SETTINGS}
        self.assertEqual(got, WEB_OIDC_SETTINGS)

    def test_a_jwk_set_uri_replaces_the_cognito_address(self) -> None:
        keys = "https://idp.example.test/oauth2/default/v1/keys"
        resolved = tm.resolve_template(
            self.template, tm.BASE_INPUTS | {"OidcJwkSetUri": keys}
        )
        for task in ("ApiTaskDefinition", "MediaTaskDefinition"):
            env = environment(resolved, task)
            self.assertEqual(env["hume.security.oidc.jwk-set-uri"], keys, task)

    def test_a_scope_reaches_only_the_web_task(self) -> None:
        scope = "openid profile email api://client-1/access"
        resolved = tm.resolve_template(
            self.template, tm.BASE_INPUTS | {"OidcScope": scope}
        )
        web = environment(resolved, "WebTaskDefinition")
        self.assertEqual(web["OIDC_SCOPE"], scope)
        for task in ("ApiTaskDefinition", "MediaTaskDefinition"):
            self.assertNotIn("OIDC_SCOPE", environment(resolved, task), task)

    def test_no_native_administrator_or_jwt_key(self) -> None:
        for name in tm.NATIVE_AUTH_RESOURCES:
            self.assertNotIn(name, self.oidc["resources"])
        for name in tm.NATIVE_AUTH_OUTPUTS:
            self.assertNotIn(name, self.oidc["outputs"])
        for task in ("ApiTaskDefinition", "MediaTaskDefinition"):
            self.assertFalse(secret_names(self.oidc, task) & NATIVE_SECRETS, task)
            env = environment(self.oidc, task)
            self.assertFalse([n for n in env if n.startswith(ADMIN_CREATION)], task)

    def test_no_debug_logging(self) -> None:
        for task in ("ApiTaskDefinition", "MediaTaskDefinition"):
            env = environment(self.oidc, task)
            self.assertFalse([n for n in env if n.startswith("logging.level")], task)


class NativeTest(TemplateCase):
    def test_native_adds_the_administrator_and_jwt_key(self) -> None:
        got = {n: s["Type"] for n, s in self.native["resources"].items()}
        self.assertEqual(got, tm.BASE_RESOURCES | tm.NATIVE_AUTH_RESOURCES)
        self.assertEqual(
            set(self.native["outputs"]),
            tm.BASE_OUTPUTS | tm.NATIVE_AUTH_OUTPUTS,
        )

    def test_native_sets_no_oidc_setting(self) -> None:
        for task in ("ApiTaskDefinition", "MediaTaskDefinition", "WebTaskDefinition"):
            env = environment(self.native, task)
            self.assertFalse(
                [n for n in env if "oidc" in n.lower() or "role-management" in n],
                task,
            )

    def test_native_keeps_the_settings_from_before_auth_provider(self) -> None:
        api = environment(self.native, "ApiTaskDefinition")
        self.assertNotIn("hume.security.provider", api)
        self.assertEqual(api[f"{ADMIN_CREATION}.auto"], "true")
        self.assertEqual(api[f"{ADMIN_CREATION}.username"], "admin")
        self.assertEqual(
            secret_names(self.native, "ApiTaskDefinition") & NATIVE_SECRETS,
            NATIVE_SECRETS,
        )
        media = environment(self.native, "MediaTaskDefinition")
        self.assertEqual(media["hume.security.provider"], "native")
        self.assertIn(
            "hume.security.native.jwt.key",
            secret_names(self.native, "MediaTaskDefinition"),
        )


if __name__ == "__main__":
    unittest.main()
