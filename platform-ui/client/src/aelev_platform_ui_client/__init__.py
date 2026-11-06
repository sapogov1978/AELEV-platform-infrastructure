"""Public transport for owner-supplied descriptions; no product catalogue."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import ssl
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
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

LOGGER = logging.getLogger("presentation.transport")
Observation = dict[str, str]


def timestamp() -> str:
    """Return the observation time, not a replacement receipt time."""
    return datetime.now(timezone.utc).isoformat()


def https_url(value: str, *, allow_query: bool = False) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or (parsed.query and not allow_query)
        or parsed.fragment
        or "\\" in value
        or any(character.isspace() for character in value)
    ):
        raise ValueError("unambiguous HTTPS URL required")
    return value.rstrip("/")


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
    """Use verified TLS, no proxy/redirect, and a bounded response."""
    url = https_url(url, allow_query=True)
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


def tls(
    root: Path, ca: str, cert: str | None = None, key: str | None = None
) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=str(root / ca))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if cert is not None and key is not None:
        context.load_cert_chain(root / cert, root / key)
    return context


def aggregate(components: list[Observation]) -> str:
    states = [item["state"] for item in components]
    return (
        "OFFLINE"
        if "OFFLINE" in states
        else (
            "ONLINE"
            if states and all(state == "ONLINE" for state in states)
            else "UNKNOWN"
        )
    )


def observe(component: str, operation: Callable[[], str]) -> Observation:
    """Record uncertainty instead of inventing a negative network observation."""
    try:
        state = operation()
        if state not in {"ONLINE", "OFFLINE", "UNKNOWN"}:
            raise ValueError("invalid probe state")
    except (OSError, ValueError, RuntimeError, TypeError, AttributeError):
        state = "UNKNOWN"
    return {"id": component, "state": state, "observed_at": timestamp()}


def observe_many(operations: list[tuple[str, Callable[[], str]]]) -> list[Observation]:
    """Keep outage duration bounded without granting discovery capabilities."""
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(operations)))) as pool:
        futures = [
            pool.submit(observe, component, operation)
            for component, operation in operations
        ]
        return [future.result() for future in futures]


@dataclass(frozen=True)
class Settings:
    ui_url: str
    identity_url: str
    realm: str
    client_id: str
    audience: str
    module_id: str
    interval: int
    timeout: int
    secret_root: Path
    state_path: Path

    @classmethod
    def environment(cls, prefix: str) -> Settings:
        def required(name: str) -> str:
            value = os.environ[prefix + "UI_" + name]
            if not value:
                raise ValueError("empty setting: " + name)
            return value

        module_id = required("MODULE_ID")
        if str(UUID(module_id)) != module_id:
            raise ValueError("canonical module UUID required")
        interval, timeout = int(required("INTERVAL_SECONDS")), int(
            required("TIMEOUT_SECONDS")
        )
        if min(interval, timeout) <= 0:
            raise ValueError("positive interval and timeout required")
        return cls(
            https_url(required("URL")),
            https_url(required("IDENTITY_URL")),
            required("REALM"),
            required("CLIENT_ID"),
            required("AUDIENCE"),
            module_id,
            interval,
            timeout,
            Path("/run/secrets/status"),
            Path("/var/lib/aelev-status/outbox.sqlite"),
        )


class Outbox:
    """One durable sequence and exact pending report per owner identity."""

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
        components: list[Observation] | None = None,
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
            report: dict[str, Any] = {
                "sequence": sequence + 1,
                "state": state,
                "observed_at": observed_at,
                "active": active,
            }
            if components is not None:
                report["components"] = components
            payload = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
            db.execute("UPDATE state SET sequence=?,pending=?", (sequence + 1, payload))
            return payload

    def acknowledge(self, payload: bytes) -> None:
        with self.connect() as db:
            if (
                db.execute(
                    "UPDATE state SET pending=NULL,withdrawal=CASE WHEN ? THEN 0 ELSE withdrawal END WHERE pending=?",
                    (json.loads(payload).get("active") is False, payload),
                ).rowcount
                != 1
            ):
                raise ValueError("acknowledgement does not match pending report")

    def begin_withdrawal(self) -> None:
        with self.connect() as db:
            db.execute("UPDATE state SET withdrawal=1")

    def withdrawing(self) -> bool:
        with self.connect() as db:
            return bool(db.execute("SELECT withdrawal FROM state").fetchone()[0])

    def heartbeat(self) -> None:
        with self.connect() as db:
            db.execute("UPDATE state SET heartbeat=?", (time.time(),))


class Publisher:
    """Transport only; the caller supplies its declaration and real probes."""

    def __init__(
        self,
        cfg: Settings,
        description: bytes,
        probe: Callable[[], tuple[str, list[Observation]]],
    ) -> None:
        self.cfg, self.description, self.probe = cfg, description, probe
        self.identity = tls(cfg.secret_root, "identity-ca.crt")
        self.transport = tls(
            cfg.secret_root, "shell-ca.crt", "publisher.crt", "publisher.key"
        )
        self.token, self.token_until = "", 0.0

    def access_token(self) -> str:
        if time.monotonic() < self.token_until:
            return self.token
        form = urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self.cfg.client_id,
                "client_secret": (self.cfg.secret_root / "client-secret")
                .read_text()
                .strip(),
            }
        ).encode()
        code, body = request(
            self.identity,
            self.cfg.identity_url
            + "/realms/"
            + quote(self.cfg.realm, safe="")
            + "/protocol/openid-connect/token",
            self.cfg.timeout,
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
            observed_at = timestamp()
            components.append(
                {"id": "publisher", "state": "ONLINE", "observed_at": observed_at}
            )
            pending = outbox.prepare(state, observed_at, components)
        self.publish("", self.description)
        self.publish("/status", pending)
        outbox.acknowledge(pending)
        LOGGER.info(
            json.dumps(
                {
                    "event": "status_published",
                    "sequence": json.loads(pending)["sequence"],
                    "state": json.loads(pending)["state"],
                }
            )
        )

    def withdraw(self, outbox: Outbox) -> None:
        """Confirm pending bytes first, then withdraw without resetting identity."""
        outbox.begin_withdrawal()
        self.publish("", self.description)
        pending = outbox.pending()
        if pending is not None and json.loads(pending).get("active") is not False:
            self.publish("/status", pending)
            outbox.acknowledge(pending)
        payload = outbox.prepare("UNKNOWN", timestamp(), active=False)
        self.publish("/status", payload)
        outbox.acknowledge(payload)
        LOGGER.info(
            json.dumps(
                {
                    "event": "publisher_withdrawn",
                    "sequence": json.loads(payload)["sequence"],
                }
            )
        )


def provision(cfg: Settings, owner: str, admin_root: Path) -> None:
    """Issue only this owner's publisher client; never change a foreign identity."""
    context = tls(cfg.secret_root, "identity-ca.crt")
    form = urlencode(
        {
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": (admin_root / "admin-user").read_text().strip(),
            "password": (admin_root / "admin-password").read_text().strip(),
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
        payload = json.dumps(value).encode() if value is not None else None
        code, reply = request(
            context,
            cfg.identity_url + "/admin/realms/" + quote(cfg.realm, safe="") + path,
            cfg.timeout,
            method,
            payload,
            {"Content-Type": "application/json", "Authorization": "Bearer " + token},
        )
        if code not in (200, 201, 204):
            raise RuntimeError("publisher provisioning rejected: " + str(code))
        return json.loads(reply) if reply else None

    role = admin("GET", "/roles/platform-ui-publisher")
    path = "/clients?clientId=" + quote(cfg.client_id, safe="")
    clients = admin("GET", path)
    if len(clients) > 1:
        raise ValueError("ambiguous publisher identity")
    declaration = {
        "clientId": cfg.client_id,
        "enabled": True,
        "publicClient": False,
        "serviceAccountsEnabled": True,
        "standardFlowEnabled": False,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "fullScopeAllowed": False,
        "defaultClientScopes": ["roles"],
        "attributes": {"aelev.owner": owner, "aelev.module_id": cfg.module_id},
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
            current.get("attributes", {}).get("aelev.owner") != owner
            or current.get("attributes", {}).get("aelev.module_id") != cfg.module_id
        ):
            raise ValueError("refusing to replace a foreign publisher identity")
        admin("PUT", "/clients/" + current["id"], declaration)
    else:
        admin("POST", "/clients", declaration)
        current = admin("GET", path)[0]
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
    LOGGER.info(json.dumps({"event": "publisher_identity_configured"}))


def run(
    cfg: Settings,
    owner: str,
    description: bytes,
    probe: Callable[[], tuple[str, list[Observation]]],
) -> None:
    """Run without making core readiness depend on the receiver."""
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("run", "once", "provision", "health"))
    action = parser.parse_args().action
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if action == "provision":
        provision(cfg, owner, Path("/run/secrets/status-admin"))
        return
    if action == "health":
        from .health import check

        check(cfg.state_path, cfg.module_id, cfg.interval * 2 + cfg.timeout * 40)
        return
    outbox, publisher = Outbox(cfg.state_path, cfg.module_id), Publisher(
        cfg, description, probe
    )
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.is_set():
        try:
            publisher.cycle(outbox)
        except Exception as error:
            LOGGER.warning(
                json.dumps(
                    {"event": "publication_failed", "error_type": type(error).__name__}
                )
            )
            if action == "once":
                raise
        finally:
            outbox.heartbeat()
        if action == "once":
            return
        stop.wait(cfg.interval)
    try:
        publisher.withdraw(outbox)
    except Exception as error:
        LOGGER.warning(
            json.dumps(
                {"event": "withdrawal_unconfirmed", "error_type": type(error).__name__}
            )
        )
