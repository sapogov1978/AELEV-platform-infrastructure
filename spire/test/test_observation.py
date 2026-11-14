"""Own observation contracts; no providers or consumers are discovered."""

import base64
import hashlib
import http.client
import json
import socket
import sqlite3
import ssl
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aelev_spire import observation
from aelev_spire.files import atomic
from aelev_spire.installation import Installer
from aelev_spire.observation import Outgoing, Server, metrics, packet, snapshot
from aelev_spire.observation_setup import (
    bind_network,
    documents,
    gateway_aliases,
    gateway_routes,
    install,
    seed,
)


def test_status_journal_preserves_sequence_and_pins_container_sqlite(
    tmp_path: Path,
) -> None:
    from aelev_platform_ui_client import Outbox

    if Path("/opt/aelev/artifacts.lock.json").is_file():
        assert sqlite3.sqlite_version_info == (3, 53, 4)
    selected = plan()
    journal = tmp_path / "outbox.sqlite"
    first = Outbox(journal, selected["presentation"]["module_id"])
    payload = first.prepare("ONLINE", datetime.now(UTC).isoformat())
    second = Outbox(journal, selected["presentation"]["module_id"])
    assert second.pending() == payload
    second.acknowledge(payload)
    newer = second.prepare("ONLINE", datetime.now(UTC).isoformat())
    assert json.loads(newer)["sequence"] == json.loads(payload)["sequence"] + 1
    with sqlite3.connect(journal) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("PRAGMA journal_mode").fetchone() == ("wal",)


def test_publisher_facade_translates_only_its_own_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aelev_platform_ui_client as client

    cfg = client.Settings(
        "https://ui.example",
        "https://idp.example",
        "aelev",
        "spire-status",
        "platform-ui",
        plan()["presentation"]["module_id"],
        15,
        2,
        tmp_path,
        tmp_path / "outbox.sqlite",
    )

    def settings(_prefix: str) -> client.Settings:
        return cfg

    monkeypatch.setattr(client.Settings, "environment", settings)
    previous = ["spire-observation", "publish", "--source", "fixture"]
    monkeypatch.setattr(sys, "argv", previous)
    called: list[str] = []

    def run(_cfg: Any, owner: str, description: bytes, _probe: Any) -> None:
        assert sys.argv == ["spire-observation", "run"]
        assert json.loads(description)["module_id"] == cfg.module_id
        called.append(owner)

    monkeypatch.setattr(client, "run", run)
    observation.publish("publish", tmp_path / "source.json")
    assert called == ["infrastructure-spire"]
    assert sys.argv is previous


def plan() -> dict[str, Any]:
    return {
        "owner": "infrastructure-spire",
        "schema_version": 1,
        "sender_image": "aelev/owner-telemetry-sender:approved",
        "central_url": "https://central.example",
        "grafana_url": "https://grafana.example",
        "datasource_uid": "aelev-prometheus",
        "presentation": {
            "url": "https://ui.example",
            "identity_url": "https://idp.example",
            "realm": "aelev",
            "client_id": "spire-status",
            "audience": "aelev-platform-ui",
            "module_id": "d2fd6052-b50c-410e-a8b6-0d07d7087366",
        },
    }


def state(source: Path, *, ready: bool = True, age: int = 0) -> None:
    atomic(
        source,
        {
            "owner": "infrastructure-spire",
            "role": "server",
            "ready": ready,
            "reason": "SPIRE_READY" if ready else "SPIRE_NOT_READY",
            "updated_at": (datetime.now(UTC) - timedelta(seconds=age)).isoformat(),
        },
    )


def renewal_state(path: Path, *, ready: bool = True, age: int = 0) -> None:
    now = datetime.now(UTC) - timedelta(seconds=age)
    for target, identities in (
        (
            path,
            (
                "metrics",
                "observer",
                "central-write",
                "local-query",
                "local-write",
                "sender",
            ),
        ),
        (path.with_name("status-query.json"), ("status-query",)),
        (path.with_name("status-publisher.json"), ("status-publisher",)),
    ):
        atomic(
            target,
            {
                "owner": "telemetry-certificate-renewal",
                "consumer_owner": "infrastructure-spire",
                "schema_version": 1,
                "healthy": ready,
                "renewal_healthy": ready,
                "ready": ready,
                "checked_at": now.timestamp(),
                "last_success_at": now.timestamp(),
                "certificates": [
                    {
                        "id": name,
                        "healthy": ready,
                        "certificate_expires_at": now.timestamp() + 90 * 86400,
                        "ca_expires_at": now.timestamp() + 365 * 86400,
                    }
                    for name in identities
                ],
            },
        )


