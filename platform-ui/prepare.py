"""Local configuration and explicit development PKI; never a network discovery tool."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
        "PROVISION_ADMIN_USER",
        "OPERATOR_USER",
    ):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", result[key]):
            raise ValueError(f"invalid identifier: {key}")
    if result["OPERATOR_USER"] != "platform-operator":
        raise ValueError("OPERATOR_USER must be platform-operator")
    for key in (
        "PORT",
        "SECRET_GID",
        "STATUS_TTL_SECONDS",
        "POLL_SECONDS",
        "JWKS_CACHE_SECONDS",
        "MAX_MODULES",
        "MAX_BODY_BYTES",
    ):
        if not result[key].isdecimal() or int(result[key]) <= 0:
            raise ValueError(f"positive integer required: {key}")
    return result


def directory(value: str, config: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else config.resolve().parent / path


def write(path: Path, content: str, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)


def render(config: Path) -> None:
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
        for filename in files:
            path = secret / role / filename
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError(f"missing required file: {role}/{filename}")
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
        write(
            runtime / name, json.dumps(data, ensure_ascii=False, indent=2) + "\n", 0o644
        )
    nginx = (
        (ROOT / "nginx.conf")
        .read_text()
        .replace("@@KEYCLOAK_ORIGIN@@", cfg["KEYCLOAK_URL"])
        .replace("@@MAX_BODY_BYTES@@", cfg["MAX_BODY_BYTES"])
    )
    write(runtime / "nginx.conf", nginx, 0o644)


def development_pki(config: Path, identity_ca: Path, admin_password: Path) -> None:
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
            + "\n",
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
    certificate("nginx", "client", "clientAuth")
    certificate("api", "server", "serverAuth", "DNS:platform-ui-api,IP:127.0.0.1")
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
    args = parser.parse_args()
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
        render(args.env)
    else:
        if not args.identity_ca or not args.admin_password_file:
            parser.error("development-pki requires identity CA and admin-password-file")
        development_pki(args.env, args.identity_ca, args.admin_password_file)


if __name__ == "__main__":
    main()
