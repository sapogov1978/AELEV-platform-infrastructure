"""Permissions accepted by the create-only local Translator preparation."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "prepare-local.py"
SPEC = importlib.util.spec_from_file_location("translator_prepare_local", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_private_input_accepts_owner_or_exact_read_group_only(tmp_path: Path) -> None:
    secret = tmp_path / "secret.key"
    secret.write_text("fixture\n")
    secret.chmod(0o600)
    assert MODULE.private_mode_allowed(secret, os.getgid())

    secret.chmod(0o640)
    assert MODULE.private_mode_allowed(secret, os.getgid())
    assert not MODULE.private_mode_allowed(secret, os.getgid() + 1)

    for mode in (0o660, 0o604, 0o644):
        secret.chmod(mode)
        assert not MODULE.private_mode_allowed(secret, os.getgid())
