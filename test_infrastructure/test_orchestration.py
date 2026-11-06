"""Offline safety regressions; these do not replace the real lifecycle."""

from __future__ import annotations

import copy
import argparse
from pathlib import Path
from typing import Any

import pytest

from lifecycle import assert_sequence_advanced, compose_prefix, run
from resources import Inventory, cleanup_plan, inventory, preflight, verify_preserved

ROOT = Path(__file__).resolve().parents[1]


def row(project: str = "", anonymous: str = "False") -> dict[str, str]:
    return {
        "project": project,
        "name": "resource",
        "state": "",
        "anonymous": anonymous,
    }


def empty() -> Inventory:
    return {kind: {} for kind in ("containers", "networks", "volumes", "images")}


def execute_empty(arguments: list[str]) -> str:
    return "[]"


def model() -> dict[str, Any]:
    return {
        "name": "owned",
        "services": {"native": {"image": "image:1"}},
        "volumes": {"data": {"name": "data"}},
    }


def baseline() -> Inventory:
    before = empty()
    before["images"]["sha"] = {"references": "image:1"}
    return before


def test_root_env_has_only_orchestration() -> None:
    for path in (ROOT / ".env.example", ROOT / ".env"):
        if path.exists():
            keys = {
                line.split("=", 1)[0]
                for line in path.read_text().splitlines()
                if line and not line.startswith("#")
            }
            assert keys == {"AELEV_INFRASTRUCTURE_COMPOSE_PROJECT"}


def test_root_is_only_inclusion_not_runtime() -> None:
    compose = (ROOT / "compose.yml").read_text()
    for declaration in ("services:", "volumes:", "networks:", "secrets:", "build:"):
        assert declaration not in compose
    assert ":-" not in compose
    assert "AELEV_INFRASTRUCTURE_COMPOSE_PROJECT:?" in compose
    assert "./translator-service/compose.remote.yml" in compose
    assert compose.count("./kafka/compose.yml") == 1
    assert compose.count("env_file:") == 8
    assert compose.count("./public-egress-gateway/compose.yml") == 1
    assert compose.count("./spire/compose.group.yml") == 1
    assert "./spire/compose.yml" not in compose
    assert "profiles: [workload-identity]" in (ROOT / "spire/compose.yml").read_text()


def test_kafka_provider_does_not_include_or_depend_on_translator() -> None:
    kafka = (ROOT / "kafka/compose.yml").read_text()
    assert "translator" not in kafka
    wiring = (ROOT / "translator-service/compose.local.yml").read_text()
    assert "kafka: {condition: service_healthy}" in wiring
    assert "kafka: !reset null" in wiring


def test_no_consumer_configuration_in_root() -> None:
    assert not list(ROOT.glob("*-client.json"))
    for name in ("realm.json", "apply-configuration.sh", "orchestration", "local"):
        assert not (ROOT / name).exists()


@pytest.mark.parametrize("owner", ["keycloak", "platform-ui", "observability"])
def test_administrative_names_are_owned_without_root_overrides(owner: str) -> None:
    assert not list(ROOT.glob("compose.*.yml"))
    source = (ROOT / owner / "compose.yml").read_text()
    assert "  configuration-applier:" not in source
    if owner == "observability":
        assert "services:" not in source
        for product in (
            "delivery",
            "otel-collector",
            "prometheus",
            "loki",
            "tempo",
            "grafana",
        ):
            assert (
                "  " + product + "-configuration-init:"
                in (ROOT / owner / product / "compose.yml").read_text()
            )
    elif owner != "keycloak":
        assert "  " + owner + "-configuration-init:" in source


def test_local_translator_recipe_is_reused_not_copied() -> None:
    root = (ROOT / "compose.yml").read_text()
    local = (ROOT / "translator-service/compose.yml").read_text()
    recipe = (ROOT / "translator-service/compose.local.yml").read_text()
    assert "./translator-service/compose.local.yml" in root
    assert "./compose.local.yml" in local
    assert "services:" not in local
    assert "profiles: !reset []" in recipe
    for forbidden in ("image:", "build:", "volumes:", "environment:", "healthcheck:"):
        assert forbidden not in recipe


def test_production_prefix_never_disables_fencing() -> None:
    assert not any("compose.test.yml" in argument for argument in compose_prefix())


def test_interrupted_preflight_is_reported_as_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = tmp_path / "interrupted.md"

    def interrupted(arguments: list[str], cwd: Path = ROOT) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("lifecycle.execute", interrupted)
    monkeypatch.setattr("lifecycle.REPORT", report)
    assert run(argparse.Namespace(reuse_existing_data=False)) == 1
    assert "Итог: FAIL" in report.read_text()
    assert "KeyboardInterrupt" in report.read_text()


def test_preflight_accepts_prepared_clean_stand() -> None:
    preflight(execute_empty, baseline(), model(), False)


