"""Prepare only this deployment using its pinned public technical artifact."""

import hashlib
import logging
from pathlib import Path
import sys
import tomllib

ROOT = Path(__file__).resolve().parent
lock = tomllib.loads((ROOT / "poetry.lock").read_text())
artifact = next(
    package
    for package in lock["package"]
    if package["name"] == "aelev-observability-tools"
)["files"][0]
wheel = ROOT / "vendor" / artifact["file"]
if "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest() != artifact["hash"]:
    raise ValueError("public preparation artifact digest mismatch")
sys.path.insert(0, str(wheel))
from aelev_observability_tools import main  # noqa: E402

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main(ROOT)
