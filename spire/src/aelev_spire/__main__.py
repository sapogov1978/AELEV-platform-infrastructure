"""Fixed entry points; no arbitrary shell, plugin commands or Docker access."""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

from .settings import load_plan
from .supervisor import log_event, run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "server",
            "agent",
            "authorize",
            "revoke",
            "inspect",
            "export-bootstrap",
        ),
    )
    parser.add_argument("--plan", type=Path, default=Path("/run/bootstrap/plan.json"))
    parser.add_argument("--state", type=Path, default=Path("/var/lib/spire"))
    parser.add_argument("--bootstrap", type=Path, default=Path("/run/bootstrap"))
    parser.add_argument("--output", type=Path, default=Path("/run/svid"))
    parser.add_argument("--export-path", type=Path)
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    try:
        if arguments.action in {"server", "agent"}:
            run(
                arguments.action,
                load_plan(arguments.plan),
                arguments.state,
                arguments.bootstrap,
                arguments.output,
            )
        else:
            from .operator import operate

            result = operate(
                arguments.action,
                arguments.plan,
                arguments.state,
                arguments.output,
                export_path=arguments.export_path,
            )
            logging.getLogger("spire").info(json.dumps(result, sort_keys=True))
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as error:
        code = str(error)
        log_event(
            code
            if re.fullmatch(r"SPIRE_[A-Z0-9_]{1,120}", code)
            else "SPIRE_OPERATION_FAILED"
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
