"""Frozen database comparisons never waive the exact runtime schema gate."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_state", ROOT / "deployment/check_state.py"
)
assert SPEC is not None and SPEC.loader is not None
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


def pair() -> tuple[dict[str, Any], dict[str, Any]]:
    before: dict[str, Any] = {
        "head": "20261013_0004",
        "tables": {
            "alembic_version": {"rows": 1, "sha256": "old"},
            "translation_job": {"rows": 10, "sha256": "exact"},
        },
    }
    after = copy.deepcopy(before)
    after["head"] = "20261014_0005"
    after["tables"]["alembic_version"]["sha256"] = "new"
    after["tables"]["translation_segment"] = {"rows": 0, "sha256": "empty"}
    return before, after


def test_exact_additive_migration() -> None:
    CHECK.verify_extension(*pair())


@pytest.mark.parametrize("mutation", ["head", "lost", "changed", "added", "segments"])
def test_migration_rejects_unexpected_changes(mutation: str) -> None:
    before, after = pair()
    if mutation == "head":
        after["head"] = "old"
    elif mutation == "lost":
        del after["tables"]["translation_job"]
    elif mutation == "changed":
        after["tables"]["translation_job"]["sha256"] = "modified"
    elif mutation == "added":
        after["tables"]["unknown"] = {"rows": 0}
    else:
        after["tables"]["translation_segment"]["rows"] = 1
    with pytest.raises(ValueError):
        CHECK.verify_extension(before, after)


def test_default_runtime_target_is_provider_neutral() -> None:
    recipe = (ROOT / "Dockerfile").read_text()
    for forbidden in (
        "translator.lock.json",
        "download-model.py",
        "index_translation_provider",
        "AELEV_TRANSLATOR_MODEL_ROOT",
        "AELEV_TRANSLATOR_RUNTIME_PATH",
        "PINNED_RUNTIME_SEED_REQUIRED",
    ):
        assert forbidden not in recipe
    assert recipe.rsplit("FROM ", 1)[1].startswith(
        "translator-core-runtime AS translator-runtime"
    )
