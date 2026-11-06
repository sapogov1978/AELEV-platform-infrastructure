"""Guard installation ownership and immutable initial configuration offline."""

import base64
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from aelev_spire import health, installation
from aelev_spire.configuration import seed
from aelev_spire.installation import Installer
from aelev_spire.settings import domain, identity


def test_direct_probe_never_imports_adjacent_operator_module() -> None:
    result = subprocess.run(
        [sys.executable, "-S", "-P", health.__file__],
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 1  # No heartbeat on this test host.
    assert result.stdout == result.stderr == b""


def test_seed_is_repeatable_but_never_overwrites(tmp_path: Path) -> None:
    root = tmp_path / "configuration"
    request: dict[str, Any] = {
        "marker": {"owner": "infrastructure-spire", "role": "agent-bootstrap"},
        "files": {"plan.json": base64.b64encode(b"approved").decode()},
    }
    seed(root, request)
    seed(root, request)
    request["files"]["plan.json"] = base64.b64encode(b"different").decode()
    with pytest.raises(ValueError, match="EXISTING_CONFIGURATION_PRESERVED"):
        seed(root, request)
    assert (root / "plan.json").read_bytes() == b"approved"
    request["files"] = {"../escape": "eA=="}
    with pytest.raises(ValueError, match="FILES_INVALID"):
        seed(root, request)


def test_volume_refuses_foreign_labels_or_persistent_credentials() -> None:
    installer = object.__new__(Installer)
    installer.project = "fixture-project"
    record: dict[str, Any] = {
        "Labels": {
            "io.aelev.owner": "infrastructure-spire",
            "io.aelev.installation": "fixture-project",
            "io.aelev.role": "svid",
        },
        "Options": {},
    }

    def docker(*arguments: str, payload: bytes | None = None) -> bytes:
        assert payload is None
        if arguments[:2] == ("volume", "ls"):
            return b"fixture-project_svid\n"
        assert arguments == ("volume", "inspect", "fixture-project_svid")
        return json.dumps([record]).encode()

    installer.docker = docker
    with pytest.raises(ValueError, match="EPHEMERAL_VOLUME_REQUIRED"):
        installer.volume("svid", ephemeral=True)
    record["Options"] = {
        "type": "tmpfs",
        "device": "tmpfs",
        "o": "size=2m,uid=10001,gid=10001,mode=0700",
    }
    assert installer.volume("svid", ephemeral=True) == "fixture-project_svid"
    record["Labels"]["io.aelev.installation"] = "another-owner"
    with pytest.raises(ValueError, match="FOREIGN_VOLUME_REFUSED"):
        installer.volume("svid", ephemeral=True)


@pytest.mark.parametrize("value", ["a..b", "-a.b", "a-.b", "a/b", "a" * 64])
def test_trust_domain_labels_are_strict(value: str) -> None:
    with pytest.raises(ValueError, match="TRUST_DOMAIN_INVALID"):
        domain(value)


def test_identity_accepts_pinned_installation_dots_but_not_dot_segments() -> None:
    assert identity("spiffe://test.example/fixture/installation.1", "test.example")
    for path in ("/fixture/..", "/fixture/./instance", "/fixture/%2e%2e"):
        with pytest.raises(ValueError, match="IDENTITY_INVALID"):
            identity("spiffe://test.example" + path, "test.example")


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("public_port", 0, "PORT_INVALID"),
        ("public_port", True, "PORT_INVALID"),
        ("bind_address", "anywhere", "BIND_ADDRESS_INVALID"),
        ("agent_ttl_seconds", 1, "AGENT_TTL_INVALID"),
    ],
)
def test_invalid_server_plan_is_rejected_before_persistence(
    tmp_path: Path, field: str, value: object, code: str
) -> None:
    installer = object.__new__(Installer)
    installer.root = tmp_path
    plan: dict[str, Any] = {
        "owner": "infrastructure-spire",
        "schema_version": 1,
        "environment": "test",
        "trust_domain": "test.example",
        field: value,
    }
    with pytest.raises(ValueError, match=code):
        installer.apply("server", plan, {}, None)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("role", "name"),
    [
        ("server", "aelev_infrastructure_spire_server"),
        ("agent", "spire-agent"),
        ("agent", "aelev_agent-with-hyphen"),
        ("agent", "aelev_"),
    ],
)
def test_explicit_runtime_container_name_is_agent_only_and_canonical(
    tmp_path: Path, role: str, name: str
) -> None:
    installer = object.__new__(Installer)
    installer.root = tmp_path
    plan = {
        "owner": "infrastructure-spire",
        "schema_version": 1,
        "environment": "test",
        "trust_domain": "test.example",
    }
    with pytest.raises(ValueError, match="CONTAINER_NAME_INVALID"):
        installer.apply(role, plan, {}, None, container_name=name)
    assert not list(tmp_path.iterdir())


