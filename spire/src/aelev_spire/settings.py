"""Render a fixed SPIRE capability; resource identities come from its callers."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization

from .files import atomic, read, reserve

OWNER = "infrastructure-spire"
SERVER = "/opt/spire/bin/spire-server"
AGENT = "/opt/spire/bin/spire-agent"
SERVER_SOCKET = "/run/spire/admin/api.sock"
AGENT_SOCKET = "/run/spire/workload/api.sock"


def policy_mountpoints(plan: dict[str, Any]) -> tuple[str, ...]:
    value = plan.get("policy_mountpoints", ["identity.json"])
    if not isinstance(value, list):
        raise ValueError("SPIRE_SVID_POLICY_MOUNTPOINT_INVALID")
    names = cast(list[object], value)
    if (
        not 1 <= len(names) <= 4
        or "identity.json" not in names
        or any(
            not isinstance(name, str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,62}\.json", name)
            or name in {"owner.json", "current.json"}
            for name in names
        )
    ):
        raise ValueError("SPIRE_SVID_POLICY_MOUNTPOINT_INVALID")
    selected = cast(list[str], names)
    if len(set(selected)) != len(selected):
        raise ValueError("SPIRE_SVID_POLICY_MOUNTPOINT_INVALID")
    return tuple(selected)


def domain(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 253
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value)
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
            for part in value.split(".")
        )
    ):
        raise ValueError("SPIRE_TRUST_DOMAIN_INVALID")
    return value


def identity(value: object, trust: str) -> str:
    if not isinstance(value, str):
        raise ValueError("SPIRE_IDENTITY_INVALID")
    url = urlsplit(value)
    if (
        url.scheme != "spiffe"
        or url.netloc != trust
        or not re.fullmatch(r"/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+", url.path)
        or any(part in {".", ".."} for part in url.path.split("/"))
        or url.query
        or url.fragment
        or len(value) > 512
    ):
        raise ValueError("SPIRE_IDENTITY_INVALID")
    return value


def environment(plan: dict[str, Any]) -> tuple[str, str, int]:
    mode = plan.get("environment")
    if mode not in {"local", "test", "production"}:
        raise ValueError("SPIRE_ENVIRONMENT_REQUIRED")
    trust = domain(plan.get("trust_domain"))
    ttl = plan.get("svid_ttl_seconds", 300)
    if type(ttl) is not int or not (20 if mode == "test" else 300) <= ttl <= 3600:
        raise ValueError("SPIRE_SVID_TTL_INVALID")
    return mode, trust, ttl


def verify_authority(secrets: Path, *, production: bool) -> None:
    paths = [secrets / name for name in ("issuer.crt", "issuer.key", "bundle.crt")]
    if any(
        path.is_symlink() or not path.is_file() or path.stat().st_size > 65536
        for path in paths
    ):
        raise ValueError("SPIRE_EXTERNAL_AUTHORITY_REQUIRED")
    chain = x509.load_pem_x509_certificates(paths[0].read_bytes())
    roots = x509.load_pem_x509_certificates(paths[2].read_bytes())
    key = serialization.load_pem_private_key(paths[1].read_bytes(), password=None)
    if not chain or not roots or paths[1].stat().st_mode & 0o077:
        raise ValueError("SPIRE_EXTERNAL_AUTHORITY_INVALID")
    now = datetime.now(UTC)
    if production and chain[0].issuer == chain[0].subject:
        raise ValueError("SPIRE_PRODUCTION_ROOT_KEY_FORBIDDEN")
    if key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    ) != chain[0].public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    ):
        raise ValueError("SPIRE_EXTERNAL_AUTHORITY_KEY_MISMATCH")
    for cert in (*chain, *roots):
        is_ca = cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        if not is_ca or not (
            cert.not_valid_before_utc
            <= now
            < cert.not_valid_after_utc - timedelta(days=2)
        ):
            raise ValueError("SPIRE_EXTERNAL_AUTHORITY_INVALID")
    for index, cert in enumerate(chain):
        parents = chain[index + 1 : index + 2] or [
            parent for parent in roots if parent.subject == cert.issuer
        ]
        if not parents:
            raise ValueError("SPIRE_EXTERNAL_AUTHORITY_CHAIN_INVALID")
        for parent in parents:
            try:
                cert.verify_directly_issued_by(parent)
                break
            except (InvalidSignature, ValueError, TypeError):
                continue
        else:
            raise ValueError("SPIRE_EXTERNAL_AUTHORITY_CHAIN_INVALID")


def node_ttl(plan: dict[str, Any]) -> int:
    mode, _, ttl = environment(plan)
    agent_ttl = plan.get("agent_ttl_seconds", ttl)
    if (
        type(agent_ttl) is not int
        or not (20 if mode == "test" else 300) <= agent_ttl <= 3600
    ):
        raise ValueError("SPIRE_AGENT_TTL_INVALID")
    return agent_ttl


def server(plan: dict[str, Any], root: Path, secrets: Path) -> Path:
    mode, trust, ttl = environment(plan)
    agent_ttl = node_ttl(plan)
    reserve(
        root,
        {"owner": OWNER, "role": "server", "environment": mode, "trust_domain": trust},
    )
    authority = ""
    if mode == "production" or (secrets / "issuer.crt").exists():
        verify_authority(secrets, production=mode == "production")
        authority = f"""UpstreamAuthority "disk" {{
  plugin_data {{
    cert_file_path = {json.dumps(str(secrets / "issuer.crt"))}
    key_file_path = {json.dumps(str(secrets / "issuer.key"))}
    bundle_file_path = {json.dumps(str(secrets / "bundle.crt"))}
  }}
}}"""
    path = Path("/run/spire/server.conf")
    database = str(root / "data/datastore.sqlite3") + (
        "?_journal_mode=WAL&_synchronous=FULL&_foreign_keys=on&_busy_timeout=5000"
    )
    text = f"""server {{
  bind_address = "0.0.0.0"
  bind_port = 8081
  socket_path = "{SERVER_SOCKET}"
  trust_domain = {json.dumps(trust)}
  data_dir = {json.dumps(str(root / "data"))}
  default_x509_svid_ttl = "{ttl}s"
  agent_ttl = "{agent_ttl}s"
  ca_ttl = "24h"
  log_level = "INFO"
  log_format = "json"
}}
plugins {{
  DataStore "sql" {{ plugin_data {{
    database_type = "sqlite3"
    connection_string = {json.dumps(database)}
    max_open_conns = 4
    max_idle_conns = 2
  }} }}
  NodeAttestor "join_token" {{ plugin_data {{}} }}
  KeyManager "disk" {{ plugin_data {{
    keys_path = {json.dumps(str(root / "data/keys.json"))}
  }} }}
  {authority}
}}
health_checks {{
  listener_enabled = true
  bind_address = "127.0.0.1"
  bind_port = "8082"
}}
"""
    atomic(path, text.encode())
    atomic(root / "approved-plan.json", plan)
    atomic(
        Path(SERVER_SOCKET).parent / "deployment.json",
        {"owner": OWNER, "environment": mode, "trust_domain": trust},
        0o644,
    )
    return path


def agent(plan: dict[str, Any], root: Path, bootstrap: Path) -> Path:
    mode, trust, _ = environment(plan)
    workload = identity(plan.get("spiffe_id"), trust)
    address = plan.get("server_address")
    port = plan.get("server_port", 8081)
    if (
        not isinstance(address, str)
        or not re.fullmatch(r"[a-zA-Z0-9.:-]{1,253}", address)
        or type(port) is not int
        or not 1 <= port <= 65535
    ):
        raise ValueError("SPIRE_SERVER_ADDRESS_INVALID")
    reserve(
        root,
        {
            "owner": OWNER,
            "role": "agent",
            "environment": mode,
            "trust_domain": trust,
            "spiffe_id": workload,
            "node_id": plan["node_id"],
        },
    )
    bundle = bootstrap / "bundle.crt"
    token = bootstrap / "join-token"
    if any(path.is_symlink() or not path.is_file() for path in (bundle, token)):
        raise ValueError("SPIRE_INITIAL_TRUST_REQUIRED")
    if not re.fullmatch(r"[a-f0-9-]{32,128}", token.read_text().strip()):
        raise ValueError("SPIRE_JOIN_TOKEN_INVALID")
    path = Path("/run/spire/agent.conf")
    text = f"""agent {{
  data_dir = {json.dumps(str(root / "data"))}
  server_address = {json.dumps(address)}
  server_port = {port}
  trust_domain = {json.dumps(trust)}
  trust_bundle_path = {json.dumps(str(bundle))}
  join_token_file = {json.dumps(str(token))}
  socket_path = "{AGENT_SOCKET}"
  insecure_bootstrap = false
  rebootstrap_mode = "never"
  disable_sds_api = true
  log_level = "INFO"
  log_format = "json"
}}
plugins {{
  KeyManager "disk" {{ plugin_data {{ directory = {json.dumps(str(root / "data"))} }} }}
  NodeAttestor "join_token" {{ plugin_data {{}} }}
  WorkloadAttestor "unix" {{ plugin_data {{
    discover_workload_path = true
    workload_size_limit = -1
  }} }}
}}
health_checks {{
  listener_enabled = true
  bind_address = "127.0.0.1"
  bind_port = "8082"
}}
"""
    atomic(path, text.encode())
    return path


def load_plan(path: Path) -> dict[str, Any]:
    plan = read(path)
    if plan.get("owner") != OWNER or plan.get("schema_version") != 1:
        raise ValueError("SPIRE_PLAN_OWNER_INVALID")
    environment(plan)
    return plan
