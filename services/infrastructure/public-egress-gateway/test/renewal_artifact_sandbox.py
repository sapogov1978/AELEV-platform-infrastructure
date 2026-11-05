"""Verify and extract only the pinned public native operator, never its state."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import renewal

MODULES = {"renewal.py", "provision.py", "authorization.py"}


def run(endpoint: str, daemon: str, image: str, destination: Path) -> dict[str, Any]:
    def docker(*arguments: str) -> bytes:
        result = subprocess.run(
            ["docker", "--host", endpoint, *arguments],
            check=True,
            capture_output=True,
            timeout=120,
        )
        return result.stdout

    assert docker("info", "--format", "{{.ID}}").decode().strip() == daemon
    inspected = json.loads(docker("image", "inspect", image, "--format", "{{json .}}"))
    assert inspected["Id"] == image
    assert inspected["Config"]["Labels"]["io.aelev.egress.renewal-interface"] == "1"
    raw = docker(
        "run",
        "--rm",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--network",
        "none",
        "--memory",
        "128m",
        "--pids-limit",
        "32",
        "--entrypoint",
        "tar",
        image,
        "-C",
        "/opt/aelev-egress",
        "-cf",
        "-",
        *sorted(MODULES),
        "renewal.sha256",
    )
    assert len(raw) <= 262144
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        for member in archive:
            assert member.name in MODULES | {"renewal.sha256"}
            assert member.name not in files and member.isreg() and member.size <= 65536
            stream = archive.extractfile(member)
            assert stream is not None
            files[member.name] = stream.read(65537)
            assert len(files[member.name]) == member.size
    assert set(files) == MODULES | {"renewal.sha256"}
    records = files["renewal.sha256"].decode().splitlines()
    assert len(records) == 3
    manifest = dict(line.split("  ", 1)[::-1] for line in records)
    actual = {name: hashlib.sha256(files[name]).hexdigest() for name in MODULES}
    assert actual == manifest
    assert all(
        hashlib.sha256((Path(renewal.__file__).parent / name).read_bytes()).hexdigest()
        == digest
        for name, digest in actual.items()
    )
    assert destination.is_absolute()
    assert not any(path.is_symlink() for path in (destination, *destination.parents))
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    assert not {path.name for path in destination.iterdir()} - files.keys()
    for name, data in files.items():
        path = destination / name
        if path.exists():
            assert (
                path.is_file() and not path.is_symlink() and path.read_bytes() == data
            )
        else:
            renewal.atomic(path, data, 0o600)
    subprocess.run(
        [sys.executable, "-B", str(destination / "renewal.py"), "describe", "--help"],
        check=True,
        capture_output=True,
        timeout=10,
    )
    return {
        "image_id": image,
        "files_verified": len(files),
        "native_cli_verified": True,
        "working_state_read": False,
        "timer_changed": False,
        "module_sha256": actual,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--daemon-id", required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            run(args.endpoint, args.daemon_id, args.image_id, args.output),
            sort_keys=True,
        )
    )
