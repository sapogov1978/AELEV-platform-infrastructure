"""Contract tests for the installation-wide certificate lifecycle gate."""

from __future__ import annotations

import hashlib
import json
import plistlib
from pathlib import Path
import time

import pytest

import certificate_lifecycle as lifecycle


def store(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def generic_fixture(root: Path) -> tuple[Path, Path, float]:
    repo = root / "repo"
    launch_agents = root / "agents"
    launch_agents.mkdir()
    state = repo / "services/owner/runtime/renewals/leaf"
    state.mkdir(parents=True)
    certificate = repo / "services/owner/secrets/client.crt"
    certificate.parent.mkdir(parents=True)
    certificate.write_bytes(b"public leaf")
    report = repo / "services/owner/observability/renewal.json"
    now = time.time()
    approval = {
        "consumer_owner": "fixture-owner",
        "report": str(report),
        "certificates": [
            {"id": "client", "certificate": str(certificate)}
        ],
    }
    store(state / "approval.json", approval)
    label = "com.aelev.telemetry-renewal.fixture"
    store(
        state / "state.json",
        {
            "owner": lifecycle.GENERIC_OWNER,
            "revoked": False,
            "timer": {"label": label},
        },
    )
    store(
        report,
        {
            "healthy": True,
            "renewal_healthy": True,
            "ready": True,
            "consumer_owner": "fixture-owner",
            "checked_at": now,
            "certificates": [
                {
                    "id": "client",
                    "healthy": True,
                    "certificate_sha256": hashlib.sha256(b"public leaf").hexdigest(),
                    "certificate_expires_at": now
                    + lifecycle.GENERIC_WINDOW_SECONDS
                    + 100,
                    "ca_expires_at": now + lifecycle.GENERIC_WINDOW_SECONDS + 200,
                }
            ],
        },
    )
    (launch_agents / f"{label}.plist").write_bytes(
        plistlib.dumps(
            {
                "Label": label,
                "ProgramArguments": [
                    "/python",
                    "/renewal.py",
                    "once",
                    "--state",
                    str(state.resolve()),
                ],
                "RunAtLoad": True,
                "StartInterval": 60,
            }
        )
    )
    return repo, launch_agents, now


def test_gate_accepts_exact_fresh_owner_lifecycle(tmp_path: Path) -> None:
    repo, launch_agents, now = generic_fixture(tmp_path)
    result = lifecycle.audit(
        repo, launch_agents, now=now, require_loaded=False
    )
    assert result["ready"] is True
    assert result["certificates"] == 1
    assert result["mechanisms"] == {"generic": 1, "postgresql": 0, "egress": 0}


@pytest.mark.parametrize("defect", ["stale", "changed", "timer"])
def test_gate_fails_closed_on_incomplete_lifecycle(
    tmp_path: Path, defect: str
) -> None:
    repo, launch_agents, now = generic_fixture(tmp_path)
    state = repo / "services/owner/runtime/renewals/leaf"
    approval = json.loads((state / "approval.json").read_text())
    if defect == "stale":
        report = json.loads(Path(approval["report"]).read_text())
        report["checked_at"] = now - lifecycle.REPORT_MAX_AGE_SECONDS - 1
        store(Path(approval["report"]), report)
    elif defect == "changed":
        Path(approval["certificates"][0]["certificate"]).write_bytes(b"changed")
    else:
        (launch_agents / "com.aelev.telemetry-renewal.fixture.plist").unlink()
    with pytest.raises(lifecycle.LifecycleError):
        lifecycle.audit(repo, launch_agents, now=now, require_loaded=False)
