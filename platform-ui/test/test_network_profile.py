"""Owner network configuration tests; synthetic files are not TLS proof."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from prepare import PREFIX, env, render
from transport_settings import api_probe_url, https_origin, network_parameters

ROOT = Path(__file__).resolve().parents[1]
PROXY = "spiffe://qualification.test/platform-ui/nginx"
FILES = {
    "nginx": (
        "server.crt",
        "server.key",
        "client.crt",
        "client.key",
        "ca.crt",
        "publisher-ca.crt",
    ),
    "api": (
        "server.crt",
        "server.key",
        "ca.crt",
        "oidc-ca.crt",
        "health.crt",
        "health.key",
    ),
    "provision": ("oidc-ca.crt", "admin-password", "operator-password"),
}


def base_config(tmp_path: Path, roles: tuple[str, ...]) -> Path:
    values = env(ROOT / ".env.example")
    values[PREFIX + "SECRET_ROOT"] = str(tmp_path / "secrets")
    values[PREFIX + "RUNTIME_ROOT"] = str(tmp_path / "runtime")
    values[PREFIX + "SECRET_GID"] = "20"
    for role in roles:
        for name in FILES[role]:
            path = tmp_path / "secrets" / role / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic configuration fixture")
    path = tmp_path / "base.env"
    path.write_text("".join(key + "=" + value + "\n" for key, value in values.items()))
    return path


def test_legacy_render_matches_pre_network_source_bytes(tmp_path: Path) -> None:
    render(base_config(tmp_path, ("nginx", "api", "provision")))
    # Derived from actual render at immutable source 696a190, not the new renderer.
    expected = {
        "api.json": "400280990a5439a90b9724e6bff1f00e736870aa3ad93920e4213131c1cd655c",
        "config.json": "56437d8963565bb09f5e6dd44c4c76f17105baa116a20b1364cf1a30eeaa2a4a",
        "nginx.conf": "5966fe0a5ce0aed734192d131a5cbdfc77c8a1639287240205cccff81d1d923d",
        "provision.json": "52df6e2e5bb7a1836837696eae41d60a0faeab5685b9ba81e587600cc3df1680",
    }
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (tmp_path / "runtime").iterdir()
    } == expected


@pytest.mark.parametrize(
    "role,outputs",
    [
        ("nginx", {"nginx.conf", "config.json"}),
        ("api", {"api.json"}),
        ("provision", {"provision.json"}),
    ],
)
def test_role_render_uses_only_its_own_secrets_without_site_packages(
    tmp_path: Path, role: str, outputs: set[str]
) -> None:
    config = base_config(tmp_path, (role,))
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            str(ROOT / "prepare.py"),
            "render",
            "--env",
            str(config),
            "--role",
            role,
            "--api-url",
            "https://api.node.example:19444",
            "--proxy-spiffe-id",
            PROXY,
        ],
        capture_output=True,
        text=True,
        umask=0o077,
    )
    assert result.returncode == 0, result.stderr
    runtime = tmp_path / "runtime"
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o750
    assert runtime.stat().st_gid == 20
    assert {path.name for path in runtime.iterdir()} == outputs
    assert {path.name for path in (tmp_path / "secrets").iterdir()} == {role}
    if role == "api":
        assert (
            json.loads((runtime / "api.json").read_text())["trusted_proxy_spiffe_id"]
            == PROXY
        )
    if role == "nginx":
        text = (runtime / "nginx.conf").read_text()
        assert 'set $platform_ui_api "https://api.node.example:19444";' in text
        assert "proxy_pass $platform_ui_api$request_uri;" in text
        assert "proxy_pass $platform_ui_api/api/proxy-readyz;" in text
        assert "=503 @platform_ui_not_ready;" in text
        assert "proxy_ssl_name api.node.example;" in text
        assert "alias /etc/nginx/conf.d/config.json;" in text
        assert "proxy_ssl_verify on;" in text


@pytest.mark.parametrize(
    "value",
    [
        "http://api.example:8444",
        "https://user:password@api.example",
        "https://api.example/path",
        "https://api.example?",
        "https://api.example#",
        "https://api.example:",
        "https://api.example:0",
        "https://api.example;return",
        "https://127.1",
        "https://api.example\n",
        "https://a\\b",
    ],
)
def test_network_origins_are_unambiguous_and_tls_only(value: str) -> None:
    with pytest.raises(ValueError):
        https_origin(value)


def test_ip_route_requires_separate_dns_certificate_name() -> None:
    with pytest.raises(ValueError, match="DNS TLS name"):
        network_parameters("https://192.0.2.10:8444", PROXY, None)
    assert network_parameters("https://192.0.2.10:8444", PROXY, "api.node.example") == (
        "https://192.0.2.10:8444",
        "api.node.example",
        PROXY,
    )


@pytest.mark.parametrize(
    "value",
    [
        "https://api.example/api/healthz",
        "http://api.example/api/readyz",
        "https://api.example/api/readyz?",
        "https://api.example/api/readyz\n",
    ],
)
def test_probe_is_exact_https_readiness(value: str) -> None:
    with pytest.raises(ValueError):
        api_probe_url(value)


def test_missing_network_identity_fails_before_render_outputs(tmp_path: Path) -> None:
    config = base_config(tmp_path, ("nginx",))
    with pytest.raises(ValueError, match="requires API URL and proxy"):
        render(config, role="nginx", api_url="https://api.example:8444")
    assert not (tmp_path / "runtime").exists()


def test_network_compose_publishes_api_and_keeps_whole_owned_directories() -> None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("AELEV_", "COMPOSE_"))
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ROOT / ".env.example"),
            "--env-file",
            str(ROOT / "network.env.example"),
            "-f",
            str(ROOT / "compose.yml"),
            "-f",
            str(ROOT / "compose.network.yml"),
            "--profile",
            "*",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    model: dict[str, Any] = json.loads(result.stdout)
    services = model["services"]
    assert len(services) == 5
    assert services["platform-ui-api"]["ports"][0]["target"] == 8444
    assert (
        services["platform-ui-api"]["environment"][PREFIX + "TRANSPORT_PROFILE"]
        == "network-mtls"
    )
    assert "depends_on" not in services["platform-ui"]
    ui_targets = {mount["target"] for mount in services["platform-ui"]["volumes"]}
    api_targets = {mount["target"] for mount in services["platform-ui-api"]["volumes"]}
    assert ui_targets == {"/run/secrets/nginx", "/etc/nginx/conf.d"}
    assert api_targets == {
        "/run/secrets/api",
        "/etc/platform-ui",
        "/var/lib/platform-ui",
    }
    assert "keycloak" not in services["platform-ui-api"].get("extra_hosts", {})
