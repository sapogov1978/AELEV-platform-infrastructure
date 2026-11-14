"""Fencing-only CLI, without importing SQL drivers or Patroni."""

from __future__ import annotations

import argparse
import http.client
import logging
import sys

from aelev_postgresql import fencing


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["serve", "health"])
    args = parser.parse_args()
    logging.basicConfig(
        format='{"level":"%(levelname)s","message":"%(message)s"}', level=logging.INFO
    )
    try:
        {"serve": fencing.serve, "health": fencing.health}[args.operation]()
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        http.client.HTTPException,
    ) as error:
        logging.error("Fencing operation failed: %s", type(error).__name__)
        sys.exit(1)


if __name__ == "__main__":
    main()
