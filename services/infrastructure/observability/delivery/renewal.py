"""Operator-only renewal of explicitly approved local telemetry leaves."""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import secrets
import ssl
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator

sys.dont_write_bytecode = True

OWNER = "telemetry-certificate-renewal"
DAYS = 90
WINDOW = 30 * 86400


def delivery_operator() -> Any:
    """Load only the shipped public standard-library helper."""
    try:
        return importlib.import_module("renewal_delivery")
    except ModuleNotFoundError:
        archive = Path(__file__).parent / "vendor/aelev-renewal-delivery-1.0.0.zip"
        require(
            archive.is_file() and not archive.is_symlink(),
            "TELEMETRY_RENEWAL_DELIVERY_ARTIFACT_MISSING",
        )
        sys.path.insert(0, str(archive))
        return importlib.import_module("renewal_delivery")


class RenewalError(ValueError):
    """Safe, bounded diagnostic; never include native stderr or private material."""


def require(value: bool, code: str) -> None:
    """Reject uncertain authority or material."""
    if not value:
        raise RenewalError(code)


def sha(data: bytes) -> str:
    """Hash exact bytes without returning them."""
    return hashlib.sha256(data).hexdigest()


def atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Publish one file atomically and sync its directory."""
    require(not path.is_symlink(), "TELEMETRY_RENEWAL_SYMLINK")
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".renewal-")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if path.exists():
                metadata = path.stat()
                os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def read(path: Path) -> dict[str, Any]:
    """Read bounded JSON without duplicate fields or non-finite values."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            require(key not in result, "TELEMETRY_RENEWAL_DUPLICATE_FIELD")
            result[key] = value
        return result

    def invalid(_: str) -> None:
        raise RenewalError("TELEMETRY_RENEWAL_INVALID_JSON")

    require(
        not path.is_symlink() and path.stat().st_size <= 131072,
        "TELEMETRY_RENEWAL_INVALID_JSON",
    )
    result = json.loads(
        path.read_bytes(), object_pairs_hook=pairs, parse_constant=invalid
    )
    require(isinstance(result, dict), "TELEMETRY_RENEWAL_INVALID_JSON")
    return dict(result)


