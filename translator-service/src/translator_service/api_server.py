"""One lifecycle for private diagnostics and an explicitly secured operator API."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import ssl
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from aelev_platform_ui_client import https_url

from translator_service import health_heartbeat

PREFIX = "AELEV_INFRASTRUCTURE_TRANSLATOR_"


def local_operator() -> bool:
    """Plain HTTP is an explicit local-only mode, never a TLS fallback."""
    return urlsplit(os.environ[PREFIX + "API_PUBLIC_URL"]).scheme == "http"


def swagger_url() -> str:
    """Return the owner's browser-routable documentation address."""
    value = os.environ[PREFIX + "API_PUBLIC_URL"]
    if local_operator():
        if (
            urlsplit(value).hostname not in {"localhost", "127.0.0.1", "::1"}
            or not re.fullmatch(
                r"(localhost|127\.0\.0\.1|\[::1\])(:[0-9]+)?",
                urlsplit(value).netloc,
                re.IGNORECASE,
            )
            or os.environ[PREFIX + "ENVIRONMENT"] != "local"
            or any(character.isspace() or ord(character) < 32 for character in value)
            or "\\" in value
        ):
            raise ValueError("TRANSLATOR_HTTP_REQUIRES_LOCAL_LOOPBACK")
    else:
        value = https_url(value)
    parsed = urlsplit(value)
    if (
        parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or "#" in value
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port == 0
        or "\x7f" in value
    ):
        raise ValueError("TRANSLATOR_API_PUBLIC_URL_REQUIRES_ORIGIN")
    return value.rstrip("/") + "/docs"


def configurations() -> tuple[uvicorn.Config, uvicorn.Config]:
    """Validate the selected security mode before either listener starts."""
    swagger_url()
    if os.environ[PREFIX + "API_BIND_ADDRESS"] not in {"127.0.0.1", "::1"}:
        raise ValueError("TRANSLATOR_PRIVATE_API_REQUIRES_LOOPBACK_BIND")
    internal_port = int(os.environ[PREFIX + "API_CONTAINER_PORT"])
    operator_port = int(os.environ[PREFIX + "API_HTTPS_CONTAINER_PORT"])
    if internal_port == operator_port:
        raise ValueError("TRANSLATOR_API_LISTENERS_REQUIRE_DISTINCT_PORTS")
    tls = Path("/run/secrets/translator-api")
    internal = uvicorn.Config(
        "translator_service.api:app",
        host="0.0.0.0",
        port=internal_port,
        proxy_headers=False,
        access_log=False,
    )
    if local_operator():
        if os.environ[PREFIX + "API_HTTPS_BIND_ADDRESS"] not in {"127.0.0.1", "::1"}:
            raise ValueError("TRANSLATOR_OPERATOR_HTTP_REQUIRES_LOOPBACK_BIND")
        from translator_service.operator_auth import Settings
        from translator_service.operator_translation import (
            Settings as TranslationSettings,
        )
        from translator_service.operator_translation import enabled

        Settings.environment()
        if enabled():
            TranslationSettings.environment()
        operator = uvicorn.Config(
            "translator_service.operator_api:local_app",
            factory=True,
            host="0.0.0.0",
            port=operator_port,
            proxy_headers=False,
            access_log=False,
        )
        return internal, operator
    operator = uvicorn.Config(
        "translator_service.operator_api:network_app",
        factory=True,
        host="0.0.0.0",
        port=operator_port,
        ssl_certfile=str(tls / "server.crt"),
        ssl_keyfile=str(tls / "server.key"),
        ssl_ca_certs=str(tls / "clients-ca.crt"),
        ssl_cert_reqs=ssl.CERT_REQUIRED,
        proxy_headers=False,
        access_log=False,
    )
    operator.load()
    assert isinstance(operator.ssl, ssl.SSLContext)
    operator.ssl.minimum_version = ssl.TLSVersion.TLSv1_2
    return internal, operator


class Server(uvicorn.Server):
    """The enclosing process handles signals for both listeners together."""

    @contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


async def serve(servers: tuple[Server, Server]) -> None:
    """Stop both listeners on a signal, failure or unexpected listener exit."""
    stopped = False

    def shutdown() -> None:
        nonlocal stopped
        stopped = True
        for server in servers:
            server.should_exit = True

    loop = asyncio.get_running_loop()
    for value in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(value, shutdown)
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    if health_heartbeat.enabled("api"):
        tasks.append(
            asyncio.create_task(
                health_heartbeat.run(
                    "api",
                    lambda: all(
                        server.started and not server.should_exit for server in servers
                    ),
                )
            )
        )
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        unexpected = not stopped
        shutdown()
        if len(tasks) == 3:
            tasks[-1].cancel()
            await asyncio.gather(tasks.pop(), return_exceptions=True)
        await asyncio.gather(*tasks)
        if unexpected:
            raise RuntimeError("TRANSLATOR_API_LISTENER_STOPPED")
    finally:
        shutdown()
        if len(tasks) == 3:
            tasks[-1].cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for value in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(value)


if __name__ == "__main__":
    from translator_service import observability
    from translator_service.runtime import JsonFormatter

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    observability.configure("api")
    private, public = configurations()
    asyncio.run(serve((Server(private), Server(public))))
