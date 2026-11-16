"""Keep failed readiness evidence useful without publishing arbitrary fields."""

import json
import http.client
import ssl
from unittest.mock import Mock

import pytest

import sender_diagnostic as diagnostic


def test_status_retains_only_bounded_counters_and_known_errors() -> None:
    value = diagnostic.sanitize(
        {
            "pending": 3,
            "failed": 1,
            "worker_alive": 1,
            "certificate_renewal_ready": 0,
            "admission_pressure": True,
            "records": -1,
            "bytes": 2**64,
            "credentials": "private-must-not-leave",
            "blocking_errors": {
                "logs": "tls_failure",
                "traces": "http_403",
                "metrics": "https://private-endpoint/credential",
                "unknown-private-kind": "private-message",
            },
        }
    )
    assert value == {
        "pending": 3,
        "failed": 1,
        "worker_alive": 1,
        "certificate_renewal_ready": 0,
        "blocking_errors": {
            "logs": "tls_failure",
            "traces": "http_403",
            "metrics": "delivery_failed",
        },
    }
    assert "private" not in json.dumps(value)


@pytest.mark.parametrize("value", [None, [], "private-string"])
def test_non_object_status_is_refused(value: object) -> None:
    with pytest.raises(ValueError, match="ROOT_SPIRE_SENDER_STATUS_INVALID"):
        diagnostic.sanitize(value)


@pytest.mark.parametrize(
    "status,body", [(200, b'{"worker_alive":1}'), (503, b"{}"), (200, b"x" * 65537)]
)
def test_probe_uses_own_mtls_bounded_existing_api_and_closes(
    monkeypatch: pytest.MonkeyPatch, status: int, body: bytes
) -> None:
    monkeypatch.setenv(
        "AELEV_INFRASTRUCTURE_SPIRE_SENDER_HEALTH_URL",
        "https://sender.example.invalid:13134",
    )
    context = Mock()
    monkeypatch.setattr(ssl, "create_default_context", Mock(return_value=context))
    reply = Mock(status=status)
    reply.read.return_value = body
    client = Mock()
    client.getresponse.return_value = reply
    connect = Mock(return_value=client)
    monkeypatch.setattr(http.client, "HTTPSConnection", connect)
    if status == 200 and len(body) <= 65536:
        assert diagnostic.probe()["worker_alive"] == 1
    else:
        with pytest.raises(ValueError, match="ROOT_SPIRE_SENDER_STATUS_INVALID"):
            diagnostic.probe()
    client.request.assert_called_once_with("GET", "/status")
    reply.read.assert_called_once_with(65537)
    client.close.assert_called_once()
    context.load_cert_chain.assert_called_once_with(
        "/run/secrets/status/query.crt", "/run/secrets/status/query.key"
    )


@pytest.mark.parametrize(
    "origin",
    [
        "http://sender.invalid",
        "https://user:password@sender.invalid",
        "https://sender.invalid/private",
    ],
)
def test_origin_cannot_supply_credentials_or_another_api(
    monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    monkeypatch.setenv("AELEV_INFRASTRUCTURE_SPIRE_SENDER_HEALTH_URL", origin)
    with pytest.raises(ValueError, match="ROOT_SPIRE_SENDER_ORIGIN_INVALID"):
        diagnostic.probe()
