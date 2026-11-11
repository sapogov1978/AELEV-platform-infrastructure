#!/usr/bin/env python3
"""Public local infrastructure lifecycle; consumers own application resources."""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import os
import secrets
import shutil
import ssl
import stat
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
OBSERVABILITY = ROOT / "observability"
TRANSLATOR = ROOT / "translator-service"
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
    "translation": (
        "translator-database",
        "translator-migrator",
        "translator-topic-provisioner",
        "translator-priority-consumer",
        "translator-normal-consumer",
        "translator-outbox",
        "translator-api",
        "translator-service-status-publisher",
        "translation-provider",
        "translator-worker",
    ),
}
BUILDS = {
    "postgresql": ("postgresql-1",),
    "kafka": ("kafka",),
    "object-storage": ("minio-site-a-1",),
    "identity": ("keycloak",),
    "platform-ui": ("platform-ui-api", "platform-ui", "platform-ui-configuration-init"),
    "translation": (
        "translator-migrator",
        "translator-topic-provisioner",
        "translator-priority-consumer",
        "translator-normal-consumer",
        "translator-outbox",
        "translator-api",
        "translator-service-status-publisher",
    ),
}
PULLS = {"translation": ("translation-provider", "translator-worker")}
ADDITIVE_TEMPLATE_KEYS = {
    "AELEV_INFRASTRUCTURE_PLATFORM_UI_LOCAL_BRIDGE_BIND",
    "AELEV_INFRASTRUCTURE_POSTGRESQL_FENCING_SECRET_GID",
    "AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_CPUS",
    "AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_EGRESS_NETWORK",
    "AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_EGRESS_SUBNET",
    "AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_IMAGE",
    "AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_MEMORY_LIMIT",
    "AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_RESTART_POLICY",
    "AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_SECRET_FILE",
    "AELEV_INFRASTRUCTURE_TRANSLATOR_DATABASE_NETWORK_EXTERNAL",
    "AELEV_INFRASTRUCTURE_TRANSLATOR_TELEMETRY_NETWORK_EXTERNAL",
    "AELEV_INFRASTRUCTURE_TRANSLATOR_WORKER_IMAGE",
}
LEGACY_TRANSLATION_IMAGE_REFERENCES = {
    "AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_IMAGE": frozenset(
        {
            "ghcr.io/sapogov1978/aelev-translation-provider@sha256:"
            "f21a70673fbd03dec14b7ed733ee2d7ecccd6e69aea6766dfd7e3dd8b38a6866",
            "ghcr.io/sapogov1978/aelev-translation-provider@sha256:"
            "0a09783d072ccc7c60ec8e6fbe5ce56def95a56601888d5c3e4cf083da3ee016",
        }
    ),
    "AELEV_INFRASTRUCTURE_TRANSLATOR_WORKER_IMAGE": frozenset(
        {
            "ghcr.io/sapogov1978/"
            "aelev-platform-infrastructure-translator-worker@sha256:"
            "a65a59b771e5cf4314db1b5fde8fbca5c00e72f5b1a729073b6025f94779df35"
        }
    ),
}
LEGACY_TRANSLATION_UI_DESCRIPTION_REVISION = "11"


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


