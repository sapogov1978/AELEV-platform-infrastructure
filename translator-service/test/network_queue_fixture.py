"""Controlled opaque HTTPS fixture; no AI, upstream fetch or semantic claim."""

import json
import os
import ssl
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

CALLS = 0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def allowed(self) -> bool:
        certificate = self.connection.getpeercert()
        names = [
            value
            for kind, value in certificate.get("subjectAltName", ())
            if kind == "URI"
        ]
        return names == [os.environ["QUEUE_FIXTURE_ALLOWED_CLIENT"]]

    def send(self, code: int, value: dict[str, Any]) -> None:
        raw = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if not self.allowed():
            self.send(403, {"error": "FOREIGN_CLIENT"})
        elif self.path == "/healthz":
            self.send(200, {"status": "ok"})
        elif self.path == "/fixture/calls":
            self.send(200, {"calls": CALLS})
        else:
            self.send(404, {})

    def do_POST(self) -> None:
        global CALLS
        if not self.allowed():
            self.send(403, {"error": "FOREIGN_CLIENT"})
            return
        assert self.path == "/v1/translate"
        length = int(self.headers["Content-Length"])
        assert 0 < length <= 65536
        value = json.loads(self.rfile.read(length))
        assert set(value) == {
            "schema_version",
            "text",
            "source_language",
            "target_language",
        }
        assert value["schema_version"] == "1.0.0"
        CALLS += 1
        self.send(
            200,
            {
                "schema_version": "1.0.0",
                "translated_text": "Controlled fixture response.",
                "warnings": ["TRANSPORT_FIXTURE_NOT_REAL_TRANSLATION"],
            },
        )


server = HTTPServer(("0.0.0.0", 18487), Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.minimum_version = ssl.TLSVersion.TLSv1_2
context.load_cert_chain(
    "/run/secrets/compute-provider/server.crt",
    "/run/secrets/compute-provider/server.key",
)
context.load_verify_locations("/run/secrets/compute-provider/client-ca.crt")
context.verify_mode = ssl.CERT_REQUIRED
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
