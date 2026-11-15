"""Coordinate explicit local SPIRE inputs and the Grafana operator interface."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from tools import full_local


def configuration(path: Path) -> dict[str, Any]:
    try:
        mode = path.lstat().st_mode
        if (
            not stat.S_ISREG(mode)
            or (os.name != "nt" and mode & 0o077)
            or path.stat().st_size > 65536
        ):
            raise ValueError
        value: dict[str, Any] = json.loads(path.read_text())
        if not isinstance(value, dict) or value.get("environment") not in {
            "local",
            "test",
        }:
            raise ValueError
        for name in ("server_plan", "observation_plan"):
            if (
                value[name]["owner"] != "infrastructure-spire"
                or value[name]["schema_version"] != 1
            ):
                raise ValueError
        if value["server_plan"]["environment"] != value["environment"]:
            raise ValueError
        return value
    except (OSError, ValueError, KeyError, TypeError):
        raise full_local.FullLocalError(
            "FULL_RUNTIME_SPIRE_LOCAL_CONFIGURATION_INVALID"
        ) from None


def private(path: Path, raw: bytes, *, replace: bool = False) -> None:
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


def save(path: Path, value: dict[str, Any], *, replace: bool = False) -> None:
    private(
        path,
        (json.dumps(value, sort_keys=True, indent=2) + "\n").encode(),
        replace=replace,
    )


def invoke(
    arguments: list[str], root: Path, directory: Path, phase: str
) -> dict[str, Any]:
    diagnostic = directory / (phase + "-" + uuid4().hex)
    try:
        result = subprocess.run(
            arguments, cwd=root, capture_output=True, text=True, timeout=600
        )
    except subprocess.TimeoutExpired:
        private(
            diagnostic.with_suffix(".unknown"), b"FULL_RUNTIME_COMMAND_RESULT_UNKNOWN\n"
        )
        raise full_local.FullLocalError("FULL_RUNTIME_COMMAND_RESULT_UNKNOWN") from None
    private(diagnostic.with_suffix(".stdout"), result.stdout.encode())
    private(diagnostic.with_suffix(".stderr"), result.stderr.encode())
    if result.returncode:
        code = result.stderr.strip()
        if re.fullmatch(r"(?:SPIRE_LOCAL|GRAFANA_LOCAL)_[A-Z_0-9]{1,96}", code):
            if code in {
                "GRAFANA_LOCAL_RESULT_UNKNOWN",
                "SPIRE_LOCAL_PKI_OPERATION_UNKNOWN",
            }:
                raise full_local.FullLocalError("FULL_RUNTIME_COMMAND_RESULT_UNKNOWN")
            raise full_local.FullLocalError("FULL_RUNTIME_" + code)
        raise full_local.FullLocalError("FULL_RUNTIME_SPIRE_LOCAL_PREPARATION_FAILED")
    try:
        value: dict[str, Any] = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except ValueError:
        raise full_local.FullLocalError(
            "FULL_RUNTIME_SPIRE_LOCAL_RESPONSE_INVALID"
        ) from None


def operator_arguments(
    root: Path, config: dict[str, Any], directory: Path
) -> list[str]:
    values = full_local.read_env(root / "observability/grafana/.env", root)
    prefix = "AELEV_INFRASTRUCTURE_GRAFANA_"
    port = values[prefix + "PORT"]
    # A credential issued in this Grafana must not be delivered to a different
    # operator-selected Grafana. External deployments supply their own tokens.
    if config["observation_plan"]["grafana_url"] not in {
        "https://grafana:" + port,
        "https://host.docker.internal:" + port,
    }:
        raise full_local.FullLocalError(
            "FULL_RUNTIME_SPIRE_LOCAL_GRAFANA_TARGET_MISMATCH"
        )
    platform = root / "platform-ui"
    settings = full_local.read_env(platform / ".env", root)
    secret_root = (
        platform / settings["AELEV_INFRASTRUCTURE_PLATFORM_UI_SECRET_ROOT"]
    ).resolve()
    password = secret_root / "provision/operator-password"
    if password.is_symlink() or not password.is_file():
        raise full_local.FullLocalError(
            "FULL_RUNTIME_SPIRE_LOCAL_OPERATOR_SECRET_INVALID"
        )
    private(directory / "operator-password", password.read_bytes())
    private(
        directory / "operator-ca.pem",
        Path(config["trust"]["grafana"]).read_bytes()
        + Path(config["trust"]["identity"]).read_bytes(),
    )
    return [
        "--environment",
        config["environment"],
        "--grafana-url",
        "https://" + values[prefix + "PUBLIC_HOST"] + ":" + port,
        "--identity-url",
        values[prefix + "OIDC_BROWSER_URL"],
        "--realm",
        values[prefix + "OIDC_REALM"],
        "--ca-file",
        str(directory / "operator-ca.pem"),
        "--operator-password-file",
        str(directory / "operator-password"),
    ]


def prepare(root: Path, source: Path) -> Path:
    config = configuration(source)
    directory = root / "runtime/spire-local-input"
    receipt_path = directory / "receipt.json"
    owner_input = directory / "owner-input.json"
    material = directory / "material"
    if directory.is_symlink():
        raise full_local.FullLocalError("FULL_RUNTIME_SPIRE_LOCAL_OUTPUT_INVALID")
    if directory.exists():
        try:
            receipt = configuration_receipt(receipt_path)
            if receipt["source"] != str(source.resolve()):
                raise ValueError
            if receipt["configuration"] != config:
                raise ValueError
            if receipt["status"] not in {"BUNDLE_PREPARED", "PROVISIONER_RETIRED"}:
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            raise full_local.FullLocalError(
                "FULL_RUNTIME_SPIRE_LOCAL_OPERATION_INSPECTION_REQUIRED"
            ) from None
    else:
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        receipt = {
            "status": "PREPARING",
            "source": str(source.resolve()),
            "configuration": config,
            "operator_arguments": [],
        }
        save(receipt_path, receipt)
        selected = dict(config)
        if "grafana_tokens" not in selected:
            arguments = operator_arguments(root, config, directory)
            receipt["operator_arguments"] = arguments
            save(receipt_path, receipt, replace=True)
            result = invoke(
                [
                    sys.executable,
                    str(root / "observability/grafana/local-publisher.py"),
                    "issue",
                    *arguments,
                    "--folder",
                    "infrastructure-spire",
                    "--title",
                    "SPIRE",
                    "--with-alert-provisioner",
                    "--output",
                    str(directory / "grafana-tokens"),
                ],
                root,
                directory,
                "grafana-issue",
            )
            if (
                result.get("status") != "TOKENS_ISSUED"
                or result.get("folder") != "infrastructure-spire"
            ):
                raise full_local.FullLocalError(
                    "FULL_RUNTIME_SPIRE_LOCAL_RESPONSE_INVALID"
                )
            selected["grafana_tokens"] = {
                "publisher": str(directory / "grafana-tokens/publisher.token"),
                "provisioner": str(directory / "grafana-tokens/provisioner.token"),
            }
        save(owner_input, selected)
    result = invoke(
        [
            sys.executable,
            str(root / "spire/local-inputs.py"),
            "--input",
            str(owner_input),
            "--output",
            str(material),
        ],
        root,
        directory,
        "owner-prepare",
    )
    expected = material / "initial-input"
    if result.get("status") != "LOCAL_INITIAL_BUNDLE_PREPARED" or result.get(
        "initial_input"
    ) != str(expected):
        raise full_local.FullLocalError("FULL_RUNTIME_SPIRE_LOCAL_RESPONSE_INVALID")
    if receipt["status"] == "PREPARING":
        receipt["status"] = "BUNDLE_PREPARED"
        save(receipt_path, receipt, replace=True)
    return expected


def configuration_receipt(path: Path) -> dict[str, Any]:
    mode = path.lstat().st_mode
    if (
        not stat.S_ISREG(mode)
        or (os.name != "nt" and mode & 0o077)
        or path.stat().st_size > 65536
    ):
        raise full_local.FullLocalError("FULL_RUNTIME_SPIRE_LOCAL_RECEIPT_INVALID")
    value: dict[str, Any] = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise full_local.FullLocalError("FULL_RUNTIME_SPIRE_LOCAL_RECEIPT_INVALID")
    return value


def retire(root: Path) -> bool:
    directory = root / "runtime/spire-local-input"
    receipt_path = directory / "receipt.json"
    receipt = configuration_receipt(receipt_path)
    if receipt["status"] == "PROVISIONER_RETIRED":
        return True
    if receipt["status"] != "BUNDLE_PREPARED":
        raise full_local.FullLocalError(
            "FULL_RUNTIME_SPIRE_LOCAL_OPERATION_INSPECTION_REQUIRED"
        )
    if not receipt["operator_arguments"]:
        return False
    if receipt["operator_arguments"]:
        result = invoke(
            [
                sys.executable,
                str(root / "observability/grafana/local-publisher.py"),
                "retire-provisioner",
                *receipt["operator_arguments"],
                "--output",
                str(directory / "grafana-tokens"),
            ],
            root,
            directory,
            "grafana-retire",
        )
        if result.get("status") != "PROVISIONER_RETIRED":
            raise full_local.FullLocalError("FULL_RUNTIME_SPIRE_LOCAL_RESPONSE_INVALID")
    receipt["status"] = "PROVISIONER_RETIRED"
    save(receipt_path, receipt, replace=True)
    return True
