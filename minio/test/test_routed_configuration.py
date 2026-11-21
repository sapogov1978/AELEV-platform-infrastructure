"""Public routing validation and effect-free real Compose model regression."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "minio_prepare_routed", ROOT / "prepare-routed.py"
)
assert SPEC is not None and SPEC.loader is not None
subject = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = subject
SPEC.loader.exec_module(subject)


def manifest() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "nodes": [
            {
                "id": role,
                "dns": role + ".example.test",
                "address": "192.0.2.10",
                "port": 19001 + index,
            }
            for index, role in enumerate(subject.ROLES)
        ],
    }


def environments(value: dict[str, Any]) -> dict[str, dict[str, str]]:
    peers = subject.topology(value)
    return {
        role: {
            **subject.environment(peers),
            subject.PREFIX + "COMPOSE_PROJECT": "routed-" + role,
            subject.PREFIX + "NETWORK": "routed-" + role + "-private",
            subject.PREFIX + "INGRESS_NETWORK": "routed-" + role + "-ingress",
            peer.prefix + "_VOLUME": "routed-" + role + "-data",
        }
        for role, peer in peers.items()
    }


class RoutedInputTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.value = manifest()

    def reject(self, code: str) -> None:
        with self.assertRaisesRegex(subject.RoutingError, code):
            subject.plan(self.value)

    def public(
        self,
        name: str,
        body: bytes = b"-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n",
    ) -> dict[str, str]:
        path = self.directory / name
        path.write_bytes(body)
        return {"path": str(path), "sha256": hashlib.sha256(body).hexdigest()}

    def public_refs(self) -> dict[str, Any]:
        ca = self.public("ca.crt")
        return {
            role: {"certificate": self.public(role + ".crt"), "ca": ca}
            for role in subject.ROLES
        }

    def test_complete_topology_emits_explicit_fields_without_runtime_claim(
        self,
    ) -> None:
        result = subject.plan(self.value)
        self.assertEqual(len(result["environment"]), 32)
        for row in self.value["nodes"]:
            peer = subject.topology(self.value)[row["id"]]
            self.assertEqual(
                result["environment"][peer.prefix + "_ENDPOINT"],
                f'https://{row["dns"]}:{row["port"]}/data',
            )
        for name in (
            "native_self_dns_verified",
            "native_network_isolation_verified",
            "stable_site_endpoints_verified",
            "runtime_readiness_verified",
            "public_tls_inputs_validated",
            "runtime_effects",
        ):
            self.assertFalse(result[name])

    def test_distinct_node_addresses_allow_equal_ports(self) -> None:
        for index, row in enumerate(self.value["nodes"]):
            row.update(address=f"192.0.2.{index + 10}", port=9000)
        self.assertEqual(len(subject.plan(self.value)["environment"]), 32)

    def test_ipv6_is_explicit_and_keeps_dns_authenticated_url(self) -> None:
        self.value["nodes"][0]["address"] = "2001:db8:1::10"
        result = subject.plan(self.value)
        self.assertEqual(
            result["environment"][subject.PREFIX + "SITE_A_NODE_1_ADDRESS"],
            "2001:db8:1::10",
        )
        self.assertEqual(
            result["environment"][subject.PREFIX + "SITE_A_NODE_1_ENDPOINT"],
            "https://minio-site-a-1.example.test:19001/data",
        )

    def test_root_only_metadata_must_be_explicitly_projected(self) -> None:
        self.value["nodes"][0]["instance"] = "instance-018"
        self.reject("NODE_SCHEMA_INVALID")

    def test_missing_or_duplicated_node_cannot_pass_coverage(self) -> None:
        self.value["nodes"][-1] = copy.deepcopy(self.value["nodes"][0])
        self.reject("NODE_COVERAGE_REQUIRED")
        self.value["nodes"].pop()
        self.reject("NODE_COVERAGE_REQUIRED")

    def test_dns_reuse_and_unsafe_or_loopback_aliases_are_rejected(self) -> None:
        for dns in (
            self.value["nodes"][1]["dns"],
            "127.0.0.1",
            "peer.localhost",
            "host.docker.internal",
            "peer.invalid",
            "peer.example;echo",
            "UPPER.example",
            "peer",
        ):
            with self.subTest(dns=dns):
                value = manifest()
                value["nodes"][0]["dns"] = dns
                with self.assertRaisesRegex(
                    subject.RoutingError, "DNS_INVALID_OR_REUSED"
                ):
                    subject.plan(value)

    def test_node_addresses_require_actual_nonloopback_ip(self) -> None:
        for address in (
            "127.0.0.1",
            "::1",
            "0.0.0.0",
            "fe80::1",
            "224.0.0.1",
            "node.example.test",
        ):
            with self.subTest(address=address):
                self.value["nodes"][0]["address"] = address
                self.reject("NODE_ADDRESS_INVALID")

    def test_native_port_is_an_integer_and_unique_on_node(self) -> None:
        for port in (True, 0, 65536, "19001"):
            with self.subTest(port=port):
                self.value["nodes"][0]["port"] = port
                self.reject("PORT_INVALID")
        self.value["nodes"][0]["port"] = self.value["nodes"][1]["port"]
        self.reject("LISTENER_REUSED")

    def test_complete_environment_set_and_distinct_local_resources_pass(self) -> None:
        result = subject.plan(self.value, environments(self.value))
        self.assertTrue(result["owner_environment_bindings_validated"])
        self.assertFalse(result["native_network_isolation_verified"])

    def test_peer_port_or_url_drift_rejected_before_effects(self) -> None:
        values = environments(self.value)
        values[subject.ROLES[0]][
            subject.PREFIX + "SITE_A_NODE_2_ENDPOINT"
        ] = "https://minio-site-a-2.example.test:9000/data"
        with self.assertRaisesRegex(subject.RoutingError, "ENVIRONMENT_ROUTE_DRIFT"):
            subject.plan(self.value, values)

    def test_resource_reuse_on_same_node_rejected(self) -> None:
        for key in ("COMPOSE_PROJECT", "NETWORK", "INGRESS_NETWORK"):
            values = environments(self.value)
            values[subject.ROLES[1]][subject.PREFIX + key] = values[subject.ROLES[0]][
                subject.PREFIX + key
            ]
            with self.subTest(key=key), self.assertRaisesRegex(
                subject.RoutingError, "RESOURCE_REUSED_ON_NODE"
            ):
                subject.plan(self.value, values)
        values = environments(self.value)
        values[subject.ROLES[0]][subject.PREFIX + "INGRESS_NETWORK"] = values[
            subject.ROLES[0]
        ][subject.PREFIX + "NETWORK"]
        with self.assertRaisesRegex(subject.RoutingError, "RESOURCE_REUSED_ON_NODE"):
            subject.plan(self.value, values)

    def test_separate_daemons_may_keep_same_local_resource_names(self) -> None:
        for index, row in enumerate(self.value["nodes"]):
            row["address"] = f"192.0.2.{index + 10}"
        values = environments(self.value)
        for role in subject.ROLES:
            values[role][subject.PREFIX + "NETWORK"] = "owner-private"
            values[role][subject.PREFIX + "INGRESS_NETWORK"] = "owner-ingress"
        self.assertTrue(
            subject.plan(self.value, values)["owner_environment_bindings_validated"]
        )

    def test_public_tls_refs_bind_both_dns_and_operator_ip_without_keys(self) -> None:
        self.value["public_certificates"] = self.public_refs()
        with mock.patch.object(
            subject.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
        ) as run:
            result = subject.plan(self.value)
        self.assertTrue(result["public_tls_inputs_validated"])
        self.assertEqual(run.call_count, 16)
        for call in run.call_args_list:
            self.assertEqual(call.args[0][:2], ["openssl", "verify"])
            self.assertIn("-trusted", call.args[0])
            self.assertEqual(call.kwargs["timeout"], 10)
        self.assertFalse(result["runtime_readiness_verified"])

    def test_wrong_ca_or_reference_identity_fails_closed(self) -> None:
        self.value["public_certificates"] = self.public_refs()
        with mock.patch.object(
            subject.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                [], 1, stderr=b"untrusted public input"
            ),
        ):
            self.reject("PUBLIC_TLS_CHECK_FAILED")

    def test_certificate_digest_drift_rejected_before_openssl(self) -> None:
        self.value["public_certificates"] = self.public_refs()
        row = self.value["public_certificates"][subject.ROLES[0]]["certificate"]
        Path(row["path"]).write_text("changed public bytes")
        with mock.patch.object(subject.subprocess, "run") as run:
            self.reject("PUBLIC_CERTIFICATE_DIGEST_MISMATCH")
        run.assert_not_called()

    def test_private_key_is_not_a_public_certificate_input(self) -> None:
        reference = self.public(
            "bad.pem",
            b"\n".join(
                (
                    b"-----BEGIN " + b"PRIVATE KEY-----",
                    b"fixture",
                    b"-----END " + b"PRIVATE KEY-----",
                    b"",
                )
            ),
        )
        with self.assertRaisesRegex(subject.RoutingError, "PUBLIC_CERTIFICATE_ONLY"):
            subject.public_file(reference)

    def test_linked_and_oversize_public_files_are_refused(self) -> None:
        ref = self.public("original.crt")
        path = self.directory / "linked.crt"
        path.symlink_to(ref["path"])
        with self.assertRaisesRegex(subject.RoutingError, "LINK_REFUSED"):
            subject.read(path)
        path.unlink()
        os.link(ref["path"], path)
        with self.assertRaisesRegex(
            subject.RoutingError, "REGULAR_BOUNDED_FILE_REQUIRED"
        ):
            subject.read(path)
        large = self.public("large.crt", b"x" * (1024**2 + 1))
        with self.assertRaisesRegex(
            subject.RoutingError, "REGULAR_BOUNDED_FILE_REQUIRED"
        ):
            subject.read(Path(large["path"]))

    def test_duplicate_json_and_cli_diagnostics_are_bounded(self) -> None:
        path = self.directory / "input.json"
        path.write_text('{"schema_version":1,"schema_version":1}')
        result = subprocess.run(
            [sys.executable, str(ROOT / "prepare-routed.py"), "--input", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            json.loads(result.stdout),
            {"status": "BLOCKED", "code": "MINIO_ROUTED_DUPLICATE_JSON_KEY"},
        )

    def test_cli_emits_environment_but_never_runtime_readiness(self) -> None:
        path = self.directory / "input.json"
        path.write_text(json.dumps(self.value))
        result = subprocess.run(
            [sys.executable, str(ROOT / "prepare-routed.py"), "--input", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            json.loads(result.stdout)["status"], "MINIO_ROUTED_INPUTS_VALIDATED"
        )
        self.assertFalse(json.loads(result.stdout)["runtime_readiness_verified"])


@unittest.skipUnless(
    shutil.which("docker"), "Compose CONFIG normalization requires installed Docker CLI"
)
class RoutedComposeTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.values = dict(
            line.split("=", 1)
            for line in (ROOT / ".env.example").read_text().splitlines()
            if line and not line.startswith("#")
        )
        self.values.update(subject.plan(manifest())["environment"])
        self.values[subject.PREFIX + "IMAGE"] = "sha256:" + "1" * 64

    def model(
        self, routed: bool, values: dict[str, str] | None = None
    ) -> dict[str, Any]:
        path = self.directory / "explicit.env"
        path.write_text(
            "".join(
                name + "=" + value + "\n"
                for name, value in sorted((values or self.values).items())
            )
        )
        command = [
            "docker",
            "compose",
            "--env-file",
            str(path),
            "--file",
            str(ROOT / "compose.yml"),
            "--file",
            str(ROOT / "compose.owner-image.yml"),
        ]
        if routed:
            command += ["--file", str(ROOT / "compose.routed.yml")]
        command += ["--profile", "*", "config", "--format", "json"]
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("AELEV_", "COMPOSE_", "MINIO_", "MC_"))
        }
        result = subprocess.run(
            command, env=env, capture_output=True, text=True, timeout=30, check=False
        )
        self.assertEqual(
            result.returncode, 0, "Compose CONFIG-only normalization failed"
        )
        return json.loads(result.stdout)

    def test_real_compose_merge_changes_only_selected_routing_fields(self) -> None:
        base, routed = self.model(False), self.model(True)
        self.assertEqual(set(base["services"]), set(routed["services"]))
        for name, old in base["services"].items():
            new = copy.deepcopy(routed["services"][name])
            if name in subject.ROLES:
                self.assertEqual(new["image"], old["image"])
                self.assertEqual(new["networks"], old["networks"])
                self.assertEqual(new["volumes"], old["volumes"])
                self.assertEqual(new["command"], old["command"])
                self.assertNotIn("network_mode", new)
                for key in ("hostname", "extra_hosts", "ports", "healthcheck"):
                    if key in old:
                        new[key] = old[key]
                    else:
                        new.pop(key, None)
                for key in ("MINIO_ADDRESS", "MC_CONFIG_DIR", "MC_HOST_AELEVHEALTH"):
                    new["environment"].pop(key)
            self.assertEqual(new, old, name)

    def test_every_instance_has_native_published_peer_and_quorum_health_alignment(
        self,
    ) -> None:
        peers = subject.topology(manifest())
        model = self.model(True)
        for name, peer in peers.items():
            row = model["services"][name]
            self.assertEqual(row["hostname"], peer.dns)
            self.assertEqual(row["environment"]["MINIO_ADDRESS"], ":" + str(peer.port))
            self.assertEqual(len(row["ports"]), 1)
            self.assertEqual(row["ports"][0]["target"], peer.port)
            self.assertEqual(row["ports"][0]["published"], str(peer.port))
            hosts = row["extra_hosts"]
            if isinstance(hosts, list):
                hosts = {
                    value.partition("=")[0]: [value.partition("=")[2]]
                    for value in hosts
                }
            hosts = {
                key: [value] if isinstance(value, str) else value
                for key, value in hosts.items()
            }
            self.assertNotIn(peer.dns, hosts)
            for other in peers.values():
                if other.id != name:
                    self.assertEqual(hosts[other.dns], [other.address])
            health = row["healthcheck"]
            self.assertEqual(health["timeout"], "5s")
            self.assertEqual(health["interval"], "10s")
            self.assertEqual(health["retries"], 30)
            self.assertEqual(health["start_period"], "20s")
            self.assertEqual(row["environment"]["MC_HOST_AELEVHEALTH"], peer.origin)
            self.assertEqual(
                row["environment"]["MC_CONFIG_DIR"], "/tmp/minio-healthcheck"
            )
            self.assertIn("exec /usr/bin/mc ready AELEVHEALTH", health["test"][1])
            self.assertNotIn("--insecure", health["test"][1])
            self.assertNotIn("--cluster-read", health["test"][1])
            site = name.split("-")[2]
            self.assertEqual(
                row["command"][5:],
                [
                    other.origin + "/data"
                    for other in peers.values()
                    if other.id.startswith("minio-site-" + site + "-")
                ],
            )

    def test_compose_refuses_missing_new_explicit_peer_input(self) -> None:
        values = dict(self.values)
        values.pop(subject.PREFIX + "SITE_B_NODE_4_DNS")
        with self.assertRaisesRegex(AssertionError, "CONFIG-only normalization failed"):
            self.model(True, values)

    def test_compose_handles_ipv6_nodes_and_equal_ports_on_separate_hosts(self) -> None:
        value = manifest()
        for index, row in enumerate(value["nodes"]):
            row.update(address=f"2001:db8::{index + 10}", port=9000)
        rendered = self.model(
            True, {**self.values, **subject.plan(value)["environment"]}
        )
        for role in subject.ROLES:
            self.assertEqual(rendered["services"][role]["ports"][0]["target"], 9000)

    def test_independently_prepared_envs_keep_separate_selected_role_networks(
        self,
    ) -> None:
        allocated = environments(manifest())
        networks: set[str] = set()
        for role, generated in allocated.items():
            row = self.model(True, {**self.values, **generated})
            selected = row["services"][role]
            self.assertEqual(set(selected["networks"]), {"minio", "minio-ingress"})
            current = {row["networks"][name]["name"] for name in selected["networks"]}
            self.assertFalse(networks & current)
            networks.update(current)
        self.assertEqual(len(networks), 16)


if __name__ == "__main__":
    unittest.main()
