#!/usr/bin/env python3
"""Build a deterministic aelev-observability-tools PKI compatibility patch."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import re
import zipfile
from pathlib import Path


CA_NEEDLE = '''            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
            "-keyout",'''
CA_REPLACEMENT = '''            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
            "-addext",
            "subjectKeyIdentifier=hash",
            "-addext",
            "authorityKeyIdentifier=keyid:always",
            "-keyout",'''
LEAF_NEEDLE = '''        "basicConstraints=critical,CA:FALSE\\nkeyUsage=critical,digitalSignature,keyEncipherment\\n"
        + f"extendedKeyUsage={usages}\\n"'''
LEAF_REPLACEMENT = '''        "basicConstraints=critical,CA:FALSE\\nkeyUsage=critical,digitalSignature,keyEncipherment\\n"
        + "subjectKeyIdentifier=hash\\nauthorityKeyIdentifier=keyid,issuer\\n"
        + f"extendedKeyUsage={usages}\\n"'''


def digest(data: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return "sha256=" + encoded.decode("ascii")


def build(base: Path, version: str, output: Path) -> None:
    with zipfile.ZipFile(base) as archive:
        entries = {
            item.filename: archive.read(item)
            for item in archive.infolist()
            if not item.filename.endswith("/RECORD")
        }
    metadata_name = next(name for name in entries if name.endswith(".dist-info/METADATA"))
    match = re.search(r"^Version: (.+)$", entries[metadata_name].decode(), re.MULTILINE)
    if match is None:
        raise ValueError("base wheel has no version")
    old_version = match.group(1)
    old_dist = f"aelev_observability_tools-{old_version}.dist-info"
    new_dist = f"aelev_observability_tools-{version}.dist-info"
    renamed = {
        name.replace(old_dist, new_dist): data for name, data in entries.items()
    }
    metadata_name = metadata_name.replace(old_dist, new_dist)
    metadata = renamed[metadata_name].decode().replace(
        f"Version: {old_version}\n", f"Version: {version}\n", 1
    )
    renamed[metadata_name] = metadata.encode()
    source_name = "aelev_observability_tools/__init__.py"
    source = renamed[source_name].decode()
    if source.count(CA_NEEDLE) != 1 or source.count(LEAF_NEEDLE) != 1:
        raise ValueError("base wheel PKI source does not match the reviewed patch")
    source = source.replace(CA_NEEDLE, CA_REPLACEMENT).replace(
        LEAF_NEEDLE, LEAF_REPLACEMENT
    )
    renamed[source_name] = source.encode()

    record_name = f"{new_dist}/RECORD"
    rows = [
        (name, digest(data), str(len(data)))
        for name, data in sorted(renamed.items())
    ]
    rows.append((record_name, "", ""))
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerows(rows)
    renamed[record_name] = stream.getvalue().encode()

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(renamed.items()):
            item = zipfile.ZipInfo(name, date_time=(2016, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = 0o100644 << 16
            archive.writestr(item, data)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    build(arguments.base, arguments.version, arguments.output)
    print(f"Built {arguments.output} sha256={hashlib.sha256(arguments.output.read_bytes()).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
