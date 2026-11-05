"""Download only the selected official, size/hash-pinned SPIRE distribution."""

import argparse
import hashlib
import json
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path


def build(architecture: str, destination: Path, lock_file: Path) -> None:
    lock = json.loads(lock_file.read_bytes())
    if lock["owner"] != "infrastructure-spire" or architecture not in lock["linux"]:
        raise ValueError("SPIRE_ARTIFACT_ARCHITECTURE_UNSUPPORTED")
    expected = lock["linux"][architecture]
    name = f"spire-{lock['version']}"
    url = (
        "https://github.com/spiffe/spire/releases/download/v"
        + lock["version"]
        + "/"
        + name
        + "-linux-"
        + architecture
        + "-musl.tar.gz"
    )
    with tempfile.TemporaryDirectory(prefix="spire-artifact-") as temporary:
        archive = Path(temporary) / "distribution.tar.gz"
        size, sha = 0, hashlib.sha256()
        with (
            urllib.request.urlopen(url, timeout=120) as response,
            archive.open("xb") as output,
        ):
            while block := response.read(1024**2):
                size += len(block)
                if size > expected["bytes"]:
                    raise ValueError("SPIRE_ARTIFACT_SIZE_INVALID")
                sha.update(block)
                output.write(block)
        if size != expected["bytes"] or sha.hexdigest() != expected["sha256"]:
            raise ValueError("SPIRE_ARTIFACT_DIGEST_INVALID")
        destination.mkdir(parents=True, exist_ok=False)
        with tarfile.open(archive) as package:
            for binary in ("spire-server", "spire-agent"):
                member = package.getmember(name + "/bin/" + binary)
                if not member.isfile() or not 0 < member.size < 200 * 1024**2:
                    raise ValueError("SPIRE_ARTIFACT_MEMBER_INVALID")
                source = package.extractfile(member)
                if source is None:
                    raise ValueError("SPIRE_ARTIFACT_MEMBER_INVALID")
                with source, (destination / binary).open("xb") as output:
                    shutil.copyfileobj(source, output)
                (destination / binary).chmod(0o755)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--architecture", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    arguments = parser.parse_args()
    build(arguments.architecture, arguments.output, arguments.lock)