@contextmanager
def lock(root: Path) -> Iterator[None]:
    """Serialize only this operator's private state on its own host."""
    require(
        root.is_absolute()
        and root.is_dir()
        and not root.stat().st_mode & 0o077
        and root.stat().st_uid == os.getuid()
        and not any(p.is_symlink() for p in (root, *root.parents)),
        "TELEMETRY_RENEWAL_PRIVATE_STATE_REQUIRED",
    )
    descriptor = os.open(root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RenewalError("TELEMETRY_RENEWAL_BUSY") from None
        yield


def native(command: list[str]) -> bytes:
    """Never expose command arguments, native messages or a private-key value."""
    try:
        result = subprocess.run(command, capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise RenewalError("TELEMETRY_RENEWAL_NATIVE_UNAVAILABLE") from None
    require(result.returncode == 0, "TELEMETRY_RENEWAL_NATIVE_VALIDATION_FAILED")
    return result.stdout


def openssl(config: dict[str, Any], *args: str) -> bytes:
    """Use the explicit operator executable, never a shell."""
    return native([config["openssl"], *args])


def regular(path: Path, *, private: bool = False) -> None:
    """Reject links, special files and exposed private authority."""
    require(
        path.is_absolute()
        and stat.S_ISREG(path.stat().st_mode)
        and not any(p.is_symlink() for p in (path, *path.parents)),
        "TELEMETRY_RENEWAL_FILE_INVALID",
    )
    if private:
        require(
            not path.stat().st_mode & 0o027, "TELEMETRY_RENEWAL_PRIVATE_MODE_INVALID"
        )


def validate(config: dict[str, Any]) -> None:
    """One approved consumer, no infrastructure catalogue of functional clients."""
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
        | ({"delivery_owner"} if config.get("schema_version") == 4 else set())
        and type(config["schema_version"]) is int
        and config["schema_version"] in {1, 2, 3, 4},
        "TELEMETRY_RENEWAL_CONFIG_INVALID",
    )
    if config["schema_version"] == 4:
        require(
            config["delivery_owner"] == "publisher-certificate-renewal",
            "TELEMETRY_RENEWAL_CONFIG_INVALID",
        )
    require(
        config["environment"] in {"local", "test"},
        "TELEMETRY_RENEWAL_PRODUCTION_CA_FORBIDDEN",
    )
    require(
        isinstance(config["consumer_owner"], str)
        and re.fullmatch(r"[a-z][a-z0-9-]{0,62}", config["consumer_owner"]) is not None,
        "TELEMETRY_RENEWAL_OWNER_INVALID",
    )
    regular(Path(config["openssl"]))
    require(
        os.access(config["openssl"], os.X_OK), "TELEMETRY_RENEWAL_EXECUTABLE_INVALID"
    )
    report = Path(config["report"])
    require(
        report.is_absolute()
        and report.parent.is_dir()
        and not any(p.is_symlink() for p in (report, *report.parents)),
        "TELEMETRY_RENEWAL_REPORT_INVALID",
    )
    certificates = config["certificates"]
    require(
        isinstance(certificates, list) and 1 <= len(certificates) <= 32,
        "TELEMETRY_RENEWAL_CERTIFICATES_INVALID",
    )
    names: set[str] = set()
    for entry in certificates:
        fields = {"id", "certificate", "key", "issuer_certificate", "issuer_key"}
        if config["schema_version"] in {2, 4}:
            fields.add("previous_issuer_certificate")
        if config["schema_version"] == 3 and "previous_issuer_certificate" in entry:
            fields.add("previous_issuer_certificate")
        if config["schema_version"] == 3:
            fields.add("initial_profile")
        require(
            isinstance(entry, dict) and set(entry) == fields,
            "TELEMETRY_RENEWAL_CERTIFICATE_INVALID",
        )
        if config["schema_version"] == 3:
            require(
                entry["initial_profile"]
                in {
                    "legacy-no-basic-constraints",
                    "legacy-self-signed-ca-leaf",
                },
                "TELEMETRY_RENEWAL_CERTIFICATE_INVALID",
            )
        name = entry["id"]
        require(
            isinstance(name, str)
            and re.fullmatch(r"[a-z][a-z0-9-]{0,62}", name) is not None
            and name not in names,
            "TELEMETRY_RENEWAL_CERTIFICATE_ID_INVALID",
        )
        names.add(name)
        for field in fields - {"id", "initial_profile"}:
            regular(Path(entry[field]), private=field in {"key", "issuer_key"})
        require(
            not Path(entry["issuer_key"]).stat().st_mode & 0o077,
            "TELEMETRY_RENEWAL_ISSUER_MODE_INVALID",
        )
        require(
            Path(entry["key"]).parent == Path(entry["certificate"]).parent
            and not Path(entry["issuer_key"]).is_relative_to(
                Path(entry["certificate"]).parent
            )
            and not Path(entry["issuer_key"]).is_relative_to(report.parent),
            "TELEMETRY_RENEWAL_ISSUER_RUNTIME_EXPOSURE",
        )


def bound(config: dict[str, Any], entry: dict[str, Any]) -> dict[str, str]:
    """Freeze the approved key, CA and executable, not a changing leaf."""
    return {
        field: sha(Path(path).read_bytes())
        for field, path in {
            **{
                key: entry[key]
                for key in (
                    "key",
                    "issuer_certificate",
                    "issuer_key",
                    "previous_issuer_certificate",
                )
                if key in entry
            },
            "openssl": config["openssl"],
        }.items()
    }


def expiry(config: dict[str, Any], path: Path) -> float:
    """Read a public certificate expiry in Unix UTC."""
    value = (
        openssl(config, "x509", "-in", str(path), "-noout", "-enddate").decode().strip()
    )
    require(value.startswith("notAfter="), "TELEMETRY_RENEWAL_DATE_INVALID")
    return ssl.cert_time_to_seconds(value.removeprefix("notAfter="))


def material(
    config: dict[str, Any],
    entry: dict[str, Any],
    path: Path,
    *,
    expired: bool = False,
    issuer: str | None = None,
    initial_profile: str | None = None,
) -> dict[str, Any]:
    """Validate the approved signing chain and key; preserve all subject/extensions."""
    regular(path)
    openssl(
        config,
        "verify",
        "-CAfile",
        issuer or entry["issuer_certificate"],
        *(["-no_check_time"] if expired else []),
        str(path),
    )
    require(
        openssl(config, "x509", "-in", str(path), "-pubkey", "-noout")
        == openssl(config, "pkey", "-in", entry["key"], "-pubout"),
        "TELEMETRY_RENEWAL_KEY_MISMATCH",
    )
    text = openssl(config, "x509", "-in", str(path), "-noout", "-text")
    marker = b"        X509v3 extensions:\n"
    require(marker in text, "TELEMETRY_RENEWAL_EXTENSIONS_REQUIRED")
    extensions = text.split(marker, 1)[1].split(b"    Signature Algorithm:", 1)[0]
    legacy_profile: str | None = None
    if b"Basic Constraints" not in extensions:
        legacy_profile = "legacy-no-basic-constraints"
    elif b"CA:TRUE" in extensions:
        legacy_profile = "legacy-self-signed-ca-leaf"
    require(
        (initial_profile is not None and legacy_profile == initial_profile)
        or (initial_profile is None and legacy_profile is None and b"CA:FALSE" in extensions),
        "TELEMETRY_RENEWAL_LEAF_REQUIRED",
    )
    profile_extensions = b"".join(
        part
        for part in extensions.split(b"            X509v3 ")
        if not part.startswith(
            (b"Authority Key Identifier:", b"Subject Key Identifier:")
        )
    )
    scope_extensions = b"".join(
        part
        for part in extensions.split(b"            X509v3 ")
        if not part.startswith(
            (
                b"Authority Key Identifier:",
                b"Subject Key Identifier:",
                b"Basic Constraints:",
            )
        )
    )
    return {
        "sha256": sha(path.read_bytes()),
        "expires_at": expiry(config, path),
        "subject_sha256": sha(
            openssl(
                config,
                "x509",
                "-in",
                str(path),
                "-noout",
                "-subject",
                "-nameopt",
                "RFC2253",
            )
        ),
        "extensions_sha256": sha(extensions),
        "profile_extensions_sha256": sha(profile_extensions),
        "scope_extensions_sha256": sha(scope_extensions),
        "legacy_profile": legacy_profile,
    }


def same_scope(
    config: dict[str, Any], left: dict[str, Any], right: dict[str, Any]
) -> bool:
    field = {
        1: "extensions_sha256",
        2: "profile_extensions_sha256",
        3: "scope_extensions_sha256",
        4: "profile_extensions_sha256",
    }[config["schema_version"]]
    return (
        left["subject_sha256"] == right["subject_sha256"]
        and left[field] == right[field]
    )


def persist(root: Path, state: dict[str, Any]) -> None:
    """Commit private approval/intent state before the corresponding effect."""
    atomic(root / "state.json", json.dumps(state, sort_keys=True).encode())


def public_owner(config: dict[str, Any]) -> str:
    return config.get("delivery_owner", OWNER)


def publish(
    config: dict[str, Any], state: dict[str, Any], *, root: Path | None = None
) -> dict[str, Any]:
    """No authority, paths, keys or raw exception messages in the public report."""
    entries = [
        {
            "id": name,
            **{
                key: item.get(key)
                for key in (
                    "healthy",
                    "code",
                    "certificate_sha256",
                    "certificate_expires_at",
                    "ca_expires_at",
                    "last_renewed_at",
                    "last_failure_at",
                    "last_failure_code",
                )
            },
        }
        for name, item in state["certificates"].items()
    ]
    checked_at = time.time()
    renewal_healthy = not state["revoked"] and all(item["healthy"] for item in entries)
    ready = renewal_healthy and all(
        min(item["certificate_expires_at"], item["ca_expires_at"]) - checked_at > WINDOW
        for item in entries
    )
    last_success_at = checked_at if ready else state.get("last_success_at")
    result = {
        "owner": public_owner(config),
        "schema_version": 1,
        "consumer_owner": config["consumer_owner"],
        "checked_at": checked_at,
        "healthy": renewal_healthy,
        "renewal_healthy": renewal_healthy,
        "ready": ready,
        "last_success_at": last_success_at,
        "last_success_age_seconds": (
            checked_at - last_success_at if last_success_at is not None else None
        ),
        "code": (
            "TELEMETRY_RENEWAL_REVOKED"
            if state["revoked"]
            else (
                (
                    "TELEMETRY_RENEWAL_READY"
                    if ready
                    else "TELEMETRY_RENEWAL_EXPIRY_WINDOW"
                )
                if all(item["healthy"] for item in entries)
                else "TELEMETRY_RENEWAL_FAILED"
            )
        ),
        "certificates": entries,
    }
    atomic(Path(config["report"]), json.dumps(result, sort_keys=True).encode(), 0o644)
    if root is not None and (
        (root / "delivery.json").exists() or (root / "delivery.json").is_symlink()
    ):
        renewal_delivery = delivery_operator()

        try:
            certificates = []
            if result["healthy"]:
                for entry in config["certificates"]:
                    raw = Path(entry["certificate"]).read_bytes()
                    issuer = Path(entry["issuer_certificate"]).read_bytes()
                    if "previous_issuer_certificate" in entry:
                        previous = Path(entry["previous_issuer_certificate"]).read_bytes()
                        if previous != issuer:
                            issuer = previous + issuer
                    require(
                        sha(raw)
                        == state["certificates"][entry["id"]]["certificate_sha256"],
                        "TELEMETRY_RENEWAL_LEAF_CHANGED",
                    )
                    certificates.append(
                        {
                            "id": entry["id"],
                            "certificate_base64": renewal_delivery.public_certificate(
                                raw
                            ),
                            "issuer_certificate_base64": renewal_delivery.public_certificate_bundle(
                                issuer
                            ),
                        }
                    )
            renewal_delivery.send(
                root,
                state["approval_sha256"],
                config["consumer_owner"],
                result,
                certificates,
            )
            if ready:
                state["last_success_at"] = checked_at
                persist(root, state)
        except (
            renewal_delivery.DeliveryError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
        ) as error:
            detail = str(error)
            code = (
                "TELEMETRY_" + detail
                if re.fullmatch(r"RENEWAL_DELIVERY_[A-Z_]{1,70}", detail)
                else (
                    detail
                    if isinstance(error, RenewalError)
                    else "TELEMETRY_RENEWAL_DELIVERY_FAILED"
                )
            )
            result.update(healthy=False, code=code)
            result.update(
                renewal_healthy=False,
                ready=False,
                last_success_at=state.get("last_success_at"),
                last_success_age_seconds=(
                    checked_at - state["last_success_at"]
                    if state.get("last_success_at") is not None
                    else None
                ),
            )
            state.update(
                delivery_last_failure_at=time.time(), delivery_last_failure_code=code
            )
            persist(root, state)
            atomic(
                Path(config["report"]),
                json.dumps(result, sort_keys=True).encode(),
                0o644,
            )
            try:
                renewal_delivery.send(
                    root, state["approval_sha256"], config["consumer_owner"], result, []
                )
            except (
                renewal_delivery.DeliveryError,
                OSError,
                ValueError,
                KeyError,
                TypeError,
            ):
                pass  # Host report stays negative; the volume report ages out.
    return result


def check(root: Path, *, max_age: int = 180) -> dict[str, Any]:
    """Deployment gate for a fresh, exact and safely distant renewal receipt."""
    require(1 <= max_age <= 3600, "TELEMETRY_RENEWAL_MAX_AGE_INVALID")
    with lock(root):
        state, config = read(root / "state.json"), read(root / "approval.json")
        validate(config)
        report = read(Path(config["report"]))
        now = time.time()
        require(
            state.get("owner") == OWNER
            and state.get("revoked") is False
            and state.get("approval_sha256")
            == sha(json.dumps(config, sort_keys=True).encode())
            and report.get("owner") == public_owner(config)
            and report.get("consumer_owner") == config["consumer_owner"]
            and report.get("schema_version") == 1
            and report.get("healthy") is True
            and report.get("renewal_healthy") is True
            and report.get("ready") is True
            and type(report.get("checked_at")) in (int, float)
            and 0 <= now - float(report["checked_at"]) <= max_age,
            "TELEMETRY_RENEWAL_RECEIPT_NOT_READY",
        )
        received = {
            item.get("id"): item
            for item in report.get("certificates", [])
            if isinstance(item, dict)
        }
        require(
            len(received) == len(config["certificates"]),
            "TELEMETRY_RENEWAL_RECEIPT_NOT_READY",
        )
        expires = []
        for entry in config["certificates"]:
            item = received.get(entry["id"])
            require(
                isinstance(item, dict)
                and item.get("healthy") is True
                and item.get("certificate_sha256")
                == sha(Path(entry["certificate"]).read_bytes())
                and type(item.get("certificate_expires_at")) in (int, float)
                and type(item.get("ca_expires_at")) in (int, float),
                "TELEMETRY_RENEWAL_RECEIPT_NOT_READY",
            )
            expires.extend(
                [float(item["certificate_expires_at"]), float(item["ca_expires_at"])]
            )
        require(
            min(expires) - now > WINDOW,
            "TELEMETRY_RENEWAL_RECEIPT_NOT_READY",
        )
        return {
            "code": "TELEMETRY_RENEWAL_INSTALLATION_READY",
            "consumer_owner": config["consumer_owner"],
            "checked_at": report["checked_at"],
            "expires_at": min(expires),
            "renewal_healthy": True,
            "last_success_age_seconds": now - float(report["last_success_at"]),
        }


def bind_delivery(
    root: Path, spec: dict[str, Any], *, expected_helper: str | None = None
) -> None:
    """Bind an output only, without issuing or replacing the existing approval."""
    renewal_delivery = delivery_operator()

    with lock(root):
        state, config = read(root / "state.json"), read(root / "approval.json")
        require(
            state.get("owner") == OWNER
            and not state.get("revoked")
            and state["approval_sha256"]
            == sha(json.dumps(config, sort_keys=True).encode()),
            "TELEMETRY_RENEWAL_APPROVAL_INVALID",
        )
        validate(config)
        for entry in config["certificates"]:
            item = state["certificates"][entry["id"]]
            require(
                not item.get("intent") and bound(config, entry) == item["bound"],
                "TELEMETRY_RENEWAL_BOUND_MATERIAL_CHANGED",
            )
            info = material(
                config,
                entry,
                Path(entry["certificate"]),
                issuer=(
                    entry.get("previous_issuer_certificate")
                    if item.get("previous_issuer_active") is True
                    else None
                ),
                initial_profile=(
                    entry.get("initial_profile")
                    if item.get("legacy_profile_active") is True
                    else None
                ),
            )
            require(
                info["sha256"] == item["certificate_sha256"]
                and info["extensions_sha256"] == item["extensions_sha256"]
                and info["subject_sha256"] == item["subject_sha256"],
                "TELEMETRY_RENEWAL_LEAF_CHANGED",
            )
        renewal_delivery.bind(
            root,
            spec,
            state["approval_sha256"],
            config["consumer_owner"],
            {entry["id"] for entry in config["certificates"]},
            atomic,
            expected_helper=expected_helper,
        )


def enroll(root: Path, config: dict[str, Any], *, allow_expired: bool = False) -> None:
    """An expired initial identity requires this explicit, one-time operator act."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock(root):
        validate(config)
        encoded = json.dumps(config, sort_keys=True).encode()
        if (root / "state.json").exists():
            previous = read(root / "state.json")
            require(
                not previous["revoked"] and previous["approval_sha256"] == sha(encoded),
                "TELEMETRY_RENEWAL_APPROVAL_REVOKED_OR_CHANGED",
            )
            return
        state: dict[str, Any] = {
            "owner": OWNER,
            "approval_sha256": sha(encoded),
            "revoked": False,
            "allow_initial_expired": allow_expired,
            "certificates": {},
        }
        for entry in config["certificates"]:
            info = material(
                config,
                entry,
                Path(entry["certificate"]),
                expired=allow_expired,
                issuer=entry.get("previous_issuer_certificate"),
                initial_profile=(
                    entry["initial_profile"] if config["schema_version"] == 3 else None
                ),
            )
            require(
                config["schema_version"] != 3
                or info["legacy_profile"] == entry["initial_profile"],
                "TELEMETRY_RENEWAL_LEGACY_PROFILE_REQUIRED",
            )
            state["certificates"][entry["id"]] = {
                **info,
                "bound": bound(config, entry),
                "issued": 0,
                "certificate_sha256": info["sha256"],
                "certificate_expires_at": info["expires_at"],
                "ca_expires_at": expiry(config, Path(entry["issuer_certificate"])),
                "healthy": False,
                "code": "TELEMETRY_RENEWAL_INITIAL_VALIDATION_PENDING",
            }
            if "previous_issuer_certificate" in entry:
                state["certificates"][entry["id"]]["previous_issuer_active"] = True
            if config["schema_version"] == 3:
                state["certificates"][entry["id"]]["legacy_profile_active"] = True
        atomic(root / "approval.json", encoded)
        persist(root, state)
        publish(config, state)


def reject_transition_candidates(root: Path) -> dict[str, Any]:
    """Explicitly archive never-published candidates from a failed CA transition."""
    with lock(root):
        state, config = read(root / "state.json"), read(root / "approval.json")
        require(
            config.get("schema_version") in {1, 2, 3, 4}
            and state.get("owner") == OWNER
            and state.get("revoked") is False
            and not (root / "delivery.json").exists(),
            "TELEMETRY_RENEWAL_TRANSITION_REPAIR_FORBIDDEN",
        )
        archive = root / "rejected-transition-candidates"
        require(not archive.is_symlink(), "TELEMETRY_RENEWAL_TRANSITION_REPAIR_INVALID")
        archive.mkdir(mode=0o700, exist_ok=True)
        rejected = 0
        for entry in config["certificates"]:
            item = state["certificates"][entry["id"]]
            intent = item.get("intent")
            require(
                isinstance(intent, dict)
                and set(intent) == {"candidate", "candidate_sha256"}
                and sha(Path(entry["certificate"]).read_bytes())
                == item["certificate_sha256"]
                and bound(config, entry) == item["bound"],
                "TELEMETRY_RENEWAL_TRANSITION_REPAIR_INVALID",
            )
            candidate = Path(entry["certificate"]).parent / intent["candidate"]
            regular(candidate)
            raw = candidate.read_bytes()
            require(
                sha(raw) == intent["candidate_sha256"],
                "TELEMETRY_RENEWAL_TRANSITION_REPAIR_INVALID",
            )
            target = archive / (intent["candidate_sha256"] + ".crt")
            if target.exists():
                require(
                    not target.is_symlink() and target.read_bytes() == raw,
                    "TELEMETRY_RENEWAL_TRANSITION_REPAIR_INVALID",
                )
            else:
                atomic(target, raw, 0o600)
            candidate.unlink()
            history = item.setdefault("rejected_transition_candidates", [])
            require(
                isinstance(history, list) and len(history) < 8,
                "TELEMETRY_RENEWAL_TRANSITION_REPAIR_INVALID",
            )
            history.append(intent["candidate_sha256"])
            item.pop("intent")
            item.update(
                healthy=False,
                code="TELEMETRY_RENEWAL_TRANSITION_CANDIDATE_REJECTED",
            )
            rejected += 1
        persist(root, state)
        publish(config, state)
        return {
            "code": "TELEMETRY_RENEWAL_TRANSITION_CANDIDATES_REJECTED",
            "rejected_candidates": rejected,
        }


def rotate(
    root: Path,
    config: dict[str, Any],
    state: dict[str, Any],
    entry: dict[str, Any],
    *,
    force: bool,
) -> None:
    """Reconcile exact publication, then issue only under unchanged approval."""
    item = state["certificates"][entry["id"]]
    require(
        bound(config, entry) == item["bound"],
        "TELEMETRY_RENEWAL_BOUND_MATERIAL_CHANGED",
    )
    certificate = Path(entry["certificate"])
    current_issuer = None
    if (
        "previous_issuer_certificate" in entry
        and sha(certificate.read_bytes()) == item["certificate_sha256"]
        and item.get("previous_issuer_active") is True
    ):
        current_issuer = entry["previous_issuer_certificate"]
    info = material(
        config,
        entry,
        certificate,
        expired=state["allow_initial_expired"]
        and item["certificate_sha256"] == item["sha256"],
        issuer=current_issuer,
        initial_profile=(
            entry.get("initial_profile")
            if item.get("legacy_profile_active") is True
            and item["certificate_sha256"] == item["sha256"]
            else None
        ),
    )
    if (
        config["schema_version"] in {2, 4}
        and item.get("previous_issuer_active") is True
        and info["sha256"] == item["certificate_sha256"]
    ):
        item["profile_extensions_sha256"] = info["profile_extensions_sha256"]
        item["scope_extensions_sha256"] = info["scope_extensions_sha256"]
    require(same_scope(config, info, item), "TELEMETRY_RENEWAL_SCOPE_CHANGED")
    intent = item.get("intent")
    if intent and info["sha256"] == intent["candidate_sha256"]:
        item.update(
            certificate_sha256=info["sha256"],
            certificate_expires_at=info["expires_at"],
            extensions_sha256=info["extensions_sha256"],
            profile_extensions_sha256=info["profile_extensions_sha256"],
            scope_extensions_sha256=info["scope_extensions_sha256"],
            last_renewed_at=time.time(),
        )
        item.pop("intent")
        item.pop("previous_issuer_active", None)
        item.pop("legacy_profile_active", None)
    else:
        require(
            info["sha256"] == item["certificate_sha256"],
            "TELEMETRY_RENEWAL_LEAF_CHANGED",
        )
        if force or intent or info["expires_at"] - time.time() <= WINDOW:
            openssl(
                config,
                "x509",
                "-in",
                entry["issuer_certificate"],
                "-noout",
                "-checkend",
                str((DAYS + 1) * 86400),
            )
            if intent:
                require(
                    set(intent) == {"candidate", "candidate_sha256"}
                    and re.fullmatch(
                        r"\.telemetry-renewal-[a-f0-9]{32}\.crt", intent["candidate"]
                    )
                    is not None,
                    "TELEMETRY_RENEWAL_INTENT_INVALID",
                )
                candidate = certificate.parent / intent["candidate"]
            else:
                candidate = certificate.parent / (
                    ".telemetry-renewal-" + secrets.token_hex(16) + ".crt"
                )
                descriptor = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
                os.close(descriptor)
                # A CA-signed leaf is not a self-signed input to `x509 -CA`.
                # Rebuild a CSR with the existing key on every supported OpenSSL.
                # The candidate still passes the exact scope checks below.
                request = certificate.parent / (
                    ".telemetry-renewal-" + secrets.token_hex(16) + ".csr"
                )
                extension = certificate.parent / (
                    ".telemetry-renewal-" + secrets.token_hex(16) + ".cnf"
                )
                try:
                    extra: tuple[str, ...] = ()
                    if item.get("legacy_profile_active") is True:
                        atomic(
                            extension,
                            b"basicConstraints=critical,CA:FALSE\n",
                            0o600,
                        )
                        extra = ("-extfile", str(extension))
                    openssl(
                        config,
                        "x509",
                        "-x509toreq",
                        "-in",
                        str(certificate),
                        "-signkey",
                        entry["key"],
                        "-copy_extensions",
                        "copyall",
                        "-out",
                        str(request),
                    )
                    openssl(
                        config,
                        "x509",
                        "-req",
                        "-in",
                        str(request),
                        "-CA",
                        entry["issuer_certificate"],
                        "-CAkey",
                        entry["issuer_key"],
                        "-set_serial",
                        str(secrets.randbits(128) | 1),
                        "-days",
                        str(DAYS),
                        "-sha256",
                        "-copy_extensions",
                        "copyall",
                        *extra,
                        "-out",
                        str(candidate),
                    )
                finally:
                    request.unlink(missing_ok=True)
                    extension.unlink(missing_ok=True)
                metadata = certificate.stat()
                os.chown(candidate, metadata.st_uid, metadata.st_gid)
                os.chmod(candidate, stat.S_IMODE(metadata.st_mode))
                with candidate.open("rb") as stream:
                    os.fsync(stream.fileno())
                item["intent"] = {
                    "candidate": candidate.name,
                    "candidate_sha256": sha(candidate.read_bytes()),
                }
                item["issued"] += 1
                persist(root, state)
            fresh = material(config, entry, candidate)
            require(
                fresh["sha256"] == item["intent"]["candidate_sha256"]
                and bound(config, entry) == item["bound"]
                and sha(certificate.read_bytes()) == item["certificate_sha256"],
                "TELEMETRY_RENEWAL_CANDIDATE_CHANGED",
            )
            require(same_scope(config, fresh, item), "TELEMETRY_RENEWAL_SCOPE_CHANGED")
            atomic(
                certificate,
                candidate.read_bytes(),
                stat.S_IMODE(certificate.stat().st_mode),
            )
            candidate.unlink()
            item.update(
                certificate_sha256=fresh["sha256"],
                certificate_expires_at=fresh["expires_at"],
                extensions_sha256=fresh["extensions_sha256"],
                profile_extensions_sha256=fresh["profile_extensions_sha256"],
                scope_extensions_sha256=fresh["scope_extensions_sha256"],
                last_renewed_at=time.time(),
            )
            item.pop("intent")
            item.pop("previous_issuer_active", None)
            item.pop("legacy_profile_active", None)
    item.update(healthy=True, code="TELEMETRY_RENEWAL_READY")


def once(root: Path, *, force: bool = False) -> dict[str, Any]:
    """Retry failures on the next scheduled pass without regranting access."""
    with lock(root):
        state, config = read(root / "state.json"), read(root / "approval.json")
        require(
            state["owner"] == OWNER
            and state["approval_sha256"]
            == sha(json.dumps(config, sort_keys=True).encode()),
            "TELEMETRY_RENEWAL_APPROVAL_INVALID",
        )
        if state["revoked"]:
            return publish(config, state, root=root)
        validate(config)
        for entry in config["certificates"]:
            item = state["certificates"][entry["id"]]
            try:
                rotate(root, config, state, entry, force=force)
            except (RenewalError, OSError, ValueError, KeyError, TypeError) as error:
                code = (
                    str(error)
                    if isinstance(error, RenewalError)
                    else "TELEMETRY_RENEWAL_IO_OR_FORMAT_FAILED"
                )
                item.update(
                    healthy=False,
                    code=code,
                    last_failure_at=time.time(),
                    last_failure_code=code,
                )
            persist(root, state)
        return publish(config, state, root=root)


def revoke(root: Path) -> None:
    """Cancel future issuance; this is not an X.509 CRL or a grant restoration."""
    with lock(root):
        state = read(root / "state.json")
        state["revoked"] = True
        persist(root, state)
        publish(read(root / "approval.json"), state, root=root)


def export_issuers(root: Path, consumer_owner: str) -> dict[str, Any]:
    """Read approved public issuers without keys, issuance or state changes."""
    with lock(root):
        state, config = read(root / "state.json"), read(root / "approval.json")
        require(
            state["owner"] == OWNER
            and state["approval_sha256"]
            == sha(json.dumps(config, sort_keys=True).encode())
            and config["consumer_owner"] == consumer_owner
            and config["environment"] in {"local", "test"},
            "TELEMETRY_RENEWAL_EXPORT_SCOPE_INVALID",
        )
        require(state["revoked"] is False, "TELEMETRY_RENEWAL_REVOKED")
        regular(Path(config["openssl"]))
        records: list[dict[str, str]] = []
        for entry in config["certificates"]:
            item = state["certificates"][entry["id"]]
            require(not item.get("intent"), "TELEMETRY_RENEWAL_EXPORT_UNCONFIRMED")
            authority, certificate = Path(entry["issuer_certificate"]), Path(
                entry["certificate"]
            )
            regular(authority)
            regular(certificate)
            require(
                0 < authority.stat().st_size <= 256 * 1024
                and 0 < certificate.stat().st_size <= 256 * 1024,
                "TELEMETRY_RENEWAL_EXPORT_SIZE_INVALID",
            )
            original = authority.read_bytes()
            leaf_sha256 = sha(certificate.read_bytes())
            require(
                sha(original) == item["bound"]["issuer_certificate"]
                and sha(Path(config["openssl"]).read_bytes())
                == item["bound"]["openssl"]
                and leaf_sha256 == item["certificate_sha256"],
                "TELEMETRY_RENEWAL_BOUND_MATERIAL_CHANGED",
            )
            public = openssl(config, "x509", "-in", str(authority), "-outform", "PEM")
            require(
                original.replace(b"\r\n", b"\n").strip() == public.strip(),
                "TELEMETRY_RENEWAL_PUBLIC_CERTIFICATE_REQUIRED",
            )
            require(
                expiry(config, authority) > time.time(), "TELEMETRY_RENEWAL_CA_EXPIRED"
            )
            openssl(config, "verify", "-CAfile", str(authority), str(certificate))
            require(
                authority.read_bytes() == original
                and sha(certificate.read_bytes()) == leaf_sha256,
                "TELEMETRY_RENEWAL_BOUND_MATERIAL_CHANGED",
            )
            records.append(
                {
                    "id": entry["id"],
                    "issuer_certificate_base64": base64.b64encode(public).decode(
                        "ascii"
                    ),
                    "issuer_sha256": sha(public),
                }
            )
        return {
            "owner": public_owner(config),
            "schema_version": 1,
            "consumer_owner": consumer_owner,
            "code": "TELEMETRY_RENEWAL_PUBLIC_ISSUERS_EXPORTED",
            "certificates": records,
        }


def install(root: Path, *, upgrade: bool = False) -> str:
    """Install only this local Mac operator task, without runtime credentials."""
    require(platform.system() == "Darwin", "TELEMETRY_RENEWAL_MACOS_TIMER_REQUIRED")
    with lock(root):
        state = read(root / "state.json")
        require(not state["revoked"], "TELEMETRY_RENEWAL_REVOKED")
        body = Path(__file__).read_bytes()
        script = (
            delivery_operator().artifact(root, body, atomic)
            if upgrade or (root / "delivery.json").exists()
            else root / ("renewal-" + sha(body) + ".py")
        )
        if script.exists():
            require(script.read_bytes() == body, "TELEMETRY_RENEWAL_ARTIFACT_CHANGED")
        else:
            atomic(script, body)
        label = "com.aelev.telemetry-renewal." + sha(str(root).encode())[:16]
        directory = Path.home() / "Library/LaunchAgents"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / (label + ".plist")
        executable = (
            delivery_operator().preserved_python(path, sys.executable)
            if upgrade
            else sys.executable
        )
        content = plistlib.dumps(
            {
                "Label": label,
                "ProgramArguments": [
                    executable,
                    str(script),
                    "once",
                    "--state",
                    str(root),
                ],
                "RunAtLoad": True,
                "StartInterval": 60,
                "ProcessType": "Background",
                "StandardOutPath": "/dev/null",
                "StandardErrorPath": "/dev/null",
            }
        )
        domain = f"gui/{os.getuid()}"
        if upgrade:
            require(
                isinstance(state.get("timer"), dict)
                and state["timer"]["label"] == label,
                "TELEMETRY_RENEWAL_EXISTING_TIMER_REQUIRED",
            )
            delivery_operator().upgrade_timer(
                root,
                state["timer"]["script_sha256"],
                path,
                content,
                script,
                label,
                atomic,
            )
            state["timer"] = {
                **state["timer"],
                "script_sha256": sha(body),
                "delivery_updated_at": time.time(),
            }
            persist(root, state)
            return label
        if path.exists():
            require(
                not path.is_symlink() and path.read_bytes() == content,
                "TELEMETRY_RENEWAL_TIMER_CHANGED",
            )
        else:
            require(
                label.encode()
                not in native(["/bin/launchctl", "print-disabled", domain]),
                "TELEMETRY_RENEWAL_TIMER_DISABLED",
            )
            atomic(path, content)
            native(["/bin/launchctl", "bootstrap", domain, str(path)])
        require(
            str(script).encode()
            in native(["/bin/launchctl", "print", domain + "/" + label]),
            "TELEMETRY_RENEWAL_TIMER_UNCONFIRMED",
        )
        state["timer"] = {
            "label": label,
            "script_sha256": sha(body),
            "installed_at": time.time(),
        }
        persist(root, state)
        return label


def main() -> None:
    """Expose explicit, bounded operator actions."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "enroll",
            "once",
            "revoke",
            "install",
            "export-issuers",
            "bind-delivery",
            "upgrade-delivery",
            "check",
            "reject-transition-candidates",
        ),
    )
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--allow-expired", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--consumer-owner")
    parser.add_argument("--expected-helper")
    parser.add_argument("--max-age", type=int, default=180)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        require(
            args.expected_helper is None or args.action == "bind-delivery",
            "TELEMETRY_RENEWAL_DELIVERY_SCOPE_INVALID",
        )
        if args.action == "enroll":
            require(args.input is not None, "TELEMETRY_RENEWAL_INPUT_REQUIRED")
            enroll(args.state, read(args.input), allow_expired=args.allow_expired)
            result: dict[str, Any] = {"code": "TELEMETRY_RENEWAL_APPROVED"}
        elif args.action == "bind-delivery":
            require(
                args.input is not None
                and not args.force
                and not args.allow_expired
                and args.consumer_owner is None,
                "TELEMETRY_RENEWAL_INPUT_REQUIRED",
            )
            bind_delivery(
                args.state, read(args.input), expected_helper=args.expected_helper
            )
            result = {"code": "TELEMETRY_RENEWAL_DELIVERY_BOUND"}
        elif args.action == "once":
            result = once(args.state, force=args.force)
        elif args.action == "revoke":
            revoke(args.state)
            result = {"code": "TELEMETRY_RENEWAL_REVOKED"}
        elif args.action == "export-issuers":
            require(
                args.consumer_owner is not None
                and args.input is None
                and not args.force
                and not args.allow_expired,
                "TELEMETRY_RENEWAL_EXPORT_SCOPE_INVALID",
            )
            result = export_issuers(args.state, args.consumer_owner)
        elif args.action == "check":
            require(
                args.input is None
                and not args.force
                and not args.allow_expired
                and args.consumer_owner is None,
                "TELEMETRY_RENEWAL_CHECK_SCOPE_INVALID",
            )
            result = check(args.state, max_age=args.max_age)
        elif args.action == "reject-transition-candidates":
            require(
                args.input is None
                and not args.force
                and not args.allow_expired
                and args.consumer_owner is None,
                "TELEMETRY_RENEWAL_TRANSITION_REPAIR_FORBIDDEN",
            )
            result = reject_transition_candidates(args.state)
        else:
            result = {
                "code": "TELEMETRY_RENEWAL_TIMER_INSTALLED",
                "label": install(args.state, upgrade=args.action == "upgrade-delivery"),
            }
        sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
        if result.get("healthy") is False or result.get("ready") is False:
            raise SystemExit(1)
    except (RenewalError, OSError, ValueError, KeyError, TypeError) as error:
        code = "TELEMETRY_RENEWAL_OPERATOR_FAILED"
        if isinstance(error, RenewalError) and re.fullmatch(
            r"TELEMETRY_RENEWAL_[A-Z0-9_]{1,96}", str(error)
        ):
            code = str(error)
        if re.fullmatch(r"RENEWAL_DELIVERY_[A-Z_]{1,70}", str(error)):
            code = "TELEMETRY_" + str(error)
        sys.stdout.write(json.dumps({"code": code}) + "\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
