"""Private health probe for the translator-owned model process."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

import httpx


def read_translator_ref(path: Path) -> str:
    value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise RuntimeError("translator lock is invalid")
    translator_ref = cast(dict[str, object], value).get("translator_ref")
    if not isinstance(translator_ref, str) or not translator_ref.strip():
        raise RuntimeError("translator ref is invalid")
    return translator_ref


def main() -> None:
    socket_path = os.environ.get("AELEV_TRANSLATOR_SOCKET_PATH", "").strip()
    lock_path = Path(
        os.environ.get("AELEV_TRANSLATOR_LOCK_PATH", "/opt/aelev/translator.lock.json")
    )
    if not socket_path:
        raise RuntimeError("translator socket path is missing")
    translator_ref = read_translator_ref(lock_path)
    transport = httpx.HTTPTransport(uds=socket_path)
    with httpx.Client(
        transport=transport,
        base_url="http://translator",
        timeout=2.0,
        trust_env=False,
    ) as client:
        response = client.get("/runtimez")
    response.raise_for_status()
    payload = response.json()
    if payload != {"status": "ok", "translator_ref": translator_ref}:
        raise RuntimeError("translator health response is invalid")


if __name__ == "__main__":
    main()
