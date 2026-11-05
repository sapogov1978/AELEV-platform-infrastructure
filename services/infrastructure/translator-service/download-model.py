"""Build-only download of immutable official weights, verified before use."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlencode

CHUNK = 1024 * 1024


def fetch_part(
    url: str | tuple[str, ...], begin: int, end: int, total: int, path: Path
) -> None:
    expected = end - begin + 1
    if path.is_file() and path.stat().st_size == expected:
        return
    temporary = path.with_suffix(".partial")
    sources = (url,) if isinstance(url, str) else url
    for attempt in range(5):
        try:
            received = temporary.stat().st_size if temporary.is_file() else 0
            if received > expected:
                temporary.unlink()
                received = 0
            if received == expected:
                temporary.replace(path)
                return
            resume = begin + received
            request = urllib.request.Request(
                sources[attempt % len(sources)],
                headers={
                    "Range": f"bytes={resume}-{end}",
                    "Accept-Encoding": "identity",
                },
            )
            with urllib.request.urlopen(request, timeout=90) as response:
                if (
                    response.status != 206
                    or response.headers.get("Content-Range")
                    != f"bytes {resume}-{end}/{total}"
                ):
                    raise RuntimeError("MODEL_DOWNLOAD_RANGE_INVALID")
                with temporary.open("ab" if received else "wb") as output:
                    while block := response.read(128 * 1024):
                        received += len(block)
                        if received > expected:
                            raise RuntimeError("MODEL_DOWNLOAD_SIZE_INVALID")
                        output.write(block)
                if received != expected:
                    raise RuntimeError(
                        f"MODEL_DOWNLOAD_INCOMPLETE:{received}/{expected}"
                    )
            temporary.replace(path)
            return
        except (OSError, urllib.error.URLError, RuntimeError) as error:
            if attempt in {0, 4}:
                reason = (
                    str(error)
                    if isinstance(error, RuntimeError)
                    else type(error).__name__
                )
                print(f"Model range {begin}: retry {attempt + 1}, {reason}", flush=True)
            if attempt == 4:
                # Redirect URLs may contain transient signatures: never print them.
                raise RuntimeError("MODEL_DOWNLOAD_FAILED") from None
            time.sleep(min(2**attempt, 10))


def download(lock_path: Path, destination: Path, cache: Path) -> None:
    lock = json.loads(lock_path.read_text())
    if lock["status"] != "SELECTED_PRODUCTION" or len(lock["files"]) != 1:
        raise RuntimeError("MODEL_DOWNLOAD_LOCK_INVALID")
    artifact = lock["files"][0]
    filename = artifact["path"]
    checksum = artifact["sha256"]
    size = artifact["size"]
    revision = artifact["official_mirror_revision"]
    if (
        filename != "Index-Translate-9B.Q4_K_M.gguf"
        or not re.fullmatch(r"[0-9a-f]{64}", checksum)
        or not re.fullmatch(r"[0-9a-f]{40}", revision)
        or lock.get("model") != "IndexTeam/Index-Translate-9B-GGUF"
        or not re.fullmatch(r"[0-9a-f]{40}", str(lock.get("revision", "")))
        or type(size) is not int
        or size <= 0
    ):
        raise RuntimeError("MODEL_DOWNLOAD_ARTIFACT_INVALID")
    url = (
        "https://modelscope.cn/api/v1/models/IndexTeam/Index-Translate-9B-GGUF/repo?"
        + urlencode({"Revision": revision, "FilePath": filename})
    )
    primary = (
        "https://huggingface.co/IndexTeam/Index-Translate-9B-GGUF/resolve/"
        + lock["revision"]
        + "/"
        + filename
        + "?download=true"
    )
    parts = cache / checksum / f"chunks-{CHUNK}"
    parts.mkdir(parents=True, exist_ok=True)
    spans = [(start, min(start + CHUNK, size) - 1) for start in range(0, size, CHUNK)]
    print(f"Model download: 0/{len(spans)} parts", flush=True)
    pool = ThreadPoolExecutor(max_workers=32)
    try:
        tasks = [
            pool.submit(
                fetch_part,
                (primary, url) if index % 2 else (url, primary),
                begin,
                end,
                size,
                parts / str(index),
            )
            for index, (begin, end) in enumerate(spans)
        ]
        for completed, task in enumerate(as_completed(tasks), 1):
            task.result()
            if completed % 8 == 0 or completed == len(tasks):
                print(f"Model download: {completed}/{len(tasks)} parts", flush=True)
    finally:
        # A failed range must not silently continue all queued multi-GB work.
        pool.shutdown(wait=True, cancel_futures=True)
    destination.mkdir(parents=True, exist_ok=True)
    temporary = destination / (filename + ".partial")
    digest = hashlib.sha256()
    with temporary.open("wb") as output:
        for index in range(len(spans)):
            with (parts / str(index)).open("rb") as source:
                while block := source.read(1024 * 1024):
                    digest.update(block)
                    output.write(block)
    if temporary.stat().st_size != size or digest.hexdigest() != checksum:
        temporary.unlink()
        raise RuntimeError("MODEL_DOWNLOAD_INTEGRITY_FAILED")
    temporary.replace(destination / filename)
    print("Model weights: size and SHA-256 VERIFIED", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    args = parser.parse_args()
    download(args.lock, args.destination, args.cache)
