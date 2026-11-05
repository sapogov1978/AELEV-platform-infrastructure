"""Read-only release evidence, hashing environment values instead of exposing them."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import subprocess
from pathlib import Path
from typing import Any, cast

NAMES = (
    "aelev-infrastructure-translator-model-1",
    "aelev-infrastructure-translator-api-1",
)
OLD = "sha256:45259d1de5bb8ccbc0c0250119b045f7ae98fc96d549af4fa9bf17a920ba7f1c"
TAG = "aelev/translator-service:local-heartbeat-0005-20261021"


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def collect(host: str) -> dict[str, Any]:
    def docker(*args: str) -> str:
        return subprocess.run(
            ["docker", "--host", host, *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout

    daemon = docker("info", "--format", "{{.ID}}").strip()
    records = cast(list[dict[str, Any]], json.loads(docker("inspect", *NAMES)))
    roles: dict[str, Any] = {}
    for record in records:
        environment = sorted(
            value
            for value in record["Config"]["Env"]
            if not value.startswith("AELEV_TRANSLATOR_HEALTH_ROLE=")
        )
        runtime = {
            "user": record["Config"]["User"],
            "command": record["Config"]["Cmd"],
            "entrypoint": record["Config"]["Entrypoint"],
            **{
                key: record["HostConfig"].get(key)
                for key in (
                    "ReadonlyRootfs",
                    "Tmpfs",
                    "SecurityOpt",
                    "Memory",
                    "NanoCpus",
                    "NetworkMode",
                    "PortBindings",
                    "ExtraHosts",
                    "CapDrop",
                    "Init",
                )
            },
        }
        roles[record["Name"].lstrip("/")] = {
            "id": record["Id"],
            "image": record["Image"],
            "started": record["State"]["StartedAt"],
            "native_count": record["RestartCount"],
            "restart": record["HostConfig"]["RestartPolicy"],
            "state": record["State"]["Status"],
            "oom": record["State"]["OOMKilled"],
            "health": record["State"].get("Health", {}),
            "environment_sha256": digest(environment),
            "mounts": sorted(record["Mounts"], key=lambda item: item["Destination"]),
            "networks": sorted(record["NetworkSettings"]["Networks"]),
            "runtime_sha256": digest(runtime),
            "runtime": runtime,
        }
    all_records = [
        json.loads(line)
        for line in docker(
            "ps", "--all", "--no-trunc", "--format", "{{json .}}"
        ).splitlines()
    ]
    others = sorted(
        (row["ID"], row["Names"], row["Image"])
        for row in all_records
        if row["Names"] not in NAMES
    )
    old, new = cast(
        list[dict[str, Any]], json.loads(docker("image", "inspect", OLD, TAG))
    )
    layers = old["RootFS"]["Layers"]
    assert new["RootFS"]["Layers"][: len(layers)] == layers
    for key in ("User", "Env", "Cmd", "Entrypoint", "WorkingDir", "Volumes"):
        assert old["Config"].get(key) == new["Config"].get(key), key
    return {
        "daemon": daemon,
        "roles": roles,
        "other_count": len(others),
        "other_sha256": digest(others),
        "candidate": new["Id"],
        "inherited_layers": len(layers),
        "new_layers": len(new["RootFS"]["Layers"]) - len(layers),
    }


def compare(before: dict[str, Any], after: dict[str, Any]) -> None:
    for key in (
        "daemon",
        "other_count",
        "other_sha256",
        "candidate",
        "inherited_layers",
    ):
        assert before[key] == after[key], key
    for name in NAMES:
        old, new = before["roles"][name], after["roles"][name]
        assert old["id"] != new["id"] and new["image"] == after["candidate"]
        assert new["state"] == "running" and not new["oom"]
        assert new["native_count"] == old["native_count"] == 0
        assert new["health"]["Status"] == "healthy"
        for key in (
            "restart",
            "environment_sha256",
            "mounts",
            "networks",
        ):
            assert old[key] == new[key], (name, key)
        if old["runtime_sha256"] != new["runtime_sha256"]:
            # Compose may reorder this set; prove the old exact hash using only
            # permutations of the same two host aliases, not broader defaults.
            runtime = new["runtime"]
            raw_aliases: object = runtime["ExtraHosts"]
            assert isinstance(raw_aliases, list)
            aliases = cast(list[object], raw_aliases)
            assert len(aliases) == 2 and all(isinstance(v, str) for v in aliases)
            assert old["runtime_sha256"] in {
                digest({**runtime, "ExtraHosts": values})
                for values in itertools.permutations(aliases)
            }, (name, "runtime_sha256")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-host", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--before", type=Path)
    args = parser.parse_args()
    result = collect(args.docker_host)
    if args.before is not None:
        compare(json.loads(args.before.read_bytes()), result)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, sort_keys=True)
    args.output.chmod(0o600)
    print(
        json.dumps(
            {
                "status": "ROLLOUT_VERIFIED" if args.before else "PREDEPLOY_CAPTURED",
                "candidate": result["candidate"],
                "other_count": result["other_count"],
                "inherited_layers": result["inherited_layers"],
                "new_layers": result["new_layers"],
            }
        )
    )


if __name__ == "__main__":
    main()
