"""Orchestrate owner preparation without owning runtime or storage settings."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
ROLES = tuple(
    Path(path).parent.name
    for path in re.findall(r"- path: ([^\s]+)", (ROOT / "compose.yml").read_text())
)


def main() -> None:
    """Invoke only the preparation entry points included by this layer."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "tls", "render"))
    parser.add_argument("--issuer-dir", type=Path)
    args = parser.parse_args()
    env_file = ROOT / ".env"
    if args.action == "init" and not env_file.exists():
        env_file.write_text(
            (ROOT / ".env.example")
            .read_text()
            .replace("ENV_FILE=.env.example", "ENV_FILE=.env")
        )
        env_file.chmod(0o600)
    for role in ROLES:
        owner = ROOT / role
        if args.action == "init" and (owner / ".env").exists():
            continue
        command = [sys.executable, "prepare.py", args.action]
        if args.issuer_dir is not None:
            command += ["--issuer-dir", str(args.issuer_dir.resolve())]
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("AELEV_")
        }
        subprocess.run(command, cwd=owner, env=environment, check=True)


if __name__ == "__main__":
    main()
