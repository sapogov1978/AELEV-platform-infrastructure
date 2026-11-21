"""Validate public MinIO routing inputs without Docker, keys or runtime effects."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any, cast

PREFIX = "AELEV_INFRASTRUCTURE_MINIO_"
ROLES = tuple(
    f"minio-site-{site}-{number}" for site in ("a", "b") for number in range(1, 5)
)
DNS = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
RESOURCE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,254}\Z")
HEX = re.compile(r"[a-f0-9]{64}\Z")


class RoutingError(ValueError):
    """Fixed diagnostic code; do not echo arbitrary input or process output."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise RoutingError("MINIO_ROUTED_" + code)


def object_value(value: Any) -> dict[str, Any]:
    require(isinstance(value, dict), "OBJECT_REQUIRED")
    return cast(dict[str, Any], value)


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def read(path: Path, limit: int = 1024**2) -> bytes:
    def signature(value: os.stat_result) -> tuple[int, ...]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_uid,
            value.st_gid,
            value.st_mode,
            value.st_nlink,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    require(path.is_absolute() and ".." not in path.parts, "PATH_INVALID")
    require(not any(p.is_symlink() for p in (path, *path.parents)), "LINK_REFUSED")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        require(
            stat.S_ISREG(before.st_mode)
            and before.st_nlink == 1
            and 0 < before.st_size <= limit,
            "REGULAR_BOUNDED_FILE_REQUIRED",
        )
        body = os.read(fd, limit + 1)
        after = os.fstat(fd)
        require(
            signature(before) == signature(after)
            and len(body) == before.st_size
            and signature(path.stat()) == signature(after),
            "INPUT_CHANGED",
        )
    finally:
        os.close(fd)
    return body


