"""Safety and corpus checks; these do not substitute for model inference."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from .run import (
    OWNER,
    ROOT,
    cache_directory,
    compute_state,
    download,
    historical_madlad_lock,
    memory_bytes,
    model_memory_limit,
    released_compute,
    remove_owned_container,
    resource_preflight,
    verify,
)
from .worker import index_prompt, literal_checks


def test_downloader_lock_excludes_the_vulnerable_filelock_releases() -> None:
    packages = tomllib.loads((ROOT / "poetry.lock").read_text())["package"]
    versions = [
        package["version"] for package in packages if package["name"] == "filelock"
    ]
    assert versions == ["4.2.1"]


def test_historical_lock_belongs_to_the_selected_image_not_the_active_model() -> None:
    value = {"model": "google/madlad400-3b-mt", "translator_ref": "historic"}
    image = "sha256:" + "a" * 64
    with patch("model_comparison.run.docker", return_value=json.dumps(value)) as call:
        assert historical_madlad_lock(image) == value
    assert image in call.call_args.args and "none" in call.call_args.args


def test_current_index_image_cannot_be_mislabeled_as_historical_madlad() -> None:
    with (
        patch(
            "model_comparison.run.docker",
            return_value='{"model":"IndexTeam/Index-Translate-9B-GGUF"}',
        ),
        pytest.raises(RuntimeError, match="requires the MADLAD image"),
    ):
        historical_madlad_lock("sha256:" + "a" * 64)


def test_corpus_is_identical_and_fits_the_existing_provider() -> None:
    cases = json.loads((ROOT / "cases.json").read_text())
    assert len(cases) == 20
    assert len({case["id"] for case in cases}) == len(cases)
    assert all(len(part) <= 800 for case in cases for part in case["text"].splitlines())
    assert {case["source"] for case in cases} == {"en", "ru", "de", "fr", "es", "zh"}


def test_prompt_matches_official_plain_translation_format() -> None:
    assert index_prompt({"source": "en", "target": "ru", "text": " Hello "}) == (
        "请将以下英语文本翻译为俄语，直接输出翻译结果，不要进行任何解释。\n\nHello"
    )


def test_raw_hallucination_is_not_silently_removed() -> None:
    case = {"checks": {"forbidden": ["Deutschland"], "required": ["request_id"]}}
    assert literal_checks(case, "Zentralbank von Deutschland") == [
        "missing:request_id",
        "added:Deutschland",
    ]


def test_identifiers_and_empty_output_are_checked() -> None:
    assert (
        literal_checks({"checks": {"required": ["{user_name}"]}}, "Hi {user_name}")
        == []
    )
    assert literal_checks({}, "  ") == ["empty_output"]


def test_sha256_and_size_are_both_required(tmp_path: Path) -> None:
    artifact = tmp_path / "weights"
    artifact.write_bytes(b"synthetic integrity fixture")
    spec = {
        "size_bytes": artifact.stat().st_size,
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
    }
    assert verify(artifact, spec)
    assert not verify(artifact, {**spec, "size_bytes": 1})
    assert not verify(artifact, {**spec, "sha256": "0" * 64})


def test_cleanup_refuses_other_owner() -> None:
    details = [{"Config": {"Labels": {"org.aelev.translator.experiment": "other-run"}}}]
    with (
        patch("model_comparison.run.docker", return_value=json.dumps(details)) as call,
        pytest.raises(RuntimeError, match="ownership mismatch"),
    ):
        remove_owned_container("experiment", "own-run")
    assert call.call_count == 1


def test_cleanup_stops_only_exact_owned_container() -> None:
    details = [
        {
            "Config": {"Labels": {"org.aelev.translator.experiment": "own-run"}},
            "State": {"Running": True},
        }
    ]
    with patch(
        "model_comparison.run.docker", side_effect=[json.dumps(details), "", ""]
    ) as call:
        remove_owned_container("experiment", "own-run")
    assert [entry.args for entry in call.call_args_list] == [
        ("inspect", "experiment"),
        ("stop", "--time", "20", "experiment"),
        ("rm", "experiment"),
    ]


def test_runtime_has_no_network_or_working_storage_dependencies() -> None:
    source = (ROOT / "run.py").read_text()
    worker = (ROOT / "worker.py").read_text()
    compact = re.sub(r"\s+", "", source)
    assert '"--network","none"' in compact
    assert '"--memory-swap",memory_limit' in compact
    assert "http://127.0.0.1:8080" in worker
    assert "--cpus" in source and "--read-only" in source
    assert "host.docker.internal" not in source + worker


def test_resource_units_and_unknown_unit_are_not_guessed() -> None:
    assert memory_bytes("2.5GiB ") == int(2.5 * 2**30)
    assert memory_bytes("128MiB") == 128 * 2**20
    with pytest.raises(RuntimeError, match="Unrecognized memory unit"):
        memory_bytes("unlimited")


def test_download_never_overwrites_an_unverified_artifact(tmp_path: Path) -> None:
    (tmp_path / ".experiment-owner").write_text(OWNER)
    artifact = tmp_path / "synthetic.gguf"
    artifact.write_bytes(b"incomplete")
    with (
        patch("model_comparison.run.CACHE", tmp_path),
        pytest.raises(RuntimeError, match="unverified artifact"),
    ):
        download({"file": artifact.name, "size_bytes": 1, "sha256": "0" * 64})


def test_verified_download_does_not_need_network(tmp_path: Path) -> None:
    (tmp_path / ".experiment-owner").write_text(OWNER)
    artifact = tmp_path / "synthetic.gguf"
    artifact.write_bytes(b"synthetic integrity fixture")
    spec = {
        "file": artifact.name,
        "size_bytes": artifact.stat().st_size,
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
    }
    with (
        patch("model_comparison.run.CACHE", tmp_path),
        patch("model_comparison.run.docker") as call,
    ):
        download(spec)
    call.assert_not_called()


def test_existing_unmarked_directory_is_never_claimed(tmp_path: Path) -> None:
    with (
        patch("model_comparison.run.CACHE", tmp_path),
        pytest.raises(RuntimeError, match="ownership mismatch"),
    ):
        cache_directory()
    assert not (tmp_path / ".experiment-owner").exists()


def test_vm_available_memory_not_only_container_stats_is_checked() -> None:
    with (
        patch(
            "model_comparison.run.docker",
            side_effect=[
                json.dumps({"MemTotal": 16 * 2**30}),
                json.dumps({"MemUsage": "8GiB / 16GiB"}),
                "MemAvailable:    6291456 kB\n",
            ],
        ) as call,
        pytest.raises(RuntimeError, match="Insufficient VM headroom"),
    ):
        resource_preflight({"id": "index-9b", "memory_limit": "6656m"})
    assert not any("stop" in entry.args for entry in call.call_args_list)


def test_memory_limit_has_one_source_for_admission_and_execution() -> None:
    assert model_memory_limit({"id": "madlad"}) == "4g"
    assert model_memory_limit({"id": "index-9b", "memory_limit": "6656m"}) == "6656m"
    assert model_memory_limit({"id": "index-9b", "memory_limit": "8g"}) == "8g"
    with (
        patch("model_comparison.run.docker") as call,
        pytest.raises(RuntimeError, match="Unsupported experiment memory limit"),
    ):
        resource_preflight({"id": "index-9b", "memory_limit": "unlimited"})
    call.assert_not_called()


@pytest.mark.parametrize("valid", [False, True])
def test_download_publishes_only_verified_bytes_after_container_cleanup(
    tmp_path: Path, valid: bool
) -> None:
    (tmp_path / ".experiment-owner").write_text(OWNER)
    stage = tmp_path / "hub-index-2b"
    stage.mkdir()
    artifact = stage / "synthetic.gguf"
    artifact.write_bytes(b"fixed bytes")
    model = {
        "id": "index-2b",
        "file": artifact.name,
        "repository": "example.invalid/model",
        "revision": "a" * 40,
        "size_bytes": artifact.stat().st_size,
        "sha256": (
            hashlib.sha256(artifact.read_bytes()).hexdigest() if valid else "0" * 64
        ),
    }
    image = {
        "Id": "sha256:immutable",
        "Config": {"Labels": {"org.aelev.translator.experiment": OWNER}},
    }
    state = {"State": {"ExitCode": 0, "OOMKilled": False}}
    with (
        patch("model_comparison.run.CACHE", tmp_path),
        patch(
            "model_comparison.run.docker",
            side_effect=[json.dumps([image]), "created", json.dumps([state])],
        ),
        patch("model_comparison.run.subprocess.run"),
        patch("model_comparison.run.remove_owned_container") as cleanup,
    ):
        if valid:
            download(model)
        else:
            with pytest.raises(RuntimeError, match="integrity failed"):
                download(model)
    cleanup.assert_called_once()
    assert (tmp_path / artifact.name).exists() is valid


def test_compute_snapshot_refuses_a_different_compose_owner() -> None:
    item = {
        "Id": "original-id",
        "Config": {"Labels": {"com.docker.compose.project": "other-project"}},
    }
    with (
        patch("model_comparison.run.docker", return_value=json.dumps([item])),
        pytest.raises(RuntimeError, match="ownership changed"),
    ):
        compute_state("original-id", "selected-project", "translator-model")


def test_no_pause_without_complete_verified_9b() -> None:
    with (
        patch("model_comparison.run.verify", return_value=False),
        patch("model_comparison.run.docker") as call,
        pytest.raises(RuntimeError, match="before pausing"),
        released_compute("selected-project", "sha256:original"),
    ):
        pytest.fail("Incomplete weights must not pause the live model")
    call.assert_not_called()


def test_default_run_never_manages_working_containers() -> None:
    with (
        patch("model_comparison.run.docker") as call,
        released_compute("", "sha256:original"),
    ):
        pass
    call.assert_not_called()


@pytest.mark.parametrize("failure", [False, True])
def test_authorized_pause_restores_same_containers_even_after_failure(
    tmp_path: Path, failure: bool
) -> None:
    (tmp_path / "models.json").write_text((ROOT / "models.json").read_text())
    states: dict[str, dict[str, Any]] = {
        role: {
            "id": role,
            "image": "sha256:original",
            "running": True,
            "health": "healthy" if role == "translator-model" else None,
            "mounts": ["own-socket"],
        }
        for role in ("translator-model", "translator-worker")
    }

    def fake_docker(*args: str) -> str:
        if args[0] == "ps":
            return args[-1].split("=", 1)[1]
        if args[0] == "stop":
            states[args[-1]]["running"] = False
        elif args[0] == "start":
            states[args[-1]]["running"] = True
        else:
            pytest.fail(f"Unexpected command: {args}")
        return ""

    def fake_state(_id: str, _project: str, role: str) -> dict[str, Any]:
        return dict(states[role])

    with (
        patch("model_comparison.run.ROOT", tmp_path),
        patch("model_comparison.run.verify", return_value=True),
        patch("model_comparison.run.compute_state", side_effect=fake_state),
        patch("model_comparison.run.docker", side_effect=fake_docker) as call,
    ):
        try:
            with released_compute("selected-project", "sha256:original"):
                assert not any(state["running"] for state in states.values())
                if failure:
                    raise RuntimeError("Synthetic benchmark failure")
        except RuntimeError as error:
            assert failure and str(error) == "Synthetic benchmark failure"
    assert all(state["running"] for state in states.values())
    assert [
        entry.args for entry in call.call_args_list if entry.args[0] == "start"
    ] == [
        ("start", "translator-model"),
        ("start", "translator-worker"),
    ]
    evidence = json.loads((tmp_path / "maintenance-result.json").read_text())
    assert evidence["restored"] and not evidence["errors"]
    before_id = evidence["before"]["translator-model"]["id"]
    assert before_id == evidence["after"]["translator-model"]["id"]
