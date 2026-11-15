"""Public bootstrap boundaries and delegation; native PKI has separate gates."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def bootstrap() -> Any:
    spec = importlib.util.spec_from_file_location(
        "owner_bootstrap", ROOT / "bootstrap.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def initial(tmp_path: Path, bootstrap: Any) -> Path:
    root = tmp_path / "initial"
    root.mkdir(mode=0o700)
    for name in ("plan.json", "observation.json"):
        path = root / name
        path.write_text(
            json.dumps(
                {
                    "owner": "infrastructure-spire",
                    "schema_version": 1,
                    "environment": "test",
                    "trust_domain": "test.example",
                }
            )
        )
        path.chmod(0o600)
    for role, names in bootstrap.ROLE_INPUTS.items():
        directory = root / role
        directory.mkdir(mode=0o700)
        for name in names:
            path = directory / name
            # Layout-only fixture. The real native installer must validate PKI.
            path.write_bytes(b"layout-only-test-material")
            path.chmod(0o600)
    return root


def test_initial_layout_validation_does_not_invoke_docker(
    bootstrap: Any, initial: Path
) -> None:
    with mock.patch.object(bootstrap, "invoke") as invoke:
        bootstrap.validate_inputs(initial)
    invoke.assert_not_called()


def test_one_missing_required_leaf_is_refused(bootstrap: Any, initial: Path) -> None:
    (initial / "sender/central-write.key").unlink()
    with pytest.raises(bootstrap.BootstrapError, match="INITIAL_BUNDLE_INVALID"):
        bootstrap.validate_inputs(initial)


def test_symlinked_secret_is_refused(bootstrap: Any, initial: Path) -> None:
    path = initial / "sender/central-write.key"
    path.unlink()
    path.symlink_to(initial / "sender/local-write.key")
    with pytest.raises(bootstrap.BootstrapError, match="INITIAL_BUNDLE_INVALID"):
        bootstrap.validate_inputs(initial)


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX permissions are checked in Linux Engine on Windows"
)
def test_shared_secret_permissions_are_refused(bootstrap: Any, initial: Path) -> None:
    (initial / "sender/central-write.key").chmod(0o640)
    with pytest.raises(bootstrap.BootstrapError, match="INITIAL_BUNDLE_INVALID"):
        bootstrap.validate_inputs(initial)


def test_socket_approval_is_not_inferred(
    bootstrap: Any, initial: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(bootstrap.APPROVAL, raising=False)
    with (
        mock.patch.object(bootstrap, "invoke") as invoke,
        pytest.raises(
            bootstrap.BootstrapError, match="DOCKER_SOCKET_APPROVAL_REQUIRED"
        ),
    ):
        bootstrap.install(
            "fixture-spire", "runtime", "installer", initial, tmp_path / "output"
        )
    invoke.assert_not_called()


def test_owner_manual_env_is_preserved_before_any_install(
    bootstrap: Any, initial: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(bootstrap.APPROVAL, bootstrap.VALUE)
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    (output / ".env").write_text("OPERATOR_SETTING=preserve\n")
    with (
        mock.patch.object(bootstrap, "invoke") as invoke,
        pytest.raises(bootstrap.BootstrapError, match="OUTPUT_BINDING_INVALID"),
    ):
        bootstrap.install("fixture-spire", "runtime", "installer", initial, output)
    invoke.assert_not_called()
    assert (output / ".env").read_text() == "OPERATOR_SETTING=preserve\n"


def test_bootstrap_delegates_in_order_with_immutable_images_and_native_no(
    bootstrap: Any, initial: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(bootstrap.APPROVAL, bootstrap.VALUE)
    monkeypatch.setattr(bootstrap.platform, "system", lambda: "Linux")
    calls: list[list[str]] = []
    output = tmp_path / "output"

    def invoke(
        args: list[str],
        environment: dict[str, str],
        timeout: int,
        diagnostic: Path | None = None,
    ) -> str:
        assert environment[bootstrap.APPROVAL] == bootstrap.VALUE
        assert environment["AELEV_INFRASTRUCTURE_SPIRE_INITIAL_INPUT"] == str(initial)
        calls.append(args)
        if args[0] == "docker":
            return "sha256:" + ("1" if args[-1] == "runtime" else "2") * 64 + "\n"
        if args[1].endswith("export.sh"):
            generation = "a" * 64
            directory = output / "runtime/installed-composition" / generation
            directory.mkdir(parents=True)
            (directory / "compose.json").write_text("{}")
            (directory.parent / "receipt.json").write_text(
                json.dumps({"generation": generation})
            )
            (output / ".env").write_text(
                "# AELEV SPIRE installed composition v1\n"
                "AELEV_INFRASTRUCTURE_SPIRE_INSTALLED_COMPOSE="
                "./runtime/installed-composition/" + generation + "/compose.json\n"
            )
        return ""

    monkeypatch.setattr(bootstrap, "invoke", invoke)
    result = bootstrap.install("fixture-spire", "runtime", "installer", initial, output)
    installs = [
        args for args in calls if len(args) > 1 and args[1].endswith("install.sh")
    ]
    assert [args[2] for args in installs] == ["server", "observation"]
    assert all(
        args[4:6] == ["sha256:" + "1" * 64, "sha256:" + "2" * 64] for args in installs
    )
    assert all(args[-2:] == ["--native-restart-policy", "no"] for args in installs)
    assert calls[-1][1].endswith("export.sh")
    assert result["status"] == "SPIRE_RUNTIME_INSTALLED"
    assert Path(result["composition"]).is_file()
    assert not any(
        word in args
        for args in calls
        for word in ["--update", "rm", "down", "restart", "RESUME", "RESET"]
    )


def test_failed_native_phase_prevents_later_operations(
    bootstrap: Any, initial: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(bootstrap.APPROVAL, bootstrap.VALUE)
    monkeypatch.setattr(bootstrap.platform, "system", lambda: "Linux")
    calls: list[list[str]] = []

    def invoke(
        args: list[str],
        environment: dict[str, str],
        timeout: int,
        diagnostic: Path | None = None,
    ) -> str:
        calls.append(args)
        if args[0] == "docker":
            return "sha256:" + "1" * 64 + "\n"
        raise bootstrap.BootstrapError("SPIRE_BOOTSTRAP_NATIVE_OPERATION_FAILED")

    monkeypatch.setattr(bootstrap, "invoke", invoke)
    with pytest.raises(bootstrap.BootstrapError, match="NATIVE_OPERATION_FAILED"):
        bootstrap.install(
            "fixture-spire", "runtime", "installer", initial, tmp_path / "output"
        )
    assert len(calls) == 3 and calls[-1][2] == "server"


def test_unknown_result_is_not_retried(bootstrap: Any, tmp_path: Path) -> None:
    with (
        mock.patch.object(
            bootstrap.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["native"], 10),
        ) as run,
        pytest.raises(bootstrap.BootstrapError, match="RESULT_UNKNOWN"),
    ):
        bootstrap.invoke(["native"], {}, 10, tmp_path / "phase")
    assert run.call_count == 1
    assert (tmp_path / "phase.unknown").stat().st_mode & 0o777 == 0o600


def test_native_diagnostics_are_private_and_not_part_of_the_error(
    bootstrap: Any, tmp_path: Path
) -> None:
    result = subprocess.CompletedProcess(
        ["native"], 1, "private stdout", "private stderr"
    )
    with (
        mock.patch.object(bootstrap.subprocess, "run", return_value=result),
        pytest.raises(
            bootstrap.BootstrapError, match="^SPIRE_BOOTSTRAP_NATIVE_OPERATION_FAILED$"
        ),
    ):
        bootstrap.invoke(["native"], {}, 10, tmp_path / "phase")
    assert (tmp_path / "phase.stderr").read_text() == "private stderr"
    assert (tmp_path / "phase.stderr").stat().st_mode & 0o777 == 0o600
