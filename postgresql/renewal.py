"""Operator-only renewal of explicitly approved local SQL client certificates.

The existing CA remains the issuer. Runtime receives only its public leaf and a
bounded report; this module never changes roles, passwords, grants or trust.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import importlib
import json
import logging
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
import uuid

sys.dont_write_bytecode = True

LOGGER = logging.getLogger(__name__)
OWNER = "postgresql-client-renewal"
DAYS = 90
RENEW_BEFORE = 30 * 86400
FIELDS = {
    "schema_version",
    "environment",
    "role",
    "client_root",
    "certificate",
    "client_key",
    "client_ca",
    "url_file",
    "issuer_certificate",
    "issuer_key",
    "report",
    "openssl",
    "docker",
    "docker_endpoint",
    "daemon_id",
    "probe_image",
    "network",
}


def delivery_operator() -> Any:
    """Load only the shipped public standard-library helper."""
    try:
        return importlib.import_module("renewal_delivery")
    except ModuleNotFoundError:
        archive = Path(__file__).parent / "vendor/aelev-renewal-delivery-1.0.0.zip"
        require(
            archive.is_file() and not archive.is_symlink(),
            "PG_RENEWAL_DELIVERY_ARTIFACT_MISSING",
        )
        sys.path.insert(0, str(archive))
        return importlib.import_module("renewal_delivery")


PROBE = r"""
import json,sys,signal
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit,parse_qs,urlencode
def deadline(signum,frame):
 raise TimeoutError('SQL probe deadline')
signal.signal(signal.SIGALRM,deadline)
signal.alarm(120)
import psycopg
from psycopg.conninfo import conninfo_to_dict
def parameters(raw):
 assert raw.startswith(('postgresql://','postgresql+psycopg://'))
 uri=urlsplit(raw.replace('postgresql+psycopg://','postgresql://',1))
 query=parse_qs(uri.query,keep_blank_values=True)
 hosts=query.pop('host',[])
 assert all(len(values)==1 for values in query.values())
 canonical=urlunsplit(uri._replace(query=urlencode({k:v[0] for k,v in query.items()})))
 result=conninfo_to_dict(canonical)
 if len(hosts)>1 or (hosts and ':' in hosts[0] and ',' not in hosts[0]):
  assert 'port' not in query
  parsed=[urlsplit('//'+host) for host in hosts]
  assert all(host.hostname and not host.username and not host.password and not host.path for host in parsed)
  result['host']=','.join(host.hostname for host in parsed)
  result['port']=','.join(str(host.port or 5432) for host in parsed)
 elif hosts:
  result['host']=hosts[0]
 return result
try:
 root=Path('/client')
 config=json.loads(sys.argv[1])
 raw=(root/config['url_file']).read_text().strip()
 values=parameters(raw)
 assert values.get('user')==config['role']
 assert values.get('sslmode')=='verify-full'
 values.update(sslcert=str(root/config['certificate']),sslkey=str(root/config['client_key']),
               sslrootcert=str(root/config['client_ca']),connect_timeout=3,target_session_attrs='read-write',
               application_name='postgresql-client-renewal',
               options='-c statement_timeout=5000 -c lock_timeout=2000 -c idle_in_transaction_session_timeout=10000')
 with psycopg.connect(**values) as connection:
  assert connection.pgconn.ssl_in_use
  with connection.cursor() as cursor:
   cursor.execute('SET TRANSACTION READ ONLY')
   cursor.execute('SELECT 1')
   assert cursor.fetchone()==(1,)
 sys.stdout.write('SQL_VERIFY_OK\n')
except Exception as error:
 code='PG_RENEWAL_SQL_VERIFY_FAILED'
 detail=str(error).lower()
 state=getattr(error,'sqlstate',None)
 if isinstance(error,TimeoutError):
  code='PG_RENEWAL_SQL_PROBE_TIMEOUT'
 elif state in ('28000','28P01'):
  code='PG_RENEWAL_SQL_AUTHORIZATION_FAILED'
 elif 'certificate has expired' in detail or 'certificate expired' in detail:
  code='PG_RENEWAL_SQL_CERTIFICATE_EXPIRED'
 elif 'certificate verify failed' in detail or 'does not match host name' in detail:
  code='PG_RENEWAL_SQL_TLS_INVALID'
 elif 'connection refused' in detail or 'timeout expired' in detail or 'could not translate host' in detail:
  code='PG_RENEWAL_SQL_ENDPOINT_UNAVAILABLE'
 elif isinstance(error,AssertionError):
  code='PG_RENEWAL_SQL_PROBE_INVALID'
 sys.stdout.write(code+'\n')