def test_gateway_aliases_are_explicit_and_only_declared_endpoints() -> None:
    selected = plan()
    assert gateway_aliases(selected) == {}
    selected["host_gateway_aliases"] = ["idp.example"]
    documents(selected)
    assert gateway_aliases(selected) == {"idp.example": "host-gateway"}
    for invalid in (["spire-observer"], ["other.example"], ["idp.example"] * 2, [42]):
        selected["host_gateway_aliases"] = invalid
        with pytest.raises(ValueError, match="GATEWAY_ALIASES_INVALID"):
            documents(selected)


def test_network_plan_uses_public_addresses_without_shared_projection_mounts() -> None:
    selected = plan()
    selected["network"] = {
        "source_url": "https://identity-node.example:18084",
        "source_identity": "cn:AELEV SPIRE source",
        "observer_url": "https://observer-node.example:18083",
        "sender_otlp_url": "https://sender-node.example:14318",
        "sender_health_url": "https://sender-node.example:13134",
    }
    public = documents(selected)
    prom = json.loads(public["prometheus.json"])
    assert prom["scrape_configs"][0]["static_configs"][0]["targets"] == [
        "observer-node.example:18083"
    ]
    # These two addresses remain internal to the single sender implementation.
    assert prom["remote_write"][0]["url"] == "https://localhost:4318/api/v1/write"
    model: dict[str, Any] = {
        "services": {
            name: {
                "environment": {},
                "volumes": [
                    {"target": "/run/observation"},
                    {"target": "/run/certificate-renewal"},
                    {"target": "/own-state"},
                ],
            }
            for name in ("spire-observer", "spire-status-publisher")
        },
        "volumes": {
            "spire-observation-public-source": {},
            "spire-certificate-renewal-status": {},
            "own-state": {},
        },
    }
    bind_network(model, selected)
    for service in model["services"].values():
        assert service["environment"][observation.PREFIX + "SOURCE_MODE"] == "https"
        assert (
            service["environment"][observation.PREFIX + "SOURCE_URL"]
            == selected["network"]["source_url"]
        )
        assert service["volumes"] == [
            {"target": "/run/certificate-renewal"},
            {"target": "/own-state"},
        ]
    assert (
        model["services"]["spire-observer"]["environment"][
            observation.PREFIX + "OTLP_ENDPOINT"
        ]
        == selected["network"]["sender_otlp_url"]
    )
    assert (
        model["services"]["spire-status-publisher"]["environment"][
            observation.PREFIX + "OBSERVER_URL"
        ]
        == selected["network"]["observer_url"]
    )
    assert set(model["volumes"]) == {"own-state", "spire-certificate-renewal-status"}


@pytest.mark.parametrize(
    "url",
    [
        "http://observer.example:8083",
        "https://user:password@observer.example:8083",
        "https://observer.example:8083/private",
    ],
)
def test_network_plan_rejects_insecure_or_credential_bearing_endpoints(
    url: str,
) -> None:
    selected = plan()
    selected["network"] = {
        "source_url": url,
        "source_identity": "cn:AELEV SPIRE source",
        "observer_url": "https://observer.example:8083",
        "sender_otlp_url": "https://sender.example:4318",
        "sender_health_url": "https://sender.example:13134",
    }
    with pytest.raises(ValueError, match="HTTPS_REQUIRED"):
        documents(selected)


def test_stale_projection_never_becomes_online(tmp_path: Path) -> None:
    source = tmp_path / "heartbeat.json"
    state(source)
    assert snapshot(source)["ready"] is True
    assert b"aelev_spire_native_ready 1" in metrics(source)
    state(source, age=30)
    with pytest.raises(ValueError, match="STALE"):
        snapshot(source)
    assert b"aelev_spire_source_fresh 0" in metrics(source)
    assert b"aelev_spire_native_ready 0" in metrics(source)


def test_gateway_aliases_never_enter_production(tmp_path: Path) -> None:
    selected = plan()
    selected["host_gateway_aliases"] = ["idp.example"]
    installer = object.__new__(Installer)
    installer.root = tmp_path
    atomic(tmp_path / "deployment.json", {"plan": {"environment": "production"}})
    with pytest.raises(ValueError, match="GATEWAY_ALIASES_LOCAL_ONLY"):
        install(installer, selected, tmp_path)