def test_preflight_refuses_existing_common_containers() -> None:
    before = baseline()
    before["containers"]["old"] = row("owned")
    with pytest.raises(RuntimeError, match="existing containers"):
        preflight(execute_empty, before, model(), True)


def test_preflight_requires_explicit_reuse_of_existing_data() -> None:
    before = baseline()
    before["volumes"]["data"] = row("old-owner")
    with pytest.raises(RuntimeError, match="reuse-existing-data"):
        preflight(execute_empty, before, model(), False)
    preflight(execute_empty, before, model(), True)


def test_preflight_refuses_a_foreign_running_writer() -> None:
    before = baseline()
    before["containers"]["writer"] = {**row("independent"), "state": "running"}

    def mounts(arguments: list[str]) -> str:
        return '[{"Type": "volume", "Name": "data"}]'

    with pytest.raises(RuntimeError, match="running container"):
        preflight(mounts, before, model(), True)


def test_preflight_refuses_missing_images() -> None:
    with pytest.raises(RuntimeError, match="prepare canonical images"):
        preflight(execute_empty, empty(), model(), False)


def test_preflight_accepts_cached_digest_not_listed_as_tag() -> None:
    pinned = model()
    pinned["services"]["native"]["image"] = "image:1@sha256:digest"

    def inspect(arguments: list[str]) -> str:
        return "sha" if arguments[1:3] == ["image", "inspect"] else "[]"

    preflight(inspect, baseline(), pinned, False)


def test_preflight_refuses_digest_resolving_outside_initial_inventory() -> None:
    pinned = model()
    pinned["services"]["native"]["image"] = "image:1@sha256:digest"

    def inspect(arguments: list[str]) -> str:
        return "new" if arguments[1:3] == ["image", "inspect"] else "[]"

    with pytest.raises(RuntimeError, match="prepare canonical images"):
        preflight(inspect, baseline(), pinned, False)


def test_cleanup_never_selects_original_resources() -> None:
    before = empty()
    for kind in ("containers", "networks", "volumes"):
        before[kind]["original"] = row("owned")
    assert cleanup_plan(before, copy.deepcopy(before), "owned", set()) == {
        "containers": [],
        "networks": [],
        "volumes": [],
    }


def test_cleanup_selects_only_created_owned_resources() -> None:
    before = empty()
    current = empty()
    for kind in ("containers", "networks", "volumes"):
        current[kind]["ours"] = row("owned")
        current[kind]["foreign"] = row("unrelated")
    assert cleanup_plan(before, current, "owned", set()) == {
        "containers": ["ours"],
        "networks": ["ours"],
        "volumes": ["ours"],
    }


def test_cleanup_anonymous_volume_requires_witnessed_ownership() -> None:
    current = empty()
    current["volumes"]["ours"] = row(anonymous="True")
    current["volumes"]["unseen"] = row(anonymous="True")
    current["volumes"]["named"] = row()
    assert cleanup_plan(empty(), current, "owned", {"ours", "named"})["volumes"] == [
        "ours"
    ]


@pytest.mark.parametrize("kind", ["containers", "networks", "volumes", "images"])
def test_verification_refuses_loss_of_original_resource(kind: str) -> None:
    before = empty()
    before[kind]["original"] = row()
    with pytest.raises(RuntimeError, match="pre-existing"):
        verify_preserved(before, empty(), "owned")


def test_verification_refuses_change_of_foreign_state() -> None:
    before = empty()
    before["containers"]["foreign"] = {**row("foreign"), "state": "running"}
    after = copy.deepcopy(before)
    after["containers"]["foreign"]["state"] = "exited"
    with pytest.raises(RuntimeError, match="pre-existing"):
        verify_preserved(before, after, "owned")


def test_verification_refuses_created_remainder() -> None:
    after = empty()
    after["networks"]["new"] = row("owned")
    with pytest.raises(RuntimeError, match="remain"):
        verify_preserved(empty(), after, "owned")


def test_inventory_merges_image_tags_without_losing_protection() -> None:
    def listing(arguments: list[str]) -> str:
        if arguments[1] == "image":
            return (
                '{"ID":"sha","Repository":"image","Tag":"1"}\n'
                '{"ID":"sha","Repository":"image","Tag":"2"}'
            )
        return ""

    assert inventory(listing)["images"]["sha"]["references"] == "image:1,image:2"


def status(sequence: int) -> dict[str, Any]:
    return {"identity": {"status": {"sequence": sequence}, "components": []}}


def test_preserved_identity_advances_sequence() -> None:
    assert_sequence_advanced(status(1), status(2))


def test_sequence_cannot_restart_from_zero() -> None:
    with pytest.raises(RuntimeError, match="sequence"):
        assert_sequence_advanced(status(10), status(1))


def test_identity_cannot_change_after_restart() -> None:
    with pytest.raises(RuntimeError, match="identities"):
        assert_sequence_advanced(status(1), {})
