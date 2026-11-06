#!/usr/bin/env python3
"""Build an owner UI image with public CI-only origins and pinned base inputs."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from ci_inventory import load, validate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("suite")
    parser.add_argument("--tag", required=True)
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    inventory = load(root)
    if validate(root, inventory) or arguments.suite not in inventory["ui"]:
        raise ValueError("CI_UI_IMAGE_SUITE_NOT_DECLARED")
    origins = {
        "VITE_KEYCLOAK_URL": "https://identity.drp.invalid/realms/drp",
        "VITE_KEYCLOAK_REALM": "drp",
        "VITE_KEYCLOAK_CLIENT_ID": "drp-ci",
        "VITE_KEYCLOAK_ORIGIN": "https://identity.drp.invalid",
        "VITE_PLATFORM_UI_ORIGIN": "https://shell.drp.invalid",
        "VITE_SWAGGER_URL": "https://owner.drp.invalid/docs",
        "AELEV_DSS_PLATFORM_UI_ORIGIN": "https://shell.drp.invalid",
        "AELEV_DSC_PLATFORM_UI_ORIGIN": "https://shell.drp.invalid",
        "AELEV_ASC_KEYCLOAK_URL": "https://identity.drp.invalid/realms/drp",
        "AELEV_ASC_KEYCLOAK_REALM": "drp",
        "AELEV_ASC_KEYCLOAK_CLIENT_ID": "drp-ci",
        "AELEV_ASC_KEYCLOAK_ORIGIN": "https://identity.drp.invalid",
        "AELEV_ASC_PLATFORM_UI_ORIGIN": "https://shell.drp.invalid",
    }
    command = ["docker", "build", "--tag", arguments.tag]
    if arguments.suite == "platform-ui":
        command.extend(["--target", "ui"])
        for line in (root / arguments.suite / ".env.example").read_text().splitlines():
            for name in ("NODE_IMAGE", "NGINX_IMAGE", "PYTHON_IMAGE"):
                prefix = "AELEV_INFRASTRUCTURE_PLATFORM_UI_" + name + "="
                if line.startswith(prefix):
                    origins[name] = line[len(prefix) :]
    for key, value in origins.items():
        command.extend(["--build-arg", f"{key}={value}"])
    command.append(str(root / arguments.suite))
    subprocess.run(command, check=True, timeout=900)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
