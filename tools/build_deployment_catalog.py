#!/usr/bin/env python3
"""Build a public role catalogue from owner Compose recipes, never machine envs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any

import yaml

EXCLUDED = {
    "test",
    "tests",
    "obsolete",
    "examples",
    "node_modules",
    ".venv",
    "runtime",
    "secrets",
}
VARIABLE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)")
OPERATION = re.compile(
    r"(?:-provision|-provisioner|-configuration-init|-data-init|-migrator|-identity-preparer|-dashboard-publisher)$"
)


def compose_tag(loader: Any, node: Any) -> Any:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return loader.construct_scalar(node)


yaml.SafeLoader.add_constructor("!reset", compose_tag)
yaml.SafeLoader.add_constructor("!override", compose_tag)


def build(
    root: Path,
    manifest: Path,
    product: str,
    node_roles: set[str],
    operations: set[str],
    opaque_roles: set[str],
) -> dict[str, Any]:
    owners = json.loads(manifest.read_text())["owners"]
    public_files = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
    )
    files = sorted({root / path.decode() for path in public_files.split(b"\0") if path})
    sources = {
        manifest.relative_to(root).as_posix(): hashlib.sha256(
            manifest.read_bytes()
        ).hexdigest()
    }
    roles: list[dict[str, Any]] = []
    for owner in owners:
        directory = root / owner
        found: dict[str, dict[str, Any]] = {}
        for path in files:
            if (
                not path.is_file()
                or not path.is_relative_to(directory)
                or set(path.relative_to(directory).parts) & EXCLUDED
            ):
                continue
            if not (
                path.name == "compose.yml"
                or path.name.startswith("compose.")
                and path.suffix == ".yml"
            ):
                continue
            if path.is_symlink():
                raise ValueError("catalogue source must be a regular file")
            source = path.relative_to(root).as_posix()
            sources[source] = hashlib.sha256(path.read_bytes()).hexdigest()
            document = yaml.safe_load(path.read_text())
            for name, definition in (document.get("services") or {}).items():
                if not isinstance(definition, dict):
                    continue
                record = found.setdefault(
                    name,
                    {
                        "id": name,
                        "owner": owner,
                        "kind": "node"
                        if name in node_roles
                        else "operation"
                        if name in operations or OPERATION.search(name)
                        else "runtime",
                        "opaque": name in opaque_roles,
                        "local_state": False,
                        "credentials_required": False,
                        "recipes": [],
                        "configuration_variables": set(),
                        "compose_profiles": set(),
                    },
                )
                record["recipes"].append(source)
                for mount in definition.get("volumes") or []:
                    if isinstance(mount, str):
                        read_only = "ro" in mount.rsplit(":", 1)[-1].split(",")
                        target = mount
                    else:
                        read_only = mount.get("read_only") is True
                        target = str(mount.get("target", ""))
                    record["local_state"] |= not read_only
                    record["credentials_required"] |= any(
                        marker in target.lower()
                        for marker in ("secret", "svid", "/certs")
                    )
                record["configuration_variables"].update(
                    VARIABLE.findall(json.dumps(definition))
                )
                record["compose_profiles"].update(definition.get("profiles") or [])
        for record in found.values():
            for key in ("recipes", "configuration_variables", "compose_profiles"):
                record[key] = sorted(record[key])
            roles.append(record)
    known = {role["id"] for role in roles}
    if not (node_roles | operations | opaque_roles) <= known:
        raise ValueError("unknown explicit role classification")
    if len(known) != len(roles):
        raise ValueError("role identifiers must be unique within a catalogue")
    return {
        "schema_version": 1,
        "product": product,
        "sources": dict(sorted(sources.items())),
        "roles": sorted(roles, key=lambda row: row["id"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--product", required=True)
    parser.add_argument("--node-role", action="append", default=[])
    parser.add_argument("--operation", action="append", default=[])
    parser.add_argument("--opaque-role", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    manifest = (root / args.manifest).resolve()
    result = build(
        root,
        manifest,
        args.product,
        set(args.node_role),
        set(args.operation),
        set(args.opaque_role),
    )
    rendered = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    output = root / args.output
    if args.check:
        if output.read_text() != rendered:
            raise SystemExit("DEPLOYMENT_CATALOG_STALE")
    else:
        output.write_text(rendered)
    counts = {
        kind: sum(role["kind"] == kind for role in result["roles"])
        for kind in ("runtime", "node", "operation")
    }
    print(
        json.dumps(
            {
                "status": "CATALOG_VERIFIED" if args.check else "CATALOG_BUILT",
                "roles": len(result["roles"]),
                "kinds": counts,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
