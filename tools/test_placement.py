"""Placement boundaries are checked without Docker, credentials, or model calls."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from placement import PlacementError, plan, read

DIGEST = "sha256:" + "a" * 64
IMAGE = "registry.test/technical-runtime@" + DIGEST


class PlacementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.catalog = self.root / "deployment-catalog.json"
        sources = {}
        for owner, names in (
            ("authority-owner", ("authority", "agent", "provision")),
            ("service-owner", ("service",)),
        ):
            source = self.root / owner / "compose.yml"
            source.parent.mkdir()
            source.write_text(
                "services:\n" + "".join("  " + name + ": {}\n" for name in names)
            )
            sources[source.relative_to(self.root).as_posix()] = hashlib.sha256(
                source.read_bytes()
            ).hexdigest()
        self.catalog_data = {
            "schema_version": 1,
            "product": "technical-platform",
            "sources": sources,
            "roles": [
                {
                    "id": name,
                    "owner": owner,
                    "kind": kind,
                    "opaque": False,
                    "local_state": True,
                    "credentials_required": True,
                    "recipes": [owner + "/compose.yml"],
                    "configuration_variables": [],
                    "compose_profiles": [],
                }
                for name, owner, kind in (
                    ("authority", "authority-owner", "runtime"),
                    ("service", "service-owner", "runtime"),
                    ("agent", "authority-owner", "node"),
                    ("provision", "authority-owner", "operation"),
                )
            ],
        }
        self.catalog.write_text(json.dumps(self.catalog_data))
        self.inventory = {
            "schema_version": 1,
            "mode": "local",
            "catalogs": {},
            "nodes": [self.node("node-a", "127.0.0.1")],
            "instances": [
                self.instance(
                    "authority-1",
                    "authority",
                    "node-a",
                    "tls://127.0.0.1:8443",
                    "tls",
                    "server",
                ),
                self.instance(
                    "service-1",
                    "service",
                    "node-a",
                    "https://127.0.0.1:8444",
                    "https",
                    "api",
                ),
            ],
            "connections": [
                {
                    "from": "service-1",
                    "to": "authority-1:server",
                    "purpose": "technical",
                    "trust_sha256": DIGEST,
                    "peer_identity": "spiffe://technical.test/authority",
                }
            ],
        }
        self.pin()

    def node(self, name: str, address: str) -> dict:
        return {
            "id": name,
            "address": address,
            "platform": "linux/arm64",
            "executor": {"kind": "local", "reference": "operator-context"},
            "identity": {
                "server_endpoint": "authority-1:server",
                "configuration_ref": "config:" + name,
                "configuration_sha256": DIGEST,
            },
        }

    def instance(
        self, name: str, role: str, node: str, url: str, protocol: str, endpoint: str
    ) -> dict:
        return {
            "id": name,
            "role": "technical-platform/" + role,
            "node": node,
            "image": IMAGE,
            "configuration_ref": "config:" + name,
            "configuration_sha256": DIGEST,
            "credentials": [{"kind": "identity", "reference": "credential:" + name}],
            "state": ["state:" + name],
            "endpoints": [{"name": endpoint, "protocol": protocol, "url": url}],
        }

    def pin(self) -> None:
        self.catalog.write_text(json.dumps(self.catalog_data))
        self.inventory["catalogs"] = {
            "technical-platform": "sha256:"
            + hashlib.sha256(self.catalog.read_bytes()).hexdigest()
        }

    def result(self) -> dict:
        return plan(self.inventory, [self.catalog])

    def denied(self, code: str) -> None:
        with self.assertRaises(PlacementError) as caught:
            self.result()
        self.assertEqual(caught.exception.code, code)

    def distributed(self) -> None:
        self.inventory["mode"] = "distributed"
        self.inventory["nodes"] = [
            self.node("node-a", "node-a.example.test"),
            self.node("node-b", "node-b.example.test"),
        ]
        self.inventory["nodes"][1]["executor"] = {
            "kind": "ssh",
            "reference": "operator-node-b",
        }
        self.inventory["instances"][0]["endpoints"][0]["url"] = (
            "tls://node-a.example.test:8443"
        )
        self.inventory["instances"][1]["node"] = "node-b"
        self.inventory["instances"][1]["endpoints"][0]["url"] = (
            "https://node-b.example.test:8444"
        )

    def test_local_plan_includes_agent_and_never_claims_started_or_resumed(
        self,
    ) -> None:
        result = self.result()
        self.assertEqual(result["status"], "VALIDATED_INVENTORY")
        self.assertEqual(result["runtime_instances"], 2)
        self.assertEqual(
            result["node_prerequisites"],
            [{"node": "node-a", "role": "technical-platform/agent"}],
        )
        self.assertFalse(result["ready_to_start"])
        self.assertFalse(result["queue_resume_issued"])

    def test_distinct_node_plan_keeps_contract_and_prepares_agent_per_node(
        self,
    ) -> None:
        self.distributed()
        result = self.result()
        self.assertEqual(result["declared_nodes"], 2)
        self.assertEqual(len(result["node_prerequisites"]), 2)
        self.assertFalse(result["ready_to_start"])

    def test_full_profile_cannot_omit_runtime(self) -> None:
        self.inventory["instances"].pop()
        self.denied("PLACEMENT_FULL_PROFILE_INCOMPLETE")

    def test_source_change_invalidates_catalog(self) -> None:
        (self.root / "service-owner/compose.yml").write_text("services: {}\n")
        self.denied("PLACEMENT_CATALOG_SOURCE_DRIFT")

    def test_catalog_content_requires_exact_pin(self) -> None:
        self.catalog_data["roles"][1]["configuration_variables"].append("NEW_BINDING")
        self.catalog.write_text(json.dumps(self.catalog_data))
        self.denied("PLACEMENT_CATALOG_PIN_MISMATCH")

    def test_mutable_image_tag_is_refused(self) -> None:
        self.inventory["instances"][1]["image"] = (
            "registry.test/technical-runtime:latest"
        )
        self.denied("PLACEMENT_IMMUTABLE_IMAGE_REQUIRED")

    def test_mutable_volume_cannot_be_omitted(self) -> None:
        self.inventory["instances"][1]["state"] = []
        self.denied("PLACEMENT_LOCAL_STATE_REQUIRED")

    def test_required_credentials_cannot_be_omitted(self) -> None:
        self.inventory["instances"][1]["credentials"] = []
        self.denied("PLACEMENT_CREDENTIALS_REQUIRED")

    def test_arbitrary_implementation_override_is_refused(self) -> None:
        self.inventory["instances"][1]["command"] = ["replacement"]
        self.denied("PLACEMENT_FIELDS_INVALID")

    def replica(self) -> dict:
        row = copy.deepcopy(self.inventory["instances"][1])
        row["id"] = "service-2"
        row["state"] = ["state:service-2"]
        row["credentials"] = [{"kind": "identity", "reference": "credential:service-2"}]
        row["endpoints"][0]["url"] = "https://127.0.0.1:8445"
        self.inventory["instances"].append(row)
        return row

    def test_replicas_keep_distinct_state_and_require_owner_admission(self) -> None:
        self.replica()
        self.assertEqual(
            self.result()["replica_admission_required"],
            {"technical-platform/service": ["service-1", "service-2"]},
        )

    def test_private_identity_cannot_be_shared_by_replicas(self) -> None:
        self.replica()["credentials"] = copy.deepcopy(
            self.inventory["instances"][1]["credentials"]
        )
        self.denied("PLACEMENT_PRIVATE_IDENTITY_REUSED")

    def test_sqlite_or_file_state_cannot_be_shared_by_replicas(self) -> None:
        self.replica()["state"] = ["state:service-1"]
        self.denied("PLACEMENT_LOCAL_STATE_REUSED")

    def test_foreign_owner_api_credential_cannot_be_reused(self) -> None:
        for row in self.inventory["instances"]:
            row["credentials"].append(
                {"kind": "api-key", "reference": "credential:shared-api"}
            )
        self.denied("PLACEMENT_FOREIGN_CREDENTIAL_REUSED")

    def test_advertised_endpoint_must_match_explicit_node_address(self) -> None:
        self.inventory["instances"][1]["endpoints"][0]["url"] = (
            "https://unassigned.example.test:8444"
        )
        self.denied("PLACEMENT_ENDPOINT_NODE_MISMATCH")

    def test_inter_node_loopback_is_refused(self) -> None:
        self.distributed()
        self.inventory["nodes"][0]["address"] = "127.0.0.1"
        self.inventory["instances"][0]["endpoints"][0]["url"] = "tls://127.0.0.1:8443"
        self.denied("PLACEMENT_INTER_NODE_ADDRESS_INVALID")

    def test_inter_node_docker_host_alias_is_refused(self) -> None:
        self.distributed()
        self.inventory["nodes"][0]["address"] = "host.docker.internal"
        self.inventory["instances"][0]["endpoints"][0]["url"] = (
            "tls://host.docker.internal:8443"
        )
        self.denied("PLACEMENT_INTER_NODE_ADDRESS_INVALID")

    def test_two_listeners_cannot_claim_one_node_port(self) -> None:
        self.inventory["instances"][1]["endpoints"][0]["url"] = "https://127.0.0.1:8443"
        self.denied("PLACEMENT_PORT_COLLISION")

    def test_domain_http_is_refused(self) -> None:
        self.inventory["connections"] = [
            {
                "from": "authority-1",
                "to": "service-1:api",
                "purpose": "domain",
                "trust_sha256": DIGEST,
                "peer_identity": "service",
            }
        ]
        self.denied("PLACEMENT_DOMAIN_REQUIRES_KAFKA")

    def test_domain_kafka_is_admitted_with_tls_contract(self) -> None:
        target = self.inventory["instances"][1]["endpoints"][0]
        target.update(protocol="kafka", url="kafka+ssl://127.0.0.1:8444")
        self.inventory["connections"] = [
            {
                "from": "authority-1",
                "to": "service-1:api",
                "purpose": "domain",
                "trust_sha256": DIGEST,
                "peer_identity": "broker",
            }
        ]
        self.assertEqual(self.result()["status"], "VALIDATED_INVENTORY")

    def test_opaque_provider_cannot_be_built_from_consumer_sources(self) -> None:
        self.catalog_data["roles"][1]["opaque"] = True
        self.pin()
        row = self.inventory["instances"][1]
        del row["image"]
        row["build"] = {
            "recipe": "service-owner/Dockerfile",
            "target": "runtime",
            "source_sha256": DIGEST,
        }
        self.denied("PLACEMENT_OPAQUE_SOURCE_BUILD_FORBIDDEN")

    def test_opaque_provider_requires_published_digest(self) -> None:
        self.catalog_data["roles"][1]["opaque"] = True
        self.pin()
        self.inventory["instances"][1]["image"] = DIGEST
        self.denied("PLACEMENT_OPAQUE_PUBLISHED_IMAGE_REQUIRED")

    def test_identity_server_reference_is_mandatory(self) -> None:
        self.inventory["nodes"][0]["identity"]["server_endpoint"] = "unknown:server"
        self.denied("PLACEMENT_IDENTITY_SERVER_REQUIRED")

    def test_catalog_path_traversal_is_refused(self) -> None:
        self.catalog_data["sources"]["../outside.yml"] = "a" * 64
        self.pin()
        self.denied("PLACEMENT_SOURCE_PATH_INVALID")

    def test_json_duplicate_keys_are_refused_before_planning(self) -> None:
        path = self.root / "bad.json"
        path.write_text('{"mode":"local","mode":"distributed"}')
        with self.assertRaises(PlacementError) as caught:
            read(path)
        self.assertEqual(caught.exception.code, "PLACEMENT_DUPLICATE_JSON_KEY")
