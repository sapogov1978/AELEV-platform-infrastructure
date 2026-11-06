"""Old replacement records are evidence, not additional three-attempt budgets."""

import json
from pathlib import Path
from typing import Any

import pytest

from aelev_spire.recovery_history import historical_attempts


def test_absent_history_does_not_invent_zero(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="HISTORY_INCOMPLETE"):
        historical_attempts(tmp_path, "sender")


def record(identity: str, count: int, predecessor: str | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "owner": "infrastructure-spire",
        "state": "COMPLETE",
        "restart": "no",
        "old_container": identity,
        "old_restart_count": count,
        "exhausted_restart_count": 3,
    }
    if predecessor is not None:
        value["predecessor"] = predecessor
    return value


def test_sum_unique_predecessors_not_exhausted_flags(tmp_path: Path) -> None:
    (tmp_path / "sender-recovery.json").write_text(
        json.dumps(record("a" * 64, 0, "b" * 64))
    )
    (tmp_path / ("sender-recovery-" + "b" * 64 + ".json")).write_text(
        json.dumps(record("b" * 64, 3))
    )
    count, digest = historical_attempts(tmp_path, "sender")
    assert count == 3 and len(digest) == 64
    assert historical_attempts(tmp_path, "sender") == (count, digest)


@pytest.mark.parametrize(
    "change", ["missing", "cycle", "incomplete", "negative", "pending", "foreign"]
)
def test_unconfirmed_history_fails_closed(tmp_path: Path, change: str) -> None:
    value = record("a" * 64, 3)
    if change == "missing":
        value["predecessor"] = "b" * 64
    elif change == "cycle":
        value["predecessor"] = "a" * 64
        (tmp_path / ("sender-recovery-" + "a" * 64 + ".json")).write_text(
            json.dumps(value)
        )
    elif change == "incomplete":
        value["old_restart_count"] = 0
    elif change == "negative":
        value["old_restart_count"] = -1
    elif change == "pending":
        value["state"] = "PREPARED"
    else:
        value["owner"] = "other"
    (tmp_path / "sender-recovery.json").write_text(json.dumps(value))
    with pytest.raises(ValueError, match="SPIRE_RECOVERY_HISTORY"):
        historical_attempts(tmp_path, "sender")
