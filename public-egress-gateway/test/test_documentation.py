"""Swagger groups, exact fetch shape and owner-published browser links."""

from __future__ import annotations

import hashlib
import json
import ssl
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from aelev_platform_ui_client import Settings
from test_gateway import _config  # pyright: ignore[reportPrivateUsage]

import api_contract
import documentation
import gateway
import status_publisher
from health import PREFIX


def schema(maximum: int = 1000000) -> dict[str, Any]:
    return cast(
        dict[str, Any], api_contract.openapi(maximum, "https://gateway.example.test")
    )


def test_groups_are_ordered_and_have_only_real_operations() -> None:
    document = schema()
    assert [tag["name"] for tag in document["tags"]] == [
        "default",
        "gateway",
        "metrics",
    ]
    assert set(document["paths"]) == {"/healthz", "/readyz", "/v1/fetch", "/metrics"}
    expected = {
        "/healthz": ("get", "default"),
        "/readyz": ("get", "default"),
        "/v1/fetch": ("post", "gateway"),
        "/metrics": ("get", "metrics"),
    }
    for path, (method, tag) in expected.items():
        assert set(document["paths"][path]) == {method}
        operation = document["paths"][path][method]
        assert operation["tags"] == [tag]
        assert operation["security"] == (
            [{"workloadIdentity": []}] if method == "post" else []
        )
    assert (
        document["components"]["securitySchemes"]["workloadIdentity"]["type"]
        == "mutualTLS"
    )
    assert document["servers"] == [{"url": "/"}]
    assert document["paths"]["/v1/fetch"]["post"]["servers"] == [
        {"url": "https://gateway.example.test"}
    ]


def test_standard_probes_publish_canonical_json_contract() -> None:
    document = schema()
    for path, status in (("/healthz", "ok"), ("/readyz", "ready")):
        response = document["paths"][path]["get"]["responses"]["200"]
        assert set(response["content"]) == {"application/json"}
        assert response["content"]["application/json"]["example"] == {"status": status}


def test_fetch_schema_uses_the_runtime_contract_and_current_limit() -> None:
    request = schema(2048)["components"]["schemas"]["FetchRequest"]
    assert set(request["properties"]) == set(api_contract.FETCH_FIELDS)
    assert request["required"] == list(api_contract.FETCH_FIELDS)
    assert request["additionalProperties"] is False
    assert request["properties"]["method"]["enum"] == list(api_contract.FETCH_METHODS)
    assert request["properties"]["max_bytes"]["maximum"] == 2048
    assert request["properties"]["url"]["maxLength"] == api_contract.URL_LIMIT
    example = schema(2048)["paths"]["/v1/fetch"]["post"]["requestBody"]["content"][
        "application/json"
    ]["example"]
    assert example["max_bytes"] == 2048 and example["truncate"] is False
    assert example["headers"] == {} and example["body"] == ""


def test_documentation_uses_only_local_assets_and_closed_paths() -> None:
    assert b"https://" not in documentation.HTML + documentation.INITIALIZE
    assert b"validatorUrl:null" in documentation.INITIALIZE
    assert b"queryConfigEnabled:false" in documentation.INITIALIZE
    assert b"persistAuthorization:false" in documentation.INITIALIZE
    assert b"supportedSubmitMethods:['get']" in documentation.INITIALIZE
    assert "connect-src 'self'" in documentation.CSP
    assert documentation.asset("/docs/assets/../secrets/gateway/server.key") is None
    assert documentation.asset("/docs/assets/unknown.js") is None
    for path, expected in (
        (
            "/docs/assets/swagger-ui-bundle.js",
            "050bc415ee7048dcd881682678f720264e7da5e373f7461d7c58c755305255f7",
        ),
        (
            "/docs/assets/swagger-ui.css",
            "1ac324f7dcd27e4b9386b4bd6421271ec147e922a22c05ba24b11515e9aa6321",
        ),
    ):
        value = documentation.asset(path)
        assert value is not None and hashlib.sha256(value[0]).hexdigest() == expected


def test_read_only_listener_never_requests_a_workload_certificate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    context = MagicMock(spec=ssl.SSLContext)
    server = MagicMock()
    monkeypatch.setattr(
        gateway.ssl, "create_default_context", MagicMock(return_value=context)
    )
    monkeypatch.setattr(gateway, "GatewayServer", server)
    gateway.build_server(
        replace(
            _config(),
            server_cert_path="/not-used/server.pem",
            server_key_path="/not-used/server.key",
            client_ca_path="/not-used/ca.pem",
        ),
        documentation_only=True,
    )
    assert context.verify_mode == ssl.CERT_NONE
    context.load_verify_locations.assert_not_called()
    handler = server.call_args.args[1]
    assert issubclass(handler, gateway.DocumentationHandler)
    assert server.call_args.args[3] == 8


@pytest.mark.parametrize(
    "value",
    [
        "",
        "http://localhost:18497",
        "https://operator@host",
        "https://host/docs",
        "https://host?token=input",
        "https://host/#x",
        "https://host:65536",
        "https://ho st",
        "https://host\\test",
        "https://host\x7f",
    ],
)
def test_browser_origin_is_explicit_and_safe(value: str) -> None:
    with pytest.raises(ValueError):
        api_contract.swagger_url(value)


def test_publisher_owns_swagger_link_and_has_no_injected_ui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        PREFIX + "DOCS_PUBLIC_URL", "https://gateway.example.test:18498/"
    )
    cfg = Settings(
        "https://shell.example.test",
        "https://idp.example.test",
        "aelev",
        "gateway-status",
        "platform-ui",
        "33e17098-a989-4398-990a-f435c163639d",
        60,
        10,
        Path("/not-used"),
        Path("/not-used"),
    )
    document: dict[str, Any] = json.loads(status_publisher.description(cfg))
    assert document["revision"] == 2
    assert document["service_kind"] == "INFRASTRUCTURE" and document["sections"] == []
    assert document["swagger_url"] == "https://gateway.example.test:18498/docs"
    components = {item["id"]: item for item in document["components"]}
    assert components["gateway"]["swagger_url"] == document["swagger_url"]
    assert "swagger_url" not in components["publisher"]
    assert "ui_url" not in document


def test_missing_browser_origin_does_not_publish_a_fabricated_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(PREFIX + "DOCS_PUBLIC_URL", raising=False)
    cfg = Settings(
        "https://shell.example.test",
        "https://idp.example.test",
        "aelev",
        "gateway-status",
        "platform-ui",
        "33e17098-a989-4398-990a-f435c163639d",
        60,
        10,
        Path("/not-used"),
        Path("/not-used"),
    )
    with pytest.raises(KeyError):
        status_publisher.description(cfg)
