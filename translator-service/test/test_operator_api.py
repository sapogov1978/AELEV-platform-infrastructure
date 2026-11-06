"""Operator documentation cannot become another translation ingress."""

import asyncio
import hashlib
import signal
import socket
from collections.abc import Callable
from typing import cast

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from translator_service import api_server, operator_api
from translator_service.api import app as internal_app


def client_for(app: FastAPI) -> httpx.Client:
    # Starlette's transitional client annotations do not describe httpx methods.
    return cast(httpx.Client, TestClient(app))


def test_swagger_only_describes_existing_read_only_operations() -> None:
    assert (
        sum(
            getattr(route, "path", None) == "/openapi.json"
            for route in operator_api.app.routes
        )
        == 1
    )
    with client_for(operator_api.app) as client, client_for(internal_app) as private:
        schema = client.get("/openapi.json").json()
        assert schema["paths"] == private.get("/openapi.json").json()["paths"]
        assert set(schema["paths"]) == {
            "/healthz",
            "/readyz",
            "/v1/status",
            "/v1/components",
            "/metrics",
        }
        assert all(set(value) == {"get"} for value in schema["paths"].values())
        assert (
            client.post("/v1/translate-text", json={"text": "text"}).status_code == 404
        )
        assert client.post("/v1/status", json={}).status_code == 405
        assert private.post("/v1/translate", json={}).status_code == 404
        assert private.get("/docs").status_code == 404


def test_documentation_is_local_and_protected_by_browser_headers() -> None:
    with client_for(operator_api.app) as client:
        response = client.get("/docs")
        assert response.status_code == 200
        assert "/docs/assets/swagger-ui-bundle.js" in response.text
        assert "/docs/assets/swagger-ui.css" in response.text
        assert "https://" not in response.text
        assert "http://" not in response.text
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cache-control"] == "no-store"
        assert "script-src 'self';" in response.headers["content-security-policy"]
        setup = client.get("/docs/init.js").text
        assert 'url:"/openapi.json"' in setup
        assert "validatorUrl:null" in setup
        assert "persistAuthorization:false" in setup


@pytest.mark.parametrize("application", [internal_app, operator_api.app])
def test_documentation_groups_have_explicit_order_and_membership(
    application: FastAPI,
) -> None:
    with client_for(application) as client:
        schema = client.get("/openapi.json").json()
        assert [tag["name"] for tag in schema["tags"]] == [
            "default",
            "translator",
            "metrics",
        ]
        expected = {
            "/healthz": "default",
            "/readyz": "default",
            "/v1/status": "default",
            "/v1/components": "default",
            "/metrics": "metrics",
        }
        assert set(schema["paths"]) == set(expected)
        for path, tag in expected.items():
            operation = schema["paths"][path]["get"]
            assert operation["tags"] == [tag]
            assert operation["summary"]
            assert operation["description"]


@pytest.mark.parametrize(
    "name,digest",
    [
        (
            "swagger-ui-bundle.js",
            "050bc415ee7048dcd881682678f720264e7da5e373f7461d7c58c755305255f7",
        ),
        (
            "swagger-ui.css",
            "1ac324f7dcd27e4b9386b4bd6421271ec147e922a22c05ba24b11515e9aa6321",
        ),
    ],
)
def test_served_assets_match_the_pinned_release(name: str, digest: str) -> None:
    with client_for(operator_api.app) as client:
        response = client.get("/docs/assets/" + name)
        assert response.status_code == 200
        assert hashlib.sha256(response.content).hexdigest() == digest
        assert client.get("/docs/assets/other.js").status_code == 404


