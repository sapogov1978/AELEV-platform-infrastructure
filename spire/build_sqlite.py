"""Build the pinned SQLite ABI used by Python's owner-local status journal."""

import argparse
import hashlib
import json
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path


def build(lock_path: Path, destination: Path) -> None:
    lock = json.loads(lock_path.read_bytes())
    if lock["owner"] != "infrastructure-spire":
        raise ValueError("SPIRE_SQLITE_LOCK_INVALID")
    expected = lock["python_sqlite"]
    with tempfile.TemporaryDirectory(prefix="spire-sqlite-") as directory:
        root = Path(directory)
        with urllib.request.urlopen(expected["url"], timeout=120) as response:
            raw = response.read(expected["bytes"] + 1)
        if (
            len(raw) != expected["bytes"]
            or hashlib.sha256(raw).hexdigest() != expected["sha256"]
        ):
            raise ValueError("SPIRE_SQLITE_ARCHIVE_INVALID")
        archive = root / "source.tar.gz"
        archive.write_bytes(raw)
        with tarfile.open(archive) as package:
            member = package.getmember(expected["member"])
            if not member.isfile() or not 0 < member.size < 20 * 1024**2:
                raise ValueError("SPIRE_SQLITE_SOURCE_INVALID")
            stream = package.extractfile(member)
            if stream is None:
                raise ValueError("SPIRE_SQLITE_SOURCE_INVALID")
            with stream:
                source = stream.read()
        if hashlib.sha3_256(source).hexdigest() != expected["source_sha3_256"]:
            raise ValueError("SPIRE_SQLITE_SOURCE_INVALID")
        path = root / "sqlite3.c"
        path.write_bytes(source)
        destination.mkdir(parents=True, exist_ok=False)
        subprocess.run(
            [
                "gcc",
                "-shared",
                "-fPIC",
                "-O1",
                "-DSQLITE_THREADSAFE=1",
                "-DSQLITE_ENABLE_COLUMN_METADATA",
                "-DSQLITE_ENABLE_FTS5",
                "-DSQLITE_ENABLE_RTREE",
                "-Wl,-soname,libsqlite3.so.0",
                str(path),
                "-o",
                str(destination / "libsqlite3.so.0"),
                "-lpthread",
                "-ldl",
                "-lm",
            ],
            check=True,
            timeout=600,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    build(arguments.lock, arguments.output)
