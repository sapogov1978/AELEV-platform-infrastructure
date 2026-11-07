"""Ownership accounting; no global prune or removal of pre-existing data."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

Inventory = dict[str, dict[str, dict[str, str]]]
Execute = Callable[[list[str]], str]


def inventory(execute: Execute) -> Inventory:
    result: Inventory = {}
    for kind, arguments in {
        "containers": ["container", "ls", "--all", "--no-trunc"],
        "networks": ["network", "ls", "--no-trunc"],
        "volumes": ["volume", "ls"],
        "images": ["image", "ls", "--all", "--no-trunc"],
    }.items():
        resources: dict[str, dict[str, str]] = {}
        for line in execute(["docker", *arguments, "--format", "json"]).splitlines():
            row = json.loads(line)
            identity = row["Name"] if kind == "volumes" else row["ID"]
            labels = dict(
                item.split("=", 1)
                for item in row.get("Labels", "").split(",")
                if "=" in item
            )
            if kind == "images":
                previous = resources.get(identity, {}).get("references", "")
                references = set(previous.split(","))
                references.discard("")
                references.add(row["Repository"] + ":" + row["Tag"])
                resources[identity] = {"references": ",".join(sorted(references))}
            else:
                resources[identity] = {
                    "name": row.get("Name", row.get("Names", "")),
                    "project": labels.get("com.docker.compose.project", ""),
                    "state": row.get("State", ""),
                    "anonymous": str("com.docker.volume.anonymous" in labels),
                }
        result[kind] = resources
    return result


def owned_containers(current: Inventory, project: str) -> list[str]:
    return [
        identity
        for identity, row in current["containers"].items()
        if row["project"] == project
    ]


def mounted_volumes(execute: Execute, containers: list[str]) -> set[str]:
    result: set[str] = set()
    for identity in containers:
        mounts: list[dict[str, Any]] = json.loads(
            execute(["docker", "inspect", "--format", "{{json .Mounts}}", identity])
        )
        result.update(row["Name"] for row in mounts if row["Type"] == "volume")
    return result


def preflight(
    execute: Execute,
    before: Inventory,
    model: dict[str, Any],
    reuse_existing_data: bool,
    active_services: set[str] | None = None,
) -> None:
    if owned_containers(before, model["name"]):
        raise RuntimeError("existing containers of the common project")
    names = {row["name"] for row in model.get("volumes", {}).values()}
    if not reuse_existing_data and names & before["volumes"].keys():
        raise RuntimeError("existing data requires explicit --reuse-existing-data")
    running = [
        identity
        for identity, row in before["containers"].items()
        if row["state"] == "running"
    ]
    if names & mounted_volumes(execute, running):
        raise RuntimeError("another running container mounts infrastructure data")
    available = {
        reference
        for row in before["images"].values()
        for reference in row["references"].split(",")
    }
    missing = set()
    selected = set(model["services"]) if active_services is None else active_services
    required_images = {model["services"][name]["image"] for name in selected}
    for image in required_images - available:
        try:
            identity = execute(
                ["docker", "image", "inspect", "--format", "{{.Id}}", image]
            ).strip()
        except RuntimeError:
            missing.add(image)
            continue
        # Digest-pinned references need not appear in `image ls` as tags.
        if identity not in before["images"]:
            missing.add(image)
    if missing:
        raise RuntimeError(
            "prepare canonical images before lifecycle testing: "
            + ", ".join(sorted(missing))
        )


def cleanup_plan(
    before: Inventory,
    current: Inventory,
    project: str,
    witnessed_mounts: set[str],
) -> dict[str, list[str]]:
    plan: dict[str, list[str]] = {}
    for kind in ("containers", "networks", "volumes"):
        plan[kind] = [
            identity
            for identity, row in current[kind].items()
            if identity not in before[kind]
            and (
                row["project"] == project
                or (
                    kind == "volumes"
                    and identity in witnessed_mounts
                    and row["anonymous"] == "True"
                    and not row["project"]
                )
            )
        ]
    return plan


def verify_preserved(before: Inventory, after: Inventory, project: str) -> None:
    for kind, resources in before.items():
        for identity, row in resources.items():
            if after[kind].get(identity) != row:
                raise RuntimeError(f"pre-existing {kind} changed")
    if any(
        row["project"] == project and identity not in before[kind]
        for kind in ("containers", "networks", "volumes")
        for identity, row in after[kind].items()
    ):
        raise RuntimeError("created project resources remain")
