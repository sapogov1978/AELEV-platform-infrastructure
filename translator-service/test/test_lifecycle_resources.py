"""Check lifecycle ownership and cleanup safety without touching Docker."""

from copy import deepcopy

import pytest
from lifecycle_resources import cleanup_plan, preflight, snapshot, verify_cleanup


def inventory() -> dict[str, dict[str, dict[str, str]]]:
    return {"containers": {}, "volumes": {}, "networks": {}, "images": {}}


def test_cleanup_preserves_existing_and_foreign_resources() -> None:
    before = inventory()
    before["networks"]["existing-network"] = {"project": "target"}
    before["volumes"]["existing-volume"] = {"project": "target"}
    current = deepcopy(before)
    current["containers"] = {
        "new": {"project": "target"},
        "foreign": {"project": "other"},
    }
    current["volumes"].update(
        {
            "new": {"project": "target"},
            "anonymous": {"project": "", "anonymous": "true"},
            "foreign": {"project": "other"},
        }
    )
    current["networks"]["new-network"] = {"project": "target"}
    assert cleanup_plan(
        before, current, {"target"}, {"anonymous", "existing-volume", "foreign"}
    ) == {
        "containers": ["new"],
        "volumes": ["anonymous", "new"],
        "networks": ["new-network"],
    }


def test_preflight_refuses_existing_project_container() -> None:
    before = inventory()
    before["containers"]["old"] = {"project": "target", "state": "exited"}
    with pytest.raises(RuntimeError, match="Existing project containers"):
        preflight(before, {"target"}, set(), set())


def test_preflight_refuses_existing_data_even_without_container() -> None:
    before = inventory()
    before["volumes"]["data"] = {"project": "target"}
    with pytest.raises(RuntimeError, match="retained data"):
        preflight(before, {"target"}, {"data"}, set())


def test_preflight_requires_images_prepared_outside_test() -> None:
    with pytest.raises(RuntimeError, match="Prepare working images"):
        preflight(inventory(), {"target"}, set(), {"aelev/translator-service:local"})


def test_verification_rejects_surviving_owned_resource() -> None:
    before = inventory()
    after = inventory()
    after["volumes"]["leftover"] = {"project": "target"}
    with pytest.raises(RuntimeError, match="Test-created volumes remain"):
        verify_cleanup(before, after, {"target"})


def test_verification_rejects_changed_existing_container() -> None:
    before = inventory()
    before["containers"]["foreign"] = {"project": "other", "state": "running"}
    after = deepcopy(before)
    after["containers"]["foreign"]["state"] = "exited"
    with pytest.raises(RuntimeError, match="Pre-existing containers changed"):
        verify_cleanup(before, after, {"target"})


def test_snapshot_retains_multiple_image_references() -> None:
    def execute(arguments: list[str]) -> str:
        if arguments[1] == "image":
            return "\n".join(
                [
                    '{"ID":"image","Repository":"repo","Tag":"a"}',
                    '{"ID":"image","Repository":"repo","Tag":"b"}',
                ]
            )
        return ""

    assert snapshot(execute)["images"]["image"] == {"references": "repo:a,repo:b"}


def test_snapshot_recognizes_only_docker_anonymous_volume_label() -> None:
    def execute(arguments: list[str]) -> str:
        if arguments[1] == "volume":
            return (
                '{"Name":"anonymous","Labels":"com.docker.volume.anonymous="}'
                '\n{"Name":"unknown","Labels":""}'
            )
        return ""

    volumes = snapshot(execute)["volumes"]
    assert volumes["anonymous"]["anonymous"] == "true"
    assert volumes["unknown"]["anonymous"] == "false"
    assert cleanup_plan(inventory(), snapshot(execute), {"target"}, set(volumes))[
        "volumes"
    ] == ["anonymous"]
