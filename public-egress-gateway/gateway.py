"""Consumer-agnostic, identity-scoped public HTTP(S) gateway."""

from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import logging
import math
import os
import signal
import socket
import ssl
import sys
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final, Protocol, cast
from urllib.parse import SplitResult, urlsplit

import documentation
import telemetry
from api_contract import (
    BODY_LIMIT,
    DOCUMENT_LIMIT,
    FETCH_FIELDS,
    FETCH_METHODS,
    URL_LIMIT,
    openapi,
    swagger_url,
)
from authorization import (
    MAX_IDENTITIES,
    GrantRejected,
    GrantStore,
    strict_json,
    validate_identity,
)

LOGGER = logging.getLogger("aelev.public_egress_gateway")
PREFIX = "AELEV_INFRASTRUCTURE_PUBLIC_EGRESS_GATEWAY_"

_DOCUMENT_LIMIT: Final = DOCUMENT_LIMIT
_REQUEST_BODY_LIMIT: Final = BODY_LIMIT
_CHUNK_SIZE: Final = 65_536
_UPSTREAM_CONTENT_LENGTH_HEADER: Final = "X-AELEV-Upstream-Content-Length"
_TRUNCATED_HEADER: Final = "X-AELEV-Response-Truncated"
_SAFE_REQUEST_HEADERS: Final = frozenset(
    {
        "accept",
        "accept-encoding",
        "accept-language",
        "if-modified-since",
        "if-none-match",
        "range",
        "user-agent",
    }
)
_SENSITIVE_REQUEST_HEADERS: Final = frozenset({"authorization", "x-subscription-token"})
_RESPONSE_HEADERS: Final = frozenset(
    {
        "accept-ranges",
        "cache-control",
        "content-disposition",
        "content-encoding",
        "content-language",
        "content-length",
        "content-range",
        "content-type",
        "date",
        "etag",
        "expires",
        "last-modified",
        "location",
        "retry-after",
        "vary",
    }
)
_BLOCKED_HOSTS: Final = frozenset(
    {
        "instance-data.ec2.internal",
        "metadata.azure.internal",
        "metadata.google.internal",
        "metadata.internal",
    }
)
_BLOCKED_SUFFIXES: Final = (
    ".cluster.local",
    ".internal",
    ".local",
    ".localhost",
)


class GatewayRejected(ValueError):
    """A request violates the public-egress policy."""


class GatewayUpstreamError(RuntimeError):
    """A validated public upstream could not be reached safely."""


class GatewayStreamAborted(RuntimeError):
    """A response failed after its HTTP headers had already been sent."""


class AttributeSpan(Protocol):
    def set_attribute(self, key: str, value: str | int) -> object: ...


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    mode: str
    bind_host: str
    port: int
    max_response_bytes: int
    timeout_seconds: float
    max_concurrency: int
    per_host_interval_seconds: float
    deny_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    credential_scopes: dict[str, frozenset[str]]
    post_scopes: dict[str, tuple[str, ...]]
    server_cert_path: str | None
    server_key_path: str | None
    client_ca_path: str | None
    grants_directory: Path
    trust_domain: str
    docs_port: int
    public_url: str

    @classmethod
    def from_env(cls) -> GatewayConfig:
        deny_networks = tuple(
            ipaddress.ip_network(value.strip(), strict=True)
            for value in _required_env("DENY_CIDRS", allow_empty=True).split(",")
            if value.strip()
        )
        config = cls(
            mode="PROVIDER",
            bind_host=_required_env("LISTEN_HOST"),
            port=_positive_int_env("LISTEN_PORT"),
            max_response_bytes=_positive_int_env("MAX_RESPONSE_BYTES"),
            timeout_seconds=_positive_float_env("TIMEOUT_SECONDS"),
            max_concurrency=_positive_int_env("MAX_CONCURRENCY"),
            per_host_interval_seconds=_nonnegative_float_env(
                "PER_HOST_INTERVAL_SECONDS"
            ),
            deny_networks=deny_networks,
            credential_scopes={},
            post_scopes={},
            server_cert_path=_required_env("SERVER_CERT_PATH"),
            server_key_path=_required_env("SERVER_KEY_PATH"),
            client_ca_path=_required_env("CLIENT_CA_PATH"),
            grants_directory=Path(_required_env("GRANTS_DIRECTORY")),
            trust_domain=_required_env("TRUST_DOMAIN"),
            docs_port=_positive_int_env("DOCS_LISTEN_PORT"),
            public_url=swagger_url(_required_env("PUBLIC_URL")).removesuffix("/docs"),
        )
        if config.port > 65_535:
            raise RuntimeError(PREFIX + "LISTEN_PORT is invalid")
        if config.docs_port > 65_535 or config.docs_port == config.port:
            raise RuntimeError(PREFIX + "DOCS_LISTEN_PORT is invalid")
        if config.max_concurrency > 96:
            raise RuntimeError(
                PREFIX + "MAX_CONCURRENCY exceeds the process budget (96)"
            )
        validate_identity("spiffe://" + config.trust_domain + "/validation")
        return config


