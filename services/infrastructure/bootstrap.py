#!/usr/bin/env python3
"""Public local infrastructure lifecycle entry; application resources stay with consumers."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import secrets
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INFRASTRUCTURE = ROOT
PLATFORM = ROOT / "platform-ui"
KEYCLOAK = ROOT / "keycloak"
POSTGRESQL = ROOT / "postgresql"
MINIO = ROOT / "minio"
RELEASE = json.loads((ROOT / "release.json").read_text())["version"]

CAPABILITIES = {
    "postgresql": (
        "etcd-1",
        "etcd-2",
        "etcd-3",
        "postgresql-fencer",
        "postgresql-1",
        "postgresql-2",
        "postgresql-3",
    ),
    "kafka": ("kafka",),
    "object-storage": tuple(
        f"minio-site-{site}-{number}" for site in ("a", "b") for number in range(1, 5)
    ),
    "identity": ("keycloak-database", "keycloak"),
    "platform-ui": ("platform-ui-api", "platform-ui"),
}
BUILDS = {
    "postgresql": ("postgresql-1",),
    "kafka": ("kafka",),
    "object-storage": ("minio-site-a-1",),
    "identity": ("keycloak",),
    "platform-ui": ("platform-ui-api", "platform-ui", "platform-ui-configuration-init"),
}


class QuickstartError(RuntimeError):
    pass


def info(message: str) -> None:
    print(f"[infrastructure] {message}", flush=True)


def run(
    arguments: list[str] | tuple[str, ...],
    *,
    cwd: Path = ROOT,
    environment: dict[str, str] | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if environment:
        merged.update(environment)
    return subprocess.run(
        list(arguments),
        cwd=cwd,
        env=merged,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def compose(
    directory: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return run(
        [
            "docker",
            "compose",
            "--profile",
            "ui",
            "--profile",
            "provision",
            "--profile",
            "provisioning",
            *arguments,
        ],
        cwd=directory,
        environment=environment,
        capture=capture,
    )


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if line and not line.lstrip().startswith("#"):
            key, separator, value = line.partition("=")
            if not separator or key in values:
                raise QuickstartError(f"invalid environment template: {path}")
            values[key] = value
    return values


def write_private(path: Path, value: str | bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, mode)
    with os.fdopen(descriptor, "wb") as output:
        output.write(value.encode() if isinstance(value, str) else value)


def initialize_env(path: Path, updates: dict[str, str] | None = None) -> None:
    if path.exists():
        return
    example = path.with_name(".env.example")
    if not example.is_file():
        raise QuickstartError(f"missing environment template: {example}")
    values = read_env(example)
    values.update(updates or {})
    write_private(path, "".join(f"{key}={value}\n" for key, value in values.items()))


def openssl(*arguments: str) -> None:
    run(["openssl", *arguments], capture=True)


def initialize_keycloak() -> None:
    initialize_env(
        KEYCLOAK / ".env",
        {
            "AELEV_INFRASTRUCTURE_KEYCLOAK_BIND": "127.0.0.1",
            "AELEV_INFRASTRUCTURE_KEYCLOAK_HTTPS_PORT": "18443",
            "AELEV_INFRASTRUCTURE_KEYCLOAK_PUBLIC_URL": "https://127.0.0.1:18443",
            "AELEV_INFRASTRUCTURE_KEYCLOAK_ADMIN_ENDPOINT": "https://127.0.0.1:18443",
            "AELEV_INFRASTRUCTURE_KEYCLOAK_UI_URL": "https://host.docker.internal:18082",
            "AELEV_INFRASTRUCTURE_KEYCLOAK_SECRET_GID": str(os.getgid()),
            "AELEV_INFRASTRUCTURE_KEYCLOAK_UI_SECRET_GID": str(os.getgid()),
            "AELEV_INFRASTRUCTURE_KEYCLOAK_UI_PROVISION_UID": str(os.getuid()),
        },
    )
    secrets_root = KEYCLOAK / "secrets"
    write_private(secrets_root / "admin-password", secrets.token_urlsafe(32) + "\n")
    write_private(secrets_root / "database-password", secrets.token_urlsafe(32) + "\n")
    tls = secrets_root / "https"
    required = (tls / "ca.crt", tls / "tls.crt", tls / "tls.key")
    if all(item.is_file() for item in required):
        return
    if any(item.exists() for item in required):
        raise QuickstartError(
            "partial Keycloak TLS identity exists; refusing to overwrite it"
        )
    tls.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="drp-keycloak-pki-") as temporary:
        scratch = Path(temporary)
        openssl(
            "req",
            "-x509",
            "-newkey",
            "rsa:3072",
            "-nodes",
            "-days",
            "365",
            "-subj",
            "/CN=DRP local identity CA",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
            "-keyout",
            str(scratch / "ca.key"),
            "-out",
            str(tls / "ca.crt"),
        )
        openssl(
            "req",
            "-new",
            "-newkey",
            "rsa:3072",
            "-nodes",
            "-subj",
            "/CN=keycloak",
            "-keyout",
            str(tls / "tls.key"),
            "-out",
            str(scratch / "server.csr"),
        )
        (scratch / "extensions").write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\n"
            "subjectAltName=DNS:keycloak,DNS:localhost,DNS:host.docker.internal,IP:127.0.0.1\n"
        )
        openssl(
            "x509",
            "-req",
            "-days",
            "365",
            "-sha256",
            "-in",
            str(scratch / "server.csr"),
            "-CA",
            str(tls / "ca.crt"),
            "-CAkey",
            str(scratch / "ca.key"),
            "-CAcreateserial",
            "-extfile",
            str(scratch / "extensions"),
            "-out",
            str(tls / "tls.crt"),
        )
    (tls / "tls.key").chmod(0o600)
    (tls / "tls.crt").chmod(0o644)
    (tls / "ca.crt").chmod(0o644)


def initialize_platform() -> None:
    initialize_env(
        PLATFORM / ".env",
        {"AELEV_INFRASTRUCTURE_PLATFORM_UI_SECRET_GID": str(os.getgid())},
    )
    secrets_root = PLATFORM / "secrets"
    if not (secrets_root / "nginx/server.crt").exists():
        run(
            [
                sys.executable,
                "prepare.py",
                "development-pki",
                "--identity-ca",
                str(KEYCLOAK / "secrets/https/ca.crt"),
                "--admin-password-file",
                str(KEYCLOAK / "secrets/admin-password"),
            ],
            cwd=PLATFORM,
        )
    status_admin = secrets_root / "status-admin"
    write_private(status_admin / "admin-user", "admin\n", 0o640)
    write_private(
        status_admin / "admin-password",
        (KEYCLOAK / "secrets/admin-password").read_bytes(),
        0o640,
    )
    run([sys.executable, "prepare.py", "render"], cwd=PLATFORM)


def initialize_minio() -> None:
    if not (MINIO / ".env").exists():
        run(["sh", "generate-local-configuration.sh"], cwd=MINIO)


def initialize_postgresql() -> None:
    initialize_env(POSTGRESQL / ".env")
    issuer = POSTGRESQL / "secrets/issuer/postgresql.crt"
    if not issuer.exists() and (POSTGRESQL / "secrets/node-1/server.crt").exists():
        info(
            "preserving the existing PostgreSQL installation without restoring its offline issuer"
        )
        return
    if not issuer.exists():
        run([sys.executable, "prepare-tls.py"], cwd=POSTGRESQL)


def initialize() -> None:
    for executable in ("docker", "openssl"):
        if shutil.which(executable) is None:
            raise QuickstartError(f"required tool unavailable: {executable}")
    initialize_env(ROOT / ".env")
    for example in sorted(ROOT.glob("**/.env.example")):
        if example.parent not in {KEYCLOAK, PLATFORM, MINIO}:
            initialize_env(example.with_name(".env"))
    initialize_keycloak()
    initialize_minio()
    initialize_postgresql()
    initialize_platform()
    compose(ROOT, "config", "--quiet")


def selected(capabilities: list[str], build: bool = False) -> tuple[str, ...]:
    catalog = BUILDS if build else CAPABILITIES
    requested = set(capabilities)
    if "platform-ui" in requested:
        requested.add("identity")
    return tuple(
        dict.fromkeys(
            role
            for capability in catalog
            if capability in requested
            for role in catalog[capability]
        )
    )


def verify_scope() -> None:
    project = read_env(ROOT / ".env")["AELEV_INFRASTRUCTURE_COMPOSE_PROJECT"]
    result = run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            '{{.Label "com.docker.compose.project.working_dir"}}',
        ],
        capture=True,
    )
    for directory in result.stdout.splitlines():
        if not directory or Path(directory).resolve() != ROOT:
            raise QuickstartError(
                "infrastructure project belongs to another installation; choose an explicit separate environment"
            )


def wait_identity() -> None:
    context = ssl.create_default_context(cafile=str(KEYCLOAK / "secrets/https/ca.crt"))
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        connection = http.client.HTTPSConnection(
            "127.0.0.1", 18443, context=context, timeout=5
        )
        try:
            connection.request("GET", "/realms/master")
            if connection.getresponse().status < 500:
                return
        except (OSError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        time.sleep(2)
    raise QuickstartError("local identity did not become ready")


def execute(command: str, capabilities: list[str], no_build: bool = False) -> None:
    if command == "plan":
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "release": RELEASE,
                    "capabilities": capabilities,
                    "services": selected(capabilities),
                    "models_included": False,
                },
                indent=2,
            )
        )
        return
    if command == "status":
        verify_scope()
        compose(ROOT, "ps", "--all", *selected(capabilities))
        return
    if command == "down":
        verify_scope()
        compose(ROOT, "stop", *selected(capabilities))
        return
    initialize()
    if command == "init":
        return
    if command == "up":
        verify_scope()
    if command == "build" or not no_build:
        builds = selected(capabilities, build=True)
        if builds:
            compose(ROOT, "build", *builds)
    if command == "build":
        return
    services = selected(capabilities)
    core = tuple(role for role in services if role not in CAPABILITIES["platform-ui"])
    if core:
        compose(ROOT, "up", "-d", "--no-build", *core)
    if "identity" in capabilities or "platform-ui" in capabilities:
        wait_identity()
    if "platform-ui" in capabilities:
        if not (PLATFORM / "secrets/status/client-secret").is_file():
            compose(ROOT, "run", "--rm", "platform-ui-configuration-init")
        compose(
            ROOT, "up", "-d", "--no-build", "--no-deps", *CAPABILITIES["platform-ui"]
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("init", "build", "up", "status", "down", "plan")
    )
    parser.add_argument("--capability", action="append", choices=tuple(CAPABILITIES))
    parser.add_argument("--no-build", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "plan":
            execute(args.command, args.capability or list(CAPABILITIES), args.no_build)
            return 0
        lock = ROOT / "runtime/lifecycle.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            lock.mkdir()
        except FileExistsError:
            raise QuickstartError(
                "another lifecycle operation is pending; inspect runtime/lifecycle.lock before retrying"
            ) from None
        try:
            execute(args.command, args.capability or list(CAPABILITIES), args.no_build)
        finally:
            lock.rmdir()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(
            f"[infrastructure] {type(error).__name__}: operation failed; local state preserved",
            file=sys.stderr,
        )
        return 2
    except QuickstartError as error:
        print(f"[infrastructure] {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
