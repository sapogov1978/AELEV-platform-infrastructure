"""Operator-only local PKI renewal; never loaded in a gateway or client runtime."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import ssl
import stat
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from authorization import Grant, GrantRejected, identity_filename, strict_json
from provision import apply_locked, audit, mutation_lock, sync_directory, verify_audit

LOGGER = logging.getLogger("aelev.egress.renewal")
RENEW_BEFORE = timedelta(days=3)


class RenewalBlocked(ValueError):
    """A bounded, safe diagnostic code, never an exception payload."""


def read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or path.stat().st_size > 65536:
        raise RenewalBlocked("INVALID_OPERATOR_FILE")
    value = strict_json(path.read_text())
    if not isinstance(value, dict):
        raise RenewalBlocked("INVALID_OPERATOR_FILE")
    return cast(dict[str, Any], value)


def atomic(path: Path, content: bytes, mode: int = 0o644) -> None:
    """Publish a complete file and preserve directory durability."""
    descriptor, temporary = tempfile.mkstemp(prefix=".renewal-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if path.exists():
                os.fchown(stream.fileno(), -1, path.stat().st_gid)
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def store(path: Path, value: dict[str, Any], mode: int = 0o644) -> None:
    atomic(path, (json.dumps(value, sort_keys=True) + "\n").encode(), mode)


def openssl(*arguments: str) -> str:
    try:
        return subprocess.run(
            ["openssl", *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError) as error:
        raise RenewalBlocked("PKI_COMMAND_FAILED") from error


def expiry(certificate: Path) -> datetime:
    value = openssl("x509", "-in", str(certificate), "-noout", "-enddate")
    return datetime.strptime(
        value.removeprefix("notAfter="), "%b %d %H:%M:%S %Y %Z"
    ).replace(tzinfo=UTC)


def scope(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "expires_at"}


def grant_digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def audit_guard(approval: dict[str, Any]) -> None:
    """A revoke remains final even if another operator later reapplies the grant."""
    journal = Path(approval["audit_directory"]) / "operator-audit.jsonl"
    verify_audit(journal)
    identity_hash = hashlib.sha256(approval["identity"].encode()).hexdigest()
    pending: set[str] = set()
    with journal.open() as stream:
        for line in stream:
            record = json.loads(line)
            if record["identity_hash"] != identity_hash:
                continue
            if record["sequence"] <= approval["audit_sequence"]:
                continue
            if record["operation"] == "revoke":
                raise RenewalBlocked("APPROVAL_REVOKED")
            if record["phase"] == "INTENT_RECORDED":
                pending.add(record["event_id"])
            else:
                pending.discard(record["event_id"])
    if pending:
        raise RenewalBlocked("AUDIT_RECONCILIATION_REQUIRED")


def describe(state: Path) -> dict[str, Any]:
    """Describe one client approval without creating files or renewing authority."""

    def load(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise RenewalBlocked("INVALID_OPERATOR_FILE")
        return read_json(path)

    if any(path.is_symlink() for path in (state, *state.parents)):
        raise RenewalBlocked("INVALID_OPERATOR_FILE")
    approval = load(state / "approval.json")
    if approval.get("kind") is not None:
        raise RenewalBlocked("CLIENT_APPROVAL_REQUIRED")
    if (
        type(approval.get("schema_version")) is not int
        or approval["schema_version"] != 1
        or type(approval.get("audit_sequence")) is not int
        or approval["audit_sequence"] < 0
    ):
        raise RenewalBlocked("INVALID_OPERATOR_FILE")
    identity_filename(approval["identity"])
    audit_root = Path(approval["audit_directory"])
    if any(path.is_symlink() for path in (audit_root, *audit_root.parents)):
        raise RenewalBlocked("INVALID_OPERATOR_FILE")
    # Read an existing lock only: even an absent lock must not be created by
    # a read-only command. LOCK_SH excludes apply/revoke/renewal's LOCK_EX.
    descriptor = os.open(
        audit_root / ".operator-mutation.lock",
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
    )
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RenewalBlocked("INVALID_OPERATOR_FILE")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RenewalBlocked("OPERATOR_MUTATION_IN_PROGRESS") from None
        if load(state / "approval.json") != approval:
            raise RenewalBlocked("APPROVAL_CHANGED")
        sequence, _ = verify_audit(audit_root / "operator-audit.jsonl")
        if sequence < approval["audit_sequence"]:
            raise RenewalBlocked("AUDIT_RECONCILIATION_REQUIRED")
        audit_guard(approval)
        issuer = Path(approval["issuer"]) / "ca.pem"
        if not issuer.is_file() or issuer.is_symlink() or issuer.stat().st_size > 65536:
            raise RenewalBlocked("INVALID_OPERATOR_FILE")
        ca_digest = hashlib.sha256(issuer.read_bytes()).hexdigest()
        if ca_digest != approval["issuer_sha256"]:
            raise RenewalBlocked("ISSUER_CHANGED")
        grant_path = Path(approval["grants_directory"]) / identity_filename(
            approval["identity"]
        )
        if not grant_path.exists():
            raise RenewalBlocked("GRANT_REVOKED_OR_MISSING")
        current = load(grant_path)
        Grant.parse(current)
        if scope(current) != scope(approval["grant"]):
            raise RenewalBlocked("APPROVED_SCOPE_CHANGED")
        if grant_digest(current) != load(state / "expected-grant.json")["digest"]:
            raise RenewalBlocked("CURRENT_GRANT_CHANGED")
        client, status = Path(approval["client_directory"]), Path(
            approval["status_directory"]
        )
        for path in (client, status):
            if (
                not path.is_absolute()
                or ".." in path.parts
                or any(c in str(path) for c in "\r\n\x00")
                or any(p.is_symlink() for p in (path, *path.parents))
            ):
                raise RenewalBlocked("INVALID_OPERATOR_FILE")
        return {
            "owner": "public-egress-gateway",
            "schema_version": 1,
            "code": "EGRESS_RENEWAL_BINDING_VERIFIED",
            "identity": approval["identity"],
            "ca_sha256": ca_digest,
            "scope_sha256": grant_digest(scope(approval["grant"])),
            "certificate_path": str(client / "client.crt"),
            "ca_path": str(client / "ca.crt"),
            "status_path": str(status / "status.json"),
        }
    finally:
        os.close(descriptor)


def renew_certificate(
    certificate: Path,
    key: Path,
    issuer: Path,
    *,
    identity: str | None,
) -> None:
    """Renew the certificate atomically against its unchanged private key."""
    alternative = openssl(
        "x509", "-in", str(certificate), "-noout", "-ext", "subjectAltName"
    ).splitlines()
    if len(alternative) != 2:
        raise RenewalBlocked("CERTIFICATE_SAN_INVALID")
    san = alternative[1].strip().replace("IP Address:", "IP:")
    if identity is not None and san != "URI:" + identity:
        raise RenewalBlocked("CERTIFICATE_IDENTITY_MISMATCH")
    usage = "clientAuth" if identity else "serverAuth"
    with tempfile.TemporaryDirectory(
        prefix=".certificate-", dir=certificate.parent
    ) as temporary:
        root = Path(temporary)
        request, extension, candidate = (
            root / "request",
            root / "extensions",
            root / "certificate",
        )
        extension.write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n"
            f"extendedKeyUsage={usage}\nsubjectAltName={san}\n"
        )
        openssl(
            "req",
            "-new",
            "-key",
            str(key),
            "-subj",
            "/CN=AELEV-egress-identity",
            "-out",
            str(request),
        )
        openssl(
            "x509",
            "-req",
            "-in",
            str(request),
            "-CA",
            str(issuer / "ca.pem"),
            "-CAkey",
            str(issuer / "ca.key"),
            "-set_serial",
            str(int.from_bytes(os.urandom(16), "big")),
            "-days",
            "7",
            "-sha256",
            "-extfile",
            str(extension),
            "-out",
            str(candidate),
        )
        openssl(
            "verify",
            "-x509_strict",
            "-purpose",
            "sslclient" if identity else "sslserver",
            "-CAfile",
            str(issuer / "ca.pem"),
            str(candidate),
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_cert_chain(str(candidate), str(key))
        atomic(certificate, candidate.read_bytes(), certificate.stat().st_mode & 0o777)


def enroll(args: argparse.Namespace) -> None:
    """Explicitly approve automatic same-scope extension, never workload approval."""
    state = args.state_directory.resolve()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    state.chmod(0o700)
    target = state / "approval.json"
    if target.exists():
        raise RenewalBlocked("APPROVAL_ALREADY_EXISTS")
    grants, audit_root = args.grants_directory.resolve(), args.audit_directory.resolve()
    with mutation_lock(audit_root):
        value = read_json(args.grant)
        grant = Grant.parse(value)
        current = read_json(grants / identity_filename(grant.identity))
        if current != value:
            raise RenewalBlocked("APPROVAL_GRANT_MISMATCH")
        sequence, _ = verify_audit(audit_root / "operator-audit.jsonl")
        client, issuer = args.client_directory.resolve(), args.issuer.resolve()
        openssl(
            "verify",
            "-x509_strict",
            "-purpose",
            "sslclient",
            "-CAfile",
            str(issuer / "ca.pem"),
            str(client / "client.crt"),
        )
        san = (
            openssl(
                "x509",
                "-in",
                str(client / "client.crt"),
                "-noout",
                "-ext",
                "subjectAltName",
            )
            .splitlines()[-1]
            .strip()
        )
        if san != "URI:" + grant.identity:
            raise RenewalBlocked("CERTIFICATE_IDENTITY_MISMATCH")
        approval = {
            "schema_version": 1,
            "identity": grant.identity,
            "grant": value,
            "audit_sequence": sequence,
            "actor": args.actor,
            "grants_directory": str(grants),
            "audit_directory": str(audit_root),
            "issuer": str(issuer),
            "issuer_sha256": hashlib.sha256(
                (issuer / "ca.pem").read_bytes()
            ).hexdigest(),
            "client_directory": str(client),
            "status_directory": str(args.status_directory.resolve()),
            "server_directory": (
                str(args.server_directory.resolve()) if args.server_directory else None
            ),
        }
        digest = hashlib.sha256(
            json.dumps(approval, sort_keys=True).encode()
        ).hexdigest()
        # Enrollment is durable and auditable; it never creates a grant.
        event = os.urandom(16).hex()
        audit(
            audit_root,
            "approve_renewal",
            grant.identity,
            digest,
            args.actor,
            event,
            "INTENT_RECORDED",
        )
        store(target, approval, 0o600)
        store(state / "expected-grant.json", {"digest": grant_digest(current)}, 0o600)
        audit(
            audit_root,
            "approve_renewal",
            grant.identity,
            digest,
            args.actor,
            event,
            "MUTATION_COMMITTED",
        )


def run(
    state: Path,
    *,
    renew_now: bool = False,
    renew_certificate_now: bool = False,
) -> dict[str, Any]:
    """One bounded cycle suitable for launchd/systemd timers; report every failure."""
    approval = read_json(state / "approval.json")
    if approval.get("kind") == "SERVER":
        return run_server(
            approval,
            renew_now=renew_now or renew_certificate_now,
        )
    status_path = Path(approval["status_directory"]) / "status.json"
    previous = read_json(status_path) if status_path.exists() else {}
    now = datetime.now(UTC)
    report: dict[str, Any] = {
        "schema_version": 1,
        "checked_at": now.timestamp(),
        "healthy": False,
        "code": "RENEWAL_FAILED",
        "last_success_at": previous.get("last_success_at"),
        "last_renewed_at": previous.get("last_renewed_at"),
        "last_failure_at": previous.get("last_failure_at"),
        "last_failure_code": previous.get("last_failure_code"),
        "last_failure_stage": previous.get("last_failure_stage"),
        "last_failure_type": previous.get("last_failure_type"),
        "stage": "VERIFY_AUDIT",
    }
    try:
        with mutation_lock(Path(approval["audit_directory"])):
            audit_guard(approval)
            report["stage"] = "CHECK_ISSUER"
            issuer = Path(approval["issuer"])
            if (
                hashlib.sha256((issuer / "ca.pem").read_bytes()).hexdigest()
                != approval["issuer_sha256"]
            ):
                raise RenewalBlocked("ISSUER_CHANGED")
            ca_expiry = expiry(issuer / "ca.pem")
            report["ca_expires_at"] = ca_expiry.timestamp()
            client = Path(approval["client_directory"])
            report["stage"] = "CHECK_CLIENT_CERTIFICATE"
            cert = client / "client.crt"
            report["certificate_expires_at"] = expiry(cert).timestamp()
            san = (
                openssl("x509", "-in", str(cert), "-noout", "-ext", "subjectAltName")
                .splitlines()[-1]
                .strip()
            )
            if san != "URI:" + approval["identity"]:
                raise RenewalBlocked("CERTIFICATE_IDENTITY_MISMATCH")
            openssl(
                "verify",
                "-x509_strict",
                "-purpose",
                "sslclient",
                "-CAfile",
                str(issuer / "ca.pem"),
                str(cert),
            )
            key_check = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            key_check.load_cert_chain(str(cert), str(client / "client.key"))
            grant_file = Path(approval["grants_directory"]) / identity_filename(
                approval["identity"]
            )
            report["stage"] = "CHECK_GRANT"
            if not grant_file.exists():
                raise RenewalBlocked("GRANT_REVOKED_OR_MISSING")
            value = read_json(grant_file)
            report["grant_expires_at"] = datetime.fromisoformat(
                value["expires_at"]
            ).timestamp()
            Grant.parse(value)
            if scope(value) != scope(approval["grant"]):
                raise RenewalBlocked("APPROVED_SCOPE_CHANGED")
            expected = read_json(state / "expected-grant.json")
            if grant_digest(value) != expected["digest"]:
                raise RenewalBlocked("CURRENT_GRANT_CHANGED")
            if ca_expiry <= now + timedelta(days=8):
                raise RenewalBlocked("CA_REPLACEMENT_REQUIRED")
            renewed = False
            server = (
                Path(approval["server_directory"])
                if approval["server_directory"]
                else None
            )
            certificates = [(cert, client / "client.key", approval["identity"])]
            # A workload approval never controls renewal of shared server TLS.
            for certificate, key, identity in certificates:
                report["stage"] = (
                    "CHECK_CLIENT_CERTIFICATE"
                    if identity
                    else "CHECK_SERVER_CERTIFICATE"
                )
                before = expiry(certificate)
                if renew_now or renew_certificate_now or before <= now + RENEW_BEFORE:
                    report["stage"] = (
                        "RENEW_CLIENT_CERTIFICATE"
                        if identity
                        else "RENEW_SERVER_CERTIFICATE"
                    )
                    if before <= now:
                        raise RenewalBlocked("CERTIFICATE_EXPIRED")
                    event = os.urandom(16).hex()
                    audit_root = Path(approval["audit_directory"])
                    name = (
                        "renew_client_certificate"
                        if identity
                        else "renew_server_certificate"
                    )
                    audit(
                        audit_root,
                        name,
                        approval["identity"],
                        "",
                        approval["actor"],
                        event,
                        "INTENT_RECORDED",
                    )
                    renew_certificate(certificate, key, issuer, identity=identity)
                    digest = hashlib.sha256(certificate.read_bytes()).hexdigest()
                    audit(
                        audit_root,
                        name,
                        approval["identity"],
                        digest,
                        approval["actor"],
                        event,
                        "MUTATION_COMMITTED",
                    )
                    renewed = True
            if (
                renew_now
                or datetime.fromisoformat(value["expires_at"]) <= now + RENEW_BEFORE
            ):
                report["stage"] = "RENEW_GRANT"
                candidate = value | {
                    "expires_at": (now + timedelta(days=7)).isoformat()
                }
                with tempfile.TemporaryDirectory(
                    prefix=".grant-", dir=state
                ) as temporary:
                    source = Path(temporary) / "grant.json"
                    store(source, candidate, 0o600)
                    apply_locked(
                        source,
                        Path(approval["grants_directory"]),
                        Path(approval["audit_directory"]),
                        approval["actor"],
                    )
                # This is a read-only copy for the owner, not an authority source.
                store(
                    state / "expected-grant.json",
                    {"digest": grant_digest(candidate)},
                    0o600,
                )
                store(client / "grant.json", candidate)
                value = candidate
                renewed = True
            report.update(
                {
                    "healthy": True,
                    "code": "READY",
                    "stage": "READY",
                    "last_success_at": now.timestamp(),
                    "certificate_expires_at": expiry(cert).timestamp(),
                    "grant_expires_at": datetime.fromisoformat(
                        value["expires_at"]
                    ).timestamp(),
                }
            )
            if server:
                report["server_certificate_expires_at"] = expiry(
                    server / "server.pem"
                ).timestamp()
            if renewed:
                report["last_renewed_at"] = now.timestamp()
    except (OSError, ValueError, ssl.SSLError, TypeError, KeyError) as error:
        report["code"] = (
            str(error)
            if isinstance(error, RenewalBlocked | GrantRejected)
            else "RENEWAL_IO_OR_VALIDATION_FAILED"
        )
        report["last_failure_at"] = now.timestamp()
        report["last_failure_code"] = report["code"]
        report["last_failure_stage"] = report["stage"]
        report["last_failure_type"] = type(error).__name__
        LOGGER.error(
            "egress_renewal_failed",
            extra={"code": report["code"], "error_type": type(error).__name__},
        )
    store(status_path, report)
    return report


def enroll_server(args: argparse.Namespace) -> None:
    """Approve gateway TLS independently of every workload grant."""
    identity_filename(args.identity)
    state = args.state_directory.resolve()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    state.chmod(0o700)
    target = state / "approval.json"
    if target.exists():
        raise RenewalBlocked("APPROVAL_ALREADY_EXISTS")
    audit_root = args.audit_directory.resolve()
    server, issuer = args.server_directory.resolve(), args.issuer.resolve()
    with mutation_lock(audit_root):
        sequence, _ = verify_audit(audit_root / "operator-audit.jsonl")
        san = (
            openssl(
                "x509",
                "-in",
                str(server / "server.pem"),
                "-noout",
                "-ext",
                "subjectAltName",
            )
            .splitlines()[-1]
            .strip()
        )
        approval = {
            "schema_version": 1,
            "kind": "SERVER",
            "identity": args.identity,
            "actor": args.actor,
            "audit_sequence": sequence,
            "audit_directory": str(audit_root),
            "server_directory": str(server),
            "server_san": san,
            "issuer": str(issuer),
            "issuer_sha256": hashlib.sha256(
                (issuer / "ca.pem").read_bytes()
            ).hexdigest(),
            "status_directory": str(args.status_directory.resolve()),
        }
        event = os.urandom(16).hex()
        digest = grant_digest(approval)
        audit(
            audit_root,
            "approve_server_renewal",
            args.identity,
            digest,
            args.actor,
            event,
            "INTENT_RECORDED",
        )
        store(target, approval, 0o600)
        audit(
            audit_root,
            "approve_server_renewal",
            args.identity,
            digest,
            args.actor,
            event,
            "MUTATION_COMMITTED",
        )


def run_server(approval: dict[str, Any], *, renew_now: bool) -> dict[str, Any]:
    """Server PKI has no client grant, client files or client revocation dependency."""
    status = Path(approval["status_directory"]) / "server-status.json"
    previous = read_json(status) if status.exists() else {}
    now = datetime.now(UTC)
    report: dict[str, Any] = {
        "schema_version": 1,
        "checked_at": now.timestamp(),
        "healthy": False,
        "code": "RENEWAL_FAILED",
        "stage": "VERIFY_AUDIT",
        **{
            name: previous.get(name)
            for name in (
                "last_success_at",
                "last_renewed_at",
                "last_failure_at",
                "last_failure_code",
                "last_failure_stage",
                "last_failure_type",
            )
        },
    }
    try:
        with mutation_lock(Path(approval["audit_directory"])):
            audit_guard(approval)
            report["stage"] = "CHECK_ISSUER"
            issuer = Path(approval["issuer"])
            if (
                hashlib.sha256((issuer / "ca.pem").read_bytes()).hexdigest()
                != approval["issuer_sha256"]
            ):
                raise RenewalBlocked("ISSUER_CHANGED")
            ca_expiry = expiry(issuer / "ca.pem")
            report["ca_expires_at"] = ca_expiry.timestamp()
            if ca_expiry <= now + timedelta(days=8):
                raise RenewalBlocked("CA_REPLACEMENT_REQUIRED")
            report["stage"] = "CHECK_SERVER_CERTIFICATE"
            server = Path(approval["server_directory"])
            cert = server / "server.pem"
            before = expiry(cert)
            report["certificate_expires_at"] = before.timestamp()
            san = (
                openssl("x509", "-in", str(cert), "-noout", "-ext", "subjectAltName")
                .splitlines()[-1]
                .strip()
            )
            if san != approval["server_san"]:
                raise RenewalBlocked("SERVER_SAN_CHANGED")
            if before <= now:
                raise RenewalBlocked("CERTIFICATE_EXPIRED")
            if renew_now or before <= now + RENEW_BEFORE:
                report["stage"] = "RENEW_SERVER_CERTIFICATE"
                event = os.urandom(16).hex()
                audit_root = Path(approval["audit_directory"])
                audit(
                    audit_root,
                    "renew_server_certificate",
                    approval["identity"],
                    "",
                    approval["actor"],
                    event,
                    "INTENT_RECORDED",
                )
                renew_certificate(cert, server / "server.key", issuer, identity=None)
                digest = hashlib.sha256(cert.read_bytes()).hexdigest()
                audit(
                    audit_root,
                    "renew_server_certificate",
                    approval["identity"],
                    digest,
                    approval["actor"],
                    event,
                    "MUTATION_COMMITTED",
                )
                report["last_renewed_at"] = now.timestamp()
            report.update(
                healthy=True,
                code="READY",
                stage="READY",
                last_success_at=now.timestamp(),
                certificate_expires_at=expiry(cert).timestamp(),
            )
    except (OSError, ValueError, ssl.SSLError, TypeError, KeyError) as error:
        report.update(
            code=(
                str(error)
                if isinstance(error, RenewalBlocked | GrantRejected)
                else "RENEWAL_IO_OR_VALIDATION_FAILED"
            ),
            last_failure_at=now.timestamp(),
            last_failure_stage=report["stage"],
            last_failure_type=type(error).__name__,
        )
        report["last_failure_code"] = report["code"]
    store(status, report)
    return report


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    approve = commands.add_parser("enroll")
    for name in (
        "state-directory",
        "grant",
        "issuer",
        "client-directory",
        "status-directory",
        "grants-directory",
        "audit-directory",
    ):
        approve.add_argument("--" + name, required=True, type=Path)
    approve.add_argument("--server-directory", type=Path)
    approve.add_argument("--actor", required=True)
    server = commands.add_parser("enroll-server")
    for name in (
        "state-directory",
        "issuer",
        "server-directory",
        "status-directory",
        "audit-directory",
    ):
        server.add_argument("--" + name, required=True, type=Path)
    server.add_argument("--identity", required=True)
    server.add_argument("--actor", required=True)
    once = commands.add_parser("once")
    once.add_argument("--state-directory", required=True, type=Path)
    once.add_argument("--renew-now", action="store_true")
    once.add_argument("--renew-certificate-now", action="store_true")
    description = commands.add_parser("describe")
    description.add_argument("--state-directory", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "enroll":
            enroll(args)
        elif args.command == "enroll-server":
            enroll_server(args)
        elif args.command == "describe":
            # Machine-readable stdout is a closed projection, not the audit or
            # a log dump. Existing mutating commands retain their old output.
            sys.stdout.buffer.write(
                (
                    json.dumps(describe(args.state_directory), sort_keys=True) + "\n"
                ).encode()
            )
        else:
            result = run(
                args.state_directory,
                renew_now=args.renew_now,
                renew_certificate_now=args.renew_certificate_now,
            )
            LOGGER.info(json.dumps(result, sort_keys=True))
            if not result["healthy"]:
                raise SystemExit(1)
    except (ValueError, OSError, TypeError, KeyError) as error:
        LOGGER.error(
            json.dumps(
                {
                    "code": (
                        str(error)
                        if isinstance(error, RenewalBlocked)
                        or args.command == "describe"
                        and isinstance(error, GrantRejected)
                        else "OPERATOR_CONFIGURATION_FAILED"
                    ),
                    "error_type": type(error).__name__,
                }
            )
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
