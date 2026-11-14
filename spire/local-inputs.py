#!/usr/bin/env python3
"""Prepare this owner's local observation bundle from explicit issued inputs."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
OWNER = "infrastructure-spire"
WINDOW = 30 * 86400
# These are this owner's creation profiles, not a catalogue of consumers.
LEAVES = {
    "metrics": (
        "observer",
        "metrics",
        "server",
        "serverAuth",
        "DNS:spire-observer,DNS:localhost,IP:127.0.0.1",
    ),
    "observer": ("observer", "observer", "write", "clientAuth", ""),
    "sender": (
        "sender",
        "sender",
        "server",
        "serverAuth",
        "DNS:spire-telemetry-sender,DNS:localhost,IP:127.0.0.1",
    ),
    "local-write": ("sender", "local-write", "write", "clientAuth", ""),
    "local-query": ("sender", "local-query", "query", "clientAuth", ""),
    "status-query": ("status", "query", "query", "clientAuth", ""),
}
ISSUED = {
    "central-write": ("sender", "central-write", "central"),
    "status-publisher": ("status", "publisher", "publisher"),
}
PROFILES = {
    "status.json": (
        "metrics",
        "observer",
        "sender",
        "local-write",
        "local-query",
        "central-write",
    ),
    "status-query.json": ("status-query",),
    "status-publisher.json": ("status-publisher",),
}
NETWORK_PROFILES = {
    "observer.json": ("metrics", "observer"),
    "sender.json": ("sender", "local-write", "local-query", "central-write"),
    "status-query.json": ("status-query",),
    "status-publisher.json": ("status-publisher",),
}


def leaves(config: dict[str, Any]) -> dict[str, tuple[str, str, str, str, str]]:
    """Extend this owner's local PKI only for explicitly declared endpoints."""
    result = dict(LEAVES)
    network = config["observation_plan"].get("network")
    source = config["server_plan"].get("public_source")
    if network is None:
        if source is not None:
            raise InputError("SPIRE_LOCAL_PUBLIC_SOURCE_NETWORK_REQUIRED")
        return result
    if not isinstance(network, dict):
        raise InputError("SPIRE_LOCAL_INPUT_PLAN_INVALID")
    network = cast(dict[str, Any], network)
    for name, endpoint in (
        ("metrics", "observer_url"),
        ("sender", "sender_health_url"),
    ):
        fields = result[name]
        result[name] = (*fields[:4], fields[4] + "," + endpoint_san(network[endpoint]))
    # Both listener ports use this sender leaf; declare both actual hostnames.
    fields = result["sender"]
    result["sender"] = (
        *fields[:4],
        fields[4] + "," + endpoint_san(network["sender_otlp_url"]),
    )
    if source is not None:
        source = object_value(source)
        if (
            source.get("identity") != ("cn:AELEV SPIRE public-source")
            or network.get("source_identity") != source["identity"]
        ):
            raise InputError("SPIRE_LOCAL_PUBLIC_SOURCE_IDENTITY_INVALID")
        result["public-source"] = (
            "public-source",
            "source",
            "server",
            "serverAuth",
            endpoint_san(network["source_url"]),
        )
    return result


def endpoint_san(value: Any) -> str:
    if not isinstance(value, str):
        raise InputError("SPIRE_LOCAL_INPUT_PLAN_INVALID")
    parsed = urlsplit(value)
    host = parsed.hostname
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,253}", host)
    ):
        raise InputError("SPIRE_LOCAL_INPUT_PLAN_INVALID")
    try:
        return "IP:" + str(ipaddress.ip_address(host))
    except ValueError:
        return "DNS:" + host


class InputError(RuntimeError):
    """Safe public code, without raw PKI output or delivered credentials."""


def regular(path: Path, *, private: bool = False) -> bytes:
    try:
        mode = path.lstat().st_mode
        if (
            not path.is_absolute()
            or not stat.S_ISREG(mode)
            or any(parent.is_symlink() for parent in path.parents)
            or (private and os.name != "nt" and mode & 0o077)
            or not 0 < path.stat().st_size <= 65536
        ):
            raise ValueError
        return path.read_bytes()
    except (OSError, ValueError):
        raise InputError("SPIRE_LOCAL_INPUT_FILE_INVALID") from None


