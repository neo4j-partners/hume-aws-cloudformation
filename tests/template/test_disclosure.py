"""Offline tests for the connection disclosure in the Hume template.

AWS Marketplace reads the ExternalConnectivityDisclosure metadata and the
template Description to judge every connection the product makes. These tests
pin the runtime connections, so that adding or dropping one is a deliberate
change, and check that no licence call to GraphAware is disclosed. Hume checks
its licence offline.

Run from the repository root:
    uv run --with boto3 --with python-dotenv --with pyyaml \
        python -m unittest tests.template.test_disclosure
"""

import unittest

from tests.helpers import template as tm

RUNTIME_ENDPOINTS = [
    "Neo4j EE cluster in the same VPC, neo4j+s on port 7687",
    "Amazon RDS database and Amazon EFS file system in the same VPC",
    "The OpenID Connect provider set in OidcIssuerUri, over HTTPS",
    "tile.openstreetmap.org",
]


class DisclosureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = tm.load()
        cls.disclosure = cls.template["Metadata"]["ExternalConnectivityDisclosure"]

    def test_runtime_connections_are_pinned(self) -> None:
        endpoints = [c["Endpoint"] for c in self.disclosure["RuntimeConnections"]]
        self.assertEqual(endpoints, RUNTIME_ENDPOINTS)

    def test_no_deploy_time_downloads(self) -> None:
        self.assertEqual(self.disclosure["DeployTimeExternalDependencies"], [])

    def test_no_licence_call_is_disclosed(self) -> None:
        texts = {
            "Description": self.template["Description"],
            "disclosure": str(self.disclosure),
        }
        for name, text in texts.items():
            with self.subTest(name):
                self.assertNotIn("GraphAware", text)
                self.assertNotIn("licence", text.lower())


if __name__ == "__main__":
    unittest.main()
