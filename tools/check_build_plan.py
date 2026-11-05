#!/usr/bin/env python3
"""Every selected custom runtime image must be built before --no-build startup."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "provider", root / "services/infrastructure/bootstrap.py"
    )
    assert spec is not None and spec.loader is not None
    provider = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(provider)
    configuration = json.loads(
        provider.compose(
            provider.ROOT, "config", "--format", "json", capture=True
        ).stdout
    )
    services = configuration["services"]
    for capability in provider.CAPABILITIES:
        built_images = {
            services[role].get("image", role)
            for role in provider.selected([capability], build=True)
        }
        for role in provider.selected([capability]):
            service = services[role]
            if service.get("build") and service.get("image", role) not in built_images:
                raise SystemExit(f"Unbuilt local runtime image: {capability}: {role}")
    print("All selected locally built runtime images are covered by the build plan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
