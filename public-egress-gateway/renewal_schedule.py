"""Install the operator renewal timer under the current macOS user, without sudo."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import plistlib
import subprocess
import sys
from pathlib import Path

from renewal import atomic


def install(state: Path, agents: Path) -> str:
    """Install only this approval's timer; launchd prevents overlapping instances."""
    if platform.system() != "Darwin":
        raise ValueError("use a platform-managed timer for renewal.py once on this OS")
    state = state.resolve(strict=True)
    if not (state / "approval.json").is_file():
        raise ValueError("enroll an operator approval first")
    suffix = hashlib.sha256(str(state).encode()).hexdigest()[:16]
    label = "com.aelev.egress-renewal." + suffix
    agents.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = agents / (label + ".plist")
    source = Path(__file__).resolve().parent
    modules = {
        name: (source / name).read_bytes()
        for name in ("renewal.py", "provision.py", "authorization.py")
    }
    release = hashlib.sha256(
        b"".join(name.encode() + data for name, data in modules.items())
    ).hexdigest()
    package = state / ("runtime-" + release)
    package.mkdir(mode=0o700, exist_ok=True)
    for name, data in modules.items():
        destination = package / name
        if destination.exists():
            if destination.read_bytes() != data:
                raise ValueError("installed operator artifact digest mismatch")
        else:
            atomic(destination, data, 0o600)
    script = package / "renewal.py"
    config = {
        "Label": label,
        "ProgramArguments": [
            sys.executable,
            str(script),
            "once",
            "--state-directory",
            str(state),
        ],
        "RunAtLoad": True,
        "StartInterval": 60,
        "ProcessType": "Background",
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        },
        # The atomic status is authoritative; avoid unbounded launchd log files.
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }
    existing = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True
    )
    if existing.returncode == 0:
        raise ValueError("timer already installed; inspect it before replacing")
    atomic(path, plistlib.dumps(config), 0o600)
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)],
        check=True,
        capture_output=True,
    )
    return label


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-directory", required=True, type=Path)
    arguments = parser.parse_args()
    install(arguments.state_directory, Path.home() / "Library" / "LaunchAgents")