"""


class RenewalError(ValueError):
    """Expose a stable safe code, never native stderr or a secret value."""


def require(condition: bool, code: str) -> None:
    """Fail closed with a bounded diagnostic."""
    if not condition:
        raise RenewalError(code)


def digest(data: bytes) -> str:
    """Hash exact material without including it in reports."""
    return hashlib.sha256(data).hexdigest()


def atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Replace an owner-controlled file and sync its containing directory."""
    require(not path.is_symlink(), "PG_RENEWAL_SYMLINK_REFUSED")
    fd, name = tempfile.mkstemp(prefix=".renewal-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def document(path: Path) -> dict[str, Any]:
    """Accept small JSON objects without duplicate keys or non-finite numbers."""

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            require(key not in result, "PG_RENEWAL_DUPLICATE_FIELD")
            result[key] = value
        return result

    def invalid(_: str) -> None:
        raise RenewalError("PG_RENEWAL_INVALID_JSON")

    require(
        not path.is_symlink() and path.stat().st_size < 32768,
        "PG_RENEWAL_INVALID_DOCUMENT",
    )
    value = json.loads(
        path.read_bytes(), object_pairs_hook=pairs, parse_constant=invalid
    )
    require(isinstance(value, dict), "PG_RENEWAL_INVALID_DOCUMENT")
    return dict(value)


def persist(root: Path, state: dict[str, Any]) -> None:
    """Commit private operator state, not a service database."""
    atomic(root / "state.json", json.dumps(state, sort_keys=True).encode())


@contextmanager
def locked(root: Path) -> Iterator[None]:
    """Serialize enrollment, renewal and revocation on the issuer host."""
    require(
        root.is_absolute()
        and root.is_dir()
        and not root.is_symlink()
        and not any(p.is_symlink() for p in root.parents)
        and root.stat().st_uid == os.getuid()
        and not root.stat().st_mode & 0o077,
        "PG_RENEWAL_PRIVATE_STATE_REQUIRED",
    )
    path = root / ".lock"
    require(not path.is_symlink(), "PG_RENEWAL_SYMLINK_REFUSED")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RenewalError("PG_RENEWAL_BUSY") from None
        yield


def regular(path: Path, *, private: bool = False) -> None:
    """Reject symlink substitution and writable private-key delivery."""
    require(
        path.is_absolute()
        and not any(p.is_symlink() for p in (path, *path.parents))
        and stat.S_ISREG(path.stat().st_mode),
        "PG_RENEWAL_FILE_INVALID",
    )
    if private:
        require(not path.stat().st_mode & 0o027, "PG_RENEWAL_KEY_PERMISSIONS_INVALID")


def execute(arguments: list[str], *, timeout: int = 30) -> bytes:
    """Keep native diagnostics inside the operator boundary."""
    try:
        result = subprocess.run(
            arguments, input=b"", capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RenewalError("PG_RENEWAL_NATIVE_UNAVAILABLE") from None
    require(result.returncode == 0, "PG_RENEWAL_NATIVE_VALIDATION_FAILED")
    return result.stdout


def openssl(config: dict[str, Any], *arguments: str) -> bytes:
    """Use the exact approved OpenSSL executable."""
    return execute([config["openssl"], *arguments])


def extensions(config: dict[str, Any], path: Path) -> bytes:
    """Preserve all source X.509 extensions, including nonstandard ones."""
    text = openssl(config, "x509", "-in", str(path), "-noout", "-text")
    marker = b"        X509v3 extensions:\n"
    require(marker in text, "PG_RENEWAL_EXTENSIONS_REQUIRED")
    return text.split(marker, 1)[1].split(b"    Signature Algorithm:", 1)[0]


def certificate_info(config: dict[str, Any], path: Path) -> dict[str, Any]:
    """Read public facts without importing an application or private SSL API."""
    subject = (
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
        .decode()
        .strip()
    )
    common_names = [
        item.removeprefix("CN=")
        for item in subject.removeprefix("subject=").split(",")
        if item.startswith("CN=")
    ]
    return {
        "role": common_names,
        "expires_at": expires_at(config, path),
        "sha256": digest(path.read_bytes()),
        "extensions_sha256": digest(extensions(config, path)),
    }


def expires_at(config: dict[str, Any], path: Path) -> float:
    """Return the leaf/issuer expiry from standard OpenSSL public output."""
    value = (
        openssl(config, "x509", "-in", str(path), "-noout", "-enddate").decode().strip()
    )
    require(value.startswith("notAfter="), "PG_RENEWAL_DATE_INVALID")
    return ssl.cert_time_to_seconds(value.removeprefix("notAfter="))


def validate_config(value: dict[str, Any]) -> dict[str, Any]:
    """Approve only existing local/test material, never a production fallback CA."""
    require(
        set(value) == FIELDS
        and type(value["schema_version"]) is int
        and value["schema_version"] == 1,
        "PG_RENEWAL_CONFIG_INVALID",
    )
    require(
        value["environment"] in {"local", "test"}, "PG_RENEWAL_PRODUCTION_CA_FORBIDDEN"
    )
    require(
        isinstance(value["role"], str)
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", value["role"]) is not None,
        "PG_RENEWAL_ROLE_INVALID",
    )
    for name in ("certificate", "client_key", "client_ca", "url_file"):
        require(
            isinstance(value[name], str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,120}", value[name])
            is not None,
            "PG_RENEWAL_CLIENT_FILE_INVALID",
        )
    root = Path(value["client_root"])
    require(
        root.is_absolute()
        and len(root.parts) > 3
        and root.is_dir()
        and not any(p.is_symlink() for p in (root, *root.parents)),
        "PG_RENEWAL_CLIENT_ROOT_INVALID",
    )
    for name in ("certificate", "client_key", "client_ca", "url_file"):
        regular(root / value[name], private=name in {"client_key", "url_file"})
    for name in ("issuer_certificate", "issuer_key"):
        regular(Path(value[name]), private=name == "issuer_key")
        require(
            not Path(value[name]).is_relative_to(root),
            "PG_RENEWAL_ISSUER_RUNTIME_EXPOSURE",
        )
    require(
        not Path(value["issuer_key"]).stat().st_mode & 0o077,
        "PG_RENEWAL_ISSUER_KEY_PERMISSIONS_INVALID",
    )
    for name in ("openssl", "docker"):
        path = Path(value[name]).resolve(strict=True)
        require(
            path.is_file() and os.access(path, os.X_OK), "PG_RENEWAL_EXECUTABLE_INVALID"
        )
        value[name] = str(path)
    require(
        isinstance(value["docker_endpoint"], str)
        and value["docker_endpoint"].startswith("unix:///"),
        "PG_RENEWAL_LOCAL_DAEMON_REQUIRED",
    )
    require(
        isinstance(value["daemon_id"], str)
        and str(uuid.UUID(value["daemon_id"])) == value["daemon_id"],
        "PG_RENEWAL_DAEMON_ID_INVALID",
    )
    require(
        isinstance(value["probe_image"], str)
        and re.fullmatch(r"sha256:[a-f0-9]{64}", value["probe_image"]) is not None,
        "PG_RENEWAL_PINNED_PROBE_REQUIRED",
    )
    require(
        isinstance(value["network"], str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,120}", value["network"])
        is not None,
        "PG_RENEWAL_NETWORK_INVALID",
    )
    report = Path(value["report"])
    require(
        report.is_absolute()
        and report.parent.is_dir()
        and not any(p.is_symlink() for p in (report, *report.parents))
        and report.suffix == ".json"
        and not report.is_relative_to(root)
        and not report.is_relative_to(Path(value["issuer_key"]).parent),
        "PG_RENEWAL_REPORT_ROOT_INVALID",
    )
    return value


def bindings(config: dict[str, Any]) -> dict[str, str]:
    """Bind key, roots, credentials and tools; certificate changes are journaled."""
    root = Path(config["client_root"])
    paths = {
        name: root / config[name] for name in ("client_key", "client_ca", "url_file")
    }
    paths.update(
        {
            name: Path(config[name])
            for name in ("issuer_certificate", "issuer_key", "openssl", "docker")
        }
    )
    return {name: digest(path.read_bytes()) for name, path in paths.items()}


def material(config: dict[str, Any], path: Path) -> dict[str, Any]:
    """Verify the same approved CN, key, CA and client-only certificate."""
    root = Path(config["client_root"])
    info = certificate_info(config, path)
    require(info["expires_at"] > time.time(), "PG_RENEWAL_CLIENT_CERTIFICATE_EXPIRED")
    require(info["role"] == [config["role"]], "PG_RENEWAL_IDENTITY_CHANGED")
    require(
        openssl(
            config, "x509", "-in", str(root / config["client_ca"]), "-outform", "DER"
        )
        == openssl(
            config, "x509", "-in", config["issuer_certificate"], "-outform", "DER"
        ),
        "PG_RENEWAL_TRUST_CHANGED",
    )
    openssl(
        config,
        "verify",
        "-CAfile",
        config["issuer_certificate"],
        "-purpose",
        "sslclient",
        str(path),
    )
    require(
        openssl(config, "x509", "-in", str(path), "-pubkey", "-noout")
        == openssl(config, "pkey", "-in", str(root / config["client_key"]), "-pubout"),
        "PG_RENEWAL_KEY_CHANGED",
    )
    ext = extensions(config, path)
    require(
        b"TLS Web Client Authentication" in ext
        and b"CA:FALSE" in ext
        and b"CA:TRUE" not in ext
        and b"TLS Web Server Authentication" not in ext,
        "PG_RENEWAL_CLIENT_SCOPE_INVALID",
    )
    return info


def sql_verify(config: dict[str, Any], certificate: str) -> None:
    """Use the pinned provider client, with read-only files and no issuer/socket."""
    root = Path(config["client_root"])
    key = (root / config["client_key"]).stat()
    probe = {
        name: config[name] for name in ("role", "client_key", "client_ca", "url_file")
    }
    probe["certificate"] = certificate
    daemon = execute(
        [
            config["docker"],
            "--host",
            config["docker_endpoint"],
            "info",
            "--format",
            "{{.ID}}",
        ]
    )
    require(daemon.decode().strip() == config["daemon_id"], "PG_RENEWAL_DAEMON_CHANGED")
    name = "aelev-postgresql-client-check-" + uuid.uuid4().hex
    try:
        output = execute(
            [
                config["docker"],
                "--host",
                config["docker_endpoint"],
                "run",
                "--rm",
                "--name",
                name,
                "--label",
                "io.aelev.owner=infrastructure-postgresql",
                "--label",
                "io.aelev.role=certificate-renewal-check",
                "--user",
                f"{key.st_uid}:{key.st_gid}",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--memory",
                "256m",
                "--cpus",
                "0.5",
                "--pids-limit",
                "64",
                "--network",
                config["network"],
                "--add-host",
                "host.docker.internal:host-gateway",
                "--tmpfs",
                "/tmp:size=8m,mode=1777",
                "--mount",
                f"type=bind,source={root},target=/client,readonly",
                "--entrypoint",
                "python",
                config["probe_image"],
                "-c",
                PROBE,
                json.dumps(probe),
            ],
            timeout=150,
        )
    except RenewalError:
        raise RenewalError("PG_RENEWAL_SQL_VERIFY_FAILED") from None
    finally:
        # A timed-out Docker CLI does not imply that its container exited.
        # Reconcile only this invocation's exact, labelled ephemeral identity.
        selected = subprocess.run(
            [
                config["docker"],
                "--host",
                config["docker_endpoint"],
                "inspect",
                "--format",
                '{{.Id}} {{index .Config.Labels "io.aelev.role"}}',
                name,
            ],
            capture_output=True,
            timeout=15,
            check=False,
        )
        if selected.returncode == 0:
            parts = selected.stdout.decode().strip().split()
            require(
                len(parts) == 2
                and re.fullmatch(r"[a-f0-9]{64}", parts[0]) is not None
                and parts[1] == "certificate-renewal-check",
                "PG_RENEWAL_PROBE_CLEANUP_REFUSED",
            )
            execute(
                [
                    config["docker"],
                    "--host",
                    config["docker_endpoint"],
                    "rm",
                    "--force",
                    parts[0],
                ]
            )
    result = output.decode().strip()
    allowed = {
        "PG_RENEWAL_SQL_VERIFY_FAILED",
        "PG_RENEWAL_SQL_AUTHORIZATION_FAILED",
        "PG_RENEWAL_SQL_CERTIFICATE_EXPIRED",
        "PG_RENEWAL_SQL_TLS_INVALID",
        "PG_RENEWAL_SQL_ENDPOINT_UNAVAILABLE",
        "PG_RENEWAL_SQL_PROBE_INVALID",
        "PG_RENEWAL_SQL_PROBE_TIMEOUT",
    }
    require(
        result == "SQL_VERIFY_OK",
        result if result in allowed else "PG_RENEWAL_SQL_VERIFY_FAILED",
    )


def publish(
    config: dict[str, Any],
    state: dict[str, Any],
    code: str,
    healthy: bool,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    """Deliver bounded public diagnostics; no paths, credentials or approvals."""
    report = {
        "owner": OWNER,
        "schema_version": 1,
        "healthy": healthy,
        "code": code,
        "checked_at": time.time(),
        "certificate_expires_at": state.get("expires_at"),
        "certificate_sha256": state.get("certificate_sha256"),
        "ca_expires_at": expires_at(config, Path(config["issuer_certificate"])),
        **{
            key: state.get(key)
            for key in (
                "last_success_at",
                "last_renewed_at",
                "last_verified_connection_at",
                "last_failure_at",
                "last_failure_code",
            )
        },
    }
    atomic(Path(config["report"]), json.dumps(report, sort_keys=True).encode(), 0o644)
    if root is not None and (
        (root / "delivery.json").exists() or (root / "delivery.json").is_symlink()
    ):
        renewal_delivery = delivery_operator()

        consumer = ""
        try:
            certificates = []
            if healthy:
                cert = Path(config["client_root"]) / config["certificate"]
                raw = cert.read_bytes()
                require(
                    digest(raw) == state["certificate_sha256"],
                    "PG_RENEWAL_LEAF_CHANGED",
                )
                certificates = [
                    {
                        "id": "client",
                        "certificate_base64": renewal_delivery.public_certificate(raw),
                        "issuer_certificate_base64": renewal_delivery.public_certificate(
                            Path(config["issuer_certificate"]).read_bytes()
                        ),
                    }
                ]
            consumer = document(root / "delivery.json")["spec"]["consumer_owner"]
            renewal_delivery.send(
                root, state["config_sha256"], consumer, report, certificates
            )
        except (
            renewal_delivery.DeliveryError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
        ) as error:
            detail = str(error)
            code = (
                "PG_" + detail
                if re.fullmatch(r"RENEWAL_DELIVERY_[A-Z_]{1,70}", detail)
                else (
                    detail
                    if isinstance(error, RenewalError)
                    else "PG_RENEWAL_DELIVERY_FAILED"
                )
            )
            report.update(healthy=False, code=code)
            state.update(last_failure_at=time.time(), last_failure_code=report["code"])
            report.update(
                last_failure_at=state["last_failure_at"],
                last_failure_code=report["code"],
            )
            persist(root, state)
            atomic(
                Path(config["report"]),
                json.dumps(report, sort_keys=True).encode(),
                0o644,
            )
            try:
                renewal_delivery.send(
                    root, state["config_sha256"], consumer, report, []
                )
            except (
                renewal_delivery.DeliveryError,
                OSError,
                ValueError,
                KeyError,
                TypeError,
            ):
                pass  # Host report stays negative; the previous volume report ages out.
    return report


def bind_delivery(
    root: Path, spec: dict[str, Any], *, expected_helper: str | None = None
) -> None:
    """Add only an output destination to the existing, unchanged approval."""
    renewal_delivery = delivery_operator()

    with locked(root):
        state, config = document(root / "state.json"), document(root / "approval.json")
        require(
            state.get("owner") == OWNER
            and not state.get("revoked")
            and digest(json.dumps(config, sort_keys=True).encode())
            == state["config_sha256"]
            and not state.get("intent"),
            "PG_RENEWAL_APPROVAL_INVALID",
        )
        validate_config(dict(config))
        require(
            bindings(config) == state["bindings"], "PG_RENEWAL_BOUND_MATERIAL_CHANGED"
        )
        info = material(config, Path(config["client_root"]) / config["certificate"])
        require(
            info["sha256"] == state["certificate_sha256"]
            and info["extensions_sha256"] == state["extensions_sha256"],
            "PG_RENEWAL_LEAF_CHANGED",
        )
        renewal_delivery.bind(
            root,
            spec,
            state["config_sha256"],
            spec["consumer_owner"],
            {"client"},
            atomic,
            expected_helper=expected_helper,
        )


def enroll(root: Path, config: dict[str, Any]) -> None:
    """Explicitly approve one existing identity; repeat never restores revocation."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with locked(root):
        config = validate_config(dict(config))
        encoded = json.dumps(config, sort_keys=True).encode()
        if (root / "state.json").exists():
            previous = document(root / "state.json")
            require(
                not previous.get("revoked")
                and previous["config_sha256"] == digest(encoded),
                "PG_RENEWAL_APPROVAL_REVOKED_OR_CHANGED",
            )
            return
        info = material(config, Path(config["client_root"]) / config["certificate"])
        sql_verify(config, config["certificate"])
        state = {
            "owner": OWNER,
            "schema_version": 1,
            "config_sha256": digest(encoded),
            "bindings": bindings(config),
            "certificate_sha256": info["sha256"],
            "extensions_sha256": info["extensions_sha256"],
            "expires_at": info["expires_at"],
            "revoked": False,
            "issued": 0,
            "last_success_at": time.time(),
            "last_verified_connection_at": time.time(),
            "events": [
                {
                    "code": "APPROVED",
                    "at": time.time(),
                    "certificate_sha256": info["sha256"],
                }
            ],
        }
        atomic(root / "approval.json", encoded)
        persist(root, state)
        publish(config, state, "PG_RENEWAL_APPROVED", True)


def once(root: Path, *, force: bool = False) -> dict[str, Any]:
    """Renew early, reconcile an interrupted publication, never regrant access."""
    with locked(root):
        state = document(root / "state.json")
        config = document(root / "approval.json")
        require(
            state.get("owner") == OWNER
            and state.get("schema_version") == 1
            and digest(json.dumps(config, sort_keys=True).encode())
            == state["config_sha256"],
            "PG_RENEWAL_APPROVAL_INVALID",
        )
        try:
            require(not state.get("revoked"), "PG_RENEWAL_APPROVAL_REVOKED")
            validate_config(dict(config))
            require(
                bindings(config) == state["bindings"],
                "PG_RENEWAL_BOUND_MATERIAL_CHANGED",
            )
            cert = Path(config["client_root"]) / config["certificate"]
            info = material(config, cert)
            intent = state.get("intent")
            if intent:
                require(
                    isinstance(intent, dict)
                    and set(intent) == {"candidate", "candidate_sha256", "old_sha256"}
                    and re.fullmatch(
                        r"\.sql-renewal-[a-f0-9]{32}\.crt", intent["candidate"]
                    )
                    is not None
                    and re.fullmatch(r"[a-f0-9]{64}", intent["candidate_sha256"])
                    is not None
                    and intent["old_sha256"] == state["certificate_sha256"],
                    "PG_RENEWAL_INTENT_INVALID",
                )
            if intent and info["sha256"] == intent["candidate_sha256"]:
                sql_verify(config, config["certificate"])
                require(
                    info["extensions_sha256"] == state["extensions_sha256"],
                    "PG_RENEWAL_SCOPE_CHANGED",
                )
                state.update(
                    certificate_sha256=info["sha256"],
                    expires_at=info["expires_at"],
                    last_renewed_at=time.time(),
                    last_verified_connection_at=time.time(),
                )
                state["events"].append(
                    {
                        "code": "PUBLICATION_RECONCILED",
                        "at": time.time(),
                        "certificate_sha256": info["sha256"],
                    }
                )
                state.pop("intent")
            else:
                require(
                    info["sha256"] == state["certificate_sha256"],
                    "PG_RENEWAL_LEAF_CHANGED",
                )
                require(
                    info["extensions_sha256"] == state["extensions_sha256"],
                    "PG_RENEWAL_SCOPE_CHANGED",
                )
                if force or intent or info["expires_at"] - time.time() <= RENEW_BEFORE:
                    sql_verify(config, config["certificate"])
                    openssl(
                        config,
                        "x509",
                        "-in",
                        config["issuer_certificate"],
                        "-noout",
                        "-checkend",
                        str((DAYS + 1) * 86400),
                    )
                    candidate = cert.parent / (
                        intent["candidate"]
                        if intent
                        else ".sql-renewal-" + uuid.uuid4().hex + ".crt"
                    )
                    if not intent:
                        descriptor = os.open(
                            candidate,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600,
                        )
                        os.close(descriptor)
                        request = cert.parent / (
                            ".sql-renewal-" + uuid.uuid4().hex + ".csr"
                        )
                        try:
                            openssl(
                                config, "x509", "-x509toreq", "-in", str(cert),
                                "-signkey", str(cert.parent / config["client_key"]),
                                "-copy_extensions", "copyall", "-out", str(request),
                            )
                            openssl(
                                config, "x509", "-req", "-in", str(request),
                                "-CA", config["issuer_certificate"],
                                "-CAkey", config["issuer_key"],
                                "-set_serial", str(secrets.randbits(128) | 1),
                                "-days", str(DAYS), "-sha256",
                                "-copy_extensions", "copyall",
                                "-out", str(candidate),
                            )
                        finally:
                            request.unlink(missing_ok=True)
                        fresh = material(config, candidate)
                        require(
                            fresh["extensions_sha256"] == state["extensions_sha256"],
                            "PG_RENEWAL_SCOPE_CHANGED",
                        )
                        metadata = cert.stat()
                        os.chown(candidate, metadata.st_uid, metadata.st_gid)
                        os.chmod(candidate, stat.S_IMODE(metadata.st_mode))
                        with candidate.open("rb") as handle:
                            os.fsync(handle.fileno())
                        state["intent"] = {
                            "candidate": candidate.name,
                            "candidate_sha256": fresh["sha256"],
                            "old_sha256": info["sha256"],
                        }
                        state["issued"] += 1
                        state["events"].append(
                            {
                                "code": "ISSUE_INTENT",
                                "at": time.time(),
                                "certificate_sha256": fresh["sha256"],
                            }
                        )
                        persist(root, state)
                    regular(candidate)
                    require(
                        digest(candidate.read_bytes())
                        == state["intent"]["candidate_sha256"],
                        "PG_RENEWAL_CANDIDATE_CHANGED",
                    )
                    sql_verify(config, candidate.name)
                    require(
                        bindings(config) == state["bindings"]
                        and digest(cert.read_bytes()) == info["sha256"],
                        "PG_RENEWAL_MATERIAL_CHANGED_DURING_CHECK",
                    )
                    os.replace(candidate, cert)
                    descriptor = os.open(cert.parent, os.O_RDONLY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    updated = material(config, cert)
                    state.update(
                        certificate_sha256=updated["sha256"],
                        expires_at=updated["expires_at"],
                        last_renewed_at=time.time(),
                        last_verified_connection_at=time.time(),
                    )
                    state["events"].append(
                        {
                            "code": "PUBLISHED",
                            "at": time.time(),
                            "certificate_sha256": updated["sha256"],
                        }
                    )
                    state.pop("intent")
            state["last_success_at"] = time.time()
            persist(root, state)
            return publish(config, state, "PG_RENEWAL_READY", True, root=root)
        except (RenewalError, OSError, ValueError, KeyError, TypeError) as error:
            code = (
                str(error)
                if isinstance(error, RenewalError)
                else "PG_RENEWAL_IO_OR_FORMAT_FAILED"
            )
            state.update(last_failure_at=time.time(), last_failure_code=code)
            persist(root, state)
            return publish(config, state, code, False, root=root)


def revoke(root: Path) -> None:
    """Stop future renewals; do not pretend to revoke an already issued X.509."""
    with locked(root):
        state = document(root / "state.json")
        if state.get("revoked"):
            return
        state["revoked"] = True
        state["events"].append({"code": "APPROVAL_REVOKED", "at": time.time()})
        persist(root, state)
        publish(
            document(root / "approval.json"),
            state,
            "PG_RENEWAL_APPROVAL_REVOKED",
            False,
            root=root,
        )


def install(root: Path, *, upgrade: bool = False) -> str:
    """Install the current user's exact local macOS operator task, without sudo."""
    require(platform.system() == "Darwin", "PG_RENEWAL_MACOS_TIMER_REQUIRED")
    require(not root.is_symlink(), "PG_RENEWAL_SYMLINK_REFUSED")
    root = root.resolve(strict=True)
    with locked(root):
        state = document(root / "state.json")
        require(not state["revoked"], "PG_RENEWAL_APPROVAL_REVOKED")
        body = Path(__file__).read_bytes()
        script = (
            delivery_operator().artifact(root, body, atomic)
            if upgrade or (root / "delivery.json").exists()
            else root / ("renewal-" + digest(body) + ".py")
        )
        if script.exists():
            require(
                script.read_bytes() == body, "PG_RENEWAL_INSTALLED_ARTIFACT_CHANGED"
            )
        else:
            atomic(script, body)
        label = "com.aelev.postgresql-renewal." + digest(str(root).encode())[:16]
        agents = Path.home() / "Library/LaunchAgents"
        agents.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = agents / (label + ".plist")
        executable = (
            delivery_operator().preserved_python(path, sys.executable)
            if upgrade
            else sys.executable
        )
        value = {
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
        content = plistlib.dumps(value)
        if upgrade:
            require(
                isinstance(state.get("timer"), dict)
                and state["timer"]["label"] == label,
                "PG_RENEWAL_EXISTING_TIMER_REQUIRED",
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
                "script_sha256": digest(body),
                "delivery_updated_at": time.time(),
            }
            persist(root, state)
            return label
        if path.exists():
            require(
                not path.is_symlink() and path.read_bytes() == content,
                "PG_RENEWAL_EXISTING_TIMER_CHANGED",
            )
            active = subprocess.run(
                ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            require(
                active.returncode == 0 and str(script).encode() in active.stdout,
                "PG_RENEWAL_TIMER_NOT_LOADED",
            )
            return label
        require(
            label.encode()
            not in execute(["/bin/launchctl", "print-disabled", f"gui/{os.getuid()}"]),
            "PG_RENEWAL_PREVIOUS_TIMER_REQUIRES_REVIEW",
        )
        atomic(path, content)
        execute(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)])
        loaded = execute(["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"])
        require(str(script).encode() in loaded, "PG_RENEWAL_TIMER_NOT_LOADED")
        state["timer"] = {
            "label": label,
            "script_sha256": digest(body),
            "installed_at": time.time(),
        }
        persist(root, state)
        return label


def main() -> None:
    """Expose explicit owner operations with safe structured output only."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "enroll",
            "once",
            "revoke",
            "install",
            "bind-delivery",
            "upgrade-delivery",
        ),
    )
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--expected-helper")
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(format='{"level":"%(levelname)s","message":"%(message)s"}')
    try:
        require(
            args.expected_helper is None or args.action == "bind-delivery",
            "PG_RENEWAL_DELIVERY_SCOPE_INVALID",
        )
        if args.action == "enroll":
            require(
                args.input is not None and not args.force, "PG_RENEWAL_INPUT_REQUIRED"
            )
            enroll(args.state, document(args.input))
            result: dict[str, Any] = {"code": "PG_RENEWAL_APPROVED"}
        elif args.action == "bind-delivery":
            require(
                args.input is not None and not args.force, "PG_RENEWAL_INPUT_REQUIRED"
            )
            bind_delivery(
                args.state, document(args.input), expected_helper=args.expected_helper
            )
            result = {"code": "PG_RENEWAL_DELIVERY_BOUND"}
        elif args.action == "once":
            result = once(args.state, force=args.force)
        elif args.action == "revoke":
            revoke(args.state)
            result = {"code": "PG_RENEWAL_APPROVAL_REVOKED"}
        else:
            result = {
                "code": "PG_RENEWAL_TIMER_INSTALLED",
                "label": install(args.state, upgrade=args.action == "upgrade-delivery"),
            }
        sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
        if result.get("healthy") is False:
            raise SystemExit(1)
    except (RenewalError, OSError, ValueError, KeyError, TypeError) as error:
        code = (
            str(error)
            if isinstance(error, RenewalError)
            else "PG_RENEWAL_INPUT_IO_FAILED"
        )
        if re.fullmatch(r"RENEWAL_DELIVERY_[A-Z_]{1,70}", str(error)):
            code = "PG_" + str(error)
        LOGGER.error("%s", code)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
