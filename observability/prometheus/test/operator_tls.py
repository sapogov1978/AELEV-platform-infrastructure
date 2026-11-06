"""Verify prod TLS using the owner's canonical role and clean up its run."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prepare  # noqa: E402
from aelev_observability_tools.operator_check import tls_modes  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(tls_modes(prepare.ROOT))
