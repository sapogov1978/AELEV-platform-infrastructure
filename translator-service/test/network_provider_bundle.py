#!/usr/bin/env python3
"""Probe exact provider/worker images across isolated networks, without inference.

Ephemeral test PKI is not production identity or physical multi-host acceptance.
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from provider_tls_fixtures import CLIENT, SERVER, Authority

PIN = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
LOCAL = re.compile(r"^sha256:[0-9a-f]{64}$")
LABEL = "io.aelev.translator-network-bundle"
TLS_ROOT = "/run/secrets/compute-provider"
HOST = "provider-node.test"


def run(
    arguments: list[str], *, data: str | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        input=data,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=check,
        timeout=90,
    )


def initialize(image: str, volume: str, files: dict[str, bytes], token: str) -> None:
    # First mount must be owned by the runtime UID, with no chown capability.
    program = """import base64,json,os,pathlib
root=pathlib.Path('/run/secrets/compute-provider')
if root.stat().st_uid!=10001 or list(root.iterdir()):
 raise RuntimeError('NETWORK_BUNDLE_FIRST_MOUNT_OWNER_INVALID')
for name,value in json.load(__import__('sys').stdin).items():
 if '/' in name or name in {'.','..'}:
  raise RuntimeError('NETWORK_BUNDLE_FILENAME_INVALID')
 path=root/name
 path.write_bytes(base64.b64decode(value))
 path.chmod(0o600 if name.endswith('.key') else 0o644)
