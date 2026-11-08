"""A stalled DCS resolver must not block fresh, authenticated quorum reads."""

from __future__ import annotations

import base64
import threading
from typing import Any
from unittest.mock import Mock

import pytest

from aelev_postgresql import fencing, fencing_return
from aelev_postgresql.configuration import PREFIX


@pytest.fixture
def configured(monkeypatch: Any) -> None:
    monkeypatch.setenv(PREFIX + "CLUSTER", "unit")
    for n in range(1, 4):
        monkeypatch.setenv(PREFIX + f"NODE_{n}_ETCD_HOST", f"etcd-{n}")
        monkeypatch.setenv(PREFIX + f"NODE_{n}_ETCD_CLIENT_PORT", "2379")
    monkeypatch.setattr(
        fencing.ssl, "create_default_context", Mock(return_value=Mock())
    )
    monkeypatch.setattr(
        fencing, "DCS_SLOTS", {n: threading.BoundedSemaphore(1) for n in range(1, 4)}
    )


def response(number: int, lease: str = "9") -> dict[str, Any]:
    return {
        "header": {"cluster_id": "42", "member_id": str(number), "revision": "99"},
        "kvs": [
            {
                "value": base64.b64encode(b"postgresql-2").decode(),
                "lease": lease,
                "create_revision": "7",
            }
        ],
    }


def test_stalled_first_endpoint_does_not_delay_or_poison_fresh_claims(
    configured: None,
    monkeypatch: Any,
) -> None:
    release, entered, done = threading.Event(), threading.Event(), threading.Event()
    lease = ["9"]
    results: list[Any] = []
    calls: list[int] = []

    def request(url: str, payload: Any, context: Any) -> Any:
        number = int(url.split("etcd-")[1][0])
        calls.append(number)
        assert payload["serializable"] is False
        if number == 1:
            entered.set()
            release.wait(5)
        return response(number, lease[0])

    monkeypatch.setattr(fencing, "request_json", request)

    def check() -> None:
        try:
            results.append(fencing.Claim.current())
            lease[0] = "10"
            results.append(fencing.Claim.current())
        except Exception as error:
            results.append(error)
        finally:
            done.set()

    thread = threading.Thread(target=check, daemon=True)
    thread.start()
    try:
        assert entered.wait(1)
        assert done.wait(1), "A stalled first endpoint blocked healthy DCS members"
        assert results == [
            fencing.Claim("postgresql-2", "9", "7"),
            fencing.Claim("postgresql-2", "10", "7"),
        ]
        assert calls.count(1) == 1
    finally:
        release.set()
        thread.join(2)


def test_return_requires_second_fresh_member_after_first_read(
    configured: None,
    monkeypatch: Any,
) -> None:
    release = threading.Event()
    calls: list[int] = []

    def request(url: str, payload: Any, context: Any) -> Any:
        number = int(url.split("etcd-")[1][0])
        calls.append(number)
        if number == 1:
            release.wait(5)
            raise OSError("unavailable")
        return response(number)

    monkeypatch.setattr(fencing_return, "request_json", request)
    try:
        assert fencing_return.confirmed_claim() == fencing.Claim(
            "postgresql-2", "9", "7"
        )
        # Two different members, and an extra fresh read after the first batch.
        assert 2 in calls and 3 in calls
        assert len([n for n in calls if n != 1]) >= 2
    finally:
        release.set()