def write(path: Path, raw: bytes, *, replace: bool = False) -> None:
    pending = path.with_name(".pending-" + uuid4().hex)
    try:
        with os.fdopen(
            os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
        ) as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        if replace:
            os.replace(pending, path)
        else:
            os.link(pending, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        pending.unlink(missing_ok=True)


def encoded(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def native(arguments: list[str]) -> bytes:
    try:
        result = subprocess.run(arguments, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise InputError("SPIRE_LOCAL_PKI_OPERATION_UNKNOWN") from None
    if result.returncode:
        raise InputError("SPIRE_LOCAL_PKI_VALIDATION_FAILED")
    return result.stdout


def verify_pair(
    binary: str, certificate: Path, key: Path, ca: Path, purpose: str
) -> bytes:
    for path in (certificate, ca):
        regular(path)
        native([binary, "x509", "-in", str(path), "-checkend", str(WINDOW), "-noout"])
    leaf_constraints = native(
        [binary, "x509", "-in", str(certificate), "-ext", "basicConstraints", "-noout"]
    )
    issuer_constraints = native(
        [binary, "x509", "-in", str(ca), "-ext", "basicConstraints", "-noout"]
    )
    if re.search(rb"(?m)^\s*CA:FALSE\s*$", leaf_constraints) is None:
        raise InputError("SPIRE_LOCAL_LEAF_REQUIRED")
    if re.search(rb"(?m)^\s*CA:TRUE(?:,.*)?\s*$", issuer_constraints) is None:
        raise InputError("SPIRE_LOCAL_ISSUER_REQUIRED")
    public = native([binary, "x509", "-in", str(certificate), "-pubkey", "-noout"])
    if public == native([binary, "x509", "-in", str(ca), "-pubkey", "-noout"]):
        raise InputError("SPIRE_LOCAL_LEAF_REUSES_ISSUER_KEY")
    native(
        [
            binary,
            "verify",
            "-x509_strict",
            "-purpose",
            purpose,
            "-CAfile",
            str(ca),
            str(certificate),
        ]
    )
    regular(key, private=True)
    if public != native([binary, "pkey", "-in", str(key), "-pubout"]):
        raise InputError("SPIRE_LOCAL_LEAF_KEY_MISMATCH")
    return public


def authority(binary: str, root: Path, name: str) -> None:
    native(
        [
            binary,
            "req",
            "-x509",
            "-newkey",
            "rsa:3072",
            "-nodes",
            "-sha256",
            "-days",
            "365",
            "-subj",
            "/CN=AELEV SPIRE local " + name,
            "-addext",
            "basicConstraints=critical,CA:TRUE,pathlen:0",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
            "-addext",
            "subjectKeyIdentifier=hash",
            "-addext",
            "authorityKeyIdentifier=keyid:always",
            "-keyout",
            str(root / (name + ".key")),
            "-out",
            str(root / (name + ".crt")),
        ]
    )
    for suffix in (".crt", ".key"):
        (root / (name + suffix)).chmod(0o600)


def issue(
    binary: str,
    authorities: Path,
    scratch: Path,
    destination: Path,
    name: str,
    issuer: str,
    purpose: str,
    sans: str,
) -> None:
    csr, extension = scratch / (name + ".csr"), scratch / (name + ".ext")
    write(
        extension,
        (
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage="
            + purpose
            + "\n"
            + ("subjectAltName=" + sans + "\n" if sans else "")
            # x509 -> CSR removes SKI/AKI, then signing appends them. Preserve
            # that exact order from initial issuance for schema-1 scope hashes.
            + "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n"
        ).encode(),
    )
    native(
        [
            binary,
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-sha256",
            "-subj",
            "/CN=AELEV SPIRE " + name,
            "-keyout",
            str(destination.with_suffix(".key")),
            "-out",
            str(csr),
        ]
    )
    native(
        [
            binary,
            "x509",
            "-req",
            "-in",
            str(csr),
            "-CA",
            str(authorities / (issuer + ".crt")),
            "-CAkey",
            str(authorities / (issuer + ".key")),
            "-set_serial",
            str(secrets.randbits(159) | 1),
            "-days",
            "90",
            "-sha256",
            "-extfile",
            str(extension),
            "-out",
            str(destination.with_suffix(".crt")),
        ]
    )
    for path in (csr, destination.with_suffix(".crt"), destination.with_suffix(".key")):
        path.chmod(0o600)
    verify_pair(
        binary,
        destination.with_suffix(".crt"),
        destination.with_suffix(".key"),
        authorities / (issuer + ".crt"),
        "sslserver" if purpose == "serverAuth" else "sslclient",
    )


def object_value(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InputError("SPIRE_LOCAL_INPUT_CONFIG_INVALID")
    return cast(dict[str, Any], value)


def path_fields(value: object, fields: set[str]) -> dict[str, str]:
    values = object_value(value)
    if set(values) != fields or not all(
        isinstance(path, str) for path in values.values()
    ):
        raise InputError("SPIRE_LOCAL_INPUT_CONFIG_INVALID")
    return cast(dict[str, str], values)


def inputs(config: dict[str, Any], binary: str) -> dict[str, bytes]:
    if (
        set(config)
        != {
            "schema_version",
            "environment",
            "server_plan",
            "observation_plan",
            "issued",
            "trust",
            "status_admin",
            "grafana_tokens",
        }
        or config.get("schema_version") != 1
        or config.get("environment") not in {"local", "test"}
    ):
        raise InputError("SPIRE_LOCAL_INPUT_CONFIG_INVALID")
    for name in ("server_plan", "observation_plan"):
        plan = object_value(config[name])
        if plan.get("owner") != OWNER or plan.get("schema_version") != 1:
            raise InputError("SPIRE_LOCAL_INPUT_PLAN_INVALID")
    if config["server_plan"].get("environment") != config["environment"]:
        raise InputError("SPIRE_LOCAL_INPUT_PRODUCTION_FORBIDDEN")
    leaves(config)
    raw: dict[str, bytes] = {}
    for name, fields in {
        "trust": {"central", "shell", "identity", "grafana"},
        "status_admin": {"user", "password"},
        "grafana_tokens": {"publisher", "provisioner"},
    }.items():
        values = path_fields(config[name], fields)
        for field, value in values.items():
            path = Path(value)
            raw[name + "/" + field] = regular(path, private=name == "grafana_tokens")
            if name == "trust":
                native([binary, "x509", "-in", str(path), "-noout"])
    supplied = object_value(config["issued"])
    if set(supplied) != set(ISSUED):
        raise InputError("SPIRE_LOCAL_ISSUED_IDENTITIES_REQUIRED")
    leaf_keys: set[bytes] = set()
    issuer_certificates: set[bytes] = set()
    for name, item in supplied.items():
        value = object_value(item)
        if set(value) != {
            "certificate",
            "key",
            "issuer_certificate",
        }:
            raise InputError("SPIRE_LOCAL_ISSUED_IDENTITIES_REQUIRED")
        value = path_fields(value, {"certificate", "key", "issuer_certificate"})
        public = verify_pair(
            binary,
            Path(value["certificate"]),
            Path(value["key"]),
            Path(value["issuer_certificate"]),
            "sslclient",
        )
        issuer_certificate = native(
            [binary, "x509", "-in", value["issuer_certificate"], "-outform", "DER"]
        )
        if public in leaf_keys or issuer_certificate in issuer_certificates:
            raise InputError("SPIRE_LOCAL_ISSUED_AUTHORITY_OR_KEY_REUSED")
        leaf_keys.add(public)
        issuer_certificates.add(issuer_certificate)
        for field, path in value.items():
            raw[name + "/" + field] = regular(Path(path), private=field == "key")
    return raw


def prepare(config: dict[str, Any], output: Path) -> dict[str, Any]:
    executable = shutil.which("openssl")
    if executable is None:
        raise InputError("SPIRE_LOCAL_OPENSSL_REQUIRED")
    binary = str(Path(executable).resolve())
    material = inputs(config, binary)
    fingerprint = hashlib.sha256(
        encoded(
            {
                "config": config,
                "inputs": {
                    key: hashlib.sha256(raw).hexdigest()
                    for key, raw in material.items()
                },
            }
        )
    ).hexdigest()
    if (
        not output.is_absolute()
        or output.is_symlink()
        or any(parent.is_symlink() for parent in output.parents)
    ):
        raise InputError("SPIRE_LOCAL_OUTPUT_INVALID")
    if output.exists():
        try:
            receipt: dict[str, Any] = json.loads(
                regular(output / "receipt.json", private=True)
            )
            if receipt["status"] != "LOCAL_INITIAL_BUNDLE_PREPARED":
                raise InputError("SPIRE_LOCAL_PREPARATION_INSPECTION_REQUIRED")
            if receipt["input_sha256"] != fingerprint:
                raise InputError("SPIRE_LOCAL_INPUT_CHANGED")
            for path, digest in receipt["files"].items():
                candidate = output / path
                if (
                    candidate.resolve().is_relative_to(output.resolve()) is False
                    or hashlib.sha256(regular(candidate, private=True)).hexdigest()
                    != digest
                ):
                    raise InputError("SPIRE_LOCAL_PREPARED_MATERIAL_CHANGED")
            return receipt
        except (OSError, ValueError, KeyError, TypeError):
            raise InputError("SPIRE_LOCAL_PREPARATION_INSPECTION_REQUIRED") from None
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    if os.name != "nt":
        directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    receipt_path = output / "receipt.json"
    write(receipt_path, encoded({"status": "PREPARING", "input_sha256": fingerprint}))
    authorities, initial, scratch = (
        output / "authorities",
        output / "initial-input",
        output / "issuance",
    )
    for directory in (authorities, initial, scratch):
        directory.mkdir(mode=0o700)
    required: dict[str, list[str]] = json.loads(
        (ROOT / "src/aelev_spire/observation-inputs.json").read_text()
    )
    selected_leaves = leaves(config)
    for role in set(required) | {fields[0] for fields in selected_leaves.values()}:
        (initial / role).mkdir(mode=0o700)
    for name in ("query", "server", "write"):
        authority(binary, authorities, name)
    records: dict[str, Any] = {}
    for name, (role, stem, issuer, purpose, sans) in selected_leaves.items():
        destination = initial / role / stem
        issue(binary, authorities, scratch, destination, name, issuer, purpose, sans)
        records[name] = {
            "certificate": str(destination.with_suffix(".crt")),
            "key": str(destination.with_suffix(".key")),
            "issuer_certificate": str(authorities / (issuer + ".crt")),
            "issuer_scope": issuer,
            "volume_role": role + "-secrets",
            "file": stem + ".crt",
        }
    for name, (role, stem, issuer) in ISSUED.items():
        write(authorities / (issuer + ".crt"), material[name + "/issuer_certificate"])
        for field, suffix in (("certificate", ".crt"), ("key", ".key")):
            write(initial / role / (stem + suffix), material[name + "/" + field])
        records[name] = {
            "certificate": str(initial / role / (stem + ".crt")),
            "key": str(initial / role / (stem + ".key")),
            "issuer_certificate": str(authorities / (issuer + ".crt")),
            "issuer_scope": issuer,
            "volume_role": role + "-secrets",
            "file": stem + ".crt",
        }
    own = {
        name: regular(authorities / (name + ".crt"))
        for name in ("query", "server", "write")
    }
    trusts = {
        "observer/query-ca.crt": own["query"],
        "observer/sender-ca.crt": own["server"],
        "sender/server-ca.crt": own["server"],
        "sender/local-write-ca.crt": own["write"],
        "sender/local-query-ca.crt": own["query"] + own["server"],
        "sender/central-ca.crt": material["trust/central"],
        "status/query-ca.crt": own["server"],
        "status/shell-ca.crt": material["trust/shell"],
        "status/identity-ca.crt": material["trust/identity"],
        "grafana/grafana-ca.crt": material["trust/grafana"],
        "grafana-admin/grafana-ca.crt": material["trust/grafana"],
        "status-admin/admin-user": material["status_admin/user"],
        "status-admin/admin-password": material["status_admin/password"],
        "grafana/token": material["grafana_tokens/publisher"],
        "grafana-admin/token": material["grafana_tokens/provisioner"],
    }
    if "public-source" in selected_leaves:
        trusts["public-source/source-client-ca.crt"] = own["write"] + own["query"]
    for relative, raw in trusts.items():
        write(initial / relative, raw)
    for name, key in (
        ("plan.json", "server_plan"),
        ("observation.json", "observation_plan"),
    ):
        write(initial / name, encoded(config[key]))
    native(
        [
            sys.executable,
            str(ROOT / "bootstrap.py"),
            "validate",
            "--initial-input",
            str(initial),
        ]
    )
    # This is a request for the separate issuer operators, never a fabricated
    # approval, scheduled task, delivery binding or positive renewal report.
    write(
        output / "renewal-requests.json",
        encoded(
            {
                "owner": OWNER,
                "schema_version": 1,
                "environment": config["environment"],
                "profiles": {
                    name: [records[key] | {"id": key} for key in ids]
                    for name, ids in (
                        (
                            NETWORK_PROFILES
                            if "network" in config["observation_plan"]
                            else PROFILES
                        )
                        | (
                            {"public-source.json": ("public-source",)}
                            if "public-source" in selected_leaves
                            else {}
                        )
                    ).items()
                },
            }
        ),
    )
    files = {
        str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in output.rglob("*")
        if path.is_file() and path != receipt_path
    }
    receipt = {
        "status": "LOCAL_INITIAL_BUNDLE_PREPARED",
        "owner": OWNER,
        "schema_version": 1,
        "input_sha256": fingerprint,
        "initial_input": str(initial),
        "renewal_requests": str(output / "renewal-requests.json"),
        "external_issuer_keys_delivered": False,
        "renewal_enrolled": False,
        "runtime_installed": False,
        "files": files,
    }
    write(receipt_path, encoded(receipt), replace=True)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        config = json.loads(regular(args.input.absolute(), private=True))
        result = prepare(config, args.output.absolute())
        print(
            json.dumps(
                {
                    key: result[key]
                    for key in (
                        "status",
                        "initial_input",
                        "renewal_requests",
                        "external_issuer_keys_delivered",
                        "renewal_enrolled",
                        "runtime_installed",
                    )
                }
            )
        )
        return 0
    except (InputError, OSError, ValueError, KeyError, TypeError):
        error = sys.exception()
        print(
            str(error)
            if isinstance(error, InputError)
            else "SPIRE_LOCAL_PREPARATION_FAILED",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
