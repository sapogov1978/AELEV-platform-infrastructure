"""Deployment example: scoped dashboard publication, not application transport."""

from __future__ import annotations

import argparse
import getpass
import http.client
import json
import os
import re
import ssl
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", value):
        raise ValueError("UID must contain 1-40 ASCII letters, digits, _ or -")
    if value == "aelev-infrastructure":
        raise ValueError("the infrastructure dashboard UID is reserved")
    return value


def read_secret(path: Path) -> str:
    if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o027:
        raise ValueError("secret must be a regular private file (0600 or 0640)")
    value = path.read_text().strip()
    if not value or any(character.isspace() for character in value):
        raise ValueError("token must be a nonempty single value")
    return value


class ApiError(RuntimeError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"Grafana HTTP {status}; consult the manual, not secret logs")


class Client:
    def __init__(self, url: str, ca: Path, authorization: str) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Grafana URL must be an HTTPS origin without credentials")
        self.host = parsed.hostname
        self.port = parsed.port or 443
        self.authorization = authorization
        self.context = ssl.create_default_context(cafile=str(ca))
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2

    def call(self, method: str, path: str, data: dict[str, Any] | None = None) -> Any:
        if not path.startswith("/api/") or path.startswith("//"):
            raise ValueError("only same-origin Grafana API paths are allowed")
        connection = http.client.HTTPSConnection(
            self.host, self.port, context=self.context, timeout=15
        )
        body = None if data is None else json.dumps(data).encode()
        try:
            connection.request(
                method,
                path,
                body=body,
                headers={
                    "Authorization": self.authorization,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            response = connection.getresponse()
            payload = response.read(4 * 1024 * 1024 + 1)
            if not 200 <= response.status < 300:
                # No redirect following and no untrusted response/secret printing.
                raise ApiError(response.status)
            if len(payload) > 4 * 1024 * 1024:
                raise ValueError("response exceeds the example's bounded limit")
            return json.loads(payload) if payload else {}
        finally:
            connection.close()

    def publish(self, folder: str, dashboard: dict[str, Any]) -> dict[str, Any]:
        folder = identifier(folder)
        uid = identifier(dashboard["uid"])
        self.call("GET", f"/api/folders/{folder}")
        prepared = dict(dashboard)
        prepared["id"] = None
        prepared["version"] = 0
        try:
            current = self.call("GET", f"/api/dashboards/uid/{uid}")
        except ApiError as error:
            if error.status != 404:
                raise
        else:
            if current["meta"].get("folderUid") != folder:
                raise ValueError("UID belongs to another folder; refusing takeover")
            prepared["id"] = current["dashboard"]["id"]
            prepared["version"] = current["dashboard"]["version"]
        result: dict[str, Any] = self.call(
            "POST",
            "/api/dashboards/db",
            {
                "dashboard": prepared,
                "folderUid": folder,
                "overwrite": False,
                "message": "Owner deployment example",
            },
        )
        if result.get("status") != "success" or result.get("uid") != uid:
            raise ValueError("publication did not confirm the requested UID")
        return result

    def query(
        self, datasource: str, expression: str, start_ms: int, end_ms: int
    ) -> dict[str, Any]:
        if start_ms >= end_ms:
            raise ValueError("query start must precede end")
        result: dict[str, Any] = self.call(
            "POST",
            "/api/ds/query",
            {
                "from": str(start_ms),
                "to": str(end_ms),
                "queries": [
                    {
                        "refId": "A",
                        "datasource": {
                            "type": "prometheus",
                            "uid": identifier(datasource),
                        },
                        "expr": expression,
                        "range": True,
                        "instant": False,
                        "intervalMs": 2000,
                        "maxDataPoints": 300,
                    }
                ],
            },
        )
        response = result.get("results", {}).get("A", {})
        if response.get("error") or response.get("status", 200) >= 400:
            raise ValueError("datasource query failed; check its TLS and address")
        if not response.get("frames"):
            raise ValueError("no series; check the exporter, delivery and time range")
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    save = sub.add_parser("save-token")
    save.add_argument("--token-file", type=Path, required=True)
    for action in ("publish", "inspect", "query", "delete"):
        command = sub.add_parser(action)
        command.add_argument("--url", required=True)
        command.add_argument("--ca", type=Path, required=True)
        command.add_argument("--token-file", type=Path, required=True)
        if action == "publish":
            command.add_argument("--folder", required=True)
            command.add_argument("--dashboard", type=Path, required=True)
        elif action in {"inspect", "delete"}:
            command.add_argument("--uid", required=True)
        else:
            command.add_argument("--datasource", required=True)
            command.add_argument("--expression", required=True)
            command.add_argument("--start-ms", type=int, required=True)
            command.add_argument("--end-ms", type=int, required=True)
    args = parser.parse_args()
    if args.action == "save-token":
        token = getpass.getpass("Grafana service-account token (hidden): ")
        if not token or any(character.isspace() for character in token):
            raise ValueError("token must be a nonempty single value")
        descriptor = os.open(
            args.token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "w") as output:
            output.write(token + "\n")
        print("Token saved privately; existing files were not overwritten")
        return
    client = Client(args.url, args.ca, "Bearer " + read_secret(args.token_file))
    if args.action == "publish":
        result = client.publish(args.folder, json.loads(args.dashboard.read_text()))
        print(json.dumps({key: result[key] for key in ("uid", "id", "version", "url")}))
    elif args.action == "query":
        result = client.query(
            args.datasource, args.expression, args.start_ms, args.end_ms
        )
        frames = result["results"]["A"]["frames"]
        points = sum(len(frame["data"]["values"][0]) for frame in frames)
        if not points:
            raise ValueError("query returned no data points")
        print(json.dumps({"series": len(frames), "points": points}))
    elif args.action == "inspect":
        result = client.call("GET", "/api/dashboards/uid/" + identifier(args.uid))
        print(
            json.dumps(
                {
                    "uid": result["dashboard"]["uid"],
                    "id": result["dashboard"]["id"],
                    "folderUid": result["meta"].get("folderUid"),
                }
            )
        )
    else:
        client.call("DELETE", "/api/dashboards/uid/" + identifier(args.uid))
        print("Dashboard deleted; telemetry history was not deleted")


if __name__ == "__main__":
    main()
