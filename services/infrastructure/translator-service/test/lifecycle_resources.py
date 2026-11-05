"""Account for Docker resources without deleting pre-existing infrastructure."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, cast

Inventory = dict[str, dict[str, dict[str, str]]]


def snapshot(execute: Callable[[list[str]], str]) -> Inventory:
    inventory: Inventory = {}
    for kind, arguments in {
        "containers": ["container", "ls", "--all", "--no-trunc"],
        "networks": ["network", "ls", "--no-trunc"],
        "volumes": ["volume", "ls"],
        "images": ["image", "ls", "--no-trunc"],
    }.items():
        output = execute(["docker", *arguments, "--format", "json"])
        resources: dict[str, dict[str, str]] = {}
        for line in output.splitlines():
            row = cast(dict[str, str], json.loads(line))
            identity = row["Name"] if kind == "volumes" else row["ID"]
            labels = dict(
                item.split("=", 1)
                for item in row.get("Labels", "").split(",")
                if "=" in item
            )
            if kind == "images":
                reference = row["Repository"] + ":" + row["Tag"]
                references = set(
                    resources.get(identity, {}).get("references", "").split(",")
                )
                references.discard("")
                references.add(reference)
                resources[identity] = {"references": ",".join(sorted(references))}
            else:
                resources[identity] = {
                    "name": row.get("Name", row.get("Names", "")),
                    "project": labels.get("com.docker.compose.project", ""),
                    "state": row.get("State", ""),
                }
                if kind == "volumes":
                    resources[identity]["anonymous"] = (
                        "true" if "com.docker.volume.anonymous" in labels else "false"
                    )
        inventory[kind] = resources
    return inventory


def preflight(
    before: Inventory,
    projects: set[str],
    volumes: set[str],
    images: set[str],
) -> None:
    if any(row["project"] in projects for row in before["containers"].values()):
        raise RuntimeError(
            "Existing project containers: lifecycle requires a clean stand"
        )
    if volumes & before["volumes"].keys():
        raise RuntimeError(
            "Existing data/socket volumes: refusing to modify retained data"
        )
    available = {
        reference
        for image in before["images"].values()
        for reference in image["references"].split(",")
    }
    if missing := images - available:
        raise RuntimeError(
            "Prepare working images before the test: " + ", ".join(sorted(missing))
        )


def cleanup_plan(
    before: Inventory,
    current: Inventory,
    projects: set[str],
    mounted_volumes: set[str],
) -> dict[str, list[str]]:
    plan: dict[str, list[str]] = {}
    for kind in ("containers", "volumes", "networks"):
        plan[kind] = sorted(
            identity
            for identity, row in current[kind].items()
            if identity not in before[kind]
            and (
                row["project"] in projects
                or (
                    kind == "volumes"
                    and identity in mounted_volumes
                    and not row["project"]
                    and row.get("anonymous") == "true"
                )
            )
        )
    return plan


def mounted_volumes(
    execute: Callable[[list[str]], str], containers: list[str]
) -> set[str]:
    names: set[str] = set()
    for identity in containers:
        output = execute(
            ["docker", "inspect", "--format", "{{json .Mounts}}", identity]
        )
        for mount in cast(list[dict[str, Any]], json.loads(output)):
            if mount["Type"] == "volume":
                names.add(str(mount["Name"]))
    return names


def verify_cleanup(before: Inventory, after: Inventory, projects: set[str]) -> None:
    for kind in before:
        for identity, original in before[kind].items():
            if after[kind].get(identity) != original:
                raise RuntimeError(f"Pre-existing {kind} changed: {identity}")
    for kind in ("containers", "volumes", "networks"):
        if any(
            identity not in before[kind] and row["project"] in projects
            for identity, row in after[kind].items()
        ):
            raise RuntimeError(f"Test-created {kind} remain")