def document(path: Path) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            require(key not in result, "DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    return object_value(json.loads(read(path), object_pairs_hook=unique))


@dataclass(frozen=True)
class Peer:
    id: str
    dns: str
    address: str
    port: int

    @property
    def prefix(self) -> str:
        site, number = self.id.removeprefix("minio-site-").split("-")
        return PREFIX + f"SITE_{site.upper()}_NODE_{number}"

    @property
    def origin(self) -> str:
        return f"https://{self.dns}:{self.port}"


def topology(value: dict[str, Any]) -> dict[str, Peer]:
    require(
        set(value)
        in (
            {"schema_version", "nodes"},
            {"schema_version", "nodes", "public_certificates"},
        ),
        "SCHEMA_INVALID",
    )
    require(
        type(value["schema_version"]) is int and value["schema_version"] == 1,
        "SCHEMA_INVALID",
    )
    rows = value["nodes"]
    require(
        isinstance(rows, list) and len(rows) == len(ROLES), "NODE_COVERAGE_REQUIRED"
    )
    peers: dict[str, Peer] = {}
    names: set[str] = set()
    listeners: set[tuple[str, int]] = set()
    for raw in rows:
        row = object_value(raw)
        require(set(row) == {"id", "dns", "address", "port"}, "NODE_SCHEMA_INVALID")
        name, dns, address, port = row["id"], row["dns"], row["address"], row["port"]
        require(
            isinstance(name, str) and name in ROLES and name not in peers,
            "NODE_COVERAGE_REQUIRED",
        )
        require(
            isinstance(dns, str)
            and DNS.fullmatch(dns) is not None
            and re.fullmatch(r"[0-9.]+", dns) is None
            and dns not in names
            and not dns.endswith((".localhost", ".invalid"))
            and dns != "host.docker.internal",
            "DNS_INVALID_OR_REUSED",
        )
        require(isinstance(address, str), "NODE_ADDRESS_INVALID")
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            raise RoutingError("MINIO_ROUTED_NODE_ADDRESS_INVALID") from None
        require(
            not any(
                (ip.is_loopback, ip.is_unspecified, ip.is_multicast, ip.is_link_local)
            )
            and "%" not in address,
            "NODE_ADDRESS_INVALID",
        )
        require(type(port) is int and 1 <= port <= 65535, "PORT_INVALID")
        pair = (str(ip), port)
        require(pair not in listeners, "LISTENER_REUSED")
        peers[name] = Peer(name, dns, str(ip), port)
        names.add(dns)
        listeners.add(pair)
    require(set(peers) == set(ROLES), "NODE_COVERAGE_REQUIRED")
    return {name: peers[name] for name in ROLES}


def environment(peers: dict[str, Peer]) -> dict[str, str]:
    result = {}
    for peer in peers.values():
        result.update(
            {
                peer.prefix + "_DNS": peer.dns,
                peer.prefix + "_ADDRESS": peer.address,
                peer.prefix + "_PORT": str(peer.port),
                peer.prefix + "_ENDPOINT": peer.origin + "/data",
            }
        )
    return result


def validate_environments(peers: dict[str, Peer], supplied: dict[str, Any]) -> None:
    """Check complete owner env mappings before pinned preparation/startup.

    Actual daemon/node identity and native network membership remain the caller's
    placement gate. Identical resource names on distinct daemons are allowed.
    """
    require(set(supplied) == set(ROLES), "ENVIRONMENT_COVERAGE_REQUIRED")
    expected = environment(peers)
    resources: dict[tuple[str, str, str], str] = {}
    for role, peer in peers.items():
        values = object_value(supplied[role])
        require(
            all(values.get(name) == value for name, value in expected.items()),
            "ENVIRONMENT_ROUTE_DRIFT",
        )
        for kind, key in (
            ("project", PREFIX + "COMPOSE_PROJECT"),
            ("network", PREFIX + "NETWORK"),
            ("network", PREFIX + "INGRESS_NETWORK"),
            ("volume", peer.prefix + "_VOLUME"),
        ):
            name = values.get(key)
            require(
                isinstance(name, str) and RESOURCE.fullmatch(name) is not None,
                "RESOURCE_BINDING_REQUIRED",
            )
            name = cast(str, name)
            if kind == "project":
                require(
                    re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", name) is not None,
                    "RESOURCE_BINDING_REQUIRED",
                )
            scope = (peer.address, kind, name)
            require(scope not in resources, "RESOURCE_REUSED_ON_NODE")
            resources[scope] = role


def public_file(value: Any) -> tuple[Path, bytes]:
    row = object_value(value)
    require(
        set(row) == {"path", "sha256"}
        and isinstance(row["path"], str)
        and isinstance(row["sha256"], str)
        and HEX.fullmatch(row["sha256"]) is not None,
        "PUBLIC_CERTIFICATE_REF_INVALID",
    )
    path = Path(row["path"])
    body = read(path)
    require(
        hashlib.sha256(body).hexdigest() == row["sha256"],
        "PUBLIC_CERTIFICATE_DIGEST_MISMATCH",
    )
    require(
        b"-----BEGIN CERTIFICATE-----" in body and b"PRIVATE KEY" not in body,
        "PUBLIC_CERTIFICATE_ONLY",
    )
    return path, body


def certificates(peers: dict[str, Peer], values: dict[str, Any]) -> None:
    require(set(values) == set(ROLES), "PUBLIC_CERTIFICATE_COVERAGE_REQUIRED")
    for role, peer in peers.items():
        row = object_value(values[role])
        require(set(row) == {"certificate", "ca"}, "PUBLIC_CERTIFICATE_REF_INVALID")
        leaf, leaf_bytes = public_file(row["certificate"])
        ca, ca_bytes = public_file(row["ca"])
        for option, identity in (
            ("-verify_hostname", peer.dns),
            ("-verify_ip", peer.address),
        ):
            try:
                result = subprocess.run(
                    [
                        "openssl",
                        "verify",
                        "-trusted",
                        str(ca),
                        "-purpose",
                        "sslserver",
                        option,
                        identity,
                        str(leaf),
                    ],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise RoutingError("MINIO_ROUTED_PUBLIC_TLS_CHECK_FAILED") from None
            require(result.returncode == 0, "PUBLIC_TLS_CHECK_FAILED")
        require(read(leaf) == leaf_bytes and read(ca) == ca_bytes, "INPUT_CHANGED")


def plan(
    value: dict[str, Any], envs: dict[str, Any] | None = None
) -> dict[str, object]:
    peers = topology(value)
    if envs is not None:
        validate_environments(peers, envs)
    refs = value.get("public_certificates")
    if refs is not None:
        certificates(peers, object_value(refs))
    return {
        "schema_version": 1,
        "status": "MINIO_ROUTED_INPUTS_VALIDATED",
        "topology_sha256": hashlib.sha256(canonical(value)).hexdigest(),
        "environment": environment(peers),
        "public_tls_inputs_validated": refs is not None,
        "owner_environment_bindings_validated": envs is not None,
        "native_self_dns_verified": False,
        "native_network_isolation_verified": False,
        "stable_site_endpoints_verified": False,
        "runtime_readiness_verified": False,
        "image_admission_verified": False,
        "credentials_or_private_keys_read": False,
        "runtime_effects": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--environments", type=Path)
    args = parser.parse_args()
    try:
        value = document(args.input)
        envs = document(args.environments) if args.environments else None
        result = plan(value, envs)
    except (ValueError, OSError, KeyError, TypeError) as error:
        code = str(error)
        if re.fullmatch(r"MINIO_ROUTED_[A-Z_]+", code) is None:
            code = "MINIO_ROUTED_INPUT_INVALID"
        print(json.dumps({"status": "BLOCKED", "code": code}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