print('INITIALIZED_OWN_EMPTY_VOLUME')
"""
    run(
        [
            "docker",
            "run",
            "--rm",
            "--interactive",
            "--network",
            "none",
            "--label",
            f"{LABEL}={token}",
            "--read-only",
            "--user",
            "10001:10001",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--mount",
            f"type=volume,src={volume},dst={TLS_ROOT}",
            "--entrypoint",
            "python",
            image,
            "-c",
            program,
        ],
        data=json.dumps(
            {
                name: base64.b64encode(value).decode("ascii")
                for name, value in files.items()
            }
        ),
    )


def validate_subnets(values: list[str]) -> None:
    selected = [ipaddress.IPv4Network(value, strict=True) for value in values]
    allowed = [
        ipaddress.IPv4Network(value)
        for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    ]
    if any(
        network.version != 4
        or network.prefixlen != 28
        or not any(network.subnet_of(parent) for parent in allowed)
        for network in selected
    ) or selected[0].overlaps(selected[1]):
        raise RuntimeError("NETWORK_BUNDLE_SUBNET_INVALID")
    identifiers = run(["docker", "network", "ls", "--quiet"]).stdout.split()
    networks: list[dict[str, Any]] = (
        json.loads(run(["docker", "network", "inspect", *identifiers]).stdout)
        if identifiers
        else []
    )
    existing: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for network in networks:
        settings: dict[str, Any] = network.get("IPAM") or {}
        declarations: list[dict[str, Any]] = settings.get("Config") or []
        for item in declarations:
            subnet = item.get("Subnet")
            if isinstance(subnet, str):
                existing.append(ipaddress.ip_network(subnet, strict=False))
    if any(
        a.version == b.version and a.overlaps(b) for a in selected for b in existing
    ):
        raise RuntimeError("NETWORK_BUNDLE_SUBNET_OVERLAP")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-image", required=True)
    parser.add_argument("--worker-image", required=True)
    parser.add_argument("--allow-local-image-ids", action="store_true")
    parser.add_argument("--provider-subnet", required=True)
    parser.add_argument("--worker-subnet", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    images = (args.provider_image, args.worker_image)
    if any(
        not PIN.fullmatch(image)
        and not (args.allow_local_image_ids and LOCAL.fullmatch(image))
        for image in images
    ):
        raise SystemExit("NETWORK_BUNDLE_IMMUTABLE_IMAGES_REQUIRED")
    if args.report.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        raise SystemExit("NETWORK_BUNDLE_EXTERNAL_EVIDENCE_REQUIRED")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    prefix = "translator-mtls-" + token
    provider = prefix + "-provider"
    networks: list[str] = []
    volumes: list[str] = []
    result: dict[str, Any] = {
        "schema_version": 1,
        "status": "FAIL",
        "provider_image": images[0],
        "worker_image": images[1],
        "images_are_release_refs": all(
            PIN.fullmatch(image) is not None for image in images
        ),
        "inference_requested": False,
        "test_pki": True,
        "physical_multihost_proven": False,
        "release_admitted": False,
        "provider_worker_shared_volume": False,
    }
    try:
        validate_subnets([args.provider_subnet, args.worker_subnet])
        for image in images:
            uid = run(
                ["docker", "image", "inspect", "--format", "{{.Config.User}}", image]
            ).stdout.strip()
            if uid != "10001:10001":
                raise RuntimeError("NETWORK_BUNDLE_RUNTIME_UID_INVALID")
        for role, subnet in (
            ("provider", args.provider_subnet),
            ("worker", args.worker_subnet),
        ):
            name = prefix + "-" + role + "-network"
            run(
                [
                    "docker",
                    "network",
                    "create",
                    "--label",
                    f"{LABEL}={token}",
                    "--subnet",
                    subnet,
                    name,
                ]
            )
            networks.append(name)
        for role in ("provider", "worker", "foreign-worker"):
            name = prefix + "-" + role + "-svid"
            run(["docker", "volume", "create", "--label", f"{LABEL}={token}", name])
            owned = json.loads(run(["docker", "volume", "inspect", name]).stdout)[0]
            if owned.get("Labels", {}).get(LABEL) != token:
                raise RuntimeError("NETWORK_BUNDLE_VOLUME_OWNER_INVALID")
            volumes.append(name)
        with tempfile.TemporaryDirectory(
            prefix="network-pki-", dir=args.report.parent
        ) as temporary:
            root = Path(temporary).resolve()
            issuer = Authority(root / "issuer")
            policy = {
                "schema_version": 1,
                "environment": "test",
                "delivery": "pem-files-v1",
                "server_spiffe_id": SERVER,
                "client_spiffe_id": CLIENT,
            }
            server_cert, server_key = issuer.leaf([SERVER], dns_names=(HOST,))
            initialize(
                images[0],
                volumes[0],
                {
                    "identity.json": json.dumps(policy).encode(),
                    "transport.json": json.dumps(
                        {
                            "schema_version": 1,
                            "transport": "mtls",
                            "listen_host": "0.0.0.0",
                            "listen_port": 18487,
                        }
                    ).encode(),
                    "server.crt": server_cert.read_bytes(),
                    "server.key": server_key.read_bytes(),
                    "client-ca.crt": issuer.cert.read_bytes(),
                },
                token,
            )
            for index, identity in (
                (1, CLIENT),
                (2, "spiffe://test.example/translator-service/foreign/worker"),
            ):
                cert, key = issuer.leaf([identity])
                initialize(
                    images[1],
                    volumes[index],
                    {
                        "identity.json": json.dumps(
                            {**policy, "client_spiffe_id": identity}
                        ).encode(),
                        "client.crt": cert.read_bytes(),
                        "client.key": key.read_bytes(),
                        "ca.crt": issuer.cert.read_bytes(),
                    },
                    token,
                )
            credential = root / "credential.env"
            credential.write_text("OPENAI_API_KEY=fixture-openai-key-not-secret\n")
            credential.chmod(0o640)
            run(
                [
                    "docker",
                    "run",
                    "--detach",
                    "--name",
                    provider,
                    "--label",
                    f"{LABEL}={token}",
                    "--network",
                    networks[0],
                    "--publish",
                    "0.0.0.0::18487",
                    "--read-only",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,size=64m,mode=1777",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--memory",
                    "1g",
                    "--cpus",
                    "1",
                    "--group-add",
                    str(os.getgid()),
                    "--mount",
                    f"type=volume,src={volumes[0]},dst={TLS_ROOT},readonly",
                    "--mount",
                    f"type=bind,src={credential},dst=/run/secrets/provider/credential.env,readonly",
                    images[0],
                ]
            )
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                observed = json.loads(
                    run(
                        [
                            "docker",
                            "inspect",
                            "--format",
                            "{{json .State}}",
                            provider,
                        ]
                    ).stdout
                )
                status = (
                    str(observed.get("Status"))
                    + " "
                    + str((observed.get("Health") or {}).get("Status", "starting"))
                )
                if status == "running healthy":
                    break
                if not status.startswith("running "):
                    raise RuntimeError("NETWORK_BUNDLE_PROVIDER_STOPPED")
                time.sleep(1)
            else:
                raise RuntimeError("NETWORK_BUNDLE_HEALTH_TIMEOUT")
            port = json.loads(
                run(
                    [
                        "docker",
                        "inspect",
                        "--format",
                        '{{json (index .NetworkSettings.Ports "18487/tcp")}}',
                        provider,
                    ]
                ).stdout
            )[0]["HostPort"]

            def probe(volume: str, *, foreign: bool = False) -> None:
                command = [
                    "docker",
                    "run",
                    "--rm",
                    "--label",
                    f"{LABEL}={token}",
                    "--network",
                    networks[1],
                    "--add-host",
                    f"{HOST}:host-gateway",
                    "--read-only",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,size=16m,mode=1777",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--memory",
                    "256m",
                    "--cpus",
                    "1",
                    "--mount",
                    f"type=volume,src={volume},dst={TLS_ROOT},readonly",
                    "--env",
                    "AELEV_ENVIRONMENT=test",
                    "--env",
                    "AELEV_TRANSLATOR_PROVIDER_TRANSPORT=mtls",
                    "--env",
                    f"AELEV_TRANSLATOR_PROVIDER_TLS_ROOT={TLS_ROOT}",
                    "--env",
                    f"AELEV_TRANSLATOR_PROVIDER_URL=https://{HOST}:{port}",
                    "--entrypoint",
                    "python",
                    images[1],
                ]
                if foreign:
                    program = """from pathlib import Path
