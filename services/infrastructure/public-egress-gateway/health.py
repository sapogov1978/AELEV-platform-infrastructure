"""Verify our own HTTPS readiness without a workload's data-plane credentials."""

from __future__ import annotations

import http.client
import os
import socket
import ssl

PREFIX = "AELEV_INFRASTRUCTURE_PUBLIC_EGRESS_GATEWAY_"


def probe(host: str, port: int, server_name: str, ca: str, timeout: float) -> str:
    context = ssl.create_default_context(cafile=ca)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with (
        socket.create_connection((host, port), timeout=timeout) as connection,
        context.wrap_socket(connection, server_hostname=server_name) as secured,
    ):
        secured.sendall(
            b"GET /readyz HTTP/1.1\r\nHost: health\r\nConnection: close\r\n\r\n"
        )
        response = http.client.HTTPResponse(secured)
        response.begin()
        return (
            "ONLINE"
            if response.status == 200 and response.read(257) == b'{"status":"ready"}'
            else "OFFLINE"
        )


if __name__ == "__main__":
    if (
        probe(
            "127.0.0.1",
            int(os.environ[PREFIX + "LISTEN_PORT"]),
            os.environ[PREFIX + "SERVER_NAME"],
            os.environ[PREFIX + "SERVER_CA_PATH"],
            5,
        )
        != "ONLINE"
    ):
        raise SystemExit(1)
