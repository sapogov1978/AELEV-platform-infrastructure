#!/usr/bin/env python3
"""Exercise local OIDC token issuance on fresh, isolated native owner products."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import ipaddress
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT.parents[1]


def module(name: str, path: Path) -> Any:
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError("ISOLATED_MODULE_MISSING")
    value = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(value)
    return value


def command(arguments: list[str], cwd: Path, output: Path) -> str:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("AELEV_")
    }
    result = subprocess.run(
        arguments, cwd=cwd, env=environment, capture_output=True, text=True, timeout=300
    )
    with output.open("a") as stream:
        stream.write(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError("ISOLATED_NATIVE_COMMAND_FAILED")
    return result.stdout


def port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def validate_scope(model: dict[str, Any], prefix: str, expected: str) -> None:
    if model["name"] != expected or any(
        not row["name"].startswith(prefix + "-")
        for section in ("volumes", "networks")
        for row in model.get(section, {}).values()
    ):
        raise RuntimeError("ISOLATED_RESOURCE_SCOPE_INVALID")


def spire_local_phase(
    output: Path, grafana: Path, identity_url: str, grafana_url: str, admin: Any
) -> dict[str, Any]:
    """Exercise the root coordinator against this real, newly issued Grafana."""
    sys.path.insert(0, str(INFRA))
    coordinator = module(
        "isolated_spire_local_coordinator", INFRA / "tools/spire_local.py"
    )

    local = module("isolated_spire_initial_inputs", INFRA / "spire/local-inputs.py")
    root = output / "root-operator"
    root.mkdir(mode=0o700)
    owner = root / "spire"
    (owner / "src/aelev_spire").mkdir(parents=True)
    for name in ("local-inputs.py", "bootstrap.py"):
        shutil.copyfile(INFRA / "spire" / name, owner / name)
    shutil.copyfile(
        INFRA / "spire/src/aelev_spire/observation-inputs.json",
        owner / "src/aelev_spire/observation-inputs.json",
    )
    presentation = root / "observability/grafana"
    presentation.mkdir(parents=True)
    shutil.copyfile(grafana / ".env", presentation / ".env")
    shutil.copyfile(ROOT / "local-publisher.py", presentation / "local-publisher.py")
    platform = root / "platform-ui"
    (platform / "secrets/provision").mkdir(parents=True)
    local.write(
        platform / ".env", b"AELEV_INFRASTRUCTURE_PLATFORM_UI_SECRET_ROOT=./secrets\n"
    )
    local.write(
        platform / "secrets/provision/operator-password",
        (output / "operator-password").read_bytes(),
    )
    # These are isolated PKI issuer operators. Only their newly issued owner
    # leaves and public chains enter the SPIRE input contract, never CA keys.
    issuers, issued, scratch = (
        root / "issuer-operators",
        root / "issued",
        root / "requests",
    )
    for path in (issuers, issued, scratch):
        path.mkdir(mode=0o700)
    binary = shutil.which("openssl")
    if binary is None:
        raise RuntimeError("ISOLATED_OPENSSL_REQUIRED")
    for scope in ("central", "publisher", "transport"):
        local.authority(binary, issuers, scope)
    credentials: dict[str, Any] = {}
    for name, scope in (
        ("central-write", "central"),
        ("status-publisher", "publisher"),
    ):
        local.issue(
            binary, issuers, scratch, issued / name, name, scope, "clientAuth", ""
        )
        credentials[name] = {
            "certificate": str(issued / (name + ".crt")),
            "key": str(issued / (name + ".key")),
            "issuer_certificate": str(issuers / (scope + ".crt")),
        }
    local.write(root / "admin-user", b"admin\n")
    config = {
        "schema_version": 1,
        "environment": "test",
        "server_plan": {
            "owner": "infrastructure-spire",
            "schema_version": 1,
            "environment": "test",
            "trust_domain": "test.example",
            "svid_ttl_seconds": 300,
        },
        "observation_plan": {
            "owner": "infrastructure-spire",
            "schema_version": 1,
            "sender_image": "aelev/spire-telemetry-sender:local",
            "central_url": "https://delivery:" + str(port()),
            "grafana_url": "https://grafana:" + str(urlsplit(grafana_url).port),
            "datasource_uid": "aelev-prometheus",
            "host_gateway_aliases": [
                "keycloak",
                "delivery",
                "grafana",
                "host.docker.internal",
            ],
            "presentation": {
                "url": "https://host.docker.internal:" + str(port()),
                "identity_url": "https://keycloak:" + str(urlsplit(identity_url).port),
                "realm": "aelev",
                "client_id": "infrastructure-spire-status",
                "audience": "platform-ui",
                "module_id": "294685b6-cb4f-502f-9c94-57ca8a3103b6",
            },
        },
        "issued": credentials,
        "trust": {
            "central": str(issuers / "central.crt"),
            "shell": str(issuers / "transport.crt"),
            "identity": str(output / "keycloak/secrets/https/ca.crt"),
            "grafana": str(grafana / "secrets/native/ca.crt"),
        },
        "status_admin": {
            "user": str(root / "admin-user"),
            "password": str(output / "keycloak/secrets/admin-password"),
        },
    }
    source = root / "local-input.json"
    coordinator.save(source, config)
    initial_count = admin.call("GET", "/api/serviceaccounts/search")["totalCount"]
    initial = coordinator.prepare(root, source)
    if (
        admin.call("GET", "/api/serviceaccounts/search")["totalCount"]
        != initial_count + 2
    ):
        raise RuntimeError("ISOLATED_SPIRE_INITIAL_GRANTS_INVALID")
    if (
        coordinator.prepare(root, source) != initial
        or admin.call("GET", "/api/serviceaccounts/search")["totalCount"]
        != initial_count + 2
    ):
        raise RuntimeError("ISOLATED_SPIRE_REPEATED_GRANT")
    api = module(
        "isolated_spire_grafana_api", ROOT / "examples/owner-metrics/grafana_api.py"
    )
    ca = output / "ca.pem"
    publisher = api.Client(
        grafana_url, ca, "Bearer " + api.read_secret(initial / "grafana/token")
    )
    provisioner = api.Client(
        grafana_url, ca, "Bearer " + api.read_secret(initial / "grafana-admin/token")
    )
    publisher.call(
        "POST",
        "/api/dashboards/db",
        {
            "folderUid": "infrastructure-spire",
            "dashboard": {
                "uid": "isolated-spire-inputs",
                "title": "SPIRE operator inputs",
                "schemaVersion": 41,
                "panels": [],
            },
        },
    )
    if not coordinator.retire(root) or not coordinator.retire(root):
        raise RuntimeError("ISOLATED_SPIRE_TEMPORARY_AUTHORITY_NOT_RETIRED")
    try:
        provisioner.call("GET", "/api/user")
    except api.ApiError as error:
        if error.status != 401:
            raise RuntimeError("ISOLATED_SPIRE_RETIREMENT_STATUS_INVALID") from None
    else:
        raise RuntimeError("ISOLATED_SPIRE_PROVISIONER_STILL_AUTHORIZED")
    if publisher.call("GET", "/api/user").get("isGrafanaAdmin") is not False:
        raise RuntimeError("ISOLATED_SPIRE_PUBLISHER_AUTHORITY_INVALID")
    print(
        "PASS: root SPIRE preparation, real scoped tokens, preserved repeat and retirement",
        flush=True,
    )
    return {
        "status": "PASS",
        "repeat_grants": 0,
        "retired_admin_http": 401,
        "publisher_valid": True,
        "native_spire_installed": False,
        "renewal_enrolled": False,
    }


def run(
    output: Path, status_image: str, *, retain_on_failure: bool = False
) -> dict[str, Any]:
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    log = output / "native.private.log"
    log.touch(mode=0o600)

    def native(*arguments: str, cwd: Path = output) -> str:
        return command(list(arguments), cwd, log)

    before = set(native("docker", "ps", "-aq").split())
    keycloak, grafana = output / "keycloak", output / "grafana"
    for path in (keycloak, grafana):
        path.mkdir(mode=0o700)
    for name in ("compose.yml", ".env.example"):
        shutil.copyfile(INFRA / "keycloak" / name, keycloak / name)
    for name in (
        "compose.yml",
        ".env.example",
        "prepare.py",
        "poetry.lock",
        "deployment.json",
    ):
        shutil.copyfile(ROOT / name, grafana / name)
    shutil.copytree(ROOT / "vendor", grafana / "vendor")
    shutil.copytree(ROOT / "config", grafana / "config")
    shutil.copytree(ROOT / "dashboards", grafana / "dashboards")
    bootstrap = module("isolated_infrastructure_bootstrap", INFRA / "bootstrap.py")
    oidc = module("isolated_grafana_oidc", ROOT / "oidc_provision.py")
    api = module("isolated_grafana_api", ROOT / "examples/owner-metrics/grafana_api.py")
    publisher = module("isolated_local_publisher", ROOT / "local-publisher.py")
    identity_port, grafana_port = port(), port()
    while identity_port == grafana_port:
        grafana_port = port()
    prefix = "aelev-grafana-oidc-" + uuid4().hex[:12]
    identity_url, grafana_url = (
        f"https://127.0.0.1:{identity_port}",
        f"https://localhost:{grafana_port}",
    )

    def image(reference: str) -> str:
        return native(
            "docker", "image", "inspect", "--format", "{{.Id}}", reference
        ).strip()

    identity_defaults = bootstrap.read_env(keycloak / ".env.example")
    grafana_defaults = bootstrap.read_env(grafana / ".env.example")
    identity_image = image(identity_defaults["AELEV_INFRASTRUCTURE_KEYCLOAK_IMAGE"])
    database_image = image(
        identity_defaults["AELEV_INFRASTRUCTURE_KEYCLOAK_DATABASE_IMAGE"]
    )
    grafana_image = image(grafana_defaults["AELEV_INFRASTRUCTURE_GRAFANA_IMAGE"])
    status_image = image(status_image)
    network_ids = native("docker", "network", "ls", "-q").split()
    networks = (
        json.loads(native("docker", "network", "inspect", *network_ids))
        if network_ids
        else []
    )
    existing = [
        ipaddress.ip_network(row["Subnet"])
        for item in networks
        for row in (item.get("IPAM", {}).get("Config") or [])
        if row.get("Subnet")
    ]
    selected: list[str] = []
    for number in range(256):
        subnet = ipaddress.ip_network(f"10.249.{number}.0/28")
        if not any(
            subnet.version == old.version and subnet.overlaps(old) for old in existing
        ):
            selected.append(str(subnet))
        if len(selected) == 3:
            break
    if len(selected) != 3:
        raise RuntimeError("ISOLATED_SUBNETS_UNAVAILABLE")
    bootstrap.KEYCLOAK = keycloak
    bootstrap.initialize_env(
        keycloak / ".env",
        {
            "AELEV_INFRASTRUCTURE_KEYCLOAK_COMPOSE_PROJECT": prefix + "-identity",
            "AELEV_INFRASTRUCTURE_KEYCLOAK_IMAGE": identity_image,
            "AELEV_INFRASTRUCTURE_KEYCLOAK_DATABASE_IMAGE": database_image,
            "AELEV_INFRASTRUCTURE_KEYCLOAK_BIND": "127.0.0.1",
            "AELEV_INFRASTRUCTURE_KEYCLOAK_HTTPS_PORT": str(identity_port),
            "AELEV_INFRASTRUCTURE_KEYCLOAK_PUBLIC_URL": identity_url,
            "AELEV_INFRASTRUCTURE_KEYCLOAK_DATABASE_VOLUME": prefix + "-identity-data",
            "AELEV_INFRASTRUCTURE_KEYCLOAK_UI_STATE_VOLUME": prefix
            + "-identity-status",
            "AELEV_INFRASTRUCTURE_KEYCLOAK_NETWORK": prefix + "-identity-private",
            "AELEV_INFRASTRUCTURE_KEYCLOAK_INGRESS_NETWORK": prefix
            + "-identity-ingress",
            "AELEV_INFRASTRUCTURE_KEYCLOAK_NETWORK_SUBNET": selected[0],
            "AELEV_INFRASTRUCTURE_KEYCLOAK_INGRESS_NETWORK_SUBNET": selected[1],
            "AELEV_INFRASTRUCTURE_KEYCLOAK_SECRET_GID": str(os.getgid()),
        },
    )
    # The public local owner initializer creates new TLS and database credentials.
    # Its OpenSSL diagnostics stay in this private evidence directory.
    with log.open("a") as stream:
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            bootstrap.initialize_keycloak()
    operator_password, client_secret = (
        secrets.token_urlsafe(32),
        secrets.token_urlsafe(32),
    )
    client = oidc.client_description("grafana", grafana_url)
    client["secret"] = client_secret
    realm = json.loads((INFRA / "keycloak/realm.json").read_text())
    realm.update(
        {
            "roles": {"realm": [{"name": "grafana.operator"}]},
            "clients": [client],
            "scopeMappings": [{"client": "grafana", "roles": ["grafana.operator"]}],
            "users": [
                {
                    "username": "platform-operator",
                    "enabled": True,
                    "firstName": "Isolated",
                    "lastName": "Operator",
                    "email": "operator@bootstrap.invalid",
                    "emailVerified": True,
                    "realmRoles": ["grafana.operator"],
                    "credentials": [
                        {
                            "type": "password",
                            "temporary": False,
                            "value": operator_password,
                        }
                    ],
                }
            ],
        }
    )
    publisher.save(keycloak / "realm.json", json.dumps(realm).encode())
    (keycloak / "realm.json").chmod(0o640)
    publisher.save(output / "operator-password", (operator_password + "\n").encode())
    bootstrap.initialize_env(
        grafana / ".env",
        {
            "AELEV_INFRASTRUCTURE_GRAFANA_COMPOSE_PROJECT": prefix + "-grafana",
            "AELEV_INFRASTRUCTURE_GRAFANA_IMAGE": grafana_image,
            "AELEV_INFRASTRUCTURE_GRAFANA_UI_IMAGE": status_image,
            "AELEV_INFRASTRUCTURE_GRAFANA_UI_NETWORK": prefix + "-grafana",
            "AELEV_INFRASTRUCTURE_GRAFANA_DATA_VOLUME": prefix + "-grafana-data",
            "AELEV_INFRASTRUCTURE_GRAFANA_UI_STATE_VOLUME": prefix + "-grafana-status",
            "AELEV_INFRASTRUCTURE_GRAFANA_DATA_OWNER": prefix,
            "AELEV_INFRASTRUCTURE_GRAFANA_BIND": "127.0.0.1",
            "AELEV_INFRASTRUCTURE_GRAFANA_PORT": str(grafana_port),
            "AELEV_INFRASTRUCTURE_GRAFANA_SECRET_GID": str(os.getgid()),
            "AELEV_INFRASTRUCTURE_GRAFANA_OIDC_BROWSER_URL": identity_url,
            "AELEV_INFRASTRUCTURE_GRAFANA_OIDC_INTERNAL_URL": "https://keycloak:8443",
        },
    )
    native(
        sys.executable,
        "prepare.py",
        "tls",
        "--issuer-dir",
        str(output / "issuer"),
        cwd=grafana,
    )
    native(sys.executable, "prepare.py", "render", cwd=grafana)
    (grafana / "secrets/oidc").mkdir(mode=0o750)
    bootstrap.write_private(
        grafana / "secrets/oidc/client-secret", client_secret, 0o640
    )
    (grafana / "trust").mkdir(mode=0o750)
    bootstrap.write_private(
        grafana / "trust/oidc-ca.crt",
        (keycloak / "secrets/https/ca.crt").read_bytes(),
        0o644,
    )
    trust = output / "ca.pem"
    publisher.save(
        trust,
        (keycloak / "secrets/https/ca.crt").read_bytes()
        + (grafana / "secrets/native/ca.crt").read_bytes(),
    )
    # Only this isolated test joins the two freshly created ingress networks.
    # Grafana's native TLS/OIDC/security configuration remains the owner model.
    # Compose's explicit reset tag removes only the test's host-gateway aliases.
    (grafana / "test.override.yml").write_text(
        "services:\n  grafana:\n    extra_hosts: !reset []\n    networks:\n      identity-test: {}\nnetworks:\n  identity-test:\n    external: true\n    name: "
        + prefix
        + "-identity-ingress\n  grafana-ui:\n    ipam:\n      config:\n        - subnet: "
        + selected[2]
        + "\n"
    )

    def compose(owner: Path, *arguments: str) -> str:
        files = ["-f", "compose.yml"]
        if owner == grafana:
            files += ["-f", "test.override.yml"]
        return native("docker", "compose", *files, *arguments, cwd=owner)

    def scope(owner: Path) -> None:
        model = json.loads(
            compose(owner, "--profile", "*", "config", "--format", "json")
        )
        expected = prefix + ("-identity" if owner == keycloak else "-grafana")
        validate_scope(model, prefix, expected)

    # Validate inactive roles as well: down --volumes sees declared volumes even
    # when their processes were never started. No shared default name is allowed.
    scope(keycloak)
    scope(grafana)

    def healthy(owner: Path, seconds: int = 180) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            rows = [
                json.loads(line)
                for line in compose(owner, "ps", "--format", "json").splitlines()
            ]
            expected = 2 if owner == keycloak else 1
            if len(rows) == expected and all(
                row["State"] == "running" and row["Health"] == "healthy" for row in rows
            ):
                return
            time.sleep(2)
        raise RuntimeError("ISOLATED_OWNER_READINESS_FAILED")

    credentials = [
        "--environment",
        "test",
        "--grafana-url",
        grafana_url,
        "--identity-url",
        identity_url,
        "--ca-file",
        str(trust),
        "--operator-password-file",
        str(output / "operator-password"),
    ]
    publisher.save(
        output / "sandbox.json",
        json.dumps(
            {
                "prefix": prefix,
                "keycloak": str(keycloak),
                "grafana": str(grafana),
                "identity_url": identity_url,
                "grafana_url": grafana_url,
            }
        ).encode(),
    )
    succeeded = False
    try:
        compose(keycloak, "up", "-d", "--no-build", "--pull", "never", "keycloak")
        healthy(keycloak)
        print("PASS: fresh isolated Keycloak and PostgreSQL", flush=True)
        compose(
            grafana,
            "--profile",
            "provision",
            "run",
            "--rm",
            "--no-deps",
            "grafana-configuration-init",
        )
        compose(grafana, "up", "-d", "--no-build", "--pull", "never", "grafana")
        healthy(grafana)
        print("PASS: fresh native HTTPS Grafana", flush=True)
        tokens = output / "tokens"
        native(
            sys.executable,
            str(ROOT / "local-publisher.py"),
            "issue",
            *credentials,
            "--folder",
            "owner-test",
            "--title",
            "Isolated owner",
            "--with-alert-provisioner",
            "--output",
            str(tokens),
        )
        admin = api.Client(
            grafana_url,
            trust,
            "Bearer " + api.read_secret(tokens / "provisioner.token"),
        )
        viewer = api.Client(
            grafana_url, trust, "Bearer " + api.read_secret(tokens / "publisher.token")
        )
        admin.call(
            "POST", "/api/folders", {"uid": "other-owner", "title": "Other owner"}
        )
        dashboard = {
            "uid": "isolated-owner",
            "title": "Isolated owner",
            "schemaVersion": 41,
            "panels": [],
        }
        viewer.call(
            "POST",
            "/api/dashboards/db",
            {"folderUid": "owner-test", "dashboard": dashboard},
        )
        for api_path, body in (
            (
                "/api/dashboards/db",
                {
                    "folderUid": "other-owner",
                    "dashboard": {**dashboard, "uid": "forbidden"},
                },
            ),
            ("/api/serviceaccounts", {"name": "forbidden", "role": "Admin"}),
        ):
            try:
                viewer.call("POST", api_path, body)
            except api.ApiError as error:
                if error.status != 403:
                    raise RuntimeError("ISOLATED_DENIAL_STATUS_INVALID") from None
            else:
                raise RuntimeError("ISOLATED_PUBLISHER_OVERPRIVILEGED")
        admin.call("GET", "/api/v1/provisioning/alert-rules")
        print(
            "PASS: own folder write, cross-owner/admin denial, separate provisioning authority",
            flush=True,
        )
        spire_inputs = spire_local_phase(
            output, grafana, identity_url, grafana_url, admin
        )
        native(
            sys.executable,
            str(ROOT / "local-publisher.py"),
            "retire-provisioner",
            *credentials,
            "--output",
            str(tokens),
        )
        try:
            admin.call("GET", "/api/user")
        except api.ApiError as error:
            if error.status != 401:
                raise RuntimeError("ISOLATED_RETIREMENT_STATUS_INVALID") from None
        else:
            raise RuntimeError("ISOLATED_PROVISIONER_STILL_AUTHORIZED")
        if viewer.call("GET", "/api/user").get("isGrafanaAdmin") is not False:
            raise RuntimeError("ISOLATED_PUBLISHER_IDENTITY_INVALID")
        print("PASS: temporary authority revoked, publisher remains valid", flush=True)
        result = {
            "status": "PASS",
            "prefix": prefix,
            "tls_verification": True,
            "basic_auth_enabled": False,
            "new_native_roles": 3,
            "identity_image": identity_image,
            "grafana_image": grafana_image,
            "database_image": database_image,
            "fresh_token_issuance": True,
            "own_folder_write": True,
            "other_folder_write_http": 403,
            "service_account_create_http": 403,
            "retired_admin_http": 401,
            "spire_local_inputs": spire_inputs,
        }
        succeeded = True
    finally:
        if succeeded or not retain_on_failure:
            scope(grafana)
            scope(keycloak)
            compose(grafana, "down", "--volumes")
            compose(keycloak, "down", "--volumes")
    after = set(native("docker", "ps", "-aq").split())
    if not before.issubset(after):
        raise RuntimeError("ISOLATED_BASELINE_RESOURCES_CHANGED")
    for resource in ("container", "volume", "network"):
        for project in (prefix + "-identity", prefix + "-grafana"):
            remaining = native(
                "docker",
                resource,
                "ls",
                "-aq" if resource == "container" else "-q",
                "--filter",
                "label=com.docker.compose.project=" + project,
            )
            if remaining.strip():
                raise RuntimeError("ISOLATED_RESOURCES_NOT_CLEANED")
    result["baseline_containers_preserved"] = True
    result["test_resources_removed"] = True
    publisher.save(
        output / "result.json", (json.dumps(result, indent=2) + "\n").encode()
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--status-image", required=True)
    parser.add_argument("--retain-on-failure", action="store_true")
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                run(
                    args.output.resolve(),
                    args.status_image,
                    retain_on_failure=args.retain_on_failure,
                )
            )
        )
        return 0
    except Exception as error:
        # Native diagnostics remain private; arbitrary HTTP bodies are not printed.
        if args.output.is_dir():
            path = args.output / "failure.private.txt"
            with os.fdopen(
                os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w"
            ) as stream:
                stream.write(traceback.format_exc())
        print(
            "ISOLATED_GRAFANA_OPERATOR_FAILED:" + type(error).__name__, file=sys.stderr
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
