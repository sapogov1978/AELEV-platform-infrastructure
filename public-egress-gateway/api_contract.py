"""Own wire contract and OpenAPI projection of the existing gateway routes."""

from __future__ import annotations

from typing import Final
from urllib.parse import urlsplit

FETCH_FIELDS: Final = ("method", "url", "headers", "body", "max_bytes", "truncate")
FETCH_METHODS: Final = ("GET", "HEAD", "POST")
DOCUMENT_LIMIT: Final = 196_608
BODY_LIMIT: Final = 65_536
URL_LIMIT: Final = 8_192
TAGS: Final = (
    ("default", "Service liveness and readiness."),
    ("gateway", "Identity-scoped public HTTP(S) fetching."),
    ("metrics", "Prometheus metrics."),
)


def swagger_url(public_url: str) -> str:
    """Require the browser-facing HTTPS origin, not a container-only URL."""
    parsed = urlsplit(public_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or len(public_url) > 2043
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in public_url)
        or "\\" in public_url
    ):
        raise ValueError("gateway PUBLIC_URL must be an explicit HTTPS origin")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("gateway PUBLIC_URL has an invalid port")
    return public_url.rstrip("/") + "/docs"


def openapi(max_response_bytes: int, fetch_origin: str) -> dict[str, object]:
    """Describe public bytes and authorization without publishing any grant."""
    paths: dict[str, object] = {
        path: {
            "get": {
                "operationId": operation,
                "summary": summary,
                "description": description,
                "tags": ["default"],
                "security": [],
                "responses": {
                    "200": {
                        "description": (
                            "The HTTPS process is responding; this is not client "
                            "authorization."
                        ),
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "status": {"type": "string", "const": status}
                                    },
                                    "required": ["status"],
                                    "additionalProperties": False,
                                },
                                "example": {"status": status},
                            }
                        },
                    }
                },
            }
        }
        for path, operation, summary, description, status in (
            (
                "/healthz",
                "health",
                "Check process liveness",
                "No dependency or grant is tested.",
                "ok",
            ),
            (
                "/readyz",
                "ready",
                "Check gateway readiness",
                "An empty grant directory is a ready, deny-by-default provider. "
                "This is not proof of upstream reachability.",
                "ready",
            ),
        )
    }
    rejection: dict[str, object] = {
        "description": (
            "Gateway rejection; distinguish an upstream status "
            "by X-AELEV-Egress-Result."
        ),
        "headers": {
            "X-AELEV-Egress-Result": {
                "schema": {"type": "string", "enum": ["REJECTED", "ALLOWED"]}
            }
        },
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/Rejection"}}
        },
    }
    paths["/v1/fetch"] = {
        "post": {
            "servers": [{"url": swagger_url(fetch_origin).removesuffix("/docs")}],
            "operationId": "fetch",
            "summary": "Fetch a bounded public HTTP(S) response",
            "description": (
                "Requires a trusted client certificate with one approved SPIFFE "
                "URI and an unexpired individual grant. The browser does not "
                "receive a publisher certificate or a substitute identity. "
                "A native upstream status, headers and streamed bytes are "
                "returned, not a domain result. Redirects are not followed. "
                "Private/metadata targets remain blocked. After response "
                "headers, overflow or an upstream failure aborts the stream; "
                "incomplete bytes are never a complete document. Workload "
                "credentials are supplied by the client, never by Swagger."
            ),
            "tags": ["gateway"],
            "security": [{"workloadIdentity": []}],
            "requestBody": {
                "required": True,
                "description": f"Exact six-field JSON, at most {DOCUMENT_LIMIT} bytes.",
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/FetchRequest"},
                        "example": {
                            "method": "GET",
                            "url": "https://example.com/",
                            "headers": {},
                            "body": "",
                            "max_bytes": min(65536, max_response_bytes),
                            "truncate": False,
                        },
                    }
                },
            },
            "responses": {
                "default": {
                    "description": (
                        "External HTTP status and raw bytes. ALLOWED means the "
                        "gateway policy passed, not upstream success. For HEAD, "
                        "the representation size is in "
                        "X-AELEV-Upstream-Content-Length. bounded never proves "
                        "completeness."
                    ),
                    "headers": {
                        "X-AELEV-Egress-Result": {
                            "schema": {"type": "string", "enum": ["ALLOWED"]}
                        },
                        "X-AELEV-Egress-Request-ID": {
                            "schema": {"type": "string", "format": "uuid"}
                        },
                        "X-AELEV-Response-Truncated": {
                            "schema": {"type": "string", "enum": ["bounded"]}
                        },
                        "X-AELEV-Upstream-Content-Length": {
                            "schema": {"type": "string"}
                        },
                    },
                    "content": {
                        "*/*": {"schema": {"type": "string", "format": "binary"}}
                    },
                },
                **{str(status): rejection for status in (400, 403, 404, 429, 502, 504)},
            },
        }
    }
    paths["/metrics"] = {
        "get": {
            "operationId": "metrics",
            "summary": "Read Prometheus process metrics",
            "tags": ["metrics"],
            "security": [],
            "responses": {
                "200": {
                    "description": (
                        "Technical counters; no client grants, credentials "
                        "or document bodies."
                    ),
                    "content": {
                        "text/plain; version=0.0.4": {"schema": {"type": "string"}}
                    },
                }
            },
        }
    }
    return {
        "openapi": "3.1.0",
        "info": {"title": "DRP Public Egress Gateway", "version": "1.0.0"},
        "servers": [{"url": "/"}],
        "tags": [
            {"name": name, "description": description} for name, description in TAGS
        ],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "workloadIdentity": {
                    "type": "mutualTLS",
                    "description": (
                        "Own CA-trusted client certificate plus an exact "
                        "operator-approved, unexpired SPIFFE grant. Swagger "
                        "cannot provision or impersonate a workload."
                    ),
                }
            },
            "schemas": {
                "FetchRequest": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(FETCH_FIELDS),
                    "properties": {
                        "method": {"type": "string", "enum": list(FETCH_METHODS)},
                        "url": {
                            "type": "string",
                            "format": "uri",
                            "maxLength": URL_LIMIT,
                            "description": (
                                "Public HTTP(S), port 80/443, no userinfo "
                                "or fragment. Every DNS address is checked "
                                "and the admitted IP is pinned."
                            ),
                        },
                        "headers": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                            "description": (
                                "Only the runtime's closed safe-header list. "
                                "Authorization/X-Subscription-Token need exact "
                                "REGULAR host scopes and HTTPS; Cookie and "
                                "CR/LF/NUL are forbidden."
                            ),
                        },
                        "body": {
                            "type": "string",
                            "format": "byte",
                            "maxLength": 4 * ((BODY_LIMIT + 2) // 3),
                            "description": (
                                f"Base64, decoded at most {BODY_LIMIT} bytes. "
                                "Empty for GET/HEAD. POST needs REGULAR exact "
                                "HTTPS host/path and valid application/json."
                            ),
                        },
                        "max_bytes": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": max_response_bytes,
                            "description": (
                                "Also limited by the individual grant. "
                                "Boolean values are not integers."
                            ),
                        },
                        "truncate": {
                            "type": "boolean",
                            "description": (
                                "Explicit bounded prefix, not completeness; "
                                "forbidden with credentials. Full documents "
                                "and canonical Range downloads use false."
                            ),
                        },
                    },
                },
                "Rejection": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["status", "code"],
                    "properties": {
                        "status": {"type": "string", "enum": ["rejected"]},
                        "code": {"type": "string"},
                    },
                },
            },
        },
    }
