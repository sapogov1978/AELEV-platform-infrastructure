#!/usr/bin/env python3
"""Prepare missing local-only Translator inputs without replacing operator state."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class PreparationError(RuntimeError):
    """A stable, secret-free local preparation failure."""


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key or key in values:
            raise PreparationError(f"TRANSLATOR_LOCAL_ENV_INVALID:{number}")
        values[key] = value
    return values


def owned_path(value: str) -> Path:
    path = (ROOT / value).resolve()
    if path != ROOT and ROOT not in path.parents:
        raise PreparationError("TRANSLATOR_LOCAL_PATH_OUTSIDE_OWNER")
    return path


def private_file(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    if path.exists():
        if (
            not path.is_file()
            or not path.read_text(encoding="utf-8").strip()
            or stat.S_IMODE(path.stat().st_mode) & 0o077
        ):
            raise PreparationError("TRANSLATOR_LOCAL_SECRET_INVALID")
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(value + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity-ca", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        env = read_env(ROOT / ".env")
        if (
            env.get("AELEV_INFRASTRUCTURE_TRANSLATOR_DATABASE_SSLMODE") != "disable"
            or env.get("AELEV_INFRASTRUCTURE_TRANSLATOR_KAFKA_SECURITY_PROTOCOL")
            != "PLAINTEXT"
            or env.get("AELEV_INFRASTRUCTURE_TRANSLATOR_OTLP_HTTP_ENDPOINT")
        ):
            raise PreparationError("TRANSLATOR_LOCAL_PROFILE_REQUIRED")
        database = owned_path(
            env["AELEV_INFRASTRUCTURE_TRANSLATOR_DATABASE_SECRET_ROOT"]
        )
        private_file(database / "postgres-password", secrets.token_urlsafe(32))
        for key in (
            "AELEV_INFRASTRUCTURE_TRANSLATOR_KAFKA_SECRET_ROOT",
            "AELEV_INFRASTRUCTURE_TRANSLATOR_KAFKA_ADMIN_SECRET_ROOT",
        ):
            owned_path(env[key]).mkdir(parents=True, exist_ok=True, mode=0o750)
        telemetry = owned_path(
            env["AELEV_INFRASTRUCTURE_TRANSLATOR_TELEMETRY_SECRET_ROOT"]
        )
        (telemetry / "client").mkdir(parents=True, exist_ok=True, mode=0o750)
        tls = owned_path(env["AELEV_INFRASTRUCTURE_TRANSLATOR_API_TLS_ROOT"])
        pki_root = tls.parent
        required = (
            pki_root / "api-ca/ca.crt",
            pki_root / "api-ca/ca.key",
            tls / "server.crt",
            tls / "server.key",
            tls / "clients-ca.crt",
            pki_root / "api-operator/operator.crt",
            pki_root / "api-operator/operator.key",
            pki_root / "api-operator/api-ca.crt",
            pki_root / "api-operator/operator.p12",
            pki_root / "api-operator/p12-password",
        )
        if pki_root.exists():
            if not all(path.is_file() and path.stat().st_size for path in required):
                raise PreparationError("TRANSLATOR_LOCAL_PKI_PARTIAL")
            private = (
                pki_root / "api-ca/ca.key",
                tls / "server.key",
                pki_root / "api-operator/operator.key",
                pki_root / "api-operator/operator.p12",
                pki_root / "api-operator/p12-password",
            )
            if any(stat.S_IMODE(path.stat().st_mode) & 0o077 for path in private):
                raise PreparationError("TRANSLATOR_LOCAL_PKI_PERMISSION_INVALID")
        else:
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "prepare-api-tls.py"),
                    "--local",
                    "--output",
                    str(pki_root),
                ],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if result.returncode:
                raise PreparationError("TRANSLATOR_LOCAL_PKI_PREPARATION_FAILED")
        identity_ca = arguments.identity_ca.resolve()
        if not identity_ca.is_file() or not identity_ca.stat().st_size:
            raise PreparationError("TRANSLATOR_LOCAL_IDENTITY_CA_MISSING")
        trusted_identity = tls / env["AELEV_INFRASTRUCTURE_TRANSLATOR_API_OIDC_CA_FILE"]
        if trusted_identity.exists():
            if trusted_identity.read_bytes() != identity_ca.read_bytes():
                raise PreparationError("TRANSLATOR_LOCAL_IDENTITY_CA_MISMATCH")
        else:
            shutil.copyfile(identity_ca, trusted_identity)
            trusted_identity.chmod(0o644)
        print(
            json.dumps(
                {
                    "status": "PREPARED",
                    "owner": "translator-service",
                    "existing_state_replaced": False,
                },
                sort_keys=True,
            )
        )
        return 0
    except (OSError, KeyError, PreparationError) as error:
        code = (
            str(error)
            if isinstance(error, PreparationError)
            else type(error).__name__
        )
        print(json.dumps({"status": "BLOCKED", "code": code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
