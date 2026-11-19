"""Local configuration and explicit development PKI; never a network discovery tool."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from transport_settings import network_parameters

ROOT = Path(__file__).resolve().parent
PREFIX = "AELEV_INFRASTRUCTURE_PLATFORM_UI_"


def env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            if key in values:
                raise ValueError(f"duplicate setting: {key}")
            values[key] = value
    return values


def settings(path: Path) -> dict[str, str]:
    values = env(path)
    required = env(ROOT / ".env.example")
    if set(values) != set(required) or any(not value for value in values.values()):
        raise ValueError(
            ".env must contain exactly the nonempty settings from .env.example"
        )
    result = {key.removeprefix(PREFIX): value for key, value in values.items()}
    for key in ("PUBLIC_URL", "KEYCLOAK_URL", "KEYCLOAK_BACKCHANNEL_URL"):
        parsed = urlsplit(result[key])
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError(f"HTTPS origin required: {key}")
        result[key] = result[key].rstrip("/")
    for key in (
        "KEYCLOAK_REALM",
        "KEYCLOAK_CLIENT_ID",
        "KEYCLOAK_INGRESS_NETWORK",
        "PROVISION_ADMIN_USER",
        "OPERATOR_USER",
    ):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", result[key]):
            raise ValueError(f"invalid identifier: {key}")
    if result["OPERATOR_USER"] != "platform-operator":
        raise ValueError("OPERATOR_USER must be platform-operator")
    for key in (
        "PORT",
        "STATUS_TTL_SECONDS",
        "POLL_SECONDS",
        "JWKS_CACHE_SECONDS",
        "MAX_MODULES",
        "MAX_BODY_BYTES",
    ):
        if not result[key].isdecimal() or int(result[key]) <= 0:
            raise ValueError(f"positive integer required: {key}")
    if (
        not result["SECRET_GID"].isdecimal()
        or not 0 <= int(result["SECRET_GID"]) < 2**32 - 1
    ):
        raise ValueError("non-negative POSIX GID required: SECRET_GID")
    return result


def directory(value: str, config: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else config.resolve().parent / path


def write(path: Path, content: str, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)


def render(
    config: Path,
    *,
    role: str = "all",
    api_url: str | None = None,
    proxy_spiffe_id: str | None = None,
    api_tls_name: str | None = None,
) -> None:
    if role not in {"all", "nginx", "api", "provision"}:
        raise ValueError("explicit Platform UI role required")
    selected_role = role
    network = network_parameters(api_url, proxy_spiffe_id, api_tls_name)
    cfg = settings(config)
    runtime = directory(cfg["RUNTIME_ROOT"], config)
    secret = directory(cfg["SECRET_ROOT"], config)
    required = {
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
    for role, files in required.items():
        if selected_role != "all" and role != selected_role:
            continue
        for filename in files:
            path = secret / role / filename
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError(f"missing required file: {role}/{filename}")
    if network is not None:
        runtime.mkdir(parents=True, exist_ok=True)
        os.chown(runtime, -1, int(cfg["SECRET_GID"]))
        runtime.chmod(0o750)
    style = hashlib.sha256(
        (ROOT / "public/presentation/v6.css").read_bytes()
    ).hexdigest()
    public = {
        "keycloak_url": cfg["KEYCLOAK_URL"],
        "realm": cfg["KEYCLOAK_REALM"],
        "client_id": cfg["KEYCLOAK_CLIENT_ID"],
        "poll_seconds": int(cfg["POLL_SECONDS"]),
        "stylesheet_sha256": style,
    }
    api: dict[str, Any] = {
        "public_url": cfg["PUBLIC_URL"],
        "issuer": cfg["KEYCLOAK_URL"] + "/realms/" + cfg["KEYCLOAK_REALM"],
        "jwks_url": cfg["KEYCLOAK_BACKCHANNEL_URL"]
        + "/realms/"
        + cfg["KEYCLOAK_REALM"]
        + "/protocol/openid-connect/certs",
        "audience": cfg["KEYCLOAK_CLIENT_ID"],
        "ca": "/run/secrets/api/oidc-ca.crt",
        "database": "/var/lib/platform-ui/metadata.sqlite",
    }
    if network is not None:
        api["trusted_proxy_spiffe_id"] = network[2]
    for name in (
        "STATUS_TTL_SECONDS",
        "JWKS_CACHE_SECONDS",
        "MAX_MODULES",
        "MAX_BODY_BYTES",
    ):
        api[name.lower()] = int(cfg[name])
    provision = {
        "url": cfg["KEYCLOAK_BACKCHANNEL_URL"],
        "realm": cfg["KEYCLOAK_REALM"],
        "client_id": cfg["KEYCLOAK_CLIENT_ID"],
        "public_url": cfg["PUBLIC_URL"],
        "admin_user": cfg["PROVISION_ADMIN_USER"],
        "operator_user": cfg["OPERATOR_USER"],
    }
    for name, data in (
        ("config.json", public),
        ("api.json", api),
        ("provision.json", provision),
    ):
        owner = {
            "config.json": "nginx",
            "api.json": "api",
            "provision.json": "provision",
        }[name]
        if selected_role != "all" and owner != selected_role:
            continue
        write(
            runtime / name, json.dumps(data, ensure_ascii=False, indent=2) + "\n", 0o644
        )
    if selected_role not in {"all", "nginx"}:
        return
    nginx = (
        (ROOT / "nginx.conf")
        .read_text()
        .replace("@@KEYCLOAK_ORIGIN@@", cfg["KEYCLOAK_URL"])
        .replace("@@MAX_BODY_BYTES@@", cfg["MAX_BODY_BYTES"])
    )
    if network is not None:
        endpoint, tls_name, _ = network
        nginx = (
            nginx.replace(
                "  server_name _;",
                '  server_name _;\n  resolver 127.0.0.11 valid=10s;\n  resolver_timeout 3s;\n  set $platform_ui_api "'
                + endpoint
                + '";',
            )
            .replace(
                "proxy_pass https://platform-ui-api:8444/api/readyz;",
                "proxy_pass $platform_ui_api/api/proxy-readyz;",
            )
            .replace(
                "proxy_pass https://platform-ui-api:8444;",
                "proxy_pass $platform_ui_api$request_uri;",
            )
            .replace(
                "proxy_ssl_name platform-ui-api;", "proxy_ssl_name " + tls_name + ";"
            )
            .replace(
                "location = /config.json { try_files $uri =404; }",
                "location = /config.json { default_type application/json; alias /etc/nginx/conf.d/config.json; }",
            )
            .replace(
                "location = /readyz {",
                "location = /readyz {\n    proxy_intercept_errors on;\n    error_page 400 401 403 404 405 500 502 503 504 =503 @platform_ui_not_ready;",
            )
            .replace(
                "  location = /config.json",
                '  location @platform_ui_not_ready {\n    default_type application/json;\n    return 503 \'{"detail":"metadata API unavailable"}\';\n  }\n  location = /config.json',
            )
        )
    write(runtime / "nginx.conf", nginx, 0o644)


def development_pki(
    config: Path,
    identity_ca: Path,
    admin_password: Path,
    *,
    api_url: str | None = None,
    proxy_spiffe_id: str | None = None,
    api_tls_name: str | None = None,
) -> None:
    network = network_parameters(api_url, proxy_spiffe_id, api_tls_name)
    if shutil.which("openssl") is None:
        raise ValueError("OpenSSL must be installed before development-pki")
    cfg = settings(config)
    root = directory(cfg["SECRET_ROOT"], config)
    if any((root / role).exists() for role in ("nginx", "api", "provision", "issuer")):
        raise ValueError("refusing to overwrite an existing identity")
    for role in ("nginx", "api", "provision", "issuer"):
        (root / role).mkdir(parents=True, mode=0o750)
    gid = int(cfg["SECRET_GID"])

    def openssl(*args: str) -> None:
        subprocess.run(
            ["openssl", *args],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    issuer = root / "issuer"
    for name in ("transport", "publisher"):
        openssl(
            "req",
            "-x509",
            "-newkey",
            "rsa:3072",
            "-nodes",
            "-days",
            "30",
            "-subj",
            f"/CN=AELEV local {name}",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
            "-addext",
            "subjectKeyIdentifier=hash",
            "-addext",
            "authorityKeyIdentifier=keyid:always",
            "-keyout",
            str(issuer / f"{name}.key"),
            "-out",
            str(issuer / f"{name}.crt"),
        )

    def certificate(role: str, name: str, purpose: str, sans: str = "") -> None:
        path = root / role
        openssl(
            "req",
            "-new",
            "-newkey",
            "rsa:3072",
            "-nodes",
            "-subj",
            f"/CN={role}-{name}",
            "-keyout",
            str(path / f"{name}.key"),
            "-out",
            str(issuer / "request.csr"),
        )
        write(
            issuer / "extension",
            "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage="
            + purpose
            + ("\nsubjectAltName=" + sans if sans else "")
            + "\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n",
            0o600,
        )
        openssl(
            "x509",
            "-req",
            "-days",
            "30",
            "-in",
            str(issuer / "request.csr"),
            "-CA",
            str(issuer / "transport.crt"),
            "-CAkey",
            str(issuer / "transport.key"),
            "-CAcreateserial",
            "-extfile",
            str(issuer / "extension"),
            "-out",
            str(path / f"{name}.crt"),
        )

    hostname = urlsplit(cfg["PUBLIC_URL"]).hostname
    public_san = (
        "IP:" + str(hostname)
        if re.fullmatch(r"[0-9.]+", str(hostname))
        else "DNS:" + str(hostname)
    )
    certificate(
        "nginx",
        "server",
        "serverAuth",
        public_san + ",IP:127.0.0.1,DNS:host.docker.internal",
    )
    certificate("nginx", "client", "clientAuth", "URI:" + network[2] if network else "")
    api_sans = "DNS:platform-ui-api,IP:127.0.0.1"
    if network:
        api_host = str(urlsplit(network[0]).hostname)
        try:
            ipaddress.ip_address(api_host)
        except ValueError:
            api_sans += ",DNS:" + api_host
        else:
            api_sans += ",IP:" + api_host
        api_sans += ",DNS:" + network[1]
    certificate("api", "server", "serverAuth", api_sans)
    certificate("api", "health", "clientAuth")
    for role in ("nginx", "api"):
        shutil.copyfile(issuer / "transport.crt", root / role / "ca.crt")
    shutil.copyfile(issuer / "publisher.crt", root / "nginx/publisher-ca.crt")
    for role in ("api", "provision"):
        shutil.copyfile(identity_ca, root / role / "oidc-ca.crt")
    shutil.copyfile(admin_password, root / "provision/admin-password")
    write(root / "provision/operator-password", secrets.token_urlsafe(32) + "\n")
    for path in root.rglob("*"):
        if path.is_file():
            path.chmod(0o600 if path.parent == issuer else 0o640)
        os.chown(path, -1, gid)
    print("Local identities generated; issuer keys are not mounted into containers.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("init", "render", "development-pki"))
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    parser.add_argument("--identity-ca", type=Path)
    parser.add_argument("--admin-password-file", type=Path)
    parser.add_argument(
        "--role", choices=("all", "nginx", "api", "provision"), default="all"
    )
    parser.add_argument("--api-url")
    parser.add_argument("--api-tls-name")
    parser.add_argument("--proxy-spiffe-id")
    args = parser.parse_args()
    if args.action != "render" and args.role != "all":
        parser.error("--role applies only to render; development PKI is operator-only")
    if args.action == "init" and any(
        (args.api_url, args.api_tls_name, args.proxy_spiffe_id)
    ):
        parser.error("network parameters apply to render or development-pki")
    if args.action == "init":
        if args.env.exists():
            raise ValueError("existing .env is not overwritten; migrate explicitly")
        data = env(ROOT / ".env.example")
        data[PREFIX + "SECRET_GID"] = str(os.getgid())
        write(
            args.env,
            "\n".join(f"{key}={value}" for key, value in data.items()) + "\n",
            0o600,
        )
    elif args.action == "render":
        render(
            args.env,
            role=args.role,
            api_url=args.api_url,
            api_tls_name=args.api_tls_name,
            proxy_spiffe_id=args.proxy_spiffe_id,
        )
    else:
        if not args.identity_ca or not args.admin_password_file:
            parser.error("development-pki requires identity CA and admin-password-file")
        development_pki(
            args.env,
            args.identity_ca,
            args.admin_password_file,
            api_url=args.api_url,
            api_tls_name=args.api_tls_name,
            proxy_spiffe_id=args.proxy_spiffe_id,
        )


if __name__ == "__main__":
    main()
