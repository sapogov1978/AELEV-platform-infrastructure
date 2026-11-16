"""Read the existing Sender status using this publisher's own mTLS identity."""

from __future__ import annotations

import http.client
import json
import os
import re
import ssl
from typing import Any
from urllib.parse import urlsplit

COUNTERS = {"records", "pending", "bytes", "failed", "capacity"}
FLAGS = {"worker_alive", "admission_pressure", "certificate_renewal_ready"}
KINDS = {"logs", "traces", "metrics", "otlp_metrics"}
REASONS = {
    "transport_failure",
    "transport_timeout",
    "tls_failure",
    "incomplete_ack",
    "delivery_failed",
}


def sanitize(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("ROOT_SPIRE_SENDER_STATUS_INVALID")
    result: dict[str, Any] = {}
    for name in COUNTERS | FLAGS:
        item = value.get(name)
        if type(item) is int and 0 <= item <= (1 if name in FLAGS else 2**63 - 1):
            result[name] = item
    errors = value.get("blocking_errors", {})
    if isinstance(errors, dict):
        result["blocking_errors"] = {
            name: (
                reason
                if reason in REASONS
                or re.fullmatch(r"http_[1-5][0-9]{2}(_or_incomplete_ack)?", reason)
                else "delivery_failed"
            )
            for name, reason in errors.items()
            if name in KINDS and isinstance(reason, str)
        }
    return result


def probe() -> dict[str, Any]:
    endpoint = urlsplit(os.environ["AELEV_INFRASTRUCTURE_SPIRE_SENDER_HEALTH_URL"])
    if (
        endpoint.scheme != "https"
        or not endpoint.hostname
        or endpoint.username
        or endpoint.password
        or endpoint.path not in {"", "/"}
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("ROOT_SPIRE_SENDER_ORIGIN_INVALID")
    context = ssl.create_default_context(cafile="/run/secrets/status/query-ca.crt")
    context.load_cert_chain(
        "/run/secrets/status/query.crt", "/run/secrets/status/query.key"
    )
    client = http.client.HTTPSConnection(
        endpoint.hostname, endpoint.port, timeout=3, context=context
    )
    try:
        client.request("GET", "/status")
        reply = client.getresponse()
        raw = reply.read(65537)
        if reply.status != 200 or len(raw) > 65536:
            raise ValueError("ROOT_SPIRE_SENDER_STATUS_INVALID")
        return sanitize(json.loads(raw))
    finally:
        client.close()


def main() -> None:
    try:
        result = {"code": "ROOT_SPIRE_SENDER_STATUS", "status": probe()}
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        result = {"code": "ROOT_SPIRE_SENDER_STATUS_UNAVAILABLE"}
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
