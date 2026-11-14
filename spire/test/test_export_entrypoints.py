"""Exercise public export activation without touching a real Docker daemon."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def shell_arguments(entrypoint: Path, *arguments: str) -> list[str]:
    # The test container's temporary filesystem is deliberately noexec.
    return [
        "sh",
        "-c",
        'docker() { "$AELEV_TEST_EXPORT_PYTHON" '
        '"$AELEV_TEST_EXPORT_DOCKER" "$@"; }; . "$0"',
        str(entrypoint),
        *arguments,
    ]


@pytest.fixture
def operator(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    package = tmp_path / "package"
    package.mkdir()
    entrypoint = package / "export.sh"
    shutil.copy2(ROOT / "export.sh", entrypoint)
    binary = tmp_path / "bin"
    binary.mkdir()
    docker = binary / "docker"
    docker.write_text(
        "#!"
        + sys.executable
        + "\n"
        + r"""import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ['AELEV_TEST_EXPORT_CALLS']).open('a') as output:
    output.write(json.dumps(args) + '\n')
if args[0] == 'context':
    print('unix:///var/run/docker.sock')
elif args[0] == 'info':
    print('linux' if args[-1] == '{{.OSType}}' else 'fixture-daemon')
elif args[0] == 'volume':
    expression = args[args.index('--format') + 1]
    print('infrastructure-spire' if 'io.aelev.owner' in expression else
          'installer' if 'io.aelev.role' in expression else 'fixture-spire')
elif args[0] == 'run':
    mount = next(
        value for value in args
        if value.startswith('type=bind,source=') and value.endswith(',target=/output')
    )
    output = Path(mount[len('type=bind,source='):-len(',target=/output')])
    generation = 'a' * 64
    (output / generation).mkdir(exist_ok=True)
    (output / generation / 'compose.json').write_text('{}\n')
    (output / 'receipt.json').write_text(json.dumps({'generation': generation}))
    (output / 'binding.env').write_text(
        '# AELEV SPIRE installed composition v1\n'
        'AELEV_INFRASTRUCTURE_SPIRE_INSTALLED_COMPOSE='
        './runtime/installed-composition/' + generation + '/compose.json\n'
    )
else:
    raise SystemExit(64)
"""
    )
    docker.chmod(0o700)
    log = tmp_path / "docker-calls.jsonl"
    environment = {
        **os.environ,
        "PATH": str(binary) + os.pathsep + os.environ["PATH"],
        "AELEV_TEST_EXPORT_CALLS": str(log),
        "AELEV_TEST_EXPORT_PYTHON": sys.executable,
        "AELEV_TEST_EXPORT_DOCKER": str(docker),
        "DRP_SPIRE_DOCKER_SOCKET_APPROVAL": "operator-root-equivalent-v1",
    }
    environment.pop("DOCKER_HOST", None)
    return entrypoint, environment, log


def test_external_output_preserves_the_owner_env(
    operator: tuple[Path, dict[str, str], Path], tmp_path: Path
) -> None:
    entrypoint, environment, log = operator
    owner_env = entrypoint.parent / ".env"
    owner_env.write_text("OPERATOR_SETTING=preserve\n")
    before = owner_env.stat()
    destination = tmp_path / "separate export"
    result = subprocess.run(
        shell_arguments(
            entrypoint, "fixture-spire", "approved-export-image", str(destination)
        ),
        env=environment,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert owner_env.read_text() == "OPERATOR_SETTING=preserve\n"
    assert (owner_env.stat().st_ino, owner_env.stat().st_mtime_ns) == (
        before.st_ino,
        before.st_mtime_ns,
    )
    assert (destination / ".env").read_bytes() == (
        destination / "runtime/installed-composition/binding.env"
    ).read_bytes()
    assert (destination / "runtime/installed-composition/receipt.json").is_file()
    calls = log.read_text()
    assert '"run"' in calls
    assert '"--network", "none"' in calls
    assert "target=/installation,readonly" in calls
    assert '"up"' not in calls and '"stop"' not in calls


def test_default_output_still_preserves_a_manual_owner_env(
    operator: tuple[Path, dict[str, str], Path],
) -> None:
    entrypoint, environment, log = operator
    owner_env = entrypoint.parent / ".env"
    owner_env.write_text("OPERATOR_SETTING=preserve\n")
    result = subprocess.run(
        shell_arguments(entrypoint, "fixture-spire", "approved-export-image"),
        env=environment,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 78
    assert owner_env.read_text() == "OPERATOR_SETTING=preserve\n"
    assert '"run"' not in log.read_text()


def test_two_argument_api_still_activates_the_default_export(
    operator: tuple[Path, dict[str, str], Path],
) -> None:
    entrypoint, environment, _ = operator
    result = subprocess.run(
        shell_arguments(entrypoint, "fixture-spire", "approved-export-image"),
        env=environment,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    package = entrypoint.parent
    assert (package / ".env").read_bytes() == (
        package / "runtime/installed-composition/binding.env"
    ).read_bytes()
    assert (package / "runtime/installed-composition/receipt.json").is_file()


@pytest.mark.parametrize(
    "component", ["server", "observer", "status-publisher", "telemetry-sender"]
)
def test_shell_forwards_only_explicit_component_scope(
    operator: tuple[Path, dict[str, str], Path], tmp_path: Path, component: str
) -> None:
    entrypoint, environment, log = operator
    result = subprocess.run(
        shell_arguments(
            entrypoint,
            "fixture-spire",
            "approved-export-image",
            str(tmp_path / component),
            component,
        ),
        env=environment,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    run = next(call for call in calls if call[0] == "run")
    assert run[-2:] == ["--component", component]
    assert any("target=/installation,readonly,volume-nocopy" in item for item in run)


def test_external_output_keeps_the_per_process_socket_approval_gate(
    operator: tuple[Path, dict[str, str], Path], tmp_path: Path
) -> None:
    entrypoint, environment, log = operator
    environment.pop("DRP_SPIRE_DOCKER_SOCKET_APPROVAL")
    destination = tmp_path / "unapproved-export"
    result = subprocess.run(
        shell_arguments(
            entrypoint, "fixture-spire", "approved-export-image", str(destination)
        ),
        env=environment,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 77
    assert not log.exists()
    assert not destination.exists()