def test_desktop_route_is_not_the_linux_vm_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def resolve(*_args: Any) -> Any:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.65.254", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    aliases = {"idp.example": "host-gateway"}
    for system in ("Darwin", "Windows"):
        assert gateway_routes(aliases, system) == {"idp.example": "192.168.65.254"}
    assert gateway_routes(aliases, "Linux") == aliases

    def unavailable(*_args: Any) -> Any:
        raise OSError("resolver unavailable")

    monkeypatch.setattr(socket, "getaddrinfo", unavailable)
    assert gateway_routes(aliases, "Linux") == aliases
    with pytest.raises(ValueError, match="DESKTOP_HOST_UNAVAILABLE"):
        gateway_routes(aliases, "Darwin")


def test_only_safe_otlp_fields_and_original_time_are_emitted(tmp_path: Path) -> None:
    source = tmp_path / "heartbeat.json"
    state(source)
    original = snapshot(source)
    values = packet(original, 1000, 2000)
    assert set(values) == {"logs", "traces"}
    assert json.loads(values["logs"])["resourceLogs"][0]["scopeLogs"][0]["logRecords"][
        0
    ]["timeUnixNano"] == str(
        int(datetime.fromisoformat(original["updated_at"]).timestamp() * 10**9)
    )
    assert (
        json.loads(values["logs"])["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0][
            "observedTimeUnixNano"
        ]
        == "2000"
    )
    assert b"SPIRE_READY" in values["logs"]
    assert b"token" not in b"".join(values.values())


def test_recipe_owns_rules_panels_and_no_private_native_mounts() -> None:
    files = documents(plan())
    prom = json.loads(files["prometheus.json"])
    assert prom["scrape_configs"][0]["static_configs"][0]["targets"] == [
        "spire-observer:8083"
    ]
    assert prom["remote_write"][0]["url"] == "https://localhost:4318/api/v1/write"
    assert prom["scrape_configs"][0]["tls_config"]["cert_file"].endswith(
        "local-query.crt"
    )
    assert prom["remote_write"][0]["tls_config"]["cert_file"].endswith(
        "local-write.crt"
    )
    assert "SpireNotReady" in files["rules.json"].decode()
    assert json.loads(files["dashboard.json"])["uid"] == "aelev-infrastructure-spire"
    model = (Path(__file__).parents[1] / "compose.observation.yml").read_text()
    provision = model.split("  spire-status-provision:", 1)[1].split(
        "  spire-dashboard-provision:", 1
    )[0]
    assert 'AELEV_INFRASTRUCTURE_SPIRE_UI_TIMEOUT_SECONDS: "30"' in provision
    assert (
        'AELEV_INFRASTRUCTURE_SPIRE_UI_TIMEOUT_SECONDS: "2"'
        in model.split("services:", 1)[0]
    )
    for forbidden in (
        "spire-server-data:",
        "spire-server-admin:",
        "issuer.key",
        "docker.sock",
    ):
        assert forbidden not in model
    bad = plan()
    bad["central_url"] = "http://central.example"
    with pytest.raises(ValueError, match="HTTPS"):
        documents(bad)


def test_observation_updates_require_explicit_operator_intent(tmp_path: Path) -> None:
    value: dict[str, Any] = {
        "role": "grafana",
        "files": {
            "grafana-ca.crt": base64.b64encode(b"approved-ca").decode(),
            "token": base64.b64encode(b"own-token").decode(),
        },
    }
    seed(tmp_path, value)
    seed(tmp_path, value)
    value["files"]["token"] = base64.b64encode(b"rotated-token").decode()
    with pytest.raises(ValueError, match="PRESERVED"):
        seed(tmp_path, value)
    seed(tmp_path, {**value, "update": True})
    assert (tmp_path / "token").read_bytes() == b"rotated-token"


