"""Public standard-library transport used by the two renewal operators.

This is operator-only transport, not a certificate issuer or a recovery timer.
The consumer helper owns validation and atomic publication inside its volumes.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import plistlib
import subprocess
import stat
import sys
import uuid
from typing import Any

LIMIT = 1024 * 1024


def validate_cache(directory: Path) -> None:
    """Allow only private CPython caches left by the previous installed task."""
    if not directory.exists() and not directory.is_symlink():
        return
    metadata = directory.lstat()
    need(
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and not metadata.st_mode & 0o077
    )
    for path in directory.iterdir():
        metadata = path.lstat()
        need(
            re.fullmatch(
                r"(?:renewal|renewal_delivery)\.cpython-313(?:\.opt-[12])?\.pyc",
                path.name,
            )
            is not None
            and stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == os.getuid()
            and metadata.st_nlink == 1
            and not metadata.st_mode & 0o077
            and metadata.st_size <= LIMIT
        )


def preserved_python(path: Path, executable: str) -> str:
    """Keep the previous command spelling only for the very same interpreter."""
    need(not path.is_symlink() and path.is_file(), "RENEWAL_DELIVERY_TIMER_MISSING")
    value = plistlib.loads(path.read_bytes())
    need(isinstance(value, dict))
    arguments = value.get("ProgramArguments")
    need(
        isinstance(arguments, list)
        and len(arguments) == 5
        and isinstance(arguments[0], str)
        and Path(arguments[0]).is_absolute()
        and Path(arguments[0]).is_file()
        and Path(arguments[0]).samefile(executable),
        "RENEWAL_DELIVERY_INTERPRETER_CHANGED",
    )
    return str(arguments[0])


def artifact(root: Path, script: bytes, atomic: Any) -> Path:
    """Stage an immutable pair before changing the same existing task."""
    helper = inspect.getsource(sys.modules[__name__]).encode()
    generation = hashlib.sha256(script + b"\x00" + helper).hexdigest()
    directory = root / ("delivery-release-" + generation)
    need(not directory.is_symlink())
    directory.mkdir(mode=0o700, exist_ok=True)
    need(
        directory.stat().st_uid == os.getuid() and not directory.stat().st_mode & 0o077
    )
    expected = {"renewal.py": script, "renewal_delivery.py": helper}
    need({p.name for p in directory.iterdir()} <= expected.keys() | {"__pycache__"})
    validate_cache(directory / "__pycache__")
    for name, raw in expected.items():
        path = directory / name
        need(not path.is_symlink())
        if path.exists():
            need(
                path.is_file() and path.read_bytes() == raw,
                "RENEWAL_DELIVERY_ARTIFACT_CHANGED",
            )
        else:
            atomic(path, raw)
    return directory / "renewal.py"


def upgrade_timer(
    root: Path,
    previous_sha: str,
    path: Path,
    content: bytes,
    script: Path,
    label: str,
    atomic: Any,
) -> None:
    """Replace only the proved existing task; retain a resumable durable intent."""

    def launch(*args: str) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                ["/bin/launchctl", *args], capture_output=True, timeout=15, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            raise DeliveryError("RENEWAL_DELIVERY_TIMER_UNCONFIRMED") from None

    need(not path.is_symlink() and path.is_file(), "RENEWAL_DELIVERY_TIMER_MISSING")
    new = plistlib.loads(content)
    current = path.read_bytes()
    intent_path = root / "timer-delivery-intent.json"
    need(not intent_path.is_symlink())
    create_intent = not intent_path.exists()
    completed_intent: bytes | None = None
    if intent_path.exists():
        intent_raw = intent_path.read_bytes()
        intent = json.loads(intent_raw)
        need(
            set(intent) == {"old", "target", "label", "source_sha256"}
            and intent["label"] == label,
            "RENEWAL_DELIVERY_TIMER_INTENT_CHANGED",
        )
        previous_target = base64.b64decode(intent["target"], validate=True)
        if previous_target == content:
            old_raw = base64.b64decode(intent["old"], validate=True)
            source_sha = intent["source_sha256"]
            need(
                previous_sha
                in {source_sha, hashlib.sha256(script.read_bytes()).hexdigest()}
            )
        else:
            # A completed generation may advance; an unfinished one may not.
            need(current == previous_target, "RENEWAL_DELIVERY_TIMER_INTENT_CHANGED")
            old_raw = current
            source_sha = previous_sha
            completed_intent = intent_raw
            create_intent = True
    else:
        old_raw = current
        source_sha = previous_sha
    old = plistlib.loads(old_raw)
    need(isinstance(old, dict) and isinstance(new, dict))
    old_args, new_args = old.get("ProgramArguments"), new.get("ProgramArguments")
    need(
        isinstance(old_args, list)
        and len(old_args) == 5
        and isinstance(new_args, list)
        and len(new_args) == 5
        and old_args[0] == new_args[0]
        and old_args[2:] == new_args[2:] == ["once", "--state", str(root)]
    )
    old_script = Path(old_args[1])
    need(
        old_script.is_relative_to(root)
        and old_script.is_file()
        and not any(p.is_symlink() for p in (old_script, *old_script.parents))
        and hashlib.sha256(old_script.read_bytes()).hexdigest() == source_sha,
        "RENEWAL_DELIVERY_TIMER_SOURCE_CHANGED",
    )
    if completed_intent is not None:
        helper = old_script.parent / "renewal_delivery.py"
        need(helper.is_file() and not helper.is_symlink())
        generation = hashlib.sha256(
            old_script.read_bytes() + b"\x00" + helper.read_bytes()
        ).hexdigest()
        need(
            old_script.name == "renewal.py"
            and old_script.parent.name == "delivery-release-" + generation,
            "RENEWAL_DELIVERY_TIMER_SOURCE_CHANGED",
        )
        validate_cache(old_script.parent / "__pycache__")
    need(
        {k: v for k, v in old.items() if k != "ProgramArguments"}
        == {k: v for k, v in new.items() if k != "ProgramArguments"}
        and current in (old_raw, content),
        "RENEWAL_DELIVERY_TIMER_CHANGED",
    )
    if create_intent:
        loaded = launch("print", f"gui/{os.getuid()}/{label}")
        need(
            loaded.returncode == 0 and str(old_script).encode() in loaded.stdout,
            "RENEWAL_DELIVERY_EXISTING_TIMER_REQUIRED",
        )
        disabled = launch("print-disabled", f"gui/{os.getuid()}")
        need(
            disabled.returncode == 0
            and not re.search(
                rb'"' + re.escape(label.encode()) + rb'"\s*=>\s*true', disabled.stdout
            ),
            "RENEWAL_DELIVERY_TIMER_DISABLED",
        )
        if completed_intent is not None:
            history = root / "timer-delivery-history"
            need(not history.is_symlink())
            history.mkdir(mode=0o700, exist_ok=True)
            need(
                history.stat().st_uid == os.getuid()
                and not history.stat().st_mode & 0o077
            )
            archived = history / (
                hashlib.sha256(completed_intent).hexdigest() + ".json"
            )
            need(not archived.is_symlink())
            if archived.exists():
                need(archived.is_file() and archived.read_bytes() == completed_intent)
            else:
                atomic(archived, completed_intent)
        atomic(
            intent_path,
            encoded(
                {
                    "old": base64.b64encode(old_raw).decode(),
                    "target": base64.b64encode(content).decode(),
                    "label": label,
                    "source_sha256": source_sha,
                }
            ),
        )
    target = f"gui/{os.getuid()}/{label}"
    loaded = launch("print", target)
    if loaded.returncode == 0 and str(script).encode() in loaded.stdout:
        need(current == content, "RENEWAL_DELIVERY_TIMER_CHANGED")
        return
    if loaded.returncode == 0:
        need(
            str(old_script).encode() in loaded.stdout, "RENEWAL_DELIVERY_TIMER_CHANGED"
        )
        need(
            launch("bootout", target).returncode == 0,
            "RENEWAL_DELIVERY_TIMER_UNCONFIRMED",
        )
        loaded = launch("print", target)
    need(loaded.returncode == 113, "RENEWAL_DELIVERY_TIMER_UNCONFIRMED")
    atomic(path, content)
    # Same label/interval/state/Python: no second timer and no access regrant.
    launch("bootstrap", f"gui/{os.getuid()}", str(path))
    loaded = launch("print", target)
    need(
        loaded.returncode == 0 and str(script).encode() in loaded.stdout,
        "RENEWAL_DELIVERY_TIMER_UNCONFIRMED",
    )


class DeliveryError(ValueError):
    """Never return Docker stderr, private paths or input payloads."""


def need(value: bool, code: str = "RENEWAL_DELIVERY_SCOPE_INVALID") -> None:
    if not value:
        raise DeliveryError(code)


def encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def public_certificate(raw: bytes) -> str:
    """Never forward a key or unrelated trailing PEM material to runtime."""
    need(
        0 < len(raw) <= 256 * 1024
        and re.fullmatch(
            rb"-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----\s*",
            raw,
        )
        is not None,
        "RENEWAL_DELIVERY_PUBLIC_CERTIFICATE_REQUIRED",
    )
    return base64.b64encode(raw).decode()


def public_certificate_bundle(raw: bytes) -> str:
    """Forward only a bounded public CA overlap bundle."""
    need(
        0 < len(raw) <= 256 * 1024
        and 1 <= raw.count(b"-----BEGIN CERTIFICATE-----") <= 8
        and re.fullmatch(
            rb"(?:-----BEGIN CERTIFICATE-----\r?\n"
            rb"[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----\s*)+",
            raw,
        )
        is not None,
        "RENEWAL_DELIVERY_PUBLIC_CERTIFICATE_REQUIRED",
    )
    return base64.b64encode(raw).decode()


def command(spec: dict[str, Any], *args: str, payload: bytes = b"") -> bytes:
    try:
        result = subprocess.run(
            [spec["docker"], "--host", spec["docker_endpoint"], *args],
            input=payload,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise DeliveryError("RENEWAL_DELIVERY_TRANSPORT_UNAVAILABLE") from None
    need(
        result.returncode == 0 and len(result.stdout) <= LIMIT,
        "RENEWAL_DELIVERY_TRANSPORT_FAILED",
    )
    return result.stdout


def validate(spec: dict[str, Any], consumer: str, ids: set[str]) -> None:
    need(
        set(spec)
        == {
            "schema_version",
            "consumer_owner",
            "installation_id",
            "project",
            "docker",
            "docker_endpoint",
            "daemon_id",
            "image",
            "destinations",
            "report",
        }
    )
    need(type(spec["schema_version"]) is int and spec["schema_version"] in {1, 2})
    need(
        spec["consumer_owner"] == consumer
        and bool(re.fullmatch(r"[a-z][a-z0-9-]{0,62}", consumer))
    )
    if spec["schema_version"] == 1:
        need(str(uuid.UUID(spec["installation_id"])) == spec["installation_id"])
    else:
        need(
            spec["installation_id"] == spec["project"]
            and bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,63}", spec["installation_id"]))
        )
    need(str(uuid.UUID(spec["daemon_id"])) == spec["daemon_id"])
    need(bool(re.fullmatch(r"sha256:[a-f0-9]{64}", spec["image"])))
    need(bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", spec["project"])))
    docker = Path(spec["docker"])
    need(
        docker.is_absolute()
        and docker.is_file()
        and os.access(docker, os.X_OK)
        and not any(p.is_symlink() for p in (docker, *docker.parents))
    )
    need(
        spec["docker_endpoint"].startswith("unix:///")
        and not any(c in spec["docker_endpoint"] for c in "\r\n\x00")
    )
    need(
        isinstance(spec["destinations"], list)
        and len(spec["destinations"]) == len(ids)
        and 1 <= len(ids) <= 16
    )
    names = set()
    paths = set()
    for item in [*spec["destinations"], spec["report"]]:
        report = item is spec["report"]
        need(
            isinstance(item, dict)
            and set(item)
            == (
                {"volume", "role", "file"}
                if report
                else {"id", "volume", "role", "file"}
            )
        )
        for field in ("role", "file"):
            need(
                isinstance(item[field], str)
                and bool(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}", item[field]))
            )
        need(item["volume"] == spec["project"] + "_" + item["role"])
        pair = (item["volume"], item["file"])
        need(pair not in paths)
        paths.add(pair)
        if not report:
            need(item["id"] in ids and item["id"] not in names)
            names.add(item["id"])


def targets(spec: dict[str, Any]) -> dict[str, Any]:
    need(
        command(spec, "info", "--format", "{{.ID}}").decode().strip()
        == spec["daemon_id"],
        "RENEWAL_DELIVERY_DAEMON_CHANGED",
    )
    image = json.loads(
        command(spec, "image", "inspect", spec["image"], "--format", "{{json .}}")
    )
    need(
        image.get("Id") == spec["image"]
        and image.get("Config", {})
        .get("Labels", {})
        .get("io.aelev.certificate-delivery")
        == "1",
        "RENEWAL_DELIVERY_HELPER_MISMATCH",
    )
    result: dict[str, Any] = {}
    for item in [*spec["destinations"], spec["report"]]:
        labels = {
            "io.aelev.owner": spec["consumer_owner"],
            "io.aelev.installation": spec["installation_id"],
            "io.aelev.role": item["role"],
        }
        if spec["schema_version"] == 1:
            labels["io.aelev.project"] = spec["project"]
        volume = json.loads(
            command(spec, "volume", "inspect", item["volume"], "--format", "{{json .}}")
        )
        need(
            volume.get("Name") == item["volume"]
            and volume.get("Driver") == "local"
            and volume.get("Scope") == "local"
            and volume.get("Options") in (None, {})
            and isinstance(volume.get("CreatedAt"), str)
            and volume.get("Labels") == labels,
            "RENEWAL_DELIVERY_VOLUME_SCOPE_INVALID",
        )
        result[item["volume"]] = {
            key: volume.get(key)
            for key in ("Name", "Driver", "Scope", "Options", "CreatedAt", "Labels")
        }
    return result


def bind(
    root: Path,
    spec: dict[str, Any],
    approval: str,
    consumer: str,
    ids: set[str],
    atomic: Any,
    *,
    expected_helper: str | None = None,
) -> None:
    """Caller holds the existing renewal lock and validates its approval."""
    validate(spec, consumer, ids)
    need(
        expected_helper is None
        or re.fullmatch(r"sha256:[a-f0-9]{64}", expected_helper) is not None
    )
    binding = {
        "schema_version": spec["schema_version"],
        "approval_sha256": approval,
        "spec": spec,
        "docker_sha256": hashlib.sha256(Path(spec["docker"]).read_bytes()).hexdigest(),
        "volumes": targets(spec),
    }
    path = root / "delivery.json"
    need(not path.is_symlink())
    if path.exists():
        need(path.stat().st_size <= 65536, "RENEWAL_DELIVERY_BINDING_CHANGED")
        raw = path.read_bytes()
        if raw == encoded(binding):
            return
        old = json.loads(raw)
        need(
            isinstance(old, dict)
            and set(old) == set(binding)
            and isinstance(old.get("spec"), dict)
            and type(old.get("schema_version")) is int
            and raw == encoded(old),
            "RENEWAL_DELIVERY_BINDING_CHANGED",
        )
        need(
            expected_helper is not None
            and old.get("spec", {}).get("image") == expected_helper
            and {**old, "spec": {**old["spec"], "image": spec["image"]}} == binding,
            "RENEWAL_DELIVERY_BINDING_CHANGED",
        )
        history = root / "delivery-helper-history"
        need(not history.is_symlink())
        history.mkdir(mode=0o700, exist_ok=True)
        need(
            history.stat().st_uid == os.getuid() and not history.stat().st_mode & 0o077
        )
        previous = history / (hashlib.sha256(raw).hexdigest() + ".json")
        need(not previous.is_symlink())
        if previous.exists():
            need(previous.read_bytes() == raw, "RENEWAL_DELIVERY_HISTORY_CHANGED")
        else:
            atomic(previous, raw)
        need(targets(spec) == binding["volumes"], "RENEWAL_DELIVERY_VOLUME_CHANGED")
        atomic(path, encoded(binding))
    else:
        need(expected_helper is None, "RENEWAL_DELIVERY_EXISTING_BINDING_REQUIRED")
        atomic(path, encoded(binding))


def send(
    root: Path,
    approval: str,
    consumer: str,
    report: dict[str, Any],
    certificates: list[dict[str, str]],
) -> None:
    """Deliver only fresh public outputs; negative reports never carry a leaf."""
    path = root / "delivery.json"
    if not path.exists() and not path.is_symlink():
        return
    need(
        not path.is_symlink()
        and path.stat().st_size <= 65536
        and path.stat().st_uid == os.getuid()
        and not path.stat().st_mode & 0o077
    )
    binding = json.loads(path.read_bytes())
    need(
        set(binding)
        == {"schema_version", "approval_sha256", "spec", "docker_sha256", "volumes"}
        and type(binding["schema_version"]) is int
        and binding["schema_version"] in {1, 2}
        and binding["approval_sha256"] == approval,
        "RENEWAL_DELIVERY_BINDING_CHANGED",
    )
    spec = binding["spec"]
    need(binding["schema_version"] == spec.get("schema_version"))
    ids = {item["id"] for item in spec["destinations"]}
    validate(spec, consumer, ids)
    need(
        hashlib.sha256(Path(spec["docker"]).read_bytes()).hexdigest()
        == binding["docker_sha256"],
        "RENEWAL_DELIVERY_TOOL_CHANGED",
    )
    need(targets(spec) == binding["volumes"], "RENEWAL_DELIVERY_VOLUME_CHANGED")
    need(isinstance(report.get("healthy"), bool))
    need(
        report["healthy"] or not certificates, "RENEWAL_DELIVERY_FAILURE_CANNOT_ROTATE"
    )
    need(not report["healthy"] or {item["id"] for item in certificates} == ids)
    need(len(certificates) <= len(ids))
    for item in certificates:
        need(set(item) == {"id", "certificate_base64", "issuer_certificate_base64"})
        public_certificate(base64.b64decode(item["certificate_base64"], validate=True))
        public_certificate_bundle(
            base64.b64decode(item["issuer_certificate_base64"], validate=True)
        )
    payload = {
        "owner": report["owner"],
        "schema_version": spec["schema_version"],
        "consumer_owner": consumer,
        "installation_id": spec["installation_id"],
        "destinations": spec["destinations"],
        "report_destination": spec["report"],
        "report": report,
        "certificates": certificates,
    }
    raw = encoded(payload)
    need(len(raw) <= LIMIT, "RENEWAL_DELIVERY_TOO_LARGE")
    name = spec["project"] + "-certificate-delivery-" + uuid.uuid4().hex
    args = [
        "run",
        "--rm",
        "--name",
        name,
        "--label",
        "io.aelev.renewal-delivery=" + approval,
        "--interactive",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--user",
        "10001:10001",
        "--memory",
        "256m",
        "--cpus",
        "0.5",
        "--pids-limit",
        "32",
    ]
    for role, volume in sorted(
        {
            item["role"]: item["volume"]
            for item in [*spec["destinations"], spec["report"]]
        }.items()
    ):
        args.extend(
            [
                "--mount",
                "type=volume,source="
                + volume
                + ",target=/var/lib/aelev-renewal/"
                + role
                + ",volume-nocopy",
            ]
        )
    args.extend(
        ["--entrypoint", "python", spec["image"], "/opt/aelev/certificate-delivery.py"]
    )
    # --rm plus a bounded helper: a lost CLI response is not success. No runtime
    # container is restarted or removed by this transport.
    result = json.loads(command(spec, *args, payload=raw))
    need(
        result
        == {
            "code": "CERTIFICATE_DELIVERY_VERIFIED",
            "installation_id": spec["installation_id"],
            "report_sha256": hashlib.sha256(encoded(report)).hexdigest(),
            "certificate_sha256": {
                item["id"]: hashlib.sha256(
                    base64.b64decode(item["certificate_base64"], validate=True)
                ).hexdigest()
                for item in certificates
            },
        },
        "RENEWAL_DELIVERY_NOT_VERIFIED",
    )
    need(targets(spec) == binding["volumes"], "RENEWAL_DELIVERY_VOLUME_CHANGED")