@pytest.mark.parametrize(
    "url", ["http://localhost:18496", "http://127.0.0.1:18496", "http://[::1]:18496"]
)
def test_explicit_local_documentation_address(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv(api_server.PREFIX + "ENVIRONMENT", "local")
    monkeypatch.setenv(api_server.PREFIX + "API_PUBLIC_URL", url)
    assert api_server.swagger_url() == url + "/docs"


@pytest.mark.parametrize("environment", ["production", "paper", "test"])
def test_http_never_works_outside_local(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    monkeypatch.setenv(api_server.PREFIX + "ENVIRONMENT", environment)
    monkeypatch.setenv(api_server.PREFIX + "API_PUBLIC_URL", "http://127.0.0.1:18496")
    with pytest.raises(ValueError):
        api_server.swagger_url()


@pytest.mark.parametrize(
    "url",
    [
        "http://0.0.0.0",
        "http://owner.invalid",
        "http://localhost.invalid",
        "http://127.1",
        "http://2130706433",
        "http://127.0.0.1:bad",
        "http://127.0.0.1:0",
        "http://user@localhost",
        "http://[::1]evil",
        "http://localhost:",
        "http://localhost/#",
    ],
)
def test_local_http_does_not_admit_remote_or_ambiguous_hosts(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv(api_server.PREFIX + "ENVIRONMENT", "local")
    monkeypatch.setenv(api_server.PREFIX + "API_PUBLIC_URL", url)
    with pytest.raises(ValueError):
        api_server.swagger_url()


@pytest.mark.parametrize(
    "value",
    [
        "http://owner.invalid",
        "https://owner.invalid/path",
        "https://owner.invalid/?secret=bad",
        "https://user:password@owner.invalid",
        "https://@owner.invalid",
        "https://owner.invalid/\x7f",
        "https://owner.invalid/#fragment",
    ],
)
def test_public_address_cannot_be_plaintext_or_carry_credentials(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(api_server.PREFIX + "API_PUBLIC_URL", value)
    with pytest.raises(ValueError):
        api_server.swagger_url()


def test_missing_tls_fails_before_any_listener_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        api_server.PREFIX + "API_PUBLIC_URL", "https://owner.invalid:8443"
    )
    monkeypatch.setenv(api_server.PREFIX + "API_BIND_ADDRESS", "127.0.0.1")
    monkeypatch.setenv(api_server.PREFIX + "API_CONTAINER_PORT", "8096")
    monkeypatch.setenv(api_server.PREFIX + "API_HTTPS_CONTAINER_PORT", "8443")
    with pytest.raises(FileNotFoundError):
        api_server.configurations()


def test_plaintext_host_port_cannot_be_exposed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(api_server.PREFIX + "API_PUBLIC_URL", "https://owner.invalid")
    monkeypatch.setenv(api_server.PREFIX + "API_BIND_ADDRESS", "0.0.0.0")
    with pytest.raises(ValueError, match="LOOPBACK"):
        api_server.configurations()


def test_local_operator_cannot_publish_on_wide_host_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in {
        "API_PUBLIC_URL": "http://localhost:18496",
        "ENVIRONMENT": "local",
        "API_BIND_ADDRESS": "127.0.0.1",
        "API_CONTAINER_PORT": "8096",
        "API_HTTPS_CONTAINER_PORT": "8443",
        "API_HTTPS_BIND_ADDRESS": "0.0.0.0",
    }.items():
        monkeypatch.setenv(api_server.PREFIX + key, value)
    with pytest.raises(ValueError, match="LOOPBACK"):
        api_server.configurations()


def test_local_operator_never_starts_without_oidc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in {
        "API_PUBLIC_URL": "http://localhost:18496",
        "ENVIRONMENT": "local",
        "API_BIND_ADDRESS": "127.0.0.1",
        "API_CONTAINER_PORT": "8096",
        "API_HTTPS_CONTAINER_PORT": "8443",
        "API_HTTPS_BIND_ADDRESS": "127.0.0.1",
    }.items():
        monkeypatch.setenv(api_server.PREFIX + key, value)
    monkeypatch.delenv(api_server.PREFIX + "API_OIDC_ISSUER", raising=False)
    with pytest.raises(KeyError):
        api_server.configurations()


def test_missing_public_address_is_not_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(api_server.PREFIX + "API_PUBLIC_URL", raising=False)
    with pytest.raises(KeyError):
        api_server.swagger_url()


class FakeServer(api_server.Server):
    def __init__(self, failure: str = "") -> None:
        super().__init__(uvicorn.Config("unused:app"))
        self.failure = failure
        self.ended = False

    async def serve(self, sockets: list[socket.socket] | None = None) -> None:
        try:
            if self.failure == "raise":
                raise RuntimeError("LISTENER_FAILURE")
            if self.failure == "return":
                return
            while not self.should_exit:
                await asyncio.sleep(0.001)
        finally:
            self.ended = True


@pytest.mark.parametrize("failure", ["raise", "return"])
def test_one_failed_listener_closes_the_other(failure: str) -> None:
    first, second = FakeServer(failure), FakeServer()
    with pytest.raises(RuntimeError):
        asyncio.run(api_server.serve((first, second)))
    assert first.ended and second.ended
    assert first.should_exit and second.should_exit


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT])
def test_signals_close_both_listeners(
    monkeypatch: pytest.MonkeyPatch, stop_signal: signal.Signals
) -> None:
    first, second = FakeServer(), FakeServer()

    async def exercise() -> None:
        loop = asyncio.get_running_loop()

        def register(value: int, callback: Callable[[], None]) -> None:
            if value == stop_signal:
                loop.call_later(0.005, callback)

        monkeypatch.setattr(loop, "add_signal_handler", register)
        await api_server.serve((first, second))

    asyncio.run(exercise())
    assert first.ended and second.ended
