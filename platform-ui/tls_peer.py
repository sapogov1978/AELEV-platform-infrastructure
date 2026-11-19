"""Bind proxy assertions to the authenticated TLS connection, never HTTP fields."""

from __future__ import annotations

import asyncio
import re
import ssl
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast
from urllib.parse import urlsplit

from cryptography import x509
from uvicorn._types import ASGIReceiveEvent, ASGISendEvent, Scope
from uvicorn.protocols.http.h11_impl import H11Protocol


def spiffe_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("canonical proxy SPIFFE ID required")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "spiffe"
        or not re.fullmatch(r"[a-z0-9._-]{1,255}", parsed.netloc)
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
        or value != "spiffe://" + parsed.netloc + parsed.path
        or any(
            not re.fullmatch(r"[A-Za-z0-9._-]+", part) or part in {".", ".."}
            for part in parsed.path.split("/")[1:]
        )
    ):
        raise ValueError("canonical proxy SPIFFE ID required")
    return value


def trusted_proxy(scope: Mapping[str, Any], expected: str) -> bool:
    """TLS verifies chain/time/EKU; this checks the authenticated leaf's role."""
    if scope.get("scheme") != "https":
        return False
    try:
        extension = scope["extensions"]["tls"]
        chain = extension["client_cert_chain"]
        if extension.get("spec_version", "0.2") not in {"0.2", "0.3"}:
            return False
        leaf = next(iter(chain))
        if not isinstance(leaf, str) or not 0 < len(leaf) <= 65536:
            return False
        certificate = x509.load_pem_x509_certificate(leaf.encode("ascii"))
        names = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value.get_values_for_type(x509.UniformResourceIdentifier)
        return names == [expected]
    except (KeyError, TypeError, ValueError, StopIteration, x509.ExtensionNotFound):
        return False


class TLSH11Protocol(H11Protocol):
    """Provide ASGI TLS 0.3 on the pinned server's per-connection application."""

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        super().connection_made(cast(asyncio.Transport, transport))
        secure = cast(ssl.SSLObject | None, transport.get_extra_info("ssl_object"))
        # A certificate obtained without mandatory TLS verification has no authority.
        if secure is None or secure.context.verify_mode != ssl.CERT_REQUIRED:
            return
        chain = tuple(
            ssl.DER_cert_to_PEM_cert(item) for item in secure.get_unverified_chain()
        )
        original = self.app

        async def authenticated(
            scope: Scope,
            receive: Callable[[], Awaitable[ASGIReceiveEvent]],
            send: Callable[[ASGISendEvent], Awaitable[None]],
        ) -> None:
            if scope["type"] == "http":
                extended = dict(scope)
                extended["extensions"] = {
                    **scope.get("extensions", {}),
                    "tls": {"spec_version": "0.3", "client_cert_chain": chain},
                }
                scope = cast(Scope, extended)
            await original(scope, receive, send)

        self.app = authenticated