def update_env(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text().splitlines()
    seen: set[str] = set()
    rendered: list[str] = []
    for line in lines:
        key, separator, _ = line.partition("=")
        if separator and key in updates:
            rendered.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            rendered.append(line)
    rendered.extend(
        f"{key}={value}" for key, value in updates.items() if key not in seen
    )
    mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        owned_descriptor = descriptor
        descriptor = -1
        with os.fdopen(owned_descriptor, "w") as output:
            output.write("\n".join(rendered) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)
        raise


def initialize_additive_local_paths() -> None:
    """Add new non-secret local path settings without changing existing values."""
    for example in sorted(ROOT.glob("**/.env.example")):
        path = example.with_name(".env")
        if not path.is_file():
            continue
        template = read_env(example)
        current = read_env(path)
        updates = {
            key: value
            for key, value in template.items()
            if (key.endswith("_OIDC_CA_FILE") or key in ADDITIVE_TEMPLATE_KEYS)
            and key not in current
        }
        if updates:
            update_env(path, updates)


def migrate_legacy_translation_image_references() -> None:
    """Move exact superseded release pins forward while preserving overrides."""
    path = TRANSLATOR / ".env"
    example = TRANSLATOR / ".env.example"
    if not path.is_file() or not example.is_file():
        return
    current = read_env(path)
    template = read_env(example)
    updates = {
        key: template[key]
        for key, legacy in LEGACY_TRANSLATION_IMAGE_REFERENCES.items()
        if current.get(key) in legacy and key in template
    }
    if updates:
        update_env(path, updates)


def migrate_legacy_translation_ui_description_revision() -> None:
    """Advance only the descriptor revision shipped by the previous release."""
    path = TRANSLATOR / ".env"
    example = TRANSLATOR / ".env.example"
    if not path.is_file() or not example.is_file():
        return
    key = "AELEV_INFRASTRUCTURE_TRANSLATOR_UI_DESCRIPTION_REVISION"
    current = read_env(path)
    template = read_env(example)
    if (
        current.get(key) == LEGACY_TRANSLATION_UI_DESCRIPTION_REVISION
        and key in template
    ):
        update_env(path, {key: template[key]})


def initialize_owner_environments() -> None:
    """Create operational owner env files without replacing existing choices."""
    for example in sorted(ROOT.glob("**/.env.example")):
        if example.parent in {KEYCLOAK, PLATFORM, MINIO}:
            continue
        updates = None
        if example.parent == OBSERVABILITY:
            updates = {
                "AELEV_INFRASTRUCTURE_OBSERVABILITY_ENV_FILE": ".env",
            }
        elif example.parent == TRANSLATOR:
            updates = {
                "AELEV_INFRASTRUCTURE_TRANSLATOR_SECRET_GID": str(os.getgid()),
            }
        initialize_env(example.with_name(".env"), updates)


def initialize_translation_provider_credential() -> None:
    values = read_env(TRANSLATOR / ".env")
    configured = values["AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_SECRET_FILE"]
    secret_gid = int(values["AELEV_INFRASTRUCTURE_TRANSLATOR_SECRET_GID"])
    if secret_gid < 0:
        raise QuickstartError("translator provider credential group is invalid")
    target = (TRANSLATOR / configured).resolve()
    if target == TRANSLATOR.resolve() or TRANSLATOR.resolve() not in target.parents:
        raise QuickstartError("translator provider credential path escapes owner root")
    if target.exists():
        if target.is_symlink() or not target.is_file():
            raise QuickstartError("translator provider credential is invalid")
        return
    source = ROOT.parent / "TranslationProvider/.env.local"
    if source.is_file() and not source.is_symlink():
        value = source.read_bytes()
    elif (
        os.environ.get("AELEV_INFRASTRUCTURE_FULL_LOCAL_PROVIDER_FIXTURE")
        == "health-only-no-inference-v1"
    ):
        value = b"OPENAI_API_KEY=fixture-openai-key-not-secret\n"
    else:
        raise QuickstartError(
            "translation provider credential source is missing; "
            "prepare TranslationProvider/.env.local"
        )
    if not value.strip() or len(value) > 4096:
        raise QuickstartError("translator provider credential is invalid")
    write_private(target, value, 0o640)
    os.chown(target, -1, secret_gid)
    target.chmod(0o640)


def openssl(*arguments: str) -> None:
    run(["openssl", *arguments], capture=True)


def docker_bridge_gateway() -> str:
    result = run(
        [
            "docker",
            "network",
            "inspect",
            "bridge",
            "--format",
            "{{(index .IPAM.Config 0).Gateway}}",
        ],
        capture=True,
    ).stdout.strip()
    try:
        address = ipaddress.ip_address(result)
    except ValueError as error:
        raise QuickstartError("Docker bridge gateway is unavailable") from error
    if (
        address.version != 4
        or not address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
    ):
        raise QuickstartError("Docker bridge gateway must be a private IPv4 address")
    return str(address)


def initialize_local_bridge_routes(gateway: str) -> None:
    routes = {
        KEYCLOAK / ".env": (
            "AELEV_INFRASTRUCTURE_KEYCLOAK_BIND",
            "AELEV_INFRASTRUCTURE_KEYCLOAK_LOCAL_BRIDGE_BIND",
        ),
        PLATFORM / ".env": (
            "AELEV_INFRASTRUCTURE_PLATFORM_UI_BIND",
            "AELEV_INFRASTRUCTURE_PLATFORM_UI_LOCAL_BRIDGE_BIND",
        ),
    }
    for path, (primary_key, bridge_key) in routes.items():
        settings = read_env(path)
        primary = settings.get(primary_key)
        try:
            primary_address = ipaddress.ip_address(primary or "")
        except ValueError as error:
            raise QuickstartError(f"{primary_key} must be an IP address") from error
        if primary_address.is_unspecified or str(primary_address) == gateway:
            raise QuickstartError(
                f"{primary_key} must not overlap the Docker bridge listener"
            )
        value = settings.get(bridge_key)
        if value is None or value == "172.17.0.1":
            update_env(path, {bridge_key: gateway})


def initialize_keycloak() -> None:
    initialize_env(
        KEYCLOAK / ".env",
        {
            "AELEV_INFRASTRUCTURE_KEYCLOAK_BIND": "127.0.0.1",
            "AELEV_INFRASTRUCTURE_KEYCLOAK_HTTPS_PORT": "18443",
            "AELEV_INFRASTRUCTURE_KEYCLOAK_PUBLIC_URL": "https://127.0.0.1:18443",
            "AELEV_INFRASTRUCTURE_KEYCLOAK_ADMIN_ENDPOINT": "https://keycloak:8443",
            "AELEV_INFRASTRUCTURE_KEYCLOAK_UI_URL": "https://host.docker.internal:18082",
            "AELEV_INFRASTRUCTURE_KEYCLOAK_SECRET_GID": str(os.getgid()),
            "AELEV_INFRASTRUCTURE_KEYCLOAK_UI_SECRET_GID": str(os.getgid()),
            "AELEV_INFRASTRUCTURE_KEYCLOAK_UI_PROVISION_UID": str(os.getuid()),
        },
    )
    keycloak_env = read_env(KEYCLOAK / ".env")
    if (
        keycloak_env.get("AELEV_INFRASTRUCTURE_KEYCLOAK_ADMIN_ENDPOINT")
        == "https://127.0.0.1:18443"
    ):
        update_env(
            KEYCLOAK / ".env",
            {"AELEV_INFRASTRUCTURE_KEYCLOAK_ADMIN_ENDPOINT": ("https://keycloak:8443")},
        )
    secrets_root = KEYCLOAK / "secrets"
    write_private(
        secrets_root / "admin-password", secrets.token_urlsafe(32) + "\n", 0o640
    )
    write_private(
        secrets_root / "database-password", secrets.token_urlsafe(32) + "\n", 0o640
    )
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
    (tls / "tls.key").chmod(0o640)
    (tls / "tls.crt").chmod(0o644)
    (tls / "ca.crt").chmod(0o644)


def initialize_platform() -> None:
    network = read_env(KEYCLOAK / ".env")[
        "AELEV_INFRASTRUCTURE_KEYCLOAK_INGRESS_NETWORK"
    ]
    initialize_env(
        PLATFORM / ".env",
        {
            "AELEV_INFRASTRUCTURE_PLATFORM_UI_SECRET_GID": str(os.getgid()),
            "AELEV_INFRASTRUCTURE_PLATFORM_UI_KEYCLOAK_BACKCHANNEL_URL": "https://keycloak:8443",
            "AELEV_INFRASTRUCTURE_PLATFORM_UI_KEYCLOAK_INGRESS_NETWORK": network,
        },
    )
    platform_env = read_env(PLATFORM / ".env")
    updates: dict[str, str] = {}
    if (
        platform_env.get("AELEV_INFRASTRUCTURE_PLATFORM_UI_KEYCLOAK_BACKCHANNEL_URL")
        == "https://keycloak:18443"
    ):
        updates["AELEV_INFRASTRUCTURE_PLATFORM_UI_KEYCLOAK_BACKCHANNEL_URL"] = (
            "https://keycloak:8443"
        )
    if "AELEV_INFRASTRUCTURE_PLATFORM_UI_KEYCLOAK_INGRESS_NETWORK" not in platform_env:
        updates["AELEV_INFRASTRUCTURE_PLATFORM_UI_KEYCLOAK_INGRESS_NETWORK"] = network
    if updates:
        update_env(PLATFORM / ".env", updates)
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
            "preserving the existing PostgreSQL installation "
            "without restoring its offline issuer"
        )
        return
    if not issuer.exists():
        run([sys.executable, "prepare-tls.py"], cwd=POSTGRESQL)


