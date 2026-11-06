"""Create and restore a complete Keycloak owner backup without exposing secrets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parent
SCHEMA_VERSION = 1
MAX_STATE_BYTES = 64 * 1024 * 1024


def execute(arguments: list[str], payload: bytes | None = None) -> bytes:
    result = subprocess.run(
        arguments,
        cwd=ROOT,
        input=payload,
        capture_output=True,
        timeout=600,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            "operator command failed without exposing its output: "
            + " ".join(arguments[:4])
        )
    return result.stdout


def compose(*arguments: str, payload: bytes | None = None) -> bytes:
    return execute(["docker", "compose", *arguments], payload)


def configuration() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(compose("--profile", "*", "config", "--format", "json")),
    )


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def regular_file(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("required regular file is unavailable: " + path.name)
    return path.read_bytes()


def source_map(model: dict[str, Any]) -> tuple[dict[str, Path], Path]:
    services = model["services"]
    keycloak = services["keycloak"]
    provision = services["keycloak-status-provision"]
    sources: dict[str, Path] = {
        "admin-password": Path(model["secrets"]["keycloak-admin-password"]["file"]),
        "database-password": Path(
            model["secrets"]["keycloak-database-password"]["file"]
        ),
    }
    for mount in keycloak["volumes"]:
        target = mount["target"]
        if target == "/opt/keycloak/conf/tls/tls.crt":
            sources["tls.crt"] = Path(mount["source"])
        elif target == "/opt/keycloak/conf/tls/tls.key":
            sources["tls.key"] = Path(mount["source"])
        elif target == "/opt/keycloak/data/import/aelev-realm.json":
            sources["realm.json"] = Path(mount["source"])
    status_root = next(
        Path(mount["source"])
        for mount in provision["volumes"]
        if mount["target"] == "/run/secrets/status"
    )
    if set(sources) != {
        "admin-password",
        "database-password",
        "tls.crt",
        "tls.key",
        "realm.json",
    }:
        raise ValueError("incomplete Keycloak recovery source map")
    return sources, status_root


def write_private(
    path: Path, payload: bytes, *, mode: int = 0o600, group: int | None = None
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        if group is not None:
            os.chown(path, -1, group)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def validated_bundle(directory: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    manifest = json.loads(regular_file(directory / "manifest.json"))
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported Keycloak backup schema")
    payloads: dict[str, bytes] = {}
    entries = manifest.get("files")
    if not isinstance(entries, dict) or not entries:
        raise ValueError("invalid Keycloak backup manifest")
    for name, expected in entries.items():
        if (
            not isinstance(name, str)
            or Path(name).is_absolute()
            or ".." in Path(name).parts
        ):
            raise ValueError("invalid Keycloak backup path")
        payload = regular_file(directory / name)
        if not isinstance(expected, dict) or expected != {
            "sha256": digest(payload),
            "size": len(payload),
        }:
            raise ValueError("Keycloak backup digest mismatch: " + name)
        payloads[name] = payload
    return manifest, payloads


def backup(directory: Path) -> None:
    directory.mkdir(mode=0o700)
    model = configuration()
    sources, status_root = source_map(model)
    database = model["services"]["keycloak-database"]["environment"]
    files: dict[str, bytes] = {
        "database.dump": compose(
            "exec",
            "-T",
            "keycloak-database",
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--no-acl",
            "--username",
            database["POSTGRES_USER"],
            "--dbname",
            database["POSTGRES_DB"],
        ),
        "publisher.sqlite": compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "keycloak-status-publisher",
            "backup",
        ),
    }
    if (
        not files["database.dump"]
        or not 0 < len(files["publisher.sqlite"]) <= MAX_STATE_BYTES
    ):
        raise RuntimeError("empty or oversized Keycloak state backup")
    for name, source in sources.items():
        files["files/" + name] = regular_file(source)
    for source in sorted(status_root.iterdir()):
        if source.name.startswith("."):
            continue
        files["files/status/" + source.name] = regular_file(source)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "module_id": model["services"]["keycloak-status-publisher"]["environment"][
            "AELEV_INFRASTRUCTURE_KEYCLOAK_UI_MODULE_ID"
        ],
        "files": {
            name: {"sha256": digest(payload), "size": len(payload)}
            for name, payload in sorted(files.items())
        },
    }
    for name, payload in files.items():
        write_private(directory / name, payload)
    write_private(
        directory / "manifest.json",
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n",
    )


def restore(directory: Path, *, data_only: bool = False) -> None:
    manifest, payloads = validated_bundle(directory)
    if compose("ps", "-q").strip():
        raise RuntimeError("stop the target Keycloak installation before restore")
    model = configuration()
    sources, status_root = source_map(model)
    module_id = model["services"]["keycloak-status-publisher"]["environment"][
        "AELEV_INFRASTRUCTURE_KEYCLOAK_UI_MODULE_ID"
    ]
    if manifest.get("module_id") != module_id:
        raise ValueError("backup belongs to another Keycloak installation")
    if regular_file(sources["realm.json"]) != payloads["files/realm.json"]:
        raise ValueError("realm artifact differs from the backup")
    secret_gid = int(model["services"]["keycloak"]["group_add"][0])
    status_gid = int(model["services"]["keycloak-status-publisher"]["group_add"][0])
    for name, target in sources.items():
        if name == "realm.json":
            continue
        write_private(
            target,
            payloads["files/" + name],
            mode=0o640 if name.startswith("tls.") else 0o600,
            group=secret_gid if name.startswith("tls.") else None,
        )
    status_root.mkdir(parents=True, exist_ok=True, mode=0o750)
    for name, payload in payloads.items():
        if name.startswith("files/status/"):
            write_private(
                status_root / Path(name).name,
                payload,
                mode=0o640,
                group=status_gid,
            )
    compose("up", "-d", "--wait", "keycloak-database")
    database = model["services"]["keycloak-database"]["environment"]
    tables = compose(
        "exec",
        "-T",
        "keycloak-database",
        "psql",
        "--tuples-only",
        "--no-align",
        "--username",
        database["POSTGRES_USER"],
        "--dbname",
        database["POSTGRES_DB"],
        "--command",
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'",
    ).strip()
    if tables != b"0":
        raise RuntimeError("target Keycloak database is not empty")
    compose(
        "exec",
        "-T",
        "keycloak-database",
        "pg_restore",
        "--single-transaction",
        "--exit-on-error",
        "--no-owner",
        "--no-acl",
        "--username",
        database["POSTGRES_USER"],
        "--dbname",
        database["POSTGRES_DB"],
        payload=payloads["database.dump"],
    )
    compose(
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "keycloak-status-publisher",
        "restore",
        payload=payloads["publisher.sqlite"],
    )
    if data_only:
        compose("up", "-d", "--wait", "keycloak-database", "keycloak")
    else:
        compose("up", "-d", "--wait")


def verify(directory: Path, *, data_only: bool = False) -> None:
    validated_bundle(directory)
    model = configuration()
    database = model["services"]["keycloak-database"]["environment"]
    count = compose(
        "exec",
        "-T",
        "keycloak-database",
        "psql",
        "--tuples-only",
        "--no-align",
        "--username",
        database["POSTGRES_USER"],
        "--dbname",
        database["POSTGRES_DB"],
        "--command",
        "SELECT count(*) FROM realm",
    ).strip()
    if not count.isdigit() or int(count) < 1:
        raise RuntimeError("restored Keycloak realms are unavailable")
    if data_only:
        compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "keycloak-status-publisher",
            "verify-restored",
        )
        return
    compose(
        "exec",
        "-T",
        "keycloak-status-publisher",
        "python",
        "/opt/keycloak-status/status_publisher.py",
        "health",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "restore", "verify"))
    parser.add_argument("directory", type=Path)
    parser.add_argument(
        "--data-only",
        action="store_true",
        help="validate restored data without starting the publisher",
    )
    args = parser.parse_args()
    target = args.directory.resolve()
    if args.action == "backup":
        backup(target)
    elif args.action == "restore":
        restore(target, data_only=args.data_only)
    else:
        verify(target, data_only=args.data_only)


if __name__ == "__main__":
    main()
