"""Private availability probe for an opaque translation provider."""

from __future__ import annotations

import os

import httpx


def main() -> None:
    socket_path = os.environ.get("AELEV_TRANSLATOR_SOCKET_PATH", "").strip()
    if not socket_path:
        raise RuntimeError("translator socket path is missing")
    transport = httpx.HTTPTransport(uds=socket_path)
    with httpx.Client(
        transport=transport,
        base_url="http://translation-provider",
        timeout=2.0,
        trust_env=False,
    ) as client:
        response = client.get("/healthz")
    response.raise_for_status()
    if response.json() != {"status": "ok"}:
        raise RuntimeError("translation provider health response is invalid")


if __name__ == "__main__":
    main()
