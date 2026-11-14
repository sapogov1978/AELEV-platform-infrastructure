#!/usr/bin/env python3
"""Validate an explicit full deployment inventory without starting workloads."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit

IDENTIFIER = re.compile(r"[a-z][a-z0-9_.-]{0,95}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
IMAGE = re.compile(
    r"(?:sha256:[0-9a-f]{64}|[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64})\Z"
)
REFERENCE = re.compile(r"(?:config|credential|state):[A-Za-z0-9_.:/-]{1,192}\Z")
SCHEMES = {
    "https": "https",
    "http": "http",
    "tls": "tls",
    "kafka": "kafka+ssl",
    "postgresql": "postgresql",
}
LIMIT = 4 * 1024 * 1024


class PlacementError(ValueError):
    def __init__(self, code: str, path: str) -> None:
        super().__init__(code)
        self.code, self.path = code, path


def require(condition: bool, code: str, path: str) -> None:
    if not condition:
        raise PlacementError(code, path)


def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        require(key not in result, "PLACEMENT_DUPLICATE_JSON_KEY", "input")
        result[key] = value
    return result


def read(path: Path) -> dict[str, Any]:
    require(
        path.is_file() and path.stat().st_size <= LIMIT,
        "PLACEMENT_INPUT_TOO_LARGE",
        "input",
    )
    with path.open("rb") as source:
        data = source.read(LIMIT + 1)
    require(len(data) <= LIMIT, "PLACEMENT_INPUT_TOO_LARGE", "input")
    result = json.loads(data, object_pairs_hook=pairs)
    require(isinstance(result, dict), "PLACEMENT_OBJECT_REQUIRED", "input")
    return result


def object_fields(
    value: Any, required: set[str], optional: set[str], path: str
) -> dict[str, Any]:
    require(isinstance(value, dict), "PLACEMENT_OBJECT_REQUIRED", path)
    require(
        required <= value.keys() and value.keys() <= required | optional,
        "PLACEMENT_FIELDS_INVALID",
        path,
    )
    return value


def identifier(value: Any, path: str) -> str:
    require(
        isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None,
        "PLACEMENT_IDENTIFIER_INVALID",
        path,
    )
    return value


def reference(value: Any, prefix: str, path: str) -> str:
    require(
        isinstance(value, str)
        and REFERENCE.fullmatch(value) is not None
        and value.startswith(prefix + ":"),
        "PLACEMENT_REFERENCE_INVALID",
        path,
    )
    return value


def digest(value: Any, path: str) -> str:
    require(
        isinstance(value, str) and DIGEST.fullmatch(value) is not None,
        "PLACEMENT_DIGEST_REQUIRED",
        path,
    )
    return value


def regular(root: Path, relative: str, path: str) -> Path:
    require(
        isinstance(relative, str)
        and bool(relative)
        and not Path(relative).is_absolute()
        and ".." not in Path(relative).parts,
        "PLACEMENT_SOURCE_PATH_INVALID",
        path,
    )
    result = root / relative
    require(
        result.resolve().is_relative_to(root.resolve())
        and result.is_file()
        and not any(
            parent.is_symlink()
            for parent in [result, *result.parents]
            if parent.is_relative_to(root)
        ),
        "PLACEMENT_SOURCE_PATH_INVALID",
        path,
    )
    return result


def load_catalogs(paths: list[Path]) -> tuple[dict[str, Any], dict[str, str]]:
    roles: dict[str, Any] = {}
    hashes: dict[str, str] = {}
    for path in paths:
        catalog = read(path)
        object_fields(
            catalog, {"schema_version", "product", "sources", "roles"}, set(), "catalog"
        )
        require(
            type(catalog["schema_version"]) is int and catalog["schema_version"] == 1,
            "PLACEMENT_CATALOG_VERSION_INVALID",
            "catalog",
        )
        product = identifier(catalog["product"], "catalog.product")
        require(product not in hashes, "PLACEMENT_CATALOG_DUPLICATE", "catalog.product")
        require(
            isinstance(catalog["sources"], dict) and bool(catalog["sources"]),
            "PLACEMENT_CATALOG_SOURCES_REQUIRED",
            "catalog.sources",
        )
        for source, expected in catalog["sources"].items():
            file = regular(path.parent, source, "catalog.sources")
            require(
                isinstance(expected, str)
                and hashlib.sha256(file.read_bytes()).hexdigest() == expected,
                "PLACEMENT_CATALOG_SOURCE_DRIFT",
                "catalog.sources",
            )
        require(
            isinstance(catalog["roles"], list) and bool(catalog["roles"]),
            "PLACEMENT_CATALOG_ROLES_REQUIRED",
            "catalog.roles",
        )
        for role in catalog["roles"]:
            object_fields(
                role,
                {
                    "id",
                    "owner",
                    "kind",
                    "recipes",
                    "configuration_variables",
                    "compose_profiles",
                    "opaque",
                    "local_state",
                    "credentials_required",
                },
                set(),
                "catalog.role",
            )
            name = identifier(role["id"], "catalog.role.id")
            require(
                role["kind"] in {"runtime", "node", "operation"},
                "PLACEMENT_ROLE_KIND_INVALID",
                "catalog.role.kind",
            )
            require(
                type(role["opaque"]) is bool,
                "PLACEMENT_ROLE_OPACITY_INVALID",
                "catalog.role.opaque",
            )
            require(
                type(role["local_state"]) is bool
                and type(role["credentials_required"]) is bool,
                "PLACEMENT_ROLE_BINDINGS_INVALID",
                "catalog.role",
            )
            require(
                isinstance(role["owner"], str)
                and bool(role["recipes"])
                and all(
                    recipe in catalog["sources"]
                    and recipe.startswith(role["owner"] + "/")
                    for recipe in role["recipes"]
                ),
                "PLACEMENT_ROLE_OWNER_INVALID",
                "catalog.role.recipes",
            )
            key = product + "/" + name
            require(key not in roles, "PLACEMENT_ROLE_DUPLICATE", "catalog.role.id")
            roles[key] = {**role, "root": path.parent}
        hashes[product] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return roles, hashes


def loopback(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def endpoint(value: Any, path: str) -> tuple[dict[str, Any], str, int]:
    row = object_fields(value, {"name", "protocol", "url"}, set(), path)
    identifier(row["name"], path + ".name")
    require(
        row["protocol"] in SCHEMES
        and isinstance(row["url"], str)
        and not any(
            ord(character) <= 32 or ord(character) == 127 for character in row["url"]
        ),
        "PLACEMENT_ENDPOINT_PROTOCOL_INVALID",
        path,
    )
    try:
        parsed = urlsplit(row["url"])
        port = parsed.port
    except ValueError as error:
        raise PlacementError("PLACEMENT_ENDPOINT_INVALID", path) from error
    require(
        parsed.scheme == SCHEMES[row["protocol"]]
        and bool(parsed.hostname)
        and port is not None
        and 1 <= port <= 65535
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and parsed.path in {"", "/"},
        "PLACEMENT_ENDPOINT_INVALID",
        path,
    )
    return row, str(parsed.hostname).lower(), port


def route(node: str, target: str, host: str, mode: str, path: str) -> None:
    if node != target:
        require(
            not loopback(host)
            and host != "host.docker.internal"
            and not host.endswith(".docker.internal"),
            "PLACEMENT_INTER_NODE_ADDRESS_INVALID",
            path,
        )
    if mode == "distributed":
        require(
            host not in {"0.0.0.0", "::"}, "PLACEMENT_ADVERTISED_ADDRESS_INVALID", path
        )


def plan(inventory: dict[str, Any], catalog_paths: list[Path]) -> dict[str, Any]:
    object_fields(
        inventory,
        {"schema_version", "mode", "catalogs", "nodes", "instances", "connections"},
        set(),
        "inventory",
    )
    require(
        type(inventory["schema_version"]) is int and inventory["schema_version"] == 1,
        "PLACEMENT_VERSION_INVALID",
        "schema_version",
    )
    mode = inventory["mode"]
    require(mode in {"local", "distributed"}, "PLACEMENT_MODE_INVALID", "mode")
    roles, hashes = load_catalogs(catalog_paths)
    require(
        any(role["kind"] == "node" for role in roles.values()),
        "PLACEMENT_NODE_PREREQUISITE_REQUIRED",
        "catalogs",
    )
    require(
        inventory["catalogs"] == hashes, "PLACEMENT_CATALOG_PIN_MISMATCH", "catalogs"
    )
    require(
        isinstance(inventory["nodes"], list) and bool(inventory["nodes"]),
        "PLACEMENT_NODES_REQUIRED",
        "nodes",
    )
    nodes: dict[str, Any] = {}
    for value in inventory["nodes"]:
        row = object_fields(
            value,
            {"id", "address", "platform", "executor", "identity"},
            {"aliases"},
            "node",
        )
        name = identifier(row["id"], "node.id")
        require(name not in nodes, "PLACEMENT_NODE_DUPLICATE", "node.id")
        require(
            row["platform"] in {"linux/amd64", "linux/arm64"},
            "PLACEMENT_PLATFORM_INVALID",
            "node.platform",
        )
        require(
            isinstance(row["address"], str)
            and bool(row["address"])
            and not any(character in row["address"] for character in "/@ \t\n")
            and not row["address"].endswith(".invalid"),
            "PLACEMENT_NODE_ADDRESS_INVALID",
            "node.address",
        )
        aliases = row.get("aliases", [])
        require(
            isinstance(aliases, list)
            and all(isinstance(alias, str) and bool(alias) for alias in aliases),
            "PLACEMENT_NODE_ALIASES_INVALID",
            "node.aliases",
        )
        executor = object_fields(
            row["executor"], {"kind", "reference"}, set(), "node.executor"
        )
        require(
            executor["kind"] in {"local", "ssh"}
            and isinstance(executor["reference"], str)
            and bool(executor["reference"])
            and "\n" not in executor["reference"],
            "PLACEMENT_EXECUTOR_INVALID",
            "node.executor",
        )
        identity = object_fields(
            row["identity"],
            {"server_endpoint", "configuration_ref", "configuration_sha256"},
            set(),
            "node.identity",
        )
        reference(
            identity["configuration_ref"], "config", "node.identity.configuration_ref"
        )
        digest(identity["configuration_sha256"], "node.identity.configuration_sha256")
        nodes[name] = row
        require(
            row["address"] not in {"0.0.0.0", "::"},
            "PLACEMENT_ADVERTISED_ADDRESS_INVALID",
            "node.address",
        )
        require(
            mode != "local" or executor["kind"] == "local",
            "PLACEMENT_LOCAL_EXECUTOR_REQUIRED",
            "node.executor",
        )
    require(
        mode != "local" or len(nodes) == 1, "PLACEMENT_LOCAL_REQUIRES_ONE_NODE", "nodes"
    )
    require(
        mode != "distributed" or len(nodes) >= 2,
        "PLACEMENT_DISTRIBUTED_REQUIRES_NODES",
        "nodes",
    )
    require(
        isinstance(inventory["instances"], list),
        "PLACEMENT_INSTANCES_REQUIRED",
        "instances",
    )
    instances: dict[str, Any] = {}
    endpoints: dict[str, Any] = {}
    state_owners: dict[str, str] = {}
    key_owners: dict[str, str] = {}
    credential_owners: dict[str, str] = {}
    ports: dict[tuple[str, int], str] = {}
    by_role: dict[str, list[str]] = {}
    for value in inventory["instances"]:
        row = object_fields(
            value,
            {
                "id",
                "role",
                "node",
                "configuration_ref",
                "configuration_sha256",
                "credentials",
                "state",
                "endpoints",
            },
            {"image", "build", "desired"},
            "instance",
        )
        name = identifier(row["id"], "instance.id")
        require(name not in instances, "PLACEMENT_INSTANCE_DUPLICATE", "instance.id")
        require(
            row["role"] in roles and roles[row["role"]]["kind"] == "runtime",
            "PLACEMENT_RUNTIME_ROLE_REQUIRED",
            "instance.role",
        )
        require(row["node"] in nodes, "PLACEMENT_NODE_UNKNOWN", "instance.node")
        require(
            row.get("desired", "running") in {"running", "standby"},
            "PLACEMENT_DESIRED_INVALID",
            "instance.desired",
        )
        reference(row["configuration_ref"], "config", "instance.configuration_ref")
        digest(row["configuration_sha256"], "instance.configuration_sha256")
        require(
            ("image" in row) != ("build" in row),
            "PLACEMENT_ARTIFACT_REQUIRED",
            "instance",
        )
        if "image" in row:
            require(
                isinstance(row["image"], str)
                and IMAGE.fullmatch(row["image"]) is not None,
                "PLACEMENT_IMMUTABLE_IMAGE_REQUIRED",
                "instance.image",
            )
            require(
                not roles[row["role"]]["opaque"] or "@sha256:" in row["image"],
                "PLACEMENT_OPAQUE_PUBLISHED_IMAGE_REQUIRED",
                "instance.image",
            )
        else:
            require(
                not roles[row["role"]]["opaque"],
                "PLACEMENT_OPAQUE_SOURCE_BUILD_FORBIDDEN",
                "instance.build",
            )
            build = object_fields(
                row["build"],
                {"recipe", "target", "source_sha256"},
                set(),
                "instance.build",
            )
            role = roles[row["role"]]
            require(
                isinstance(build["recipe"], str)
                and build["recipe"].startswith(role["owner"] + "/"),
                "PLACEMENT_BUILD_OWNER_INVALID",
                "instance.build.recipe",
            )
            source = regular(role["root"], build["recipe"], "instance.build.recipe")
            require(
                digest(build["source_sha256"], "instance.build.source_sha256")
                == "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest(),
                "PLACEMENT_BUILD_SOURCE_DRIFT",
                "instance.build",
            )
            identifier(build["target"], "instance.build.target")
        require(
            isinstance(row["credentials"], list)
            and isinstance(row["state"], list)
            and isinstance(row["endpoints"], list),
            "PLACEMENT_BINDINGS_REQUIRED",
            "instance",
        )
        require(
            not roles[row["role"]]["local_state"] or bool(row["state"]),
            "PLACEMENT_LOCAL_STATE_REQUIRED",
            "instance.state",
        )
        require(
            not roles[row["role"]]["credentials_required"] or bool(row["credentials"]),
            "PLACEMENT_CREDENTIALS_REQUIRED",
            "instance.credentials",
        )
        for credential in row["credentials"]:
            item = object_fields(
                credential, {"kind", "reference"}, set(), "instance.credential"
            )
            require(
                item["kind"]
                in {"identity", "trust", "api-key", "database", "oidc-client"},
                "PLACEMENT_CREDENTIAL_KIND_INVALID",
                "instance.credential.kind",
            )
            ref = reference(
                item["reference"], "credential", "instance.credential.reference"
            )
            if item["kind"] == "identity":
                require(
                    ref not in key_owners,
                    "PLACEMENT_PRIVATE_IDENTITY_REUSED",
                    "instance.credentials",
                )
                key_owners[ref] = name
            elif item["kind"] != "trust":
                owner = row["role"].split("/", 1)[0] + "/" + roles[row["role"]]["owner"]
                require(
                    ref not in credential_owners or credential_owners[ref] == owner,
                    "PLACEMENT_FOREIGN_CREDENTIAL_REUSED",
                    "instance.credentials",
                )
                credential_owners[ref] = owner
        for state in row["state"]:
            ref = reference(state, "state", "instance.state")
            require(
                ref not in state_owners,
                "PLACEMENT_LOCAL_STATE_REUSED",
                "instance.state",
            )
            state_owners[ref] = name
        for value_endpoint in row["endpoints"]:
            item, host, port = endpoint(value_endpoint, "instance.endpoint")
            key = name + ":" + item["name"]
            require(
                key not in endpoints,
                "PLACEMENT_ENDPOINT_DUPLICATE",
                "instance.endpoints",
            )
            allowed = {
                nodes[row["node"]]["address"].lower(),
                *(alias.lower() for alias in nodes[row["node"]].get("aliases", [])),
            }
            require(
                host in allowed,
                "PLACEMENT_ENDPOINT_NODE_MISMATCH",
                "instance.endpoint.url",
            )
            binding = row["node"], port
            require(
                binding not in ports or ports[binding] == name,
                "PLACEMENT_PORT_COLLISION",
                "instance.endpoints",
            )
            ports[binding] = name
            endpoints[key] = {
                **item,
                "host": host,
                "node": row["node"],
                "instance": name,
            }
        instances[name] = row
        by_role.setdefault(row["role"], []).append(name)
    required_roles = {name for name, role in roles.items() if role["kind"] == "runtime"}
    require(
        required_roles <= by_role.keys(),
        "PLACEMENT_FULL_PROFILE_INCOMPLETE",
        "instances",
    )
    require(
        all(
            any(
                instances[name].get("desired", "running") == "running" for name in names
            )
            for names in by_role.values()
        ),
        "PLACEMENT_ACTIVE_ROLE_REQUIRED",
        "instances",
    )
    require(
        set(nodes) == {row["node"] for row in instances.values()},
        "PLACEMENT_UNUSED_NODE",
        "nodes",
    )
    require(
        isinstance(inventory["connections"], list),
        "PLACEMENT_CONNECTIONS_REQUIRED",
        "connections",
    )
    for connection in inventory["connections"]:
        row = object_fields(
            connection,
            {"from", "to", "purpose", "trust_sha256", "peer_identity"},
            set(),
            "connection",
        )
        require(
            row["from"] in instances and row["to"] in endpoints,
            "PLACEMENT_CONNECTION_UNKNOWN",
            "connection",
        )
        target = endpoints[row["to"]]
        require(
            row["purpose"] in {"domain", "technical", "operator"},
            "PLACEMENT_CONNECTION_PURPOSE_INVALID",
            "connection.purpose",
        )
        require(
            row["purpose"] != "domain" or target["protocol"] == "kafka",
            "PLACEMENT_DOMAIN_REQUIRES_KAFKA",
            "connection",
        )
        require(
            target["protocol"] != "http"
            or row["purpose"] == "operator"
            and mode == "local"
            and loopback(target["host"]),
            "PLACEMENT_HTTP_NOT_ADMITTED",
            "connection",
        )
        digest(row["trust_sha256"], "connection.trust_sha256")
        require(
            isinstance(row["peer_identity"], str)
            and bool(row["peer_identity"])
            and not any(character in row["peer_identity"] for character in "\r\n"),
            "PLACEMENT_PEER_IDENTITY_REQUIRED",
            "connection.peer_identity",
        )
        route(
            instances[row["from"]]["node"],
            target["node"],
            target["host"],
            mode,
            "connection.to",
        )
    for node in nodes.values():
        require(
            node["identity"]["server_endpoint"] in endpoints,
            "PLACEMENT_IDENTITY_SERVER_REQUIRED",
            "node.identity.server_endpoint",
        )
        target = endpoints[node["identity"]["server_endpoint"]]
        authority = instances[target["instance"]]["role"]
        authority_owner = authority.split("/", 1)[0] + "/" + roles[authority]["owner"]
        node_owners = {
            name.split("/", 1)[0] + "/" + role["owner"]
            for name, role in roles.items()
            if role["kind"] == "node"
        }
        require(
            authority_owner in node_owners,
            "PLACEMENT_IDENTITY_SERVER_OWNER_INVALID",
            "node.identity.server_endpoint",
        )
        require(
            target["protocol"] == "tls",
            "PLACEMENT_IDENTITY_SERVER_PROTOCOL_INVALID",
            "node.identity.server_endpoint",
        )
        route(
            node["id"],
            target["node"],
            target["host"],
            mode,
            "node.identity.server_endpoint",
        )
    prerequisites = [
        {"node": node, "role": name}
        for node in nodes
        for name, role in roles.items()
        if role["kind"] == "node"
    ]
    return {
        "schema_version": 1,
        "status": "VALIDATED_INVENTORY",
        "mode": mode,
        "ready_to_start": False,
        "declared_nodes": len(nodes),
        "runtime_instances": len(instances),
        "node_prerequisites": prerequisites,
        "owner_operations": sorted(
            name for name, role in roles.items() if role["kind"] == "operation"
        ),
        "source_builds": sorted(
            name for name, row in instances.items() if "build" in row
        ),
        "replica_admission_required": {
            name: ids for name, ids in by_role.items() if len(ids) > 1
        },
        "required_checks": [
            "exact release and image admission",
            "owner-specific configuration and endpoint preflight",
            "node-local identity preparation and renewal",
            "owner-controlled state and recovery handoff",
            "actual node reachability and physical placement evidence",
        ],
        "queue_resume_issued": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "plan"))
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--catalog", action="append", type=Path)
    args = parser.parse_args()
    try:
        result = plan(
            read(args.inventory),
            args.catalog
            or [Path(__file__).resolve().parents[1] / "deployment-catalog.json"],
        )
    except PlacementError as error:
        print(json.dumps({"status": "BLOCKED", "code": error.code, "path": error.path}))
        return 2
    except (OSError, ValueError, TypeError):
        print(json.dumps({"status": "BLOCKED", "code": "PLACEMENT_INPUT_INVALID"}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
