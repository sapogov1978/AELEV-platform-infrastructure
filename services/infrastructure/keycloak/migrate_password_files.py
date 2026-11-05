"""Move this owner's existing dotenv passwords into files without rotation."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import recovery

PREFIX = "AELEV_INFRASTRUCTURE_KEYCLOAK_"
PASSWORDS = {
    "ADMIN_PASSWORD": "admin-password",
    "DATABASE_PASSWORD": "database-password",
}
KEY = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=")


def values(root: Path) -> dict[str, str]:
    """Use Compose's canonical dotenv interpretation, without starting anything."""
    names = [*PASSWORDS, *(name + "_FILE" for name in PASSWORDS), "SECRET_GID"]
    model = {
        "name": "keycloak-secret-migration",
        "services": {
            "probe": {
                "image": "never-executed",
                "environment": {name: "${" + PREFIX + name + "-}" for name in names},
            }
        },
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(root),
            "--env-file",
            str(root / ".env"),
            "-f",
            "-",
            "config",
            "--format",
            "json",
        ],
        input=json.dumps(model),
        text=True,
        capture_output=True,
        timeout=60,
        env={
            key: value
            for key, value in os.environ.items()
            if not key.startswith(PREFIX)
        },
        check=False,
    )
    if result.returncode:
        raise ValueError("DOTENV_RESOLUTION_FAILED")
    data: Any = json.loads(result.stdout)["services"]["probe"]["environment"]
    if not isinstance(data, dict) or any(
        not isinstance(value, str) for value in data.values()
    ):
        raise ValueError("DOTENV_VALUE_INVALID")
    return {str(key): str(value) for key, value in data.items()}


def plan(
    root: Path, source: bytes, resolved: dict[str, str]
) -> tuple[bytes, dict[Path, bytes], int]:
    lines = source.decode("utf-8").splitlines(keepends=True)
    tracked = {PREFIX + name for name in resolved}
    seen: set[str] = set()
    kept: list[str] = []
    for line in lines:
        match = KEY.match(line)
        name = match[1] if match else None
        if name in tracked:
            if name in seen:
                raise ValueError("DUPLICATE_MIGRATION_VARIABLE")
            seen.add(name)
        if name in {PREFIX + item for item in PASSWORDS}:
            literal = line.split("=", 1)[1].strip()
            # Refuse multiline dotenv values rather than leaving a fragment behind.
            if literal.startswith(("'", '"')) and literal.count(literal[0]) < 2:
                raise ValueError("MULTILINE_SECRET_REQUIRES_EXPLICIT_MIGRATION")
            if literal.endswith("\\"):
                raise ValueError("CONTINUED_SECRET_REQUIRES_EXPLICIT_MIGRATION")
            continue
        kept.append(line)
    for name in ("ADMIN_PASSWORD_FILE", "DATABASE_PASSWORD_FILE", "SECRET_GID"):
        if PREFIX + name in seen and not resolved.get(name):
            raise ValueError("EMPTY_FILE_DELIVERY_SETTING")
    directory = root / "secrets"
    if directory.is_symlink():
        raise ValueError("SECRET_DIRECTORY_SYMLINK_FORBIDDEN")
    gid = int(resolved.get("SECRET_GID") or os.getgid())
    if gid < 0:
        raise ValueError("SECRET_GID_INVALID")
    files: dict[Path, bytes] = {}
    append: dict[str, str] = {}
    for name, filename in PASSWORDS.items():
        setting = resolved.get(name + "_FILE") or "./secrets/" + filename
        path = root / setting
        if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError("SECRET_PATH_OUTSIDE_OWNER_DIRECTORY")
        if path.parent.resolve() != directory.resolve():
            raise ValueError("SECRET_PATH_MUST_USE_OWNER_DIRECTORY")
        value = resolved.get(name, "")
        payload = value.encode() if value else recovery.regular_file(path)
        if not payload or any(
            character in payload for character in (b"\r", b"\n", b"\x00")
        ):
            raise ValueError("SECRET_VALUE_INVALID")
        if path.exists() and recovery.regular_file(path) != payload:
            raise ValueError("EXISTING_SECRET_MISMATCH")
        files[path] = payload
        if PREFIX + name + "_FILE" not in seen:
            append[PREFIX + name + "_FILE"] = setting
    if PREFIX + "SECRET_GID" not in seen:
        append[PREFIX + "SECRET_GID"] = str(gid)
    if len(files) != len(PASSWORDS):
        raise ValueError("SEPARATE_SECRET_FILES_REQUIRED")
    rewritten = "".join(kept)
    if append:
        rewritten = (
            rewritten.rstrip("\r\n")
            + "\n"
            + "".join(f"{key}={value}\n" for key, value in append.items())
        )
    return rewritten.encode(), files, gid


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def migrate(root: Path, apply: bool) -> dict[str, Any]:
    env = root / ".env"
    source = recovery.regular_file(env)
    rewritten, files, gid = plan(root, source, values(root))
    if apply:
        for path, payload in files.items():
            if not path.exists():
                recovery.write_private(path, payload, mode=0o640, group=gid)
            else:
                if recovery.regular_file(path) != payload:
                    raise ValueError("EXISTING_SECRET_CHANGED")
                os.chown(path, -1, gid)
                path.chmod(0o640)
        # Make the secret filenames durable before dropping their env copies.
        for directory in {path.parent for path in files}:
            sync_directory(directory)
        sync_directory(root)
        if recovery.regular_file(env) != source:
            raise ValueError("DOTENV_CHANGED_CONCURRENTLY")
        descriptor, temporary = tempfile.mkstemp(prefix=".env-migration-", dir=root)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(rewritten)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, env)
            sync_directory(root)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return {
        "event": "password_file_migration",
        "applied": apply,
        "passwords": len(files),
        "dotenv_changed": rewritten != source,
        "rotated": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        sys.stdout.write(json.dumps(migrate(recovery.ROOT, args.apply)) + "\n")
    except (ValueError, OSError, subprocess.SubprocessError):
        # Never expose dotenv, commands, secret values or Compose's diagnostics.
        sys.stderr.write("PASSWORD_FILE_MIGRATION_FAILED_WITHOUT_SECRET_OUTPUT\n")
        sys.exit(1)
