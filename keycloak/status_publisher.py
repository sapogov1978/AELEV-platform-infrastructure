"""Publish only this Keycloak installation's presentation and readiness."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import ssl
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import (
    HTTPSHandler,
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)
from uuid import UUID

PREFIX = "AELEV_INFRASTRUCTURE_KEYCLOAK_"
LOGGER = logging.getLogger("keycloak.status")


def event(name: str, **fields: Any) -> None:
    LOGGER.info(json.dumps({"event": name, **fields}))


def required(name: str) -> str:
    value = os.environ[PREFIX + name]
    if not value:
        raise ValueError("empty setting: " + name)
    return value


def required_secret(name: str) -> str:
    path = Path(required(name))
    if path.is_symlink() or not path.is_file():
        raise ValueError("regular secret file required: " + name)
    value = path.read_text(encoding="utf-8").strip()
    if not value or len(value) > 4096 or "\x00" in value:
        raise ValueError("invalid secret file: " + name)
    return value


def https_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or "\\" in value
        or any(c.isspace() for c in value)
    ):
        raise ValueError("unambiguous HTTPS URL required")
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    ui_url: str
    identity_url: str
    realm: str
    client_id: str
    audience: str
    module_id: str
    health_url: str
    interval: int
    timeout: int
    secret_root: Path
    state_path: Path
    public_url: str

    @classmethod
    def environment(cls) -> Settings:
        module_id = required("UI_MODULE_ID")
        if str(UUID(module_id)) != module_id:
            raise ValueError("canonical module UUID required")
        interval, timeout = int(required("UI_INTERVAL_SECONDS")), int(
            required("UI_TIMEOUT_SECONDS")
        )
        if min(interval, timeout) <= 0:
            raise ValueError("positive interval and timeout required")
        health_url = https_url(required("UI_HEALTH_URL"))
        if not health_url.endswith("/health/ready"):
            raise ValueError("Keycloak management /health/ready URL required")
        return cls(
            https_url(required("UI_URL")),
            https_url(required("ADMIN_ENDPOINT")),
            required("UI_REALM"),
            required("UI_CLIENT_ID"),
            required("UI_AUDIENCE"),
            module_id,
            health_url,
            interval,
            timeout,
            Path("/run/secrets/status"),
            Path("/var/lib/keycloak-status/outbox.sqlite"),
            https_url(required("PUBLIC_URL")),
        )

    def description(self) -> bytes:
        console_url = https_url(self.public_url) + "/admin/"
        return json.dumps(
            {
                "schema_version": 1,
                "module_id": self.module_id,
                "revision": 4,
                "ui_url": console_url,
                "service_kind": "INFRASTRUCTURE",
                "title": {
                    "ru": "Идентификация и доступ",
                    "en": "Identity and access",
                },
                "sections": [],
                "components": [
                    {
                        "id": "keycloak",
                        "title": {"ru": "Keycloak", "en": "Keycloak"},
                        "ui_url": console_url,
                    },
                    {
                        "id": "database",
                        "title": {"ru": "Keycloak Database", "en": "Keycloak Database"},
                    },
                    {
                        "id": "publisher",
                        "title": {
                            "ru": "Keycloak Status Publisher",
                            "en": "Keycloak Status Publisher",
                        },
                    },
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def request(
    context: ssl.SSLContext,
    url: str,
    timeout: int,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context), NoRedirect())
    try:
        reply = opener.open(
            Request(url, data=body, method=method, headers=headers or {}),
            timeout=timeout,
        )
    except HTTPError as error:
        reply = error
    with reply:
        payload = reply.read(65537)
        if len(payload) > 65536:
            raise ValueError("oversized response")
        return int(reply.code), payload


class Outbox:
    """Persist the exact pending observation and monotonic sequence before dispatch."""

    def __init__(self, path: Path, module_id: str) -> None:
        self.path = path
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), module TEXT NOT NULL, sequence INTEGER NOT NULL, pending BLOB, heartbeat REAL NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO state(id,module,sequence,pending,heartbeat) VALUES(1,?,0,NULL,0)",
                (module_id,),
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(state)")}
            if "withdrawal" not in columns:
                db.execute(
                    "ALTER TABLE state ADD COLUMN withdrawal INTEGER NOT NULL DEFAULT 0"
                )
            if db.execute("SELECT module FROM state").fetchone()[0] != module_id:
                raise ValueError("state volume belongs to another module")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def pending(self) -> bytes | None:
        with self.connect() as db:
            value = db.execute("SELECT pending FROM state").fetchone()[0]
            return bytes(value) if value is not None else None

    def prepare(
        self,
        state: str,
        observed_at: str,
        components: list[dict[str, str]] | None = None,
        *,
        active: bool = True,
    ) -> bytes:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            sequence, pending = db.execute(
                "SELECT sequence,pending FROM state"
            ).fetchone()
            if pending is not None:
                return bytes(pending)
            observation: dict[str, Any] = {
                "sequence": sequence + 1,
                "state": state,
                "observed_at": observed_at,
                "active": active,
            }
            if components is not None:
                observation["components"] = components
            body = json.dumps(
                observation,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            db.execute("UPDATE state SET sequence=?,pending=?", (sequence + 1, body))
            return body

    def acknowledge(self, body: bytes) -> None:
        with self.connect() as db:
            if (
                db.execute(
                    "UPDATE state SET pending=NULL,withdrawal=CASE WHEN ? THEN 0 ELSE withdrawal END WHERE pending=?",
                    (json.loads(body).get("active") is False, body),
                ).rowcount
                != 1
            ):
                raise ValueError("acknowledgement does not match pending observation")

    def begin_withdrawal(self) -> None:
        with self.connect() as db:
            db.execute("UPDATE state SET withdrawal=1")

    def withdrawing(self) -> bool:
        with self.connect() as db:
            return bool(db.execute("SELECT withdrawal FROM state").fetchone()[0])

    def heartbeat(self) -> None:
        with self.connect() as db:
            db.execute("UPDATE state SET heartbeat=?", (time.time(),))

    def backup_bytes(self) -> bytes:
        """Return a consistent SQLite backup, including committed WAL content."""
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as temporary:
            with self.connect() as source, sqlite3.connect(temporary.name) as target:
                source.backup(target)
            return Path(temporary.name).read_bytes()

    @classmethod
    def restore_bytes(cls, path: Path, module_id: str, payload: bytes) -> None:
        """Install a validated backup only into a fresh publisher volume."""
        if path.exists() or path.with_name(path.name + "-wal").exists():
            raise ValueError("publisher state already exists")
        if not payload or len(payload) > 64 * 1024 * 1024:
            raise ValueError("invalid publisher backup size")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".restore")
        try:
            with temporary.open("xb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            with sqlite3.connect(
                "file:" + str(temporary) + "?mode=ro&immutable=1", uri=True
            ) as db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("publisher backup integrity failure")
                row = db.execute("SELECT module FROM state WHERE id=1").fetchone()
                if row is None or row[0] != module_id:
                    raise ValueError("publisher backup belongs to another module")
            temporary.replace(path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)


class Publisher:
    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.identity = ssl.create_default_context(
            cafile=str(cfg.secret_root / "identity-ca.crt")
        )
        self.transport = ssl.create_default_context(
            cafile=str(cfg.secret_root / "shell-ca.crt")
        )
        self.transport.load_cert_chain(
            str(cfg.secret_root / "publisher.crt"),
            str(cfg.secret_root / "publisher.key"),
        )
        self.token = ""
        self.token_until = 0.0

    def access_token(self) -> str:
        if time.monotonic() < self.token_until:
            return self.token
        cfg = self.cfg
        form = urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": cfg.client_id,
                "client_secret": (cfg.secret_root / "client-secret")
                .read_text()
                .strip(),
            }
        ).encode()
        code, body = request(
            self.identity,
            cfg.identity_url
            + "/realms/"
            + quote(cfg.realm, safe="")
            + "/protocol/openid-connect/token",
            cfg.timeout,
            "POST",
            form,
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        if code != 200:
            raise RuntimeError("token request rejected")
        value = json.loads(body)
        token, expiry = value["access_token"], value["expires_in"]
        if (
            not isinstance(token, str)
            or not token
            or type(expiry) is not int
            or expiry <= 0
        ):
            raise ValueError("invalid token reply")
        self.token, self.token_until = token, time.monotonic() + max(0, expiry - 10)
        return self.token

    def health(self, url: str) -> tuple[str, dict[str, Any]]:
        try:
            code, body = request(self.identity, url, self.cfg.timeout)
            value = json.loads(body)
            if code == 200 and value.get("status") == "UP":
                return "ONLINE", value
            if code == 503 and value.get("status") == "DOWN":
                return "OFFLINE", value
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return "UNKNOWN", {}

    def probe(self) -> tuple[str, list[dict[str, str]]]:
        """Observe the process and its own database without storage or Docker access."""
        live_url = self.cfg.health_url.rsplit("/", 1)[0] + "/live"
        live, _ = self.health(live_url)
        live_at = datetime.now(timezone.utc).isoformat()
        ready, details = self.health(self.cfg.health_url)
        ready_at = datetime.now(timezone.utc).isoformat()
        database = "UNKNOWN"
        checks = details.get("checks")
        if isinstance(checks, list):
            matches = [
                check
                for check in checks
                if isinstance(check, dict)
                and check.get("name")
                == "Keycloak database connections async health check"
            ]
            if len(matches) == 1:
                database = {"UP": "ONLINE", "DOWN": "OFFLINE"}.get(
                    str(matches[0].get("status")), "UNKNOWN"
                )
        states = (live, ready, database)
        state = (
            "OFFLINE"
            if "OFFLINE" in states
            else "ONLINE" if all(item == "ONLINE" for item in states) else "UNKNOWN"
        )
        return state, [
            {"id": "keycloak", "state": live, "observed_at": live_at},
            {"id": "database", "state": database, "observed_at": ready_at},
        ]

    def publish(self, suffix: str, body: bytes) -> None:
        code, _ = request(
            self.transport,
            self.cfg.ui_url + "/api/modules/" + self.cfg.module_id + suffix,
            self.cfg.timeout,
            "PUT",
            body,
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.access_token(),
            },
        )
        if code != 200:
            if code == 401:
                self.token_until = 0
            raise RuntimeError("publication rejected: " + str(code))

    def cycle(self, outbox: Outbox) -> None:
        if outbox.withdrawing():
            self.withdraw(outbox)
        pending = outbox.pending()
        if pending is None:
            state, components = self.probe()
            observed_at = datetime.now(timezone.utc).isoformat()
            components.append(
                {"id": "publisher", "state": "ONLINE", "observed_at": observed_at}
            )
            pending = outbox.prepare(state, observed_at, components)
        self.publish("", self.cfg.description())
        self.publish("/status", pending)
        outbox.acknowledge(pending)
        event(
            "status_published",
            sequence=json.loads(pending)["sequence"],
            state=json.loads(pending)["state"],
        )

    def withdraw(self, outbox: Outbox) -> None:
        """Leave the live presentation only after an acknowledged status."""
        outbox.begin_withdrawal()
        self.publish("", self.cfg.description())
        pending = outbox.pending()
        if pending is not None and json.loads(pending).get("active") is not False:
            self.publish("/status", pending)
            outbox.acknowledge(pending)
        body = outbox.prepare(
            "UNKNOWN", datetime.now(timezone.utc).isoformat(), active=False
        )
        self.publish("/status", body)
        outbox.acknowledge(body)
        event("publisher_withdrawn", sequence=json.loads(body)["sequence"])


class RealmAdmin(Protocol):
    def __call__(self, method: str, path: str, value: Any = None) -> Any: ...


def realm_admin(cfg: Settings) -> RealmAdmin:
    """Create a deployment-only client for this delivery's realm."""
    context = ssl.create_default_context(
        cafile=str(cfg.secret_root / "identity-ca.crt")
    )
    form = urlencode(
        {
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": required("ADMIN_USER"),
            "password": required_secret("ADMIN_PASSWORD_FILE"),
        }
    ).encode()
    code, body = request(
        context,
        cfg.identity_url + "/realms/master/protocol/openid-connect/token",
        cfg.timeout,
        "POST",
        form,
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    if code != 200:
        raise RuntimeError("administrative token rejected")
    token = json.loads(body)["access_token"]

    def admin(method: str, path: str, value: Any = None) -> Any:
        data = json.dumps(value).encode() if value is not None else None
        code, reply = request(
            context,
            cfg.identity_url + "/admin/realms/" + quote(cfg.realm, safe="") + path,
            cfg.timeout,
            method,
            data,
            {"Content-Type": "application/json", "Authorization": "Bearer " + token},
        )
        if code not in (200, 201, 204):
            raise RuntimeError("publisher provisioning rejected: " + str(code))
        return json.loads(reply) if reply else None

    return admin


def configure_login_theme(admin: RealmAdmin) -> None:
    """Select our login theme without replacing realm or client settings."""
    realm = admin("GET", "", None)
    if realm.get("loginTheme") == "aelev":
        return
    if realm.get("loginTheme") not in (None, "", "keycloak", "keycloak.v2"):
        raise ValueError("refusing to replace a custom login theme")
    admin("PUT", "", {"loginTheme": "aelev"})
    if admin("GET", "", None).get("loginTheme") != "aelev":
        raise RuntimeError("login theme configuration was not applied")
    event("login_theme_configured")


def provision(cfg: Settings) -> None:
    """Manage our theme and publisher identity, never consumer identities."""
    admin = realm_admin(cfg)
    role = admin("GET", "/roles/platform-ui-publisher", None)
    clients = admin("GET", "/clients?clientId=" + quote(cfg.client_id, safe=""))
    description = {
        "clientId": cfg.client_id,
        "enabled": True,
        "publicClient": False,
        "serviceAccountsEnabled": True,
        "standardFlowEnabled": False,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "fullScopeAllowed": False,
        "defaultClientScopes": ["roles"],
        "attributes": {
            "aelev.owner": "keycloak-status",
            "aelev.module_id": cfg.module_id,
        },
        "protocolMappers": [
            {
                "name": "presentation-audience",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper",
                "config": {
                    "included.client.audience": cfg.audience,
                    "access.token.claim": "true",
                    "id.token.claim": "false",
                },
            }
        ],
    }
    if clients:
        current = clients[0]
        if (
            current.get("attributes", {}).get("aelev.owner") != "keycloak-status"
            or current.get("attributes", {}).get("aelev.module_id") != cfg.module_id
        ):
            raise ValueError("refusing to replace a foreign publisher identity")
        admin("PUT", "/clients/" + current["id"], description)
    else:
        admin("POST", "/clients", description)
        current = admin("GET", "/clients?clientId=" + quote(cfg.client_id, safe=""))[0]
    client_path = "/clients/" + current["id"]
    account = admin("GET", client_path + "/service-account-user")
    admin("POST", "/users/" + account["id"] + "/role-mappings/realm", [role])
    admin("POST", client_path + "/scope-mappings/realm", [role])
    secret = admin("GET", client_path + "/client-secret")["value"]
    destination = cfg.secret_root / "client-secret"
    temporary = destination.with_suffix(".pending")
    with temporary.open("w") as output:
        os.chmod(temporary, 0o640)
        output.write(secret + "\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(destination)
    event("publisher_identity_configured")
    configure_login_theme(admin)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=(
            "run",
            "once",
            "provision",
            "login-theme",
            "health",
            "backup",
            "restore",
            "verify-restored",
        ),
    )
    args = parser.parse_args()
    cfg = Settings.environment()
    if args.action == "login-theme":
        configure_login_theme(realm_admin(cfg))
        return
    if args.action == "provision":
        provision(cfg)
        return
    if args.action == "health":
        with sqlite3.connect(
            "file:" + str(cfg.state_path) + "?mode=ro", uri=True
        ) as db:
            heartbeat = float(db.execute("SELECT heartbeat FROM state").fetchone()[0])
        if time.time() - heartbeat > cfg.interval * 2 + cfg.timeout * 4:
            raise RuntimeError("publisher loop stalled")
        return
    if args.action == "backup":
        sys.stdout.buffer.write(Outbox(cfg.state_path, cfg.module_id).backup_bytes())
        return
    if args.action == "restore":
        Outbox.restore_bytes(cfg.state_path, cfg.module_id, sys.stdin.buffer.read())
        return
    if args.action == "verify-restored":
        with sqlite3.connect(
            "file:" + str(cfg.state_path) + "?mode=ro&immutable=1", uri=True
        ) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("publisher state integrity failure")
            row = db.execute("SELECT module FROM state WHERE id=1").fetchone()
            if row is None or row[0] != cfg.module_id:
                raise ValueError("publisher state belongs to another module")
        return
    outbox = Outbox(cfg.state_path, cfg.module_id)
    publisher = Publisher(cfg)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.is_set():
        try:
            publisher.cycle(outbox)
        except Exception as error:
            event("publication_failed", error_type=type(error).__name__)
            if args.action == "once":
                raise
        finally:
            outbox.heartbeat()
        if args.action == "once":
            return
        stop.wait(cfg.interval)
    try:
        publisher.withdraw(outbox)
    except Exception as error:
        event("withdrawal_unconfirmed", error_type=type(error).__name__)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        main()
    except Exception as error:
        event("publisher_stopped", error_type=type(error).__name__)
        sys.exit(1)
