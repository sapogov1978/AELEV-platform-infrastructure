"""Opaque network port; authenticate its exact peer before sending any text."""

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import httpx

from .provider_identity import Identity, IdentityError, leaf_certificate, peer_matches

MAX_RESPONSE = 1_048_576


def mode() -> Literal["uds", "mtls"]:
    selected = os.environ.get("AELEV_TRANSLATOR_PROVIDER_TRANSPORT", "uds")
    if selected not in {"uds", "mtls"} or (
        selected == "uds"
        and any(
            os.environ.get(key)
            for key in (
                "AELEV_TRANSLATOR_PROVIDER_URL",
                "AELEV_TRANSLATOR_PROVIDER_TLS_ROOT",
            )
        )
    ):
        raise RuntimeError("TRANSLATOR_PROVIDER_CONFIG_INVALID")
    return cast(Literal["uds", "mtls"], selected)


def request(
    payload: dict[str, Any] | None = None, *, timeout: float = 240
) -> httpx.Response:
    origin = os.environ.get("AELEV_TRANSLATOR_PROVIDER_URL", "")
    root = os.environ.get("AELEV_TRANSLATOR_PROVIDER_TLS_ROOT", "")
    try:
        parsed = urlsplit(origin)
        selected_port = 443 if parsed.port is None else parsed.port
        if (
            not origin
            or origin != origin.strip()
            or any(
                ord(character) <= 32 or ord(character) == 127 for character in origin
            )
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or not 1 <= selected_port <= 65535
            or not root
            or not Path(root).is_absolute()
        ):
            raise ValueError
        host, port = parsed.hostname, selected_port
    except ValueError:
        raise RuntimeError("TRANSLATOR_PROVIDER_CONFIG_INVALID") from None
    connection: http.client.HTTPSConnection | None = None
    channel: ssl.SSLSocket | None = None
    expired = threading.Event()

    def expire() -> None:
        expired.set()
        current = channel or (connection.sock if connection is not None else None)
        if current is not None:
            with suppress(OSError):
                current.shutdown(socket.SHUT_RDWR)

    deadline = threading.Timer(timeout, expire)
    deadline.daemon = True
    deadline.start()
    try:
        identity = Identity(Path(root), "translator-service", "client")
        if identity.policy["environment"] != os.environ.get(
            "AELEV_ENVIRONMENT", "production"
        ):
            raise IdentityError
        context = identity.current()
        connection = http.client.HTTPSConnection(
            host, port, context=context, timeout=timeout
        )
        # Explicit handshake before request() prevents sending source material
        # to a same-CA server with a different workload URI.
        connection.connect()
        if expired.is_set():
            raise TimeoutError
        if not isinstance(connection.sock, ssl.SSLSocket):
            raise IdentityError
        channel = connection.sock
        certificate, raw = channel.getpeercert(), channel.getpeercert(binary_form=True)
        if (
            not isinstance(certificate, dict)
            or not isinstance(raw, bytes)
            or not leaf_certificate(ssl.DER_cert_to_PEM_cert(raw).encode("ascii"))
            or not peer_matches(cast(dict[str, Any], certificate), identity.peer_id)
            or identity.current() is not context
        ):
            raise IdentityError
        method, route = (
            ("GET", "/healthz") if payload is None else ("POST", "/v1/translate")
        )
        connection.request(
            method,
            route,
            None
            if payload is None
            else json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            {"Content-Type": "application/json", "Accept": "application/json"},
        )
        response = connection.getresponse()
        content = response.read(MAX_RESPONSE + 1)
        if expired.is_set():
            raise TimeoutError
        if len(content) > MAX_RESPONSE:
            raise RuntimeError("TRANSLATOR_PRIVATE_RESPONSE_INVALID")
        return httpx.Response(
            response.status,
            content=content,
            request=httpx.Request(method, origin.rstrip("/") + route),
        )
    except (IdentityError, ssl.SSLError):
        if expired.is_set():
            raise TimeoutError from None
        raise RuntimeError("TRANSLATOR_PROVIDER_TLS_UNAVAILABLE") from None
    except TimeoutError:
        raise
    except (OSError, http.client.HTTPException):
        if expired.is_set():
            raise TimeoutError from None
        raise RuntimeError("TRANSLATOR_PROVIDER_TRANSPORT_ERROR") from None
    finally:
        deadline.cancel()
        if connection is not None:
            connection.close()
