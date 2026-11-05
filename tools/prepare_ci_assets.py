#!/usr/bin/env python3
"""Materialize test assets from the owner's checksum-pinned Docker build source."""

from __future__ import annotations

import hashlib
import http.client
import re
import ssl
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

ASSET = re.compile(
    r"^ADD --checksum=sha256:([0-9a-f]{64}) "
    r"(https://raw\.githubusercontent\.com/swagger-api/swagger-ui/[^ ]+) "
    r"/assets/([A-Za-z0-9_.-]+)$"
)
LIMIT = 16 * 1024 * 1024


def prepare(directory: Path) -> None:
    dockerfile = directory / "Dockerfile"
    if not dockerfile.is_file():
        return
    stage = False
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        if line.startswith("FROM "):
            stage = line.endswith(" AS swagger-assets")
        match = ASSET.fullmatch(line) if stage else None
        if match is None:
            continue
        expected, url, name = match.groups()
        destination = directory / "swagger-assets" / name
        if destination.exists():
            raw = destination.read_bytes()
        else:
            parsed = urlsplit(url)
            with closing(
                http.client.HTTPSConnection(
                    "raw.githubusercontent.com",
                    timeout=30,
                    context=ssl.create_default_context(),
                )
            ) as connection:
                connection.request("GET", parsed.path)
                response = connection.getresponse()
                if response.status != 200:
                    raise ValueError(f"CI_ASSET_HTTP_FAILED: {name}: {response.status}")
                raw = response.read(LIMIT + 1)
        if len(raw) > LIMIT or hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError(f"CI_ASSET_DIGEST_MISMATCH: {name}")
        if not destination.exists():
            destination.parent.mkdir(exist_ok=True)
            destination.write_bytes(raw)
        print(f"Verified owner build asset: {name}")