class GatewayMetrics:
    """Small dependency-free Prometheus collector."""

    def __init__(self, mode: str) -> None:
        self._mode = mode
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, str], int] = {}
        self._bytes = 0
        self._active = 0

    def begin(self) -> None:
        with self._lock:
            self._active += 1

    def finish(self, route: str, outcome: str, transferred: int) -> None:
        with self._lock:
            self._active -= 1
            key = (route, outcome)
            self._requests[key] = self._requests.get(key, 0) + 1
            self._bytes += transferred

    def render(self) -> bytes:
        with self._lock:
            request_lines = [
                "# HELP aelev_egress_requests_total Completed gateway requests.",
                "# TYPE aelev_egress_requests_total counter",
            ]
            request_lines.extend(
                (
                    "aelev_egress_requests_total"
                    f'{{mode="{self._mode}",route="{route}",outcome="{outcome}"}} '
                    f"{count}"
                )
                for (route, outcome), count in sorted(self._requests.items())
            )
            request_lines.extend(
                [
                    "# HELP aelev_egress_response_bytes_total "
                    "Bytes returned by upstreams.",
                    "# TYPE aelev_egress_response_bytes_total counter",
                    "aelev_egress_response_bytes_total"
                    f'{{mode="{self._mode}"}} {self._bytes}',
                    "# HELP aelev_egress_active_requests Current gateway requests.",
                    "# TYPE aelev_egress_active_requests gauge",
                    "aelev_egress_active_requests"
                    f'{{mode="{self._mode}"}} {self._active}',
                ]
            )
        return ("\n".join(request_lines) + "\n").encode()


class AddressPolicy:
    def __init__(self, config: GatewayConfig) -> None:
        self._config = config

    def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        normalized = hostname.lower().rstrip(".")
        if (
            not normalized
            or normalized in {"localhost", "localhost.localdomain"}
            or normalized in _BLOCKED_HOSTS
            or normalized.endswith(_BLOCKED_SUFFIXES)
        ):
            raise GatewayRejected("EGRESS_HOST_BLOCKED")
        try:
            literal = ipaddress.ip_address(normalized)
        except ValueError:
            try:
                records = socket.getaddrinfo(
                    normalized,
                    port,
                    type=socket.SOCK_STREAM,
                    proto=socket.IPPROTO_TCP,
                )
            except socket.gaierror as error:
                raise GatewayUpstreamError(_dns_failure_code(error)) from error
            addresses = tuple(sorted({str(item[4][0]) for item in records}))
        else:
            addresses = (str(literal),)
        if not addresses:
            raise GatewayUpstreamError("EGRESS_DNS_EMPTY")
        parsed = tuple(ipaddress.ip_address(value) for value in addresses)
        if any(not _is_public_unicast(address) for address in parsed):
            raise GatewayRejected("EGRESS_ADDRESS_NOT_GLOBAL")
        if any(
            address in network
            for address in parsed
            for network in self._config.deny_networks
            if address.version == network.version
        ):
            raise GatewayRejected("EGRESS_ADDRESS_DENIED")
        return tuple(str(address) for address in parsed)


class PerHostRateLimiter:
    def __init__(self, interval_seconds: float) -> None:
        self._interval = interval_seconds
        self._lock = threading.Lock()
        self._next_allowed: dict[str, float] = {}

    def wait(self, hostname: str, interval_seconds: float | None = None) -> None:
        interval = self._interval if interval_seconds is None else interval_seconds
        if interval == 0:
            return
        with self._lock:
            now = time.monotonic()
            if hostname not in self._next_allowed and len(self._next_allowed) >= 4096:
                self._next_allowed = {
                    host: due for host, due in self._next_allowed.items() if due > now
                }
                if len(self._next_allowed) >= 4096:
                    raise GatewayRejected("EGRESS_HOST_CAPACITY")
            admitted = max(now, self._next_allowed.get(hostname, now))
            self._next_allowed[hostname] = admitted + interval
        delay = admitted - time.monotonic()
        if delay > 0:
            time.sleep(delay)


