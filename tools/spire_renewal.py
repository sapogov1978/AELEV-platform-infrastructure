"""Coordinate explicit SPIRE approvals through the public renewal CLI."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from tools import full_local, spire_local

OWNER = "infrastructure-spire"
PROFILES = {
    "status.json": {
        "metrics": ("observer-secrets", "metrics.crt"),
        "observer": ("observer-secrets", "observer.crt"),
        "central-write": ("sender-secrets", "central-write.crt"),
        "local-query": ("sender-secrets", "local-query.crt"),
        "local-write": ("sender-secrets", "local-write.crt"),
        "sender": ("sender-secrets", "sender.crt"),
    },
    "status-query.json": {"status-query": ("status-secrets", "query.crt")},
    "status-publisher.json": {"status-publisher": ("status-secrets", "publisher.crt")},
}
PUBLIC_SOURCE_PROFILE = {
    "public-source.json": {"public-source": ("public-source-secrets", "source.crt")},
}
ROLE_PROFILES = {
    "server": PUBLIC_SOURCE_PROFILE,
    "observer": {
        "observer.json": {
            key: PROFILES["status.json"][key] for key in ("metrics", "observer")
        }
    },
    "telemetry-sender": {
        "sender.json": {
            key: PROFILES["status.json"][key]
            for key in ("sender", "local-write", "local-query", "central-write")
        }
    },
    "status-publisher": {
        name: PROFILES[name] for name in ("status-query.json", "status-publisher.json")
    },
}
NETWORK_PROFILES = {
    name: mapping for role in ROLE_PROFILES.values() for name, mapping in role.items()
}


def profiles(value: dict[str, Any]) -> dict[str, dict[str, tuple[str, str]]]:
    names = set(value["profiles"])
    if value.get("schema_version") == 2:
        selected = ROLE_PROFILES.get(value.get("component", ""))
        require(
            selected is not None and names == set(selected), "COMPONENT_SCOPE_INVALID"
        )
        assert selected is not None
        return selected
    if names == set(NETWORK_PROFILES):
        return NETWORK_PROFILES
    require(names in (set(PROFILES), set(PROFILES) | set(PUBLIC_SOURCE_PROFILE)))
    return {
        **PROFILES,
        **(PUBLIC_SOURCE_PROFILE if "public-source.json" in names else {}),
    }


def require(value: Any, code: str = "CONFIGURATION_INVALID") -> None:
    if not value:
        raise full_local.FullLocalError("FULL_RUNTIME_SPIRE_RENEWAL_" + code)


def path(value: Any, *, existing: bool = True) -> Path:
    require(isinstance(value, str))
    selected = Path(value)
    require(
        selected.is_absolute()
        and selected == selected.resolve()
        and not any(p.is_symlink() for p in (selected, *selected.parents))
        and (not existing or selected.is_file())
    )
    return selected


def read(source: Path) -> dict[str, Any]:
    try:
        path(str(source))
        metadata = source.stat()
        require(
            stat.S_ISREG(metadata.st_mode)
            and (os.name == "nt" or not metadata.st_mode & 0o077)
            and metadata.st_size <= 65536
        )
        result: dict[str, Any] = json.loads(source.read_text())
        require(isinstance(result, dict))
        return result
    except (OSError, ValueError, TypeError):
        raise full_local.FullLocalError(
            "FULL_RUNTIME_SPIRE_RENEWAL_CONFIGURATION_INVALID"
        ) from None


def configuration(source: Path) -> dict[str, Any]:
    """Validate independent approvals without copying any issuer key."""
    try:
        value = read(source)
        require(
            set(value) - {"component"}
            == {
                "schema_version",
                "environment",
                "maintenance",
                "profiles",
                "helper_image",
            }
            and type(value["schema_version"]) is int
            and value["schema_version"] in {1, 2}
            and ((value["schema_version"] == 2) == ("component" in value))
            and value["environment"] in {"local", "test"}
            and value["maintenance"] in {"launchd", "operator"}
            and isinstance(value["profiles"], dict)
            and isinstance(value["helper_image"], str)
            and re.fullmatch(r"sha256:[a-f0-9]{64}", value["helper_image"])
        )
        require(
            value["maintenance"] != "launchd" or sys.platform == "darwin",
            "MACOS_TIMER_REQUIRED",
        )
        roots: list[Path] = []
        certificates: set[Path] = set()
        issuers: dict[str, set[str]] = {}
        for name, destinations in profiles(value).items():
            profile = value["profiles"][name]
            require(isinstance(profile, dict) and set(profile) == {"approval", "state"})
            state = path(profile["state"], existing=False)
            require(state.parent.is_dir() and (not state.exists() or state.is_dir()))
            if os.name != "nt":
                require(
                    not (state if state.exists() else state.parent).stat().st_mode
                    & 0o077
                )
            config = read(path(profile["approval"]))
            require(
                set(config)
                == {
                    "schema_version",
                    "environment",
                    "consumer_owner",
                    "openssl",
                    "report",
                    "certificates",
                }
                and type(config["schema_version"]) is int
                and config["schema_version"] == 1
                and config["environment"] == value["environment"]
                and config["consumer_owner"] == OWNER
                and isinstance(config["certificates"], list)
                and len(config["certificates"]) == len(destinations)
                and {entry["id"] for entry in config["certificates"]}
                == set(destinations)
            )
            report = path(config["report"], existing=False)
            require(report.parent.is_dir() and report.name == name)
            require(os.name == "nt" or not report.parent.stat().st_mode & 0o077)
            roots.extend((state, report.parent))
            issuers[name] = set()
            for entry in config["certificates"]:
                require(
                    set(entry)
                    == {"id", "certificate", "key", "issuer_certificate", "issuer_key"}
                )
                certificate = path(entry["certificate"])
                require(certificate not in certificates, "APPROVAL_SCOPE_OVERLAP")
                certificates.add(certificate)
                # Detect shared publisher authority even when copied to another path.
                for field in ("issuer_certificate", "issuer_key"):
                    material = path(entry[field])
                    require(material.stat().st_size <= 65536)
                    issuers[name].add(hashlib.sha256(material.read_bytes()).hexdigest())
        require(
            not issuers.get("status-publisher.json", set())
            & (
                issuers.get("status.json", set())
                | issuers.get("status-query.json", set())
                | issuers.get("observer.json", set())
                | issuers.get("sender.json", set())
            ),
            "APPROVAL_SCOPE_OVERLAP",
        )
        require(
            all(
                not a.is_relative_to(b) and not b.is_relative_to(a)
                for i, a in enumerate(roots)
                for b in roots[i + 1 :]
            ),
            "APPROVAL_SCOPE_OVERLAP",
        )
        return value
    except (OSError, ValueError, TypeError, KeyError):
        raise full_local.FullLocalError(
            "FULL_RUNTIME_SPIRE_RENEWAL_CONFIGURATION_INVALID"
        ) from None


def invoke(arguments: list[str], root: Path, directory: Path) -> Any:
    # BUSY means the public CLI refused before taking its state lock. A loaded
    # minute task may overlap an explicit operator action; unknown results never
    # take this path and are never repeated.
    for attempt in range(21):
        try:
            return execute(arguments, root, directory)
        except full_local.FullLocalError as error:
            if (
                str(error) != "FULL_RUNTIME_SPIRE_RENEWAL_TELEMETRY_RENEWAL_BUSY"
                or attempt == 20
            ):
                raise
            time.sleep(1)
    raise AssertionError("unreachable")


def execute(arguments: list[str], root: Path, directory: Path) -> Any:
    diagnostic = directory / ("operation-" + uuid4().hex)
    try:
        result = subprocess.run(arguments, cwd=root, capture_output=True, timeout=600)
    except subprocess.TimeoutExpired:
        raise full_local.FullLocalError("FULL_RUNTIME_COMMAND_RESULT_UNKNOWN") from None
    except OSError:
        raise full_local.FullLocalError(
            "FULL_RUNTIME_SPIRE_RENEWAL_OPERATOR_UNAVAILABLE"
        ) from None
    spire_local.private(diagnostic.with_suffix(".stdout"), result.stdout)
    spire_local.private(diagnostic.with_suffix(".stderr"), result.stderr)
    try:
        value = json.loads(result.stdout)
    except ValueError:
        # A truncated/killed CLI can have completed a durable effect. Empty or
        # malformed output is not evidence of failure and must not be replayed.
        raise full_local.FullLocalError("FULL_RUNTIME_COMMAND_RESULT_UNKNOWN") from None
    if result.returncode:
        code = value.get("code") if isinstance(value, dict) else None
        require(
            False,
            code
            if isinstance(code, str)
            and re.fullmatch(r"TELEMETRY_[A-Z_0-9]{1,96}", code)
            else "OPERATOR_FAILED",
        )
    return value


def binding(
    root: Path, native: dict[str, dict[str, Any]], directory: Path, helper_image: str
) -> dict[str, Any]:
    """Use logical installation labels, never the physical Compose project name."""
    labels = [row["Config"]["Labels"] for row in native.values()]
    installations = {row.get("io.aelev.installation") for row in labels}
    require(
        len(installations) == 1
        and all(row.get("io.aelev.owner") == OWNER for row in labels),
        "INSTALLATION_INVALID",
    )
    project = next(iter(installations))
    require(
        isinstance(project, str) and re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", project),
        "INSTALLATION_INVALID",
    )
    executable = shutil.which("docker")
    if executable is None:
        raise full_local.FullLocalError("FULL_RUNTIME_SPIRE_RENEWAL_DOCKER_REQUIRED")
    docker = str(Path(executable).resolve())
    endpoint = (
        os.environ.get("DOCKER_HOST") if not os.environ.get("DOCKER_CONTEXT") else None
    )
    if endpoint is None:
        endpoint = invoke(
            [
                docker,
                "context",
                "inspect",
                "--format",
                "{{json .Endpoints.docker.Host}}",
            ],
            root,
            directory,
        )
    require(
        isinstance(endpoint, str) and re.fullmatch(r"unix:///[^\r\n\x00]+", endpoint),
        "LOCAL_DAEMON_REQUIRED",
    )
    identity = invoke(
        [docker, "--host", endpoint, "info", "--format", '{"id":{{json .ID}}}'],
        root,
        directory,
    )
    return {
        "schema_version": 2,
        "consumer_owner": OWNER,
        "installation_id": project,
        "project": project,
        "docker": docker,
        "docker_endpoint": endpoint,
        "daemon_id": identity["id"],
        "image": helper_image,
    }


def apply(
    root: Path, source: Path, native: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Enroll, bind, deliver and check; never fabricate state, reports or timers."""
    config = configuration(source)
    directory = root / "runtime/spire-renewal"
    require(not directory.is_symlink(), "OUTPUT_INVALID")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(os.name == "nt" or not directory.stat().st_mode & 0o077, "OUTPUT_INVALID")
    receipt_path = directory / "receipt.json"
    scope = {
        "configuration": config,
        "approvals": {
            name: read(Path(profile["approval"]))
            for name, profile in config["profiles"].items()
        },
    }
    receipt: dict[str, Any] = {"scope": scope, "status": "PREPARING"}
    if receipt_path.exists():
        previous = read(receipt_path)
        require(previous.get("scope") == scope, "SCOPE_CHANGED")
        require(previous.get("status") != "INSPECTION_REQUIRED", "INSPECTION_REQUIRED")
    else:
        spire_local.save(receipt_path, receipt)
    common = binding(root, native, directory, config["helper_image"])
    cli = [sys.executable, str(root / "observability/delivery/renewal.py")]
    labels: list[str] = []
    try:
        selected_profiles = profiles(config)
        for name, destinations in selected_profiles.items():
            profile = config["profiles"][name]
            Path(profile["state"]).mkdir(mode=0o700, exist_ok=True)
            state = ["--state", profile["state"]]
            invoke(
                [*cli, "enroll", *state, "--input", profile["approval"]],
                root,
                directory,
            )
            spec = {
                **common,
                "destinations": [
                    {
                        "id": key,
                        "volume": common["project"] + "_" + role,
                        "role": role,
                        "file": file,
                    }
                    for key, (role, file) in destinations.items()
                ],
                "report": {
                    "volume": common["project"] + "_certificate-renewal-status",
                    "role": "certificate-renewal-status",
                    "file": name,
                },
            }
            target = directory / (name + ".binding.json")
            if target.exists():
                require(read(target) == spec, "BINDING_CHANGED")
            else:
                spire_local.save(target, spec)
            invoke(
                [*cli, "bind-delivery", *state, "--input", str(target)], root, directory
            )
            invoke([*cli, "once", *state], root, directory)
            checked = invoke(
                [*cli, "check", *state, "--max-age", "180"], root, directory
            )
            require(
                checked.get("code") == "TELEMETRY_RENEWAL_INSTALLATION_READY"
                and checked.get("renewal_healthy") is True,
                "RECEIPT_NOT_READY",
            )
            if config["maintenance"] == "launchd":
                timer = invoke([*cli, "install", *state], root, directory)
                require(
                    timer.get("code") == "TELEMETRY_RENEWAL_TIMER_INSTALLED"
                    and isinstance(timer.get("label"), str),
                    "TIMER_UNCONFIRMED",
                )
                labels.append(timer["label"])
        receipt["status"] = "FRESH_RECEIPTS_VERIFIED"
        spire_local.save(receipt_path, receipt, replace=True)
    except full_local.FullLocalError as error:
        if str(error) == "FULL_RUNTIME_COMMAND_RESULT_UNKNOWN":
            receipt["status"] = "INSPECTION_REQUIRED"
            spire_local.save(receipt_path, receipt, replace=True)
        raise
    return {
        "status": "FRESH_RECEIPTS_VERIFIED",
        "profiles": sorted(selected_profiles),
        "automatic_maintenance_verified": config["maintenance"] == "launchd"
        and len(set(labels)) == len(selected_profiles),
        "timer_labels": labels,
    }
