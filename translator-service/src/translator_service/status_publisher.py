"""Observe owned queue, transport, API and runtime presence, not consumers."""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import stat
from pathlib import Path
from urllib.parse import urlsplit

from aelev_platform_ui_client import Settings, aggregate, observe, run, timestamp

from translator_service.api import verify_kafka
from translator_service.api_server import swagger_url
from translator_service.presence import read
from translator_service.runtime import repository

PREFIX = "AELEV_INFRASTRUCTURE_TRANSLATOR_"
NETWORK_PREFIX = PREFIX + "UI_API_PROBE_"
NETWORK_SUFFIXES = ("URL", "SERVER_NAME", "CA_FILE", "CERT_FILE", "KEY_FILE")
COMPONENTS = {
    "database": "PostgreSQL переводчика",
    "kafka-connection": "Соединение с Kafka",
    "priority-consumer": "Приоритетный consumer",
    "normal-consumer": "Обычный consumer",
    "outbox": "Outbox",
    "api": "Операционный API",
    "publisher": "Издатель состояния переводчика",
}


def network_name(value: str) -> str:
    if not value.isascii() or len(value) > 253 or "%" in value:
        raise ValueError("TRANSLATOR_PUBLISHER_NAME_INVALID")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if not all(
            re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
            for label in value.split(".")
        ):
            raise ValueError("TRANSLATOR_PUBLISHER_NAME_INVALID") from None
    return value


def network_material(value: str, root: Path, *, key: bool = False) -> Path:
    """Query material belongs to this publisher, never a sibling mount."""
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("TRANSLATOR_PUBLISHER_QUERY_PATH_INVALID")
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("TRANSLATOR_PUBLISHER_OWN_QUERY_ROOT_REQUIRED")
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or not 0 < info.st_size <= 1024**2
        or key
        and stat.S_IMODE(info.st_mode) not in (0o600, 0o640)
    ):
        raise ValueError("TRANSLATOR_PUBLISHER_QUERY_FILE_INVALID")
    return path


def probe_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("TRANSLATOR_PUBLISHER_DUPLICATE_RESPONSE_FIELD")
        result[name] = value
    return result


def network_api(cfg: Settings) -> str | None:
    """Read only the explicitly selected operator mTLS liveness endpoint."""
    profile = os.environ.get(PREFIX + "UI_PROBE_PROFILE")
    present = any(NETWORK_PREFIX + key in os.environ for key in NETWORK_SUFFIXES)
    if profile is None and not present:
        return None
    if profile != "network":
        raise ValueError("TRANSLATOR_PUBLISHER_NETWORK_PROFILE_REQUIRED")
    values = {key: os.environ.get(NETWORK_PREFIX + key, "") for key in NETWORK_SUFFIXES}
    if not all(values.values()):
        raise ValueError("TRANSLATOR_PUBLISHER_NETWORK_INPUTS_REQUIRED")
    raw = values["URL"]
    try:
        route = urlsplit(raw)
        port = route.port
        if (
            not raw.isascii()
            or len(raw) > 512
            or any(ord(char) <= 32 or ord(char) == 127 for char in raw)
            or any(char in raw for char in ("\\", "%", "?", "#"))
            or route.scheme != "https"
            or not route.hostname
            or route.username is not None
            or route.password is not None
            or route.path != "/healthz"
            or route.query
            or route.fragment
            or route.netloc.endswith(":")
            or port is not None
            and not 1 <= port <= 65535
        ):
            raise ValueError
    except ValueError:
        raise ValueError("TRANSLATOR_PUBLISHER_HTTPS_HEALTHZ_REQUIRED") from None
    host = network_name(route.hostname)
    server_name = network_name(values["SERVER_NAME"])
    ca = network_material(values["CA_FILE"], cfg.secret_root)
    cert = network_material(values["CERT_FILE"], cfg.secret_root)
    key = network_material(values["KEY_FILE"], cfg.secret_root, key=True)
    context = ssl.create_default_context(cafile=str(ca))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    with (
        socket.create_connection((host, port or 443), timeout=cfg.timeout) as peer,
        context.wrap_socket(peer, server_hostname=server_name) as channel,
    ):
        channel.sendall(
            (
                "GET /healthz HTTP/1.1\r\nHost: "
                + route.netloc
                + "\r\nConnection: close\r\n\r\n"
            ).encode("ascii")
        )
        response = http.client.HTTPResponse(channel)
        response.begin()
        body = response.read(257)
        if response.status != 200 or len(body) > 256:
            return "UNKNOWN"
        try:
            payload = json.loads(body, object_pairs_hook=probe_object)
            return "ONLINE" if payload == {"status": "ok"} else "UNKNOWN"
        except ValueError:
            return "UNKNOWN"


def description(cfg: Settings) -> bytes:
    url = swagger_url()
    revision = int(os.environ[PREFIX + "UI_DESCRIPTION_REVISION"])
    if not 1 <= revision <= 9223372036854775807:
        raise ValueError("TRANSLATOR_UI_REVISION_INVALID")
    return json.dumps(
        {
            "schema_version": 1,
            "module_id": cfg.module_id,
            "revision": revision,
            "swagger_url": url,
            "title": {
                "ru": "Перевод текстов",
                "en": "Text translation",
            },
            "service_kind": "INFRASTRUCTURE",
            "sections": [],
            "components": [
                {
                    "id": name,
                    "title": {"ru": title, "en": name},
                    **({"swagger_url": url} if name == "api" else {}),
                }
                for name, title in COMPONENTS.items()
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def probe(cfg: Settings) -> tuple[str, list[dict[str, str]]]:
    def kafka() -> str:
        try:
            verify_kafka()
            return "ONLINE"
        except Exception:
            return "UNKNOWN"

    observations = [observe("kafka-connection", kafka)]
    try:
        repo = repository()
        try:
            roles = read(
                repo.engine, int(os.environ["AELEV_TRANSLATOR_PRESENCE_TTL_SECONDS"])
            )
        finally:
            repo.engine.dispose()
        observations.append(
            {"id": "database", "state": "ONLINE", "observed_at": timestamp()}
        )
        runtime = [
            {
                "id": str(role["id"]),
                "state": str(role["state"]),
                "observed_at": role["observed_at"] or timestamp(),
            }
            for role in roles
        ]
        observations.extend(
            observation
            for observation in runtime
            if observation["id"] not in {"provider", "worker"}
        )
    except Exception:
        observations.extend(
            {"id": role, "state": "UNKNOWN", "observed_at": timestamp()}
            for role in (
                "database",
                "priority-consumer",
                "normal-consumer",
                "outbox",
            )
        )

    def api() -> str:
        network = network_api(cfg)
        if network is not None:
            return network
        # This is an owner-private local endpoint, never the inter-service interface.
        import httpx

        with httpx.Client(timeout=cfg.timeout, trust_env=False) as client:
            try:
                response = client.get(
                    "http://translator-api:"
                    + os.environ["AELEV_INFRASTRUCTURE_TRANSLATOR_API_CONTAINER_PORT"]
                    + "/healthz"
                )
            except httpx.HTTPError:
                return "UNKNOWN"
            return (
                "ONLINE"
                if response.status_code == 200 and response.json() == {"status": "ok"}
                else "UNKNOWN"
            )

    observations.append(observe("api", api))
    return aggregate(observations), observations


if __name__ == "__main__":
    settings = Settings.environment(PREFIX)
    run(settings, "translator-status", description(settings), lambda: probe(settings))