class ConcurrencyGate:
    """Keep active admissions across grant edits; quota updates cannot reset them."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.active = 0
        self.lock = threading.Lock()

    def acquire(self, *, blocking: bool = False, limit: int | None = None) -> bool:
        if blocking:
            raise ValueError("only nonblocking admission is supported")
        with self.lock:
            if self.active >= (self.limit if limit is None else limit):
                return False
            self.active += 1
            return True

    def release(self) -> None:
        with self.lock:
            self.active -= 1


class IdentityState:
    def __init__(self, config: GatewayConfig) -> None:
        self.concurrency = ConcurrencyGate(config.max_concurrency)
        self.rate_limiter = PerHostRateLimiter(config.per_host_interval_seconds)


class IdentityApplications:
    """Authorize exact TLS identity; never trust identity or policy in request JSON."""

    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self.grants = GrantStore(config.grants_directory)
        self.states: dict[str, IdentityState] = {}
        self.lock = threading.Lock()

    def authorized(self, identities: set[str]) -> GatewayApplication:
        if len(identities) != 1:
            raise GrantRejected("EGRESS_IDENTITY_BLOCKED")
        identity = next(iter(identities))
        grant = self.grants.load(identity)
        if urlsplit(identity).hostname != self.config.trust_domain:
            raise GrantRejected("EGRESS_IDENTITY_BLOCKED")
        scoped = replace(
            self.config,
            mode=grant.mode,
            credential_scopes=grant.credential_scopes,
            post_scopes=grant.post_scopes,
            max_response_bytes=min(
                self.config.max_response_bytes, grant.max_response_bytes
            ),
            timeout_seconds=min(self.config.timeout_seconds, grant.timeout_seconds),
            max_concurrency=min(self.config.max_concurrency, grant.max_concurrency),
            per_host_interval_seconds=max(
                self.config.per_host_interval_seconds, grant.per_host_interval_seconds
            ),
        )
        with self.lock:
            if identity not in self.states:
                if len(self.states) >= MAX_IDENTITIES:
                    raise GrantRejected("EGRESS_IDENTITY_CAPACITY")
                self.states[identity] = IdentityState(scoped)
            state = self.states[identity]
        app = GatewayApplication(scoped)
        app.concurrency = state.concurrency
        app.rate_limiter = state.rate_limiter
        return app


class PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(
        self,
        hostname: str,
        address: str,
        port: int,
        timeout: float,
    ) -> None:
        super().__init__(hostname, port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._address, self.port),
            self.timeout,
        )


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        hostname: str,
        address: str,
        port: int,
        timeout: float,
    ) -> None:
        ssl_context = ssl.create_default_context()
        super().__init__(
            hostname,
            port,
            timeout=timeout,
            context=ssl_context,
        )
        self._address = address
        self._ssl_context = ssl_context

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._address, self.port),
            self.timeout,
        )
        self.sock = self._ssl_context.wrap_socket(
            raw_socket,
            server_hostname=self.host,
        )


@dataclass(frozen=True, slots=True)
class FetchRequest:
    method: str
    parsed_url: SplitResult
    headers: dict[str, str]
    body: bytes
    max_bytes: int
    truncate: bool

    @property
    def hostname(self) -> str:
        hostname = self.parsed_url.hostname
        if hostname is None:
            raise GatewayRejected("EGRESS_URL_INVALID")
        return hostname.lower().rstrip(".")

    @property
    def port(self) -> int:
        default = 443 if self.parsed_url.scheme == "https" else 80
        try:
            return self.parsed_url.port or default
        except ValueError as error:
            raise GatewayRejected("EGRESS_PORT_INVALID") from error


class GatewayApplication:
    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self.addresses = AddressPolicy(config)
        self.metrics = GatewayMetrics(config.mode)
        self.rate_limiter = PerHostRateLimiter(config.per_host_interval_seconds)
        self.concurrency = ConcurrencyGate(config.max_concurrency)

    def parse_fetch(self, document: object) -> FetchRequest:
        if not isinstance(document, dict):
            raise GatewayRejected("EGRESS_REQUEST_INVALID")
        fields = cast(dict[str, object], document)
        if set(fields) != set(FETCH_FIELDS):
            raise GatewayRejected("EGRESS_REQUEST_INVALID")
        method = fields["method"]
        raw_url = fields["url"]
        raw_headers = fields["headers"]
        raw_body = fields["body"]
        max_bytes = fields["max_bytes"]
        truncate = fields["truncate"]
        if not isinstance(method, str) or method not in FETCH_METHODS:
            raise GatewayRejected("EGRESS_METHOD_BLOCKED")
        if not isinstance(raw_url, str) or len(raw_url) > URL_LIMIT:
            raise GatewayRejected("EGRESS_URL_INVALID")
        parsed = urlsplit(raw_url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise GatewayRejected("EGRESS_URL_INVALID")
        if (
            type(max_bytes) is not int
            or not 0 < max_bytes <= self.config.max_response_bytes
        ):
            raise GatewayRejected("EGRESS_LIMIT_INVALID")
        if not isinstance(truncate, bool):
            raise GatewayRejected("EGRESS_REQUEST_INVALID")
        if not isinstance(raw_headers, dict):
            raise GatewayRejected("EGRESS_HEADERS_INVALID")
        if not isinstance(raw_body, str):
            raise GatewayRejected("EGRESS_BODY_INVALID")
        try:
            body = base64.b64decode(raw_body, validate=True)
        except ValueError as error:
            raise GatewayRejected("EGRESS_BODY_INVALID") from error
        if len(body) > _REQUEST_BODY_LIMIT:
            raise GatewayRejected("EGRESS_BODY_TOO_LARGE")
        if method in {"GET", "HEAD"} and body:
            raise GatewayRejected("EGRESS_BODY_BLOCKED")
        if method == "POST":
            if self.config.mode != "REGULAR":
                raise GatewayRejected("EGRESS_METHOD_BLOCKED")
            if parsed.scheme != "https":
                raise GatewayRejected("EGRESS_POST_REQUIRES_TLS")
            allowed_paths = self.config.post_scopes.get(
                parsed.hostname.lower().rstrip("."), ()
            )
            if parsed.path not in allowed_paths:
                raise GatewayRejected("EGRESS_POST_SCOPE_BLOCKED")
        headers: dict[str, str] = {}
        for raw_name, raw_value in cast(dict[object, object], raw_headers).items():
            if not isinstance(raw_name, str) or not isinstance(raw_value, str):
                raise GatewayRejected("EGRESS_HEADERS_INVALID")
            if any(c in raw_name + raw_value for c in ("\r", "\n", "\x00")):
                raise GatewayRejected("EGRESS_HEADERS_INVALID")
            name = raw_name.strip().lower()
            if name in _SAFE_REQUEST_HEADERS or (
                method == "POST"
                and name == "content-type"
                and raw_value.split(";", 1)[0].strip().lower() == "application/json"
            ):
                headers[name] = raw_value
                continue
            if name not in _SENSITIVE_REQUEST_HEADERS:
                raise GatewayRejected("EGRESS_HEADER_BLOCKED")
            if self.config.mode != "REGULAR":
                raise GatewayRejected("EGRESS_CREDENTIAL_BLOCKED")
            allowed_hosts = self.config.credential_scopes.get(name, frozenset())
            if parsed.hostname.lower().rstrip(".") not in allowed_hosts:
                raise GatewayRejected("EGRESS_CREDENTIAL_SCOPE_BLOCKED")
            if parsed.scheme != "https":
                raise GatewayRejected("EGRESS_CREDENTIAL_REQUIRES_TLS")
            headers[name] = raw_value
        if truncate and any(header in headers for header in _SENSITIVE_REQUEST_HEADERS):
            raise GatewayRejected("EGRESS_CREDENTIAL_BLOCKED")
        if method == "POST":
            if (
                headers.get("content-type", "").split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                raise GatewayRejected("EGRESS_CONTENT_TYPE_INVALID")
            try:
                strict_json(body.decode("utf-8"))
            except (ValueError, UnicodeError) as error:
                raise GatewayRejected("EGRESS_BODY_INVALID") from error
        return FetchRequest(method, parsed, headers, body, max_bytes, truncate)


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AELEV-Egress/1"
    app: GatewayApplication
    identities: IdentityApplications
    body_consumed = False

    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/docs", "/docs/"}:
            self._plain_response(200, documentation.HTML, "text/html; charset=utf-8")
            return
        if self.path == "/openapi.json":
            self._plain_response(
                200,
                json.dumps(
                    openapi(
                        self.app.config.max_response_bytes, self.app.config.public_url
                    )
                ).encode(),
                "application/json",
            )
            return
        if self.path.startswith("/docs/assets/"):
            value = documentation.asset(self.path)
            if value is not None:
                self._plain_response(200, *value)
                return
        if self.path == "/healthz":
            self._plain_response(200, b'{"status":"ok"}', "application/json")
            return
        if self.path == "/readyz":
            self._plain_response(200, b'{"status":"ready"}', "application/json")
            return
        if self.path == "/metrics":
            self._plain_response(
                200,
                self.app.metrics.render(),
                "text/plain; version=0.0.4",
            )
            return
        self._error_response(404, "EGRESS_ROUTE_NOT_FOUND")

    def do_POST(self) -> None:  # noqa: N802
        route = "/v1/fetch" if self.path == "/v1/fetch" else "/unknown"
        with telemetry.request_span(self.headers, route) as span:
            self._handle_post(span, route)

    def _handle_post(self, span: AttributeSpan, route: str) -> None:
        request_id = str(uuid.uuid4())
        started = time.monotonic()
        transferred = 0
        outcome = "INTERNAL_ERROR"
        hostname = ""
        operation = "POST"
        acquired = False
        root_app = self.app
        root_app.metrics.begin()
        global_acquired = False
        try:
            self.app = self.identities.authorized(self._peer_identities())
            if not self.app.concurrency.acquire(limit=self.app.config.max_concurrency):
                outcome = "EGRESS_IDENTITY_CONCURRENCY_LIMIT"
                self._error_response(429, outcome, request_id)
                return
            acquired = True
            if not root_app.concurrency.acquire():
                outcome = "EGRESS_CONCURRENCY_LIMIT"
                self._error_response(429, outcome, request_id)
                return
            global_acquired = True
            document = self._request_document()
            if route == "/v1/fetch":
                request = self.app.parse_fetch(document)
                hostname = request.hostname
                operation = request.method
                transferred = self._fetch(request, request_id)
            else:
                raise GatewayRejected("EGRESS_ROUTE_NOT_FOUND")
            outcome = "ALLOWED"
        except (GatewayRejected, GrantRejected) as error:
            outcome = str(error)
            self._error_response(403, outcome, request_id)
        except GatewayStreamAborted as error:
            outcome = str(error)
        except (BrokenPipeError, ConnectionResetError):
            outcome = "EGRESS_CLIENT_CLOSED"
        except TimeoutError:
            outcome = "EGRESS_UPSTREAM_TIMEOUT"
            self._error_response(504, outcome, request_id)
        except (GatewayUpstreamError, OSError, http.client.HTTPException) as error:
            outcome = upstream_failure_code(error)
            self._error_response(502, outcome, request_id)
        except (json.JSONDecodeError, UnicodeDecodeError):
            outcome = "EGRESS_REQUEST_INVALID"
            self._error_response(400, outcome, request_id)
        finally:
            if acquired:
                self.app.concurrency.release()
            if global_acquired:
                root_app.concurrency.release()
            root_app.metrics.finish(route, outcome, transferred)
            span.set_attribute("aelev.outcome", outcome)
            span.set_attribute("aelev.response_bytes", transferred)
            span.set_attribute(
                "aelev.duration_ms", int((time.monotonic() - started) * 1_000)
            )
            if hostname:
                span.set_attribute("server.address", hostname)
            LOGGER.info(
                "egress_request_completed",
                extra={
                    "request_id": request_id,
                    "mode": self.app.config.mode,
                    "route": route,
                    "method": operation,
                    "hostname": hostname,
                    "outcome": outcome,
                    "response_bytes": transferred,
                    "duration_ms": int((time.monotonic() - started) * 1_000),
                },
            )
            self.app = root_app

    def _fetch(self, request: FetchRequest, request_id: str) -> int:
        if request.port not in {80, 443}:
            raise GatewayRejected("EGRESS_PORT_BLOCKED")
        addresses = self.app.addresses.resolve(request.hostname, request.port)
        self.app.rate_limiter.wait(
            request.hostname, self.app.config.per_host_interval_seconds
        )
        address = addresses[0]
        connection: http.client.HTTPConnection
        if request.parsed_url.scheme == "https":
            connection = PinnedHTTPSConnection(
                request.hostname,
                address,
                request.port,
                self.app.config.timeout_seconds,
            )
        else:
            connection = PinnedHTTPConnection(
                request.hostname,
                address,
                request.port,
                self.app.config.timeout_seconds,
            )
        path = request.parsed_url.path or "/"
        if request.parsed_url.query:
            path += "?" + request.parsed_url.query
        try:
            connection.request(
                request.method,
                path,
                body=request.body or None,
                headers=request.headers,
            )
            upstream = connection.getresponse()
            raw_length = upstream.getheader("content-length")
            if raw_length is not None:
                try:
                    declared_length = int(raw_length)
                except ValueError as error:
                    raise GatewayUpstreamError(
                        "EGRESS_UPSTREAM_LENGTH_INVALID"
                    ) from error
                # HEAD carries the size of the corresponding GET representation,
                # but has no response body to cross the transfer limit.
                if (
                    request.method != "HEAD"
                    and declared_length > request.max_bytes
                    and not request.truncate
                ):
                    raise GatewayRejected("EGRESS_RESPONSE_TOO_LARGE")
            return self._stream_http_response(
                upstream,
                request.method,
                request.max_bytes,
                request_id,
                request.truncate,
            )
        finally:
            connection.close()

    def _stream_http_response(
        self,
        upstream: http.client.HTTPResponse,
        method: str,
        max_bytes: int,
        request_id: str,
        truncate: bool,
    ) -> int:
        headers = {
            name.lower(): value
            for name, value in upstream.getheaders()
            if name.lower() in _RESPONSE_HEADERS
        }
        if method == "HEAD":
            upstream_length = headers.pop("content-length", None)
            self.send_response_only(upstream.status)
            for name, value in headers.items():
                self.send_header(name, value)
            if upstream_length is not None:
                self.send_header(
                    _UPSTREAM_CONTENT_LENGTH_HEADER,
                    upstream_length,
                )
            self.send_header("Content-Length", "0")
            self.send_header("X-AELEV-Egress-Request-ID", request_id)
            self.send_header("X-AELEV-Egress-Result", "ALLOWED")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            return 0
        headers.pop("content-length", None)
        self.send_response_only(upstream.status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-AELEV-Egress-Request-ID", request_id)
        self.send_header("X-AELEV-Egress-Result", "ALLOWED")
        if truncate:
            self.send_header(_TRUNCATED_HEADER, "bounded")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        transferred = 0
        while True:
            chunk = upstream.read(_CHUNK_SIZE)
            if not chunk:
                break
            remaining = max_bytes - transferred
            if len(chunk) > remaining and truncate:
                self._write_chunk(chunk[:remaining])
                transferred = max_bytes
                break
            transferred += len(chunk)
            if transferred > max_bytes:
                self._abort_stream()
                raise GatewayStreamAborted("EGRESS_RESPONSE_TOO_LARGE")
            self._write_chunk(chunk)
        self._finish_chunks()
        return transferred

    def _request_document(self) -> object:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise GatewayRejected("EGRESS_REQUEST_LENGTH_REQUIRED")
        try:
            length = int(raw_length)
        except ValueError as error:
            raise GatewayRejected("EGRESS_REQUEST_LENGTH_INVALID") from error
        if not 0 < length <= _DOCUMENT_LIMIT:
            raise GatewayRejected("EGRESS_REQUEST_TOO_LARGE")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
        if content_type != "application/json":
            raise GatewayRejected("EGRESS_CONTENT_TYPE_INVALID")
        self.body_consumed = True
        return cast(object, json.loads(self.rfile.read(length)))

    def _peer_identities(self) -> set[str]:
        peer = cast(ssl.SSLSocket, self.connection).getpeercert()
        if peer is None:
            return set()
        return {
            value
            for kind, value in peer.get("subjectAltName", ())
            if kind == "URI" and isinstance(value, str)
        }

    def _write_chunk(self, content: bytes) -> None:
        self.wfile.write(f"{len(content):x}\r\n".encode())
        self.wfile.write(content)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _finish_chunks(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _abort_stream(self) -> None:
        self.close_connection = True
        with suppress(OSError):
            self.connection.shutdown(socket.SHUT_RDWR)

    def _plain_response(
        self,
        status: int,
        content: bytes,
        content_type: str,
        *,
        request_id: str | None = None,
        result: str | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        if self.path in {"/docs", "/docs/", "/openapi.json"} or self.path.startswith(
            "/docs/assets/"
        ):
            self.send_header("Content-Security-Policy", documentation.CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
        if request_id is not None:
            self.send_header("X-AELEV-Egress-Request-ID", request_id)
        if result is not None:
            self.send_header("X-AELEV-Egress-Result", result)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(content)

    def _error_response(
        self,
        status: int,
        code: str,
        request_id: str | None = None,
    ) -> None:
        # An early rejection must drain a bounded body before closing TLS.
        # Otherwise unread TCP bytes can cause RST and discard the error body.
        if self.command == "POST" and not self.body_consumed:
            self.body_consumed = True
            with suppress(ValueError, OSError):
                length = int(self.headers.get("Content-Length", "0"))
                if 0 < length <= _DOCUMENT_LIMIT:
                    self.connection.settimeout(1)
                    self.rfile.read(length)
        content = json.dumps(
            {"status": "rejected", "code": code},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        self._plain_response(
            status,
            content,
            "application/json",
            request_id=request_id,
            result="REJECTED",
        )

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class DocumentationHandler(GatewayHandler):
    """Read-only HTTPS never requests a workload certificate or admits fetch."""

    def do_POST(self) -> None:  # noqa: N802
        self._error_response(405, "EGRESS_DOCUMENTATION_READ_ONLY")


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields: dict[str, object] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        for name in (
            "request_id",
            "mode",
            "route",
            "method",
            "hostname",
            "outcome",
            "response_bytes",
            "duration_ms",
            "error_type",
        ):
            value = getattr(record, name, None)
            if value is not None:
                fields[name] = value
        return json.dumps(fields, sort_keys=True, separators=(",", ":"))


class GatewayServer(ThreadingHTTPServer):
    """Bound accepted sockets; a stalled TLS peer never blocks the accept loop."""

    daemon_threads = True

    def __init__(
        self,
        config: GatewayConfig,
        handler: type[GatewayHandler],
        context: ssl.SSLContext,
        connection_limit: int,
    ) -> None:
        self.context = context
        self.config = config
        self.documentation_only = context.verify_mode == ssl.CERT_NONE
        self.context_lock = threading.Lock()
        self.certificate_stamp: tuple[tuple[int, int, int], ...] | None = None
        self.tls_checked_at = 0.0
        self.socket_timeout = min(config.timeout_seconds, 10.0)
        self.connections = ConcurrencyGate(connection_limit)
        super().__init__((config.bind_host, config.port), handler)

    def tls_stamp(self) -> tuple[tuple[int, int, int], ...]:
        """Detect atomic leaf replacement without reading secrets per handshake."""
        return tuple(
            (value.st_ino, value.st_mtime_ns, value.st_size)
            for value in (
                Path(cast(str, self.config.server_cert_path)).stat(),
                Path(cast(str, self.config.server_key_path)).stat(),
            )
        )

    def current_context(self) -> ssl.SSLContext:
        """Swap only a fully loaded context; existing TLS streams keep their own."""
        with self.context_lock:
            now = time.monotonic()
            if now - self.tls_checked_at < 1:
                return self.context
            self.tls_checked_at = now
            try:
                stamp = self.tls_stamp()
                if stamp != self.certificate_stamp:
                    candidate = server_context(self.config, self.documentation_only)
                    # Retry if the operator changed either file while it was read.
                    if self.tls_stamp() != stamp:
                        return self.context
                    self.context, self.certificate_stamp = candidate, stamp
                    LOGGER.info("egress_tls_context_reloaded")
            except (OSError, ssl.SSLError):
                LOGGER.error("egress_tls_context_reload_failed")
            return self.context

    def process_request(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: tuple[str, int],
    ) -> None:
        if not isinstance(request, socket.socket):
            raise TypeError("HTTP requires a stream socket")
        if not self.connections.acquire():
            self.shutdown_request(request)
            return
        request.settimeout(self.socket_timeout)
        try:
            super().process_request(request, client_address)
        except Exception:
            self.connections.release()
            raise

    def process_request_thread(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: tuple[str, int],
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()

    def finish_request(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: tuple[str, int],
    ) -> None:
        if not isinstance(request, socket.socket):
            raise TypeError("HTTP requires a stream socket")
        with self.current_context().wrap_socket(
            request, server_side=True, do_handshake_on_connect=False
        ) as secured:
            try:
                secured.do_handshake()
                self.RequestHandlerClass(secured, client_address, self)
            except (ssl.SSLError, TimeoutError, ConnectionError):
                LOGGER.info("egress_client_transport_rejected")

    def handle_error(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: tuple[str, int],
    ) -> None:
        del request, client_address
        error_type = sys.exc_info()[0]
        LOGGER.error(
            "egress_handler_failed",
            extra={"error_type": error_type.__name__ if error_type else "Unknown"},
        )


def server_context(config: GatewayConfig, documentation_only: bool) -> ssl.SSLContext:
    """Build a replacement with the same client authentication policy."""
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.verify_mode = ssl.CERT_NONE if documentation_only else ssl.CERT_OPTIONAL
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cast(str, config.server_cert_path), config.server_key_path)
    if not documentation_only:
        context.load_verify_locations(cast(str, config.client_ca_path))
    return context


def build_server(
    config: GatewayConfig,
    *,
    documentation_only: bool = False,
    application: GatewayApplication | None = None,
) -> ThreadingHTTPServer:
    if not all(
        (config.server_cert_path, config.server_key_path, config.client_ca_path)
    ):
        raise RuntimeError("gateway requires mTLS in every environment")
    context = server_context(config, documentation_only)
    app = application or GatewayApplication(config)
    identities = IdentityApplications(config)
    handler = type(
        "ConfiguredGatewayHandler",
        (DocumentationHandler if documentation_only else GatewayHandler,),
        {"app": app, "identities": identities},
    )
    return GatewayServer(
        config,
        cast(type[GatewayHandler], handler),
        context,
        8 if documentation_only else config.max_concurrency + 16,
    )


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(_required_env("LOG_LEVEL").upper())


def _is_public_unicast(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return (
        address.is_global
        and not address.is_multicast
        and not address.is_unspecified
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_reserved
    )


def upstream_failure_code(error: Exception) -> str:
    if isinstance(error, GatewayUpstreamError):
        code = str(error)
        if code.startswith("EGRESS_") and code.replace("_", "").isalnum():
            return code
    return "EGRESS_UPSTREAM_FAILED"


def _dns_failure_code(error: socket.gaierror) -> str:
    """Separate an absent DNS name from resolver failures worth retrying."""
    if error.errno == getattr(socket, "EAI_AGAIN", object()):
        return "EGRESS_DNS_TEMPORARY"
    not_found = {
        value
        for value in (
            getattr(socket, "EAI_NONAME", None),
            getattr(socket, "EAI_NODATA", None),
        )
        if value is not None
    }
    if error.errno in not_found:
        return "EGRESS_DNS_NOT_FOUND"
    return "EGRESS_DNS_UNAVAILABLE"


def main() -> None:
    configure_logging()
    telemetry.configure(
        _required_env("OTLP_HTTP_ENDPOINT", allow_empty=True),
        environment=_required_env("ENVIRONMENT"),
        ca_file=_required_env("TELEMETRY_CA_FILE", allow_empty=True),
        client_cert_file=_required_env("TELEMETRY_CLIENT_CERT_FILE", allow_empty=True),
        client_key_file=_required_env("TELEMETRY_CLIENT_KEY_FILE", allow_empty=True),
    )
    config = GatewayConfig.from_env()
    application = GatewayApplication(config)
    server = build_server(config, application=application)
    try:
        docs = build_server(
            replace(config, port=config.docs_port),
            documentation_only=True,
            application=application,
        )
    except Exception:
        server.server_close()
        raise
    docs_thread = threading.Thread(target=docs.serve_forever, daemon=True)
    docs_thread.start()
    LOGGER.info(
        "egress_gateway_started",
        extra={"mode": config.mode, "route": "startup"},
    )

    def stop(signum: int, frame: object) -> None:
        del signum, frame
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        docs.shutdown()
        docs_thread.join(timeout=5)
        docs.server_close()
        server.server_close()


def _required_env(name: str, *, allow_empty: bool = False) -> str:
    key = PREFIX + name
    if key not in os.environ or (not allow_empty and not os.environ[key].strip()):
        raise RuntimeError(f"set required variable {key}")
    return os.environ[key].strip()


def _positive_int_env(name: str) -> int:
    try:
        value = int(_required_env(name))
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer") from error
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def _positive_float_env(name: str) -> float:
    value = _nonnegative_float_env(name)
    if value == 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def _nonnegative_float_env(name: str) -> float:
    try:
        value = float(_required_env(name))
    except ValueError as error:
        raise RuntimeError(f"{name} must be numeric") from error
    if value < 0 or not math.isfinite(value):
        raise RuntimeError(f"{name} must be nonnegative")
    return value


if __name__ == "__main__":
    main()
