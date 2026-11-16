"""Declare fresh clean-room published routes before its build and BOM."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from resources import inventory, preflight
from spire_fixture import ROOT, Fixture
from tools import full_local, spire_local

OWNERS = {"delivery": "DELIVERY", "grafana": "GRAFANA"}


def prepare(root: Path = ROOT) -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("ROOT_SPIRE_EPHEMERAL_CI_REQUIRED")
    fixture = Fixture(root)
    manifest = full_local.load_json(root / "full-local.json", root)
    prefix = full_local.compose_prefix(manifest, root)
    model = json.loads(
        full_local.command(
            [*prefix, "--profile", "*", "config", "--format", "json"], cwd=root
        ).stdout
    )
    before = inventory(lambda args: full_local.command(args, cwd=root).stdout)
    preflight(
        lambda args: full_local.command(args, cwd=root).stdout,
        before,
        model,
        False,
        set(),
    )
    fixture.preflight(before)
    record: dict[str, Any] = {"schema_version": 1, "files": {}}
    path = root / "runtime/root-spire-routes.json"
    if path.exists() or path.is_symlink():
        raise RuntimeError("ROOT_SPIRE_ROUTE_INSPECTION_REQUIRED")
    for owner, prefix_key in OWNERS.items():
        env = root / "observability" / owner / ".env"
        raw = env.read_bytes()
        key = "AELEV_INFRASTRUCTURE_" + prefix_key + "_BIND"
        if full_local.read_env(env, root)[key] != "127.0.0.1":
            raise RuntimeError("ROOT_SPIRE_OPERATOR_ROUTE_REFUSED")
        old, new = (key + "=127.0.0.1").encode(), (key + "=0.0.0.0").encode()
        lines = raw.splitlines(keepends=True)
        if sum(line.rstrip(b"\r\n") == old for line in lines) != 1:
            raise RuntimeError("ROOT_SPIRE_ROUTE_INVALID")
        changed = b"".join(
            new + line[len(old) :] if line.rstrip(b"\r\n") == old else line
            for line in lines
        )
        record["files"][owner] = {"original": raw.decode(), "changed": changed.decode()}
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    spire_local.save(path, record)
    for owner, item in record["files"].items():
        Fixture.replace(
            root / "observability" / owner / ".env",
            item["original"].encode(),
            item["changed"].encode(),
        )


def restore(root: Path = ROOT) -> None:
    path = root / "runtime/root-spire-routes.json"
    if not path.exists():
        return
    record = json.loads(path.read_bytes())
    if record.get("schema_version") != 1 or set(record["files"]) != set(OWNERS):
        raise RuntimeError("ROOT_SPIRE_ROUTE_RECORD_INVALID")
    ensure_stopped(root)
    # Validate every file before restoring any; a partial preparation is supported.
    for owner, item in record["files"].items():
        env = root / "observability" / owner / ".env"
        if env.is_symlink() or env.read_text() not in {
            item["original"],
            item["changed"],
        }:
            raise RuntimeError("ROOT_SPIRE_ENV_CHANGED")
    for owner, item in record["files"].items():
        env = root / "observability" / owner / ".env"
        Fixture.replace(env, env.read_bytes(), item["original"].encode())


def ensure_stopped(root: Path) -> None:
    core = full_local.read_env(root / ".env", root)[
        "AELEV_INFRASTRUCTURE_COMPOSE_PROJECT"
    ]
    fixture = Fixture(root)
    before = inventory(lambda args: full_local.command(args, cwd=root).stdout)
    if any(
        row.get("project") in {core, fixture.project}
        or (row.get("owner"), row.get("installation")) in fixture.owners
        for row in before["containers"].values()
    ):
        raise RuntimeError("ROOT_SPIRE_OWNED_CONTAINERS_REMAIN")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "restore"))
    args = parser.parse_args()
    try:
        (prepare if args.operation == "prepare" else restore)()
    except Exception:
        print("ROOT_SPIRE_ROUTE_OPERATION_FAILED")
        return 2
    print("ROOT_SPIRE_ROUTES_" + args.operation.upper() + "D")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
