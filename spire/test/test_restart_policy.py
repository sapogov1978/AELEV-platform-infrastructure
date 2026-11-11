"""A repeated install must not reopen a native retry budget after handoff."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from aelev_spire.files import atomic, read, reserve
from aelev_spire.installation import Installer, restart_no_roles

PROJECT = "fixture-identity"


def operator(tmp_path: Path) -> tuple[Installer, list[dict[str, Any]]]:
    installer = object.__new__(Installer)
    installer.project, installer.root = PROJECT, tmp_path
    installer.native_restart_no = False
    rows: list[dict[str, Any]] = []
    atomic(
        tmp_path / "owner.json",
        {"owner": "infrastructure-spire", "role": "installer", "project": PROJECT},
    )

    def docker(*args: str, payload: bytes | None = None) -> bytes:
        assert payload is None
        if args[0] == "ps":
            return b"\n".join(r["Id"].encode() for r in rows)
        assert args[0] == "inspect"
        return json.dumps(rows).encode()

    installer.docker = docker
    return installer, rows


def test_explicit_no_survives_a_missing_predecessor_and_fences_older_installer(
    tmp_path: Path,
) -> None:
    installer, _rows = operator(tmp_path)
    old_marker = read(tmp_path / "owner.json")
    atomic(tmp_path / "recovery-proof.json", {"attempts": 2, "blocked": True})
    history = (tmp_path / "recovery-proof.json").read_bytes()
    original: dict[str, Any] = {
        "services": {"spire-server": {"restart": "on-failure:3"}}
    }
    installer.native_restart_no = True
    first = copy.deepcopy(original)
    installer.preserve_restart_policies(first)
    assert first["services"]["spire-server"]["restart"] == "no"
    saved = (tmp_path / "owner.json").read_bytes()
    installer.native_restart_no = False
    repeated = copy.deepcopy(original)
    installer.preserve_restart_policies(repeated)
    assert repeated == first
    assert (tmp_path / "owner.json").read_bytes() == saved
    assert (tmp_path / "recovery-proof.json").read_bytes() == history
    with pytest.raises(ValueError, match="OWNER_MISMATCH"):
        reserve(tmp_path, old_marker)


def test_observed_no_is_retained_without_changing_unmanaged_roles(
    tmp_path: Path,
) -> None:
    installer, rows = operator(tmp_path)
    rows.append(
        {
            "Id": "a" * 64,
            "Config": {
                "Labels": {
                    "io.aelev.owner": "infrastructure-spire",
                    "io.aelev.installation": PROJECT,
                    "com.docker.compose.project": PROJECT,
                    "com.docker.compose.service": "spire-server",
                }
            },
            "HostConfig": {"RestartPolicy": {"Name": "no"}},
        }
    )
    model: dict[str, Any] = {
        "services": {
            "spire-server": {"restart": "on-failure:3"},
            "spire-status-publisher": {"restart": "on-failure:3"},
        }
    }
    installer.preserve_restart_policies(model)
    assert model["services"]["spire-server"]["restart"] == "no"
    assert model["services"]["spire-status-publisher"]["restart"] == "on-failure:3"
    assert read(tmp_path / "owner.json")["native_restart_no_roles"] == ["spire-server"]
    saved = (tmp_path / "owner.json").read_bytes()
    rows[0]["Config"]["Labels"]["io.aelev.installation"] = "foreign"
    with pytest.raises(ValueError, match="FOREIGN_CONTAINER_REFUSED"):
        installer.preserve_restart_policies(model)
    assert (tmp_path / "owner.json").read_bytes() == saved


@pytest.mark.parametrize(
    "value",
    [
        [],
        ["foreign-role"],
        ["spire-server", "spire-server"],
        ["spire-server", "spire-agent"],
        "spire-server",
        [True],
    ],
)
def test_restart_policy_state_cannot_add_arbitrary_roles(value: object) -> None:
    with pytest.raises(ValueError, match="NATIVE_RESTART_STATE_INVALID"):
        restart_no_roles(value)