def test_pending_telemetry_survives_transport_failure(tmp_path: Path) -> None:
    outgoing = Outgoing(
        tmp_path / "queue", "https://localhost:1", tmp_path / "absent-secrets"
    )
    outgoing.capture({"ready": False, "reason": "SPIRE_NOT_READY"}, 1, 2)
    pending = next(p for p in outgoing.root.glob("*.json") if p.name != "owner.json")
    raw = pending.read_bytes()
    with pytest.raises(OSError):
        outgoing.flush_one()
    assert pending.read_bytes() == raw
    again = Outgoing(
        tmp_path / "queue", "https://localhost:1", tmp_path / "absent-secrets"
    )
    with pytest.raises(OSError):
        again.flush_one()
    assert pending.read_bytes() == raw


def test_exact_durable_receipt_is_required_and_capacity_preserves_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    phases: list[str] = []
    completed: list[str] = []
    outgoing = Outgoing(
        tmp_path / "queue",
        "https://localhost",
        tmp_path / "secrets",
        phases.append,
        lambda: completed.append(phases[-1]),
    )
    monkeypatch.setattr(observation, "CAPACITY", 1)
    outgoing.capture({"ready": True, "reason": "SPIRE_READY"}, 1, 2)
    pending = next(p for p in outgoing.root.glob("*.json") if p.name != "owner.json")
    original = pending.read_bytes()
    with pytest.raises(ValueError, match="CAPACITY"):
        outgoing.capture({"ready": True, "reason": "SPIRE_READY"}, 3, 4)
    assert pending.read_bytes() == original
    accepted = [False]

    class TLS:
        minimum_version = ssl.TLSVersion.TLSv1_2

        def load_cert_chain(self, *_args: Any) -> None:
            return

    class Connection:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.raw = b""

        def request(
            self, method: str, route: str, raw: bytes, headers: dict[str, str]
        ) -> None:
            kind = route.rsplit("/", 1)[1]
            digest = hashlib.sha256(
                json.dumps(
                    [kind, {"Content-Type": "application/json"}],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                + b"\x00"
                + raw
            ).hexdigest()
            self.raw = (
                json.dumps(
                    {
                        "id": headers["X-Aelev-Delivery-Id"],
                        "sha256": digest,
                        "durable": True,
                    }
                ).encode()
                if accepted[0]
                else b"{}"
            )

        def getresponse(self) -> Any:
            class Reply:
                status = 200

                def read(_self, _size: int) -> bytes:
                    return self.raw

            return Reply()

        def close(self) -> None:
            return

    def context(**_kwargs: Any) -> TLS:
        return TLS()

    monkeypatch.setattr(observation.ssl, "create_default_context", context)
    monkeypatch.setattr(observation.http.client, "HTTPSConnection", Connection)
    with pytest.raises(ValueError, match="ACK_REQUIRED"):
        outgoing.flush_one()
    assert pending.read_bytes() == original
    accepted[0] = True
    outgoing.flush_one()
    assert not pending.exists()
    assert completed.count("HTTP_CONNECT") == 3
    assert completed.count("HTTP_REPLY") == 3
    assert completed.count("HTTP_BODY") == 3
    assert completed.count("JOURNAL_ACK") == 2
    assert completed[-1] == "JOURNAL_DELETE"


def test_real_mtls_readiness_does_not_replace_liveness(
    tmp_path: Path, credential: tuple[bytes, bytes, bytes]
) -> None:
    cert, key, ca = credential
    for name, raw in {
        "metrics.crt": cert,
        "metrics.key": key,
        "query-ca.crt": ca,
    }.items():
        atomic(tmp_path / name, raw)
    source = tmp_path / "heartbeat.json"
    renewal = tmp_path / "renewal.json"
    renewal_state(renewal)
    state(source, ready=False)
    with Server(("127.0.0.1", 0), source, tmp_path, renewal=renewal) as server:
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        thread.start()
        try:
            context = ssl.create_default_context(cafile=str(tmp_path / "query-ca.crt"))
            context.load_cert_chain(tmp_path / "metrics.crt", tmp_path / "metrics.key")

            def request(path: str) -> tuple[int, bytes]:
                client = http.client.HTTPSConnection(
                    "localhost", server.server_port, context=context, timeout=2
                )
                try:
                    client.request("GET", path)
                    reply = client.getresponse()
                    return reply.status, reply.read(4096)
                finally:
                    client.close()

            assert request("/healthz") == (200, b'{"status":"ok"}')
            assert request("/readyz")[0] == 503
            state(source)
            assert request("/readyz") == (200, b'{"status":"ready"}')
            assert b"aelev_spire_native_ready 1" in request("/metrics")[1]
        finally:
            server.shutdown()
            thread.join(timeout=3)
