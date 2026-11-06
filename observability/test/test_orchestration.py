"""Only composition belongs to this layer; native regressions belong to owners."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ROLES = re.findall(r"  - path: ([^/]+)/compose.yml", (ROOT / "compose.yml").read_text())


def model(root: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            ".env.example",
            "--profile",
            "ui",
            "--profile",
            "provision",
            "config",
            "--format",
            "json",
        ],
        cwd=root,
        env={k: v for k, v in os.environ.items() if not k.startswith("AELEV_")},
        capture_output=True,
        text=True,
        check=True,
    )
    decoded: dict[str, Any] = json.loads(result.stdout)
    return decoded


def test_layer_only_includes_owner_models() -> None:
    source = (ROOT / "compose.yml").read_text()
    assert len(ROLES) == 7
    for declaration in ("services:", "networks:", "volumes:", "build:", ":-"):
        assert declaration not in source
    keys = {
        line.split("=", 1)[0]
        for line in (ROOT / ".env.example").read_text().splitlines()
        if line.startswith("AELEV_")
    }
    assert keys == {
        "AELEV_INFRASTRUCTURE_OBSERVABILITY_COMPOSE_PROJECT",
        "AELEV_INFRASTRUCTURE_OBSERVABILITY_ENV_FILE",
    }


def test_inclusion_does_not_patch_standalone_products() -> None:
    together = model(ROOT)
    for role in ROLES:
        standalone = model(ROOT / role)
        for category in ("services", "networks", "volumes"):
            for name, value in standalone.get(category, {}).items():
                assert together[category][name] == value, (role, category, name)


def test_operator_helper_has_no_native_templates_or_shared_image() -> None:
    source = (ROOT / "prepare.py").read_text()
    assert "subprocess.run" in source
    assert "deployment.json" not in source
    assert "FROM" not in (ROOT / "compose.yml").read_text()
    assert not list(ROOT.glob("*.json"))