def initialize() -> None:
    for executable in ("docker", "openssl"):
        if shutil.which(executable) is None:
            raise QuickstartError(f"required tool unavailable: {executable}")
    initialize_env(ROOT / ".env")
    initialize_owner_environments()
    initialize_additive_local_paths()
    migrate_legacy_translation_image_references()
    migrate_legacy_translation_ui_description_revision()
    initialize_translation_provider_credential()
    initialize_keycloak()
    initialize_minio()
    initialize_postgresql()
    initialize_platform()
    initialize_local_bridge_routes(docker_bridge_gateway())
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


def selected_pulls(capabilities: list[str]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            role
            for capability in PULLS
            if capability in set(capabilities)
            for role in PULLS[capability]
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
                "infrastructure project belongs to another installation; "
                "choose an explicit separate environment"
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
                    "translation_provider_included": "translation" in capabilities,
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
    pulls = selected_pulls(capabilities)
    if pulls:
        compose(ROOT, "pull", *pulls)
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
                "another lifecycle operation is pending; "
                "inspect runtime/lifecycle.lock before retrying"
            ) from None
        try:
            execute(args.command, args.capability or list(CAPABILITIES), args.no_build)
        finally:
            lock.rmdir()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(
            f"[infrastructure] {type(error).__name__}: "
            "operation failed; local state preserved",
            file=sys.stderr,
        )
        return 2
    except QuickstartError as error:
        print(f"[infrastructure] {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
