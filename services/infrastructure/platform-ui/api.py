"""Technical metadata only: authenticated injection, ownership and stale status."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import ssl
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Protocol
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import jwt
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def origin(url: str, *, loopback_http: bool = False) -> str:
    """Normalize HTTPS, or canonical HTTP restricted to an exact loopback host."""
    parsed = urlsplit(url)
    if (
        (
            parsed.scheme != "https"
            and not (
                loopback_http
                and parsed.scheme == "http"
                and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                and re.fullmatch(
                    r"(localhost|127\.0\.0\.1|\[::1\])(:[0-9]+)?",
                    parsed.netloc,
                    re.IGNORECASE,
                )
                and parsed.port != 0
            )
        )
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError(
            "an HTTPS URL or exact loopback HTTP URL without credentials or fragment is required"
        )
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default_port = 443 if parsed.scheme == "https" else 80
    port = parsed.port or default_port
    return f"{parsed.scheme}://{host}" + (f":{port}" if port != default_port else "")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Title(StrictModel):
    ru: str = Field(min_length=1, max_length=120)
    en: str = Field(min_length=1, max_length=120)


class Section(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    title: Title
    url: str = Field(max_length=2048)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        origin(value, loopback_http=True)
        if (
            any(character.isspace() or ord(character) < 32 for character in value)
            or "\\" in value
            or "#" in value
        ):
            raise ValueError("ambiguous URL")
        return value


class ExternalLinks(StrictModel):
    ui_url: str | None = Field(default=None, max_length=2048)
    swagger_url: str | None = Field(default=None, max_length=2048)

    @field_validator("ui_url", "swagger_url")
    @classmethod
    def valid_link(cls, value: str | None) -> str | None:
        if value is None:
            return None
        origin(value, loopback_http=True)
        if (
            any(character.isspace() or ord(character) < 32 for character in value)
            or "\\" in value
            or urlsplit(value).username is not None
            or "\x7f" in value
            or "#" in value
        ):
            raise ValueError("ambiguous external URL")
        return value


class Component(ExternalLinks):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    title: Title


class Description(ExternalLinks):
    schema_version: Literal[1]
    module_id: str
    revision: int = Field(gt=0, le=9223372036854775807)
    title: Title
    sections: list[Section] = Field(max_length=32)
    service_kind: Literal["FUNCTIONAL", "INFRASTRUCTURE"] | None = None
    navigation_order: int | None = Field(default=None, ge=0, le=2147483647)
    components: list[Component] | None = Field(default=None, max_length=32)

    @field_validator("module_id")
    @classmethod
    def uuid_id(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("canonical UUID required")
        return value

    @model_validator(mode="after")
    def unique_sections(self) -> Description:
        if len({section.id for section in self.sections}) != len(self.sections):
            raise ValueError("section identifiers must be unique")
        components = self.components or []
        if len({component.id for component in components}) != len(components):
            raise ValueError("component identifiers must be unique")
        if self.service_kind == "FUNCTIONAL" and self.sections:
            if self.ui_url is not None or any(
                component.ui_url is not None for component in components
            ):
                raise ValueError("embedded functional UI exposes only Swagger links")
        return self


class Health(StrictModel):
    state: Literal["ONLINE", "OFFLINE", "UNKNOWN"]
    observed_at: str

    @field_validator("observed_at")
    @classmethod
    def timestamp(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.timestamp() > time.time() + 60:
            raise ValueError("bounded timezone-aware time required")
        return value


class ComponentStatus(Health):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")


class Status(Health):
    sequence: int = Field(gt=0, le=9223372036854775807)
    active: bool | None = None
    components: list[ComponentStatus] | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def unique_components(self) -> Status:
        components = self.components or []
        if len({component.id for component in components}) != len(components):
            raise ValueError("component identifiers must be unique")
        return self


@dataclass(frozen=True)
class Settings:
    public_url: str
    issuer: str
    jwks_url: str
    audience: str
    ca: str
    database: str
    status_ttl_seconds: int
    jwks_cache_seconds: int
    max_modules: int
    max_body_bytes: int

    @classmethod
    def read(cls, path: Path) -> Settings:
        data = json.loads(path.read_text())
        if set(data) != set(cls.__dataclass_fields__):
            raise ValueError("exact Settings fields required")
        for name in ("public_url", "issuer", "jwks_url"):
            origin(data[name])
        for name in (
            "status_ttl_seconds",
            "jwks_cache_seconds",
            "max_modules",
            "max_body_bytes",
        ):
            if type(data[name]) is not int or data[name] <= 0:
                raise ValueError(f"positive integer required: {name}")
        return cls(**data)


class Authenticator(Protocol):
    async def verify(self, token: str) -> dict[str, Any]: ...


class OIDC:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.context = ssl.create_default_context(cafile=settings.ca)
        self.keys: dict[str, jwt.PyJWK] = {}
        self.expires = 0.0
        self.refresh_not_before = 0.0
        self.lock = asyncio.Lock()

    async def verify(self, token: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
            kid = header["kid"]
            if not isinstance(kid, str) or header.get("alg") != "RS256":
                raise ValueError("unsupported key")
            async with self.lock:
                if time.monotonic() >= self.expires or kid not in self.keys:
                    if time.monotonic() < self.refresh_not_before:
                        if time.monotonic() >= self.expires:
                            raise HTTPException(503, "identity trust unavailable")
                        raise ValueError("unknown key")
                    self.refresh_not_before = time.monotonic() + 5
                    try:
                        async with httpx.AsyncClient(
                            verify=self.context,
                            timeout=5,
                            trust_env=False,
                            follow_redirects=False,
                        ) as client:
                            async with client.stream(
                                "GET", self.settings.jwks_url
                            ) as response:
                                response.raise_for_status()
                                body = bytearray()
                                async for chunk in response.aiter_bytes():
                                    body.extend(chunk)
                                    if len(body) > 262144:
                                        raise ValueError("oversized JWKS")
                            keys = jwt.PyJWKSet.from_json(bytes(body).decode()).keys
                            self.keys = {
                                key.key_id: key
                                for key in keys
                                if key.key_id and key.algorithm_name == "RS256"
                            }
                            self.expires = (
                                time.monotonic() + self.settings.jwks_cache_seconds
                            )
                    except (httpx.HTTPError, ValueError, jwt.PyJWTError) as error:
                        raise HTTPException(
                            503, "identity trust unavailable"
                        ) from error
                key = self.keys.get(kid)
            if key is None:
                raise ValueError("unknown key")
            claims: dict[str, Any] = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
            if (
                claims.get("typ") != "Bearer"
                or not isinstance(claims["sub"], str)
                or not claims["sub"]
            ):
                raise ValueError("access token required")
            return claims
        except (jwt.PyJWTError, ValueError, KeyError, TypeError) as error:
            raise HTTPException(
                401, "invalid access token", headers={"WWW-Authenticate": "Bearer"}
            ) from error


def canonical(data: dict[str, Any]) -> tuple[str, str]:
    body = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return body, hashlib.sha256(body.encode()).hexdigest()


class Store:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = Path(settings.database)
        self._connection_lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS modules(id TEXT PRIMARY KEY,owner TEXT NOT NULL,revision INTEGER NOT NULL,body TEXT NOT NULL,digest TEXT NOT NULL,deleted INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL DEFAULT 'UNKNOWN',sequence INTEGER NOT NULL DEFAULT 0,status_digest TEXT,observed_at TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS audit(sequence INTEGER PRIMARY KEY,subject TEXT NOT NULL,module_id TEXT NOT NULL,operation TEXT NOT NULL,digest TEXT NOT NULL,recorded_at TEXT NOT NULL)"
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(modules)")}
            if "component_status" not in columns:
                db.execute(
                    "ALTER TABLE modules ADD COLUMN component_status TEXT NOT NULL DEFAULT '[]'"
                )
            if "present" not in columns:
                db.execute(
                    "ALTER TABLE modules ADD COLUMN present INTEGER NOT NULL DEFAULT 0"
                )
            if "presence_received_at" not in columns:
                db.execute("ALTER TABLE modules ADD COLUMN presence_received_at REAL")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        # SQLite <= 3.51.2 has a WAL-reset race only when separate connections
        # write or checkpoint concurrently. This single-process store therefore
        # serializes the complete connection lifetime, including checkpoints.
        with self._connection_lock:
            db = sqlite3.connect(self.path, timeout=5)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            try:
                with db:
                    yield db
            finally:
                db.close()

    @staticmethod
    def owned(row: sqlite3.Row | None, subject: str) -> None:
        if row is not None and row["owner"] != subject:
            raise HTTPException(403, "module belongs to another identity")

    @staticmethod
    def audit(
        db: sqlite3.Connection,
        subject: str,
        module_id: str,
        operation: str,
        digest: str,
    ) -> None:
        db.execute(
            "INSERT INTO audit(subject,module_id,operation,digest,recorded_at) VALUES(?,?,?,?,?)",
            (
                subject,
                module_id,
                operation,
                digest,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    def publish(self, description: Description, subject: str) -> None:
        body, digest = canonical(description.model_dump(exclude_none=True))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM modules WHERE id=?", (description.module_id,)
            ).fetchone()
            self.owned(row, subject)
            if row is not None and description.revision <= row["revision"]:
                if (
                    description.revision == row["revision"]
                    and digest == row["digest"]
                    and not row["deleted"]
                ):
                    return
                raise HTTPException(409, "revision conflict")
            if row is None:
                if (
                    db.execute("SELECT count(*) FROM modules").fetchone()[0]
                    >= self.settings.max_modules
                ):
                    raise HTTPException(503, "metadata capacity reached")
                db.execute(
                    "INSERT INTO modules(id,owner,revision,body,digest,present,presence_received_at) VALUES(?,?,?,?,?,1,?)",
                    (
                        description.module_id,
                        subject,
                        description.revision,
                        body,
                        digest,
                        time.time(),
                    ),
                )
            else:
                db.execute(
                    "UPDATE modules SET revision=?,body=?,digest=?,deleted=0 WHERE id=?",
                    (description.revision, body, digest, description.module_id),
                )
                if json.loads(row["body"]).get("components") != json.loads(body).get(
                    "components"
                ):
                    db.execute(
                        "UPDATE modules SET component_status='[]' WHERE id=?",
                        (description.module_id,),
                    )
                if row["deleted"]:
                    db.execute(
                        "UPDATE modules SET state='UNKNOWN',observed_at=NULL,component_status='[]',present=1,presence_received_at=? WHERE id=?",
                        (time.time(), description.module_id),
                    )
            self.audit(db, subject, description.module_id, "publish", digest)

    def status(self, module_id: str, status: Status, subject: str) -> None:
        _, digest = canonical(status.model_dump(exclude_none=True))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM modules WHERE id=?", (module_id,)
            ).fetchone()
            if row is None or row["deleted"]:
                raise HTTPException(404, "active module not found")
            self.owned(row, subject)
            if status.sequence <= row["sequence"]:
                if (
                    status.sequence == row["sequence"]
                    and digest == row["status_digest"]
                ):
                    return
                raise HTTPException(409, "status sequence conflict")
            declared = {
                item["id"] for item in json.loads(row["body"]).get("components", [])
            }
            components = [item.model_dump() for item in status.components or []]
            if any(item["id"] not in declared for item in components):
                raise HTTPException(422, "undeclared component")
            db.execute(
                "UPDATE modules SET state=?,sequence=?,status_digest=?,observed_at=?,component_status=?,present=?,presence_received_at=? WHERE id=?",
                (
                    status.state,
                    status.sequence,
                    digest,
                    status.observed_at,
                    json.dumps(components),
                    int(status.active is not False),
                    time.time(),
                    module_id,
                ),
            )
            self.audit(db, subject, module_id, "status", digest)

    def delete(self, module_id: str, subject: str) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM modules WHERE id=?", (module_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(404, "module not found")
            self.owned(row, subject)
            if not row["deleted"]:
                db.execute("UPDATE modules SET deleted=1 WHERE id=?", (module_id,))
                self.audit(db, subject, module_id, "delete", row["digest"])

    def list(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM modules WHERE deleted=0 AND present=1 ORDER BY id"
            ).fetchall()
        result = []
        now = time.time()
        for row in rows:
            data = json.loads(row["body"])
            observed = row["observed_at"]
            fresh = (
                observed is not None
                and now - datetime.fromisoformat(observed).timestamp()
                <= self.settings.status_ttl_seconds
            )
            data["status"] = {
                "state": row["state"] if fresh else "UNKNOWN",
                "reported_state": row["state"],
                "observed_at": observed,
                "sequence": row["sequence"],
                "connection": (
                    "CONNECTED"
                    if row["presence_received_at"] is not None
                    and now - row["presence_received_at"]
                    <= self.settings.status_ttl_seconds
                    else "LOST"
                ),
                "received_at": (
                    datetime.fromtimestamp(
                        row["presence_received_at"], timezone.utc
                    ).isoformat()
                    if row["presence_received_at"] is not None
                    else None
                ),
            }
            if data.get("components"):
                reports = {
                    item["id"]: item for item in json.loads(row["component_status"])
                }
                children = []
                for component in data["components"]:
                    report = reports.get(component["id"])
                    child_observed = report["observed_at"] if report else None
                    child_fresh = (
                        fresh
                        and child_observed is not None
                        and now - datetime.fromisoformat(child_observed).timestamp()
                        <= self.settings.status_ttl_seconds
                    )
                    children.append(
                        {
                            "id": component["id"],
                            "state": (
                                report["state"]
                                if report is not None and child_fresh
                                else "UNKNOWN"
                            ),
                            "reported_state": (
                                report["state"] if report is not None else "UNKNOWN"
                            ),
                            "observed_at": child_observed,
                        }
                    )
                data["status"]["components"] = children
            result.append(data)
        return result


def create_app(
    settings: Settings, authenticator: Authenticator | None = None
) -> FastAPI:
    store = Store(settings)
    auth = authenticator or OIDC(settings)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def limits(request: Request, call_next: Any) -> Any:
        if request.method in ("PUT", "POST"):
            length = request.headers.get("content-length", "")
            if not length.isdecimal() or int(length) > settings.max_body_bytes:
                return JSONResponse(
                    {"detail": "bounded Content-Length required"}, status_code=413
                )
        try:
            response = await call_next(request)
        except sqlite3.Error:
            return JSONResponse(
                {"detail": "metadata storage unavailable"}, status_code=503
            )
        response.headers["Cache-Control"] = "no-store"
        return response

    async def identity(request: Request) -> dict[str, Any]:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer ") or len(header) > 16384:
            raise HTTPException(
                401, "access token required", headers={"WWW-Authenticate": "Bearer"}
            )
        return await auth.verify(header[7:])

    def require(claims: dict[str, Any], role: str) -> str:
        access = claims.get("realm_access")
        roles = access.get("roles") if isinstance(access, dict) else None
        if not isinstance(roles, list) or role not in roles:
            raise HTTPException(403, "role not granted")
        return str(claims["sub"])

    async def publisher(
        request: Request, claims: dict[str, Any] = Depends(identity)
    ) -> str:
        if request.headers.get("x-verified-publisher") != "SUCCESS":
            raise HTTPException(403, "publisher mTLS required")
        return require(claims, "platform-ui-publisher")

    @app.get("/api/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/readyz")
    def ready() -> dict[str, str]:
        with store.connect() as db:
            db.execute("SELECT 1").fetchone()
        return {"status": "ready"}

    @app.get("/api/modules")
    def modules(claims: dict[str, Any] = Depends(identity)) -> dict[str, Any]:
        require(claims, "platform-ui-viewer")
        return {"modules": store.list()}

    @app.put("/api/modules/{module_id}")
    def publish(
        module_id: UUID, description: Description, subject: str = Depends(publisher)
    ) -> dict[str, str]:
        if str(module_id) != description.module_id:
            raise HTTPException(422, "module identifier mismatch")
        if any(
            origin(section.url, loopback_http=True) == origin(settings.public_url)
            for section in description.sections
        ):
            raise HTTPException(422, "a separate UI origin is required")
        for links in [description, *(description.components or [])]:
            for url in (links.ui_url, links.swagger_url):
                if url is not None and origin(url, loopback_http=True) == origin(
                    settings.public_url
                ):
                    raise HTTPException(
                        422, "a separate external UI origin is required"
                    )
        store.publish(description, subject)
        return {"status": "stored"}

    @app.put("/api/modules/{module_id}/status")
    def status(
        module_id: UUID, value: Status, subject: str = Depends(publisher)
    ) -> dict[str, str]:
        store.status(str(module_id), value, subject)
        return {"status": "stored"}

    @app.delete("/api/modules/{module_id}")
    def delete(module_id: UUID, subject: str = Depends(publisher)) -> dict[str, str]:
        store.delete(str(module_id), subject)
        return {"status": "stored"}

    return app


def main() -> None:
    settings = Settings.read(Path(sys.argv[1]))
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=8444,
        ssl_certfile="/run/secrets/api/server.crt",
        ssl_keyfile="/run/secrets/api/server.key",
        ssl_ca_certs="/run/secrets/api/ca.crt",
        ssl_cert_reqs=ssl.CERT_REQUIRED,
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
