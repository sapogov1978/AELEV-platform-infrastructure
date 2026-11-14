#!/usr/bin/env python3
"""Check repository-controlled supply-chain references that can be proven offline."""

from __future__ import annotations

import hashlib
import re
import sys
import zipfile
from pathlib import Path

from source_scope import source_files

USE = re.compile(r"^\s*-\s+uses:\s+([^\s#]+)", re.MULTILINE)
SHA = re.compile(r"^[^@]+@[0-9a-f]{40}$")
PLATFORM_UI_CLIENT_WHEEL = "aelev_platform_ui_client-1.1.2-py3-none-any.whl"
PLATFORM_UI_CLIENT_SHA256 = (
    "d7fc03eee7df98d906a5390505a15e67c008fbeccd856f73ac0d5c67f62efb86"
)
OBSERVABILITY_TOOLS = {
    "observability/blackbox-exporter": "aelev_observability_tools-1.1.3-py3-none-any.whl",
    "observability/delivery": "aelev_observability_tools-1.0.1-py3-none-any.whl",
    "observability/grafana": "aelev_observability_tools-1.0.1-py3-none-any.whl",
    "observability/loki": "aelev_observability_tools-1.0.1-py3-none-any.whl",
    "observability/otel-collector": "aelev_observability_tools-1.0.1-py3-none-any.whl",
    "observability/prometheus": "aelev_observability_tools-1.1.3-py3-none-any.whl",
    "observability/tempo": "aelev_observability_tools-1.0.1-py3-none-any.whl",
}


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    errors: list[str] = []
    references = 0
    for path in sorted((root / ".github/workflows").glob("*.yml")):
        for value in USE.findall(path.read_text(encoding="utf-8")):
            references += 1
            if value.startswith("./"):
                continue
            if SHA.fullmatch(value) is None:
                errors.append(
                    f"GitHub Action is not pinned to a full SHA: {path.name}: {value}"
                )
    active_wheels = set()
    for path in source_files(root, "pyproject.toml"):
        configured_clients = re.findall(
            r"vendor/(aelev_platform_ui_client-[0-9.]+-py3-none-any\.whl)",
            path.read_text(encoding="utf-8"),
        )
        active_wheels.update(configured_clients)
        for name in configured_clients:
            artifact = path.parent / "vendor" / name
            if artifact.is_symlink() or not artifact.is_file():
                errors.append(f"missing regular Platform UI client wheel: {artifact}")
            elif (
                hashlib.sha256(artifact.read_bytes()).hexdigest()
                != PLATFORM_UI_CLIENT_SHA256
            ):
                errors.append(
                    f"unexpected Platform UI client artifact digest: {artifact}"
                )
    if active_wheels != {PLATFORM_UI_CLIENT_WHEEL}:
        errors.append(
            f"unexpected active Platform UI client wheels: {sorted(active_wheels)}"
        )
    for owner, expected in OBSERVABILITY_TOOLS.items():
        pyproject = root / owner / "pyproject.toml"
        configured = re.findall(
            r"vendor/(aelev_observability_tools-[0-9.]+-py3-none-any\.whl)",
            pyproject.read_text(encoding="utf-8"),
        )
        if configured != [expected]:
            errors.append(
                f"unexpected Observability tools wheel for {owner}: {configured}"
            )
            continue
        wheel = pyproject.parent / "vendor" / expected
        if not wheel.is_file():
            errors.append(f"missing active Observability tools wheel: {wheel}")
            continue
        with zipfile.ZipFile(wheel) as archive:
            source = archive.read("aelev_observability_tools/__init__.py").decode()
        required_extensions = (
            "subjectKeyIdentifier=hash",
            "authorityKeyIdentifier=keyid:always",
            "authorityKeyIdentifier=keyid,issuer",
        )
        missing = [value for value in required_extensions if value not in source]
        if missing:
            errors.append(f"{owner} wheel lacks PKI extensions: {missing}")
    if errors:
        print("Supply-chain validation failed:")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print(
        f"Supply-chain references passed: {references} pinned workflow uses; "
        "active Platform UI client is exactly 1.1.2 with verified SHA-256; "
        f"{len(OBSERVABILITY_TOOLS)} Observability wheels include SKI/AKI"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
