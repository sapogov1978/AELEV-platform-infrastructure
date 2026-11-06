from __future__ import annotations

import base64
import socket
from pathlib import Path

import pytest

import gateway


def _config(*, mode: str = "EXPLORATION") -> gateway.GatewayConfig:
    return gateway.GatewayConfig(
        mode=mode,
        bind_host="127.0.0.1",
        port=0,
        max_response_bytes=1_000_000,
        timeout_seconds=2.0,
        max_concurrency=4,
        per_host_interval_seconds=0.0,
        deny_networks=(),
        credential_scopes=(
            {
                "authorization": frozenset({"download.example"}),
                "x-subscription-token": frozenset({"search.example"}),
            }
            if mode == "REGULAR"
            else {}
        ),
        post_scopes=(
            {"account.example": ("/api/authenticate",)} if mode == "REGULAR" else {}
        ),
        server_cert_path=None,
        server_key_path=None,
        client_ca_path=None,
        grants_directory=Path("."),
        trust_domain="example.test",
        docs_port=0,
        public_url="https://gateway.example.test",
    )


def _fetch_document(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    truncate: bool = False,
    max_bytes: int = 100_000,
) -> dict[str, object]:
    return {
        "method": method,
        "url": url,
        "headers": headers or {},
        "body": base64.b64encode(body).decode("ascii"),
        "max_bytes": max_bytes,
        "truncate": truncate,
    }


def test_address_policy_allows_unknown_public_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def public_result(*args: object, **kwargs: object) -> list[tuple[object, ...]]:
        del args, kwargs
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", public_result)

    assert gateway.AddressPolicy(_config()).resolve("never-listed.example", 443) == (
        "93.184.216.34",
    )


def test_address_policy_blocks_mixed_public_private_dns_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def rebinding_result(
        *args: object,
        **kwargs: object,
    ) -> list[tuple[object, ...]]:
        del args, kwargs
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", rebinding_result)

    with pytest.raises(gateway.GatewayRejected, match="EGRESS_ADDRESS_NOT_GLOBAL"):
        gateway.AddressPolicy(_config()).resolve("rebinding.example", 443)


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "10.1.2.3",
        "169.254.169.254",
        "224.0.0.1",
        "::1",
        "ff02::1",
        "metadata.google.internal",
        "service.cluster.local",
    ],
)
def test_address_policy_blocks_non_public_targets(host: str) -> None:
    with pytest.raises(gateway.GatewayRejected):
        gateway.AddressPolicy(_config()).resolve(host, 443)


def test_exploration_accepts_read_of_any_public_url() -> None:
    parsed = gateway.GatewayApplication(_config()).parse_fetch(
        _fetch_document("https://previously-unknown.example/document")
    )

    assert parsed.hostname == "previously-unknown.example"
    assert parsed.method == "GET"


def test_exploration_rejects_post_body_and_credentials() -> None:
    application = gateway.GatewayApplication(_config())

    with pytest.raises(gateway.GatewayRejected, match="EGRESS_METHOD_BLOCKED"):
        application.parse_fetch(
            _fetch_document(
                "https://account.example/api/authenticate",
                method="POST",
                headers={"content-type": "application/json"},
                body=b'"secret"',
            )
        )
    with pytest.raises(gateway.GatewayRejected, match="EGRESS_CREDENTIAL_BLOCKED"):
        application.parse_fetch(
            _fetch_document(
                "https://search.example/",
                headers={"x-subscription-token": "secret"},
            )
        )


def test_regular_post_and_credential_headers_are_exactly_scoped() -> None:
    application = gateway.GatewayApplication(_config(mode="REGULAR"))

    request = application.parse_fetch(
        _fetch_document(
            "https://account.example/api/authenticate",
            method="POST",
            headers={"content-type": "application/json"},
            body=b'{"username":"operator"}',
        )
    )
    assert request.body == b'{"username":"operator"}'
    with pytest.raises(gateway.GatewayRejected, match="EGRESS_POST_SCOPE_BLOCKED"):
        application.parse_fetch(
            _fetch_document(
                "https://other.example/api/authenticate",
                method="POST",
                headers={"content-type": "application/json"},
                body=b"{}",
            )
        )
    with pytest.raises(gateway.GatewayRejected, match="EGRESS_POST_SCOPE_BLOCKED"):
        application.parse_fetch(
            _fetch_document(
                "https://account.example/api/authenticate-impersonated",
                method="POST",
                headers={"content-type": "application/json"},
                body=b"{}",
            )
        )
    with pytest.raises(
        gateway.GatewayRejected,
        match="EGRESS_CREDENTIAL_SCOPE_BLOCKED",
    ):
        application.parse_fetch(
            _fetch_document(
                "https://other.example/",
                headers={"authorization": "Bearer secret"},
            )
        )


def test_metrics_never_go_negative_and_local_has_no_tls_bypass() -> None:
    collector = gateway.GatewayMetrics("EXPLORATION")
    collector.begin()
    collector.finish("/v1/fetch", "REJECTED", 0)
    metrics = collector.render().decode()
    assert 'aelev_egress_active_requests{mode="EXPLORATION"} 0' in metrics
    with pytest.raises(RuntimeError, match="requires mTLS"):
        gateway.build_server(_config())


def test_nonlocal_configuration_fails_closed_without_mtls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(gateway.PREFIX + "DENY_CIDRS", raising=False)
    with pytest.raises(RuntimeError, match="set required variable"):
        gateway.GatewayConfig.from_env()


def test_upstream_failure_metric_code_never_uses_platform_error_text() -> None:
    assert gateway.upstream_failure_code(OSError("address and secret path")) == (
        "EGRESS_UPSTREAM_FAILED"
    )
    assert (
        gateway.upstream_failure_code(
            gateway.GatewayUpstreamError("EGRESS_DNS_UNAVAILABLE")
        )
        == "EGRESS_DNS_UNAVAILABLE"
    )


def test_regular_post_never_sends_plaintext_or_non_json() -> None:
    application = gateway.GatewayApplication(_config(mode="REGULAR"))
    with pytest.raises(gateway.GatewayRejected, match="POST_REQUIRES_TLS"):
        application.parse_fetch(
            _fetch_document(
                "http://account.example/api/authenticate",
                method="POST",
                headers={"content-type": "application/json"},
                body=b"{}",
            )
        )
    with pytest.raises(gateway.GatewayRejected, match="BODY_INVALID"):
        application.parse_fetch(
            _fetch_document(
                "https://account.example/api/authenticate",
                method="POST",
                headers={"content-type": "application/json"},
                body=b"not JSON",
            )
        )


@pytest.mark.parametrize(
    ("error_number", "expected_code"),
    [
        (socket.EAI_NONAME, "EGRESS_DNS_NOT_FOUND"),
        (socket.EAI_AGAIN, "EGRESS_DNS_TEMPORARY"),
    ],
)
def test_address_policy_distinguishes_absent_name_from_temporary_dns_failure(
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
    expected_code: str,
) -> None:
    def failed_resolution(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise socket.gaierror(error_number, "resolver failure")

    monkeypatch.setattr(socket, "getaddrinfo", failed_resolution)

    with pytest.raises(gateway.GatewayUpstreamError, match=expected_code):
        gateway.AddressPolicy(_config()).resolve("observed.example", 443)