from translator_service.provider_identity import Identity
from translator_service.provider_healthcheck import main
identity = Identity(Path('/run/secrets/compute-provider'),'translator-service','client')
assert identity.available()
try:main()
except RuntimeError as error:
 if str(error) not in {
  'TRANSLATOR_PROVIDER_TLS_UNAVAILABLE','TRANSLATOR_PROVIDER_TRANSPORT_ERROR'
 }:raise
 print('FOREIGN_VALID_CLIENT_REJECTED')
else:raise RuntimeError('NETWORK_BUNDLE_FOREIGN_CLIENT_ACCEPTED')
"""
                    observed = run(command + ["-c", program]).stdout.strip()
                    if observed != "FOREIGN_VALID_CLIENT_REJECTED":
                        raise RuntimeError("NETWORK_BUNDLE_FOREIGN_CLIENT_NOT_PROVEN")
                else:
                    run(command + ["-m", "translator_service.provider_healthcheck"])

            probe(volumes[1])
            probe(volumes[2], foreign=True)
            probe(volumes[1])
            result.update(
                status="PASS",
                provider_health="healthy",
                worker_mtls_probe="PASS",
                foreign_valid_client_rejected=True,
                first_mount_nonroot="PASS",
                distinct_networks=True,
                published_endpoint_used=True,
            )
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        code = (
            str(error)
            if isinstance(error, RuntimeError)
            else "NETWORK_BUNDLE_EXECUTION_FAILED"
        )
        if isinstance(error, subprocess.CalledProcessError):
            diagnostic = args.report.with_suffix(".private-error.log")
            diagnostic.write_text((error.stdout or "")[:1_048_576])
            diagnostic.chmod(0o600)
            result["private_diagnostic"] = str(diagnostic)
            bounded = re.findall(r"NETWORK_BUNDLE_[A-Z_]+", error.stdout or "")
            if bounded:
                code = bounded[-1]
        result["error_code"] = (
            code
            if re.fullmatch(r"NETWORK_BUNDLE_[A-Z_]+", code)
            else "NETWORK_BUNDLE_EXECUTION_FAILED"
        )
    finally:
        cleanup = True
        inspected = run(
            ["docker", "inspect", "--format", "{{json .Config.Labels}}", provider],
            check=False,
        )
        if (
            inspected.returncode == 0
            and json.loads(inspected.stdout).get(LABEL) == token
        ):
            run(["docker", "stop", "--timeout", "5", provider], check=False)
            cleanup = run(["docker", "rm", provider], check=False).returncode == 0
        for volume in reversed(volumes):
            cleanup = (
                run(["docker", "volume", "rm", volume], check=False).returncode == 0
                and cleanup
            )
        for network in reversed(networks):
            cleanup = (
                run(["docker", "network", "rm", network], check=False).returncode == 0
                and cleanup
            )
        result["cleanup_preserved"] = cleanup
        if not cleanup:
            result["status"] = "FAIL"
        args.report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        args.report.chmod(0o600)
    print(
        json.dumps(
            {
                "status": result["status"],
                "report": str(args.report),
                "error_code": result.get("error_code"),
            }
        )
    )
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
