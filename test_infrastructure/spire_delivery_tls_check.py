"""Actual Delivery ingestion with the generated fresh SPIRE client identity."""

from __future__ import annotations

import importlib
import http.client
import json
import os
from pathlib import Path
import ssl
import sys
import threading
from uuid import uuid4

OWNER = Path(__file__).resolve().parents[1] / "observability/delivery"
sys.path.insert(0, str(OWNER))
prepare = importlib.import_module("prepare")  # verifies the pinned public wheel
tooling = importlib.import_module("aelev_observability_tools")
delivery = importlib.import_module("delivery")


def main() -> None:
    os.umask(0o077)
    cfg = json.loads(Path(sys.argv[1]).read_bytes())
    wrong = Path(sys.argv[2])
    client = cfg["issued"]["central-write"]
    issuer = Path(client["issuer_certificate"]).parent
    root = Path(sys.argv[1]).parent / "delivery-tls-boundary"
    root.mkdir(mode=0o700)
    spec = json.loads((prepare.ROOT / "deployment.json").read_bytes())
    env = tooling.read_env(OWNER / ".env.example", spec["prefix"])
    env.update(
        SECRET_ROOT="secrets", SECRET_GID=str(os.getgid()), PUBLIC_HOST="localhost"
    )
    tooling.generate_tls(root, env, spec, issuer)
    secrets = root / "secrets"
    settings = delivery.Settings(
        mode="archive",
        listen_host="127.0.0.1",
        listen_port=0,
        health_port=0,
        journal=root / "journal",
        capacity_bytes=100_000,
        request_bytes=4096,
        retry_seconds=0.1,
        timeout_seconds=2,
        server_cert=str(secrets / "server.crt"),
        server_key=str(secrets / "server.key"),
        client_ca=str(secrets / "publisher-ca.crt"),
        ca=str(secrets / "ca.crt"),
        client_cert=str(secrets / "client.crt"),
        client_key=str(secrets / "client.key"),
        health_ca=str(secrets / "ca.crt"),
        health_cert=str(secrets / "client.crt"),
        health_key=str(secrets / "client.key"),
        destinations={},
        prometheus_command=[],
    )
    agent = delivery.Delivery(settings)
    server = delivery.DeliveryServer(agent)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    body = b'{"resourceLogs":[]}'
    identifier = str(uuid4())

    def send(certificate: Path, key: Path) -> tuple[int, bytes]:
        context = ssl.create_default_context(cafile=cfg["trust"]["central"])
        context.load_cert_chain(certificate, key)
        connection = http.client.HTTPSConnection(
            "localhost", server.server_port, timeout=3, context=context
        )
        try:
            connection.request(
                "POST",
                "/v1/logs",
                body,
                {"Content-Type": "application/json", "X-Aelev-Delivery-Id": identifier},
            )
            reply = connection.getresponse()
            return reply.status, reply.read(4097)
        finally:
            connection.close()

    try:
        try:
            send(wrong, wrong.with_suffix(".key"))
        except ssl.SSLError:
            pass
        else:
            raise AssertionError("WRONG_CLIENT_ISSUER_ACCEPTED")
        assert agent.journal.status()["records"] == 0
        status, raw = send(Path(client["certificate"]), Path(client["key"]))
        assert status == 200
        record = agent.journal.first("logs")
        assert record is not None and record.body == body
        assert json.loads(raw) == {
            "id": identifier,
            "sha256": record.digest,
            "durable": True,
        }
        assert agent.journal.status()["records"] == 1
        print(
            json.dumps(
                {
                    "accepted_packets": 1,
                    "wrong_client_issuer_rejected": True,
                    "durable_ack": True,
                }
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        agent.lease.close()


if __name__ == "__main__":
    main()
