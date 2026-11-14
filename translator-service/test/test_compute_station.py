"""Portable compute station rejects mutable images and remote Docker contexts."""

from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any

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
    env_file.write_text(
        "AELEV_INFRASTRUCTURE_TRANSLATOR_COMPUTE_COMPOSE_PROJECT="
        "aelev-translator-compute\n"
    )
    command = module._compose("/usr/bin/docker", "unix:///run/docker.sock", env_file)
    assert command[:6] == [
        "/usr/bin/docker",
        "--host",
        "unix:///run/docker.sock",
        "compose",
        "--project-name",
        "aelev-translator-compute",
    ]


def test_compose_rejects_missing_project_identity(tmp_path: Path) -> None:
    module = station()
    env_file = tmp_path / "compute.env"
    env_file.write_text("OWNER=translator\n")
    with pytest.raises(RuntimeError, match="COMPUTE_PROJECT_INVALID"):
        module._compose("/usr/bin/docker", "unix:///run/docker.sock", env_file)


def test_unix_and_windows_entries_expose_same_lifecycle() -> None:
    unix = (ROOT / "compute-runtime" / "bootstrap.sh").read_text()
    windows = (ROOT / "compute-runtime" / "bootstrap.ps1").read_text()
    source = (ROOT / "compute-runtime" / "station.py").read_text()
    for action in ("doctor", "prepare", "start", "status", "stop"):
        assert action in source
    assert "station.py" in unix
    assert "station.py" in windows


def network_configuration(role: str) -> dict[str, Any]:
    volume = role + "-svid"
    targets = ["/run/secrets/compute-provider/identity.json"] + (
        [
            "/run/secrets/provider/credential.env",
            "/run/secrets/compute-provider/transport.json",
        ]
        if role == "provider"
        else ["/run/secrets/database", "/run/secrets/telemetry/client"]
    )
    service: dict[str, Any] = {
        "image": "registry.example/" + role + "@sha256:" + "a" * 64,
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "restart": "on-failure:3",
        "volumes": [
            {
                "type": "volume",
                "source": volume,
                "target": "/run/secrets/compute-provider",
                "read_only": True,
            },
            *[
                {
                    "type": "bind",
                    "source": "/operator" + target,
                    "target": target,
                    "read_only": True,
                    "bind": {"create_host_path": False},
                }
                for target in targets
            ],
        ],
    }
    if role == "provider":
        # Compose normalizes inherited image defaults to null. They are not
        # implementation overrides and must remain distinct from a command.
        service["command"] = None
        service["entrypoint"] = None
        service["ports"] = [
            {"target": 18487, "published": "38487", "host_ip": "0.0.0.0"}
        ]
    else:
        service["command"] = ["python", "-m", "translator_service.runtime", "worker"]
        service["environment"] = {
            "AELEV_TRANSLATOR_PROVIDER_TRANSPORT": "mtls",
            "AELEV_TRANSLATOR_PROVIDER_TLS_ROOT": "/run/secrets/compute-provider",
            "AELEV_TRANSLATOR_PROVIDER_URL": "https://provider.example:38487",
        }
    return {
        "services": {
            "translation-provider"
            if role == "provider"
            else "translator-worker": service
        },
        "volumes": {volume: {"external": True, "name": "this-node-" + volume}},
    }


@pytest.mark.parametrize("role", ["provider", "worker"])
def test_network_placement_is_independent_and_preserves_security_boundary(
    role: str,
) -> None:
    module = station()
    original = network_configuration(role)
    assert module._network_configuration(original, role) is original
    service_name = next(iter(original["services"]))
    for key, value in (
        ("privileged", True),
        ("read_only", False),
        ("restart", "always"),
        ("network_mode", "host"),
        ("depends_on", {"other": {"condition": "service_healthy"}}),
    ):
        changed = deepcopy(original)
        changed["services"][service_name][key] = value
        with pytest.raises(RuntimeError, match="CONFIGURATION_INVALID"):
            module._network_configuration(changed, role)
    for mutation in ("credential", "socket", "write", "local-volume"):
        changed = deepcopy(original)
        service = changed["services"][service_name]
        if mutation == "credential":
            service.setdefault("environment", {})["OPENAI_API_KEY"] = (
                "forbidden-not-a-real-key"
            )
        elif mutation == "socket":
            service["volumes"].append(
                {
                    "type": "volume",
                    "target": "/run/aelev-translator",
                    "source": "forbidden",
                }
            )
        elif mutation == "write":
            service["volumes"][0]["read_only"] = False
        else:
            changed["volumes"][role + "-svid"]["external"] = False
        with pytest.raises(RuntimeError, match="CONFIGURATION_INVALID"):
            module._network_configuration(changed, role)


@pytest.mark.parametrize("role", ["provider", "worker"])
def test_network_recipe_can_be_selected_without_other_role_inputs(
    tmp_path: Path, role: str
) -> None:
    module = station()
    env = tmp_path / "placement.env"
    env.write_text(
        "AELEV_INFRASTRUCTURE_TRANSLATOR_COMPUTE_COMPOSE_PROJECT=translator-"
        + role
        + "\n"
    )
    command = module._compose("/usr/bin/docker", "unix:///run/docker.sock", env, role)
    assert command[-1] == str(ROOT / f"compose.compute-{role}.mtls.yml")


@pytest.mark.parametrize(
    "change", ["foreign-owner", "stopped", "image", "config", "replicas"]
)
def test_network_start_requires_owned_unchanged_running_incarnation(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    module = station()
    configuration = network_configuration("provider")
    service_name = "translation-provider"
    command = [
        "/usr/bin/docker",
        "--host",
        "unix:///run/docker.sock",
        "compose",
        "--project-name",
        "test-provider",
    ]
    row: dict[str, Any] = {
        "Config": {
            "Image": configuration["services"][service_name]["image"],
            "Labels": {
                "com.docker.compose.service": service_name,
                "com.docker.compose.project": "test-provider",
                "com.docker.compose.project.working_dir": str(ROOT.resolve()),
                "com.docker.compose.config-hash": "a" * 64,
            },
        },
        "Image": "sha256:" + "b" * 64,
        "State": {"Running": True, "Health": {"Status": "healthy"}},
    }
    if change == "foreign-owner":
        row["Config"]["Labels"]["com.docker.compose.project.working_dir"] = "/foreign"
    elif change == "stopped":
        row["State"]["Running"] = False
    elif change == "image":
        row["Image"] = "sha256:" + "c" * 64
    elif change == "config":
        row["Config"]["Labels"]["com.docker.compose.config-hash"] = "d" * 64

    def execute(arguments: list[str], *, capture: bool = False) -> str:
        assert not any(
            value in {"up", "stop", "start", "restart", "rm"} for value in arguments
        )
        if arguments[-3:] == ["ps", "--all", "--quiet"]:
            return "a" * 64 + ("\n" + "b" * 64 if change == "replicas" else "")
        if "--hash" in arguments:
            return service_name + " " + "a" * 64
        if "image" in arguments:
            return "sha256:" + "b" * 64
        return json.dumps([row])

    monkeypatch.setattr(module, "_execute", execute)
    with pytest.raises(RuntimeError, match="OWNER_HANDOFF_REQUIRED"):
        module._network_owner(command[0], command[2], command, configuration, "start")
