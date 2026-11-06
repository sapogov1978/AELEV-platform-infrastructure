"""Read-only checks of this owner's actual operator UI and technical mTLS."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prepare  # noqa: E402
from aelev_observability_tools.operator_check import run  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(run(prepare.ROOT))
