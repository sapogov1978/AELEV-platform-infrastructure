"""Portable compute station rejects mutable images and remote Docker contexts."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def station() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "translator_compute_station", ROOT / "compute-runtime" / "station.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configuration(provider: str, worker: str) -> dict[str, object]:
    return {
        "services": {
            "translation-provider": {
                "image": provider,
                "volumes": [
                    {
                        "type": "volume",
                        "source": "translation-provider-socket",
                        "target": "/run/aelev-translator",
                    },
                    {
                        "type": "bind",
                        "source": "/station/provider-credential.env",
                        "target": "/run/secrets/provider/credential.env",
                        "read_only": True,
                        "bind": {"create_host_path": False},
                    },
                ],
            },
            "translator-worker": {"image": worker},
        }
    }


def test_compute_bundle_is_not_declared_by_control_compose() -> None:
    control = (ROOT / "compose.remote.yml").read_text()
    compute = (ROOT / "compose.compute.yml").read_text()
    assert "translation-provider:" not in control
    assert "translator-worker:" not in control
    assert "profiles: [compute]" not in control
    assert "extends:" not in compute
    assert "translation-provider:" in compute
    assert "translator-worker:" in compute


def test_station_requires_separate_exact_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = station()
    provider = "registry.example/provider@sha256:" + "a" * 64
    worker = "registry.example/worker@sha256:" + "b" * 64

    def pinned_configuration(_arguments: list[str], *, capture: bool = False) -> str:
        return json.dumps(configuration(provider, worker))

    monkeypatch.setattr(module, "_execute", pinned_configuration)
    assert set(module._configuration(["docker"])["services"]) == {
        "translation-provider",
        "translator-worker",
    }

    for forbidden in ("command", "entrypoint", "environment", "healthcheck"):
        opaque = configuration(provider, worker)
        opaque["services"]["translation-provider"][forbidden] = (
            ["forbidden"]
            if forbidden in {"command", "entrypoint"}
            else {"forbidden": True}
        )

        def coupled_configuration(
            _arguments: list[str], *, capture: bool = False, value: object = opaque
        ) -> str:
            return json.dumps(value)

        monkeypatch.setattr(module, "_execute", coupled_configuration)
        with pytest.raises(RuntimeError, match="CONFIGURATION_INVALID"):
            module._configuration(["docker"])
    for forbidden in ("network_mode", "ports", "expose"):
        exposed = configuration(provider, worker)
        exposed["services"]["translation-provider"][forbidden] = (
            "none" if forbidden == "network_mode" else ["443"]
        )

        def exposed_configuration(
            _arguments: list[str], *, capture: bool = False, value: object = exposed
        ) -> str:
            return json.dumps(value)

        monkeypatch.setattr(module, "_execute", exposed_configuration)
        with pytest.raises(RuntimeError, match="CONFIGURATION_INVALID"):
            module._configuration(["docker"])
    mutable = "registry.example/translator:latest"

    def mutable_configuration(_arguments: list[str], *, capture: bool = False) -> str:
        return json.dumps(configuration(mutable, worker))

    monkeypatch.setattr(module, "_execute", mutable_configuration)
    with pytest.raises(RuntimeError, match="PINNED_IMAGE_REQUIRED"):
        module._configuration(["docker"])

    def monolith_configuration(_arguments: list[str], *, capture: bool = False) -> str:
        return json.dumps(configuration(provider, provider))

    monkeypatch.setattr(module, "_execute", monolith_configuration)
    with pytest.raises(RuntimeError, match="CONFIGURATION_INVALID"):
        module._configuration(["docker"])


def test_station_rejects_remote_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    module = station()
    answers = iter(
        (
            "remote",
            json.dumps([{"Endpoints": {"docker": {"Host": "ssh://compute.example"}}}]),
        )
    )

    def execute(_arguments: list[str], *, capture: bool = False) -> str:
        return next(answers)

    monkeypatch.setattr(module, "_execute", execute)
    with pytest.raises(RuntimeError, match="REMOTE_DOCKER_FORBIDDEN"):
        module._local_engine("/usr/bin/docker")


def test_compose_is_pinned_to_the_verified_local_endpoint(tmp_path: Path) -> None:
    module = station()
    env_file = tmp_path / "compute.env"
    env_file.write_text("OWNER=translator\n")
    command = module._compose("/usr/bin/docker", "unix:///run/docker.sock", env_file)
    assert command[:4] == [
        "/usr/bin/docker",
        "--host",
        "unix:///run/docker.sock",
        "compose",
    ]


def test_unix_and_windows_entries_expose_same_lifecycle() -> None:
    unix = (ROOT / "compute-runtime" / "bootstrap.sh").read_text()
    windows = (ROOT / "compute-runtime" / "bootstrap.ps1").read_text()
    source = (ROOT / "compute-runtime" / "station.py").read_text()
    for action in ("doctor", "prepare", "start", "status", "stop"):
        assert action in source
    assert "station.py" in unix
    assert "station.py" in windows