def test_agent_adds_canonical_name_to_an_unchanged_existing_plan(
    tmp_path: Path,
) -> None:
    installer = object.__new__(Installer)
    installer.project = "fixture-agent"
    installer.root = tmp_path
    installer.image = "fixed"
    installer.image_id = "fixed-id"
    installer.host_os = "Linux"
    installer.update = False
    plan = {
        "owner": "infrastructure-spire",
        "schema_version": 1,
        "environment": "test",
        "trust_domain": "test.example",
    }
    installation.atomic(
        tmp_path / "deployment.json",
        {
            "role": "agent",
            "plan": plan,
            "image": "fixed",
            "image_id": "fixed-id",
            "authority_source": None,
            "access_network": None,
        },
    )

    def volume(name: str, *, ephemeral: bool = False) -> str:
        return "fixture-agent_" + name

    def resolve(file: str, values: dict[str, str]) -> dict[str, Any]:
        del file, values
        return {
            "name": "fixture-agent",
            "services": {"spire-agent": {}},
            "volumes": {
                "state": {"name": "fixture-agent_agent"},
                "configuration": {"name": "fixture-agent_configuration"},
                "svid": {"name": "fixture-agent_svid"},
            },
        }

    def docker(*arguments: str, payload: bytes | None = None) -> bytes:
        assert payload is None
        if arguments[0] == "ps":
            return b""
        if arguments[0] == "compose":
            return b"c" * 64 if "ps" in arguments else b""
        if arguments[0] == "inspect":
            return b"healthy" if "Health.Status" in arguments[2] else b"running"
        assert arguments[0] == "exec"
        return json.dumps(
            {
                "owner": "infrastructure-spire",
                "ready": True,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        ).encode()

    installer.volume = volume
    installer.resolve = resolve
    installer.docker = docker
    installer.bind_compose_project("fixture-agent")
    installer.apply(
        "agent",
        plan,
        {},
        None,
        container_name="aelev_infrastructure_spire_fixture_agent",
    )
    assert installation.read(tmp_path / "deployment.json")["container_name"] == (
        "aelev_infrastructure_spire_fixture_agent"
    )
    assert (
        installation.read(tmp_path / "compose.json")["services"]["spire-agent"][
            "container_name"
        ]
        == "aelev_infrastructure_spire_fixture_agent"
    )
    assert not (tmp_path / "previous-deployment.json").exists()


@pytest.mark.parametrize("foreign", [False, True])
def test_timed_out_helper_is_named_and_only_exact_owner_can_be_removed(
    monkeypatch: pytest.MonkeyPatch, foreign: bool
) -> None:
    installer = object.__new__(Installer)
    installer.project = "fixture-project"
    selected = [""]
    removed: list[str] = []

    def execute(
        arguments: list[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        if arguments[1] == "run":
            selected[0] = arguments[arguments.index("--name") + 1]
            assert "io.aelev.role=operator-helper" in arguments
            raise subprocess.TimeoutExpired(arguments, 180)
        if arguments[1] == "inspect":
            return subprocess.CompletedProcess(
                arguments,
                0,
                json.dumps(
                    [
                        {
                            "Name": "/" + selected[0],
                            "Config": {
                                "Labels": {
                                    "io.aelev.owner": (
                                        "foreign" if foreign else "infrastructure-spire"
                                    ),
                                    "io.aelev.installation": "fixture-project",
                                    "io.aelev.role": "operator-helper",
                                }
                            },
                        }
                    ]
                ).encode(),
                b"",
            )
        assert arguments[1:3] == ["rm", "--force"]
        removed.append(arguments[3])
        return subprocess.CompletedProcess(arguments, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", execute)
    with pytest.raises(
        ValueError, match="OWNER_MISMATCH" if foreign else "HELPER_TIMEOUT"
    ):
        installer.docker("run", "--rm", "selected-runtime")
    assert removed == ([] if foreign else selected)


def test_windows_authority_uses_its_verified_engine_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = object.__new__(Installer)
    installer.project, installer.host_os = "fixture-project", "Windows"
    host = tmp_path / "hostname"
    host.write_text("0123456789ab")
    actual_path = Path

    def path(value: str) -> Path:
        return host if value == "/etc/hostname" else actual_path(value)

    monkeypatch.setattr(installation, "Path", path)
    record: dict[str, Any] = {
        "Config": {
            "Labels": {
                "io.aelev.owner": "infrastructure-spire",
                "io.aelev.installation": "fixture-project",
                "io.aelev.role": "operator-installer",
            }
        },
        "Mounts": [
            {
                "Destination": "/run/authority-source",
                "Type": "bind",
                "RW": False,
                "Source": "/run/desktop/mnt/host/c/identity/issuer",
            }
        ],
    }

    def docker(*arguments: str, payload: bytes | None = None) -> bytes:
        assert arguments == ("inspect", "0123456789ab") and payload is None
        return json.dumps([record]).encode()

    installer.docker = docker
    assert (
        installer.authority_binding(r"C:\identity\issuer")
        == "/run/desktop/mnt/host/c/identity/issuer"
    )
    record["Config"]["Labels"]["io.aelev.installation"] = "foreign"
    with pytest.raises(ValueError, match="SELF_IDENTITY"):
        installer.authority_binding(r"C:\identity\issuer")
