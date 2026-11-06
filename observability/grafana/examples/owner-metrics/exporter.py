"""Bounded technical example, deliberately unrelated to any domain service."""

from __future__ import annotations

import ssl
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

STARTED = time.monotonic()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        if self.path != "/metrics":
            self.send_error(404)
            return
        ticks = int(time.monotonic() - STARTED)
        body = (
            "# HELP aelev_demo_ticks_total Seconds counted by the technical example.\n"
            "# TYPE aelev_demo_ticks_total counter\n"
            f'aelev_demo_ticks_total{{owner_id="owner-demo"}} {ticks}\n'
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 9443), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(
        "/run/secrets/telemetry/exporter.crt", "/run/secrets/telemetry/exporter.key"
    )
    context.load_verify_locations("/run/secrets/telemetry/local-write-ca.crt")
    context.verify_mode = ssl.CERT_REQUIRED
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()
