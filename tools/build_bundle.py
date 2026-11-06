#!/usr/bin/env python3
"""Publish the deterministic, secret-free infrastructure source release format."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path


def build(root: Path, output: Path) -> dict:
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    raw = subprocess.check_output(
        [
            "git",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.eol=lf",
            "archive",
            revision,
        ],
        cwd=root,
    )
    records = {}
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for member in archive:
            if member.name.split("/", 1)[0] in {".codex", "graphify-out"}:
                continue
            if not member.isfile():
                continue
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("BUNDLE_CONTENT_MISSING")
            records[member.name] = (stream.read(), member.mode)
    output.parent.mkdir(parents=True, exist_ok=True)
    inventory = {}
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_STORED) as archive:
        for name, (data, mode) in sorted(records.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (
                stat.S_IFREG | (0o755 if mode & 0o111 else 0o644)
            ) << 16
            archive.writestr(info, data)
            try:
                data.decode("utf-8")
                text = b"\0" not in data
            except UnicodeDecodeError:
                text = False
            normalized = data.replace(b"\r\n", b"\n") if text else data
            inventory[name] = {
                "sha256": hashlib.sha256(normalized).hexdigest(),
                "text": text,
            }
    return {
        "schema_version": 2,
        "excluded_roots": [".codex", "graphify-out"],
        "repository": "sapogov1978/AELEV-platform-infrastructure",
        "version": json.loads(
            subprocess.check_output(
                ["git", "show", revision + ":release.json"],
                cwd=root,
            )
        )["version"],
        "source_commit": revision,
        "archive_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "archive_url": None,
        "files": inventory,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    lock = build(Path(__file__).resolve().parents[1], args.output)
    args.output.with_suffix(".lock.json").write_text(
        json.dumps(lock, indent=2, sort_keys=True) + "\n"
    )
    print(
        f"Built {len(lock['files'])} public infrastructure source files at {lock['source_commit']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
