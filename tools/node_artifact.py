"""Deliver pinned source archives or explicitly scoped private inputs to a node."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
from typing import Any, BinaryIO
import zipfile

ARCHIVE_LIMIT = 256 * 1024 * 1024
HEADER_LIMIT = 4 * 1024 * 1024
BOOTSTRAP = """import base64,hashlib,json,sys
v=json.loads(sys.stdin.buffer.readline(4194305))
c=base64.b64decode(v['code'],validate=True)
if hashlib.sha256(c).hexdigest()!=v['request']['receiver_sha256']:
 raise ValueError('ARTIFACT_RECEIVER_MISMATCH')
n={'__name__':'artifact_carrier','__receiver_sha256__':hashlib.sha256(c).hexdigest()}
exec(compile(c,'artifact-carrier.py','exec'),n)
try:
 r=n['receive'](v['request'],sys.stdin.buffer)
except n['DeliveryError'] as e:
 r={'status':'BLOCKED','code':str(e),'request_sha256':n['digest'](n['encode'](v['request']))}
except (OSError,ValueError,KeyError,TypeError,n['zipfile'].BadZipFile):
 r={'status':'BLOCKED','code':'ARTIFACT_INPUT_OR_ENVIRONMENT_INVALID','request_sha256':n['digest'](n['encode'](v['request']))}
print(json.dumps(r))
"""


class DeliveryError(ValueError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise DeliveryError(code)


def encode(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_hash(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(part)
    return result.hexdigest()


def absolute(value: Any, *, inspect: bool = True) -> Path:
    require(
        isinstance(value, str) and not any(c in value for c in "\0\r\n"),
        "ARTIFACT_PATH_INVALID",
    )
    result = Path(value)
    require(result.is_absolute() and ".." not in result.parts, "ARTIFACT_PATH_INVALID")
    require(
        not inspect or not any(p.is_symlink() for p in [result, *result.parents]),
        "ARTIFACT_SYMLINK_REFUSED",
    )
    return result


def relative(value: Any) -> str:
    require(
        isinstance(value, str)
        and value
        and "\\" not in value
        and not any(c in value for c in "\0\r\n"),
        "ARTIFACT_MEMBER_INVALID",
    )
    path = PurePosixPath(value)
    require(
        bool(path.parts)
        and not path.is_absolute()
        and ".." not in path.parts
        and str(path) == value
        and path.parts[0] not in {".git", ".codex", "graphify-out"},
        "ARTIFACT_MEMBER_INVALID",
    )
    return value


def sha(value: Any) -> str:
    require(
        isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None,
        "ARTIFACT_SHA256_INVALID",
    )
    return value


def sync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def save(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(encode(value) + b"\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        sync(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load(path: Path) -> dict[str, Any]:
    require(path.stat().st_size <= HEADER_LIMIT, "ARTIFACT_MANIFEST_TOO_LARGE")

    def unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            require(key not in result, "ARTIFACT_DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    result = json.loads(path.read_bytes(), object_pairs_hook=unique)
    require(isinstance(result, dict), "ARTIFACT_OBJECT_REQUIRED")
    return result


def validate(request: dict[str, Any], *, inspect: bool) -> tuple[Path, dict[str, Any]]:
    require(
        set(request)
        == {
            "schema_version",
            "scope",
            "root",
            "kind",
            "manifest",
            "manifest_sha256",
            "archive_size",
            "receiver_sha256",
        },
        "ARTIFACT_REQUEST_FIELDS_INVALID",
    )
    require(
        type(request["schema_version"]) is int and request["schema_version"] == 1,
        "ARTIFACT_VERSION_INVALID",
    )
    require(
        isinstance(request["scope"], str)
        and re.fullmatch(r"[a-z][a-z0-9_.-]{0,95}", request["scope"]) is not None,
        "ARTIFACT_SCOPE_INVALID",
    )
    root = absolute(request["root"], inspect=inspect)
    require(
        type(request["archive_size"]) is int
        and 0 < request["archive_size"] <= ARCHIVE_LIMIT,
        "ARTIFACT_SIZE_INVALID",
    )
    require(request["kind"] in {"source", "owner-input"}, "ARTIFACT_KIND_INVALID")
    manifest = request["manifest"]
    require(
        isinstance(manifest, dict)
        and digest(encode(manifest)) == sha(request["manifest_sha256"]),
        "ARTIFACT_MANIFEST_MISMATCH",
    )
    sha(manifest["archive_sha256"])
    sha(request["receiver_sha256"])
    if request["kind"] == "source":
        require(
            type(manifest["schema_version"]) is int and manifest["schema_version"] == 2,
            "ARTIFACT_SOURCE_FORMAT_INVALID",
        )
        require(
            isinstance(manifest.get("repository"), str)
            and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", manifest["repository"])
            is not None
            and re.fullmatch(r"[a-f0-9]{40}", manifest.get("source_commit", ""))
            is not None,
            "ARTIFACT_SOURCE_IDENTITY_INVALID",
        )
    else:
        require(
            set(manifest) == {"schema_version", "scope", "archive_sha256", "files"}
            and type(manifest["schema_version"]) is int
            and manifest["schema_version"] == 1
            and manifest["scope"] == request["scope"],
            "ARTIFACT_INPUT_SCOPE_INVALID",
        )
    files = manifest["files"]
    require(
        isinstance(files, dict)
        and 0 < len(files) <= (10000 if request["kind"] == "source" else 128),
        "ARTIFACT_FILES_INVALID",
    )
    for name, record in files.items():
        relative(name)
        require(isinstance(record, dict), "ARTIFACT_FILE_RECORD_INVALID")
        require(
            set(record)
            == ({"sha256", "text"} if request["kind"] == "source" else {"sha256"}),
            "ARTIFACT_FILE_RECORD_INVALID",
        )
        sha(record["sha256"])
        if request["kind"] == "source":
            require(type(record["text"]) is bool, "ARTIFACT_FILE_RECORD_INVALID")
        require(
            not any(
                str(parent) in files
                for parent in PurePosixPath(name).parents
                if str(parent) != "."
            ),
            "ARTIFACT_MEMBER_COLLISION",
        )
    return root, files


def canonical(data: bytes, record: dict[str, Any], kind: str) -> bytes:
    if kind == "source" and record["text"]:
        data.decode("utf-8")
        require(b"\0" not in data, "ARTIFACT_TEXT_INVALID")
        return data.replace(b"\r\n", b"\n")
    return data


def archive_modes(archive: Path, files: dict[str, Any], kind: str) -> dict[str, int]:
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        require(
            len(members) == len(files)
            and len({m.filename for m in members}) == len(members)
            and {m.filename for m in members} == set(files),
            "ARTIFACT_MEMBER_SET_INVALID",
        )
        modes = {}
        for member in members:
            mode = member.external_attr >> 16
            require(
                stat.S_ISREG(mode)
                and member.compress_type == zipfile.ZIP_STORED
                and not member.flag_bits & 1,
                "ARTIFACT_MEMBER_TYPE_INVALID",
            )
            require(
                member.file_size <= (64 * 1024 * 1024 if kind == "source" else 65536),
                "ARTIFACT_MEMBER_TOO_LARGE",
            )
            permissions = stat.S_IMODE(mode)
            require(
                permissions in ({0o644, 0o755} if kind == "source" else {0o600}),
                "ARTIFACT_MEMBER_PERMISSIONS_INVALID",
            )
            modes[member.filename] = permissions
        return modes


def verify_tree(
    root: Path, files: dict[str, Any], kind: str, modes: dict[str, int]
) -> None:
    require(root.is_dir() and not root.is_symlink(), "ARTIFACT_TREE_MISSING")
    directory_mode = 0o755 if kind == "source" else 0o700
    require(
        stat.S_IMODE(root.stat().st_mode) == directory_mode,
        "ARTIFACT_TREE_PERMISSIONS_INVALID",
    )
    present = set()
    directories = set()
    expected_directories = {
        str(p) for name in files for p in PurePosixPath(name).parents if str(p) != "."
    }
    for path in root.rglob("*"):
        require(not path.is_symlink(), "ARTIFACT_SYMLINK_REFUSED")
        if path.is_dir():
            require(
                stat.S_IMODE(path.stat().st_mode) == directory_mode,
                "ARTIFACT_TREE_PERMISSIONS_INVALID",
            )
            directories.add(path.relative_to(root).as_posix())
            continue
        name = path.relative_to(root).as_posix()
        require(path.is_file() and name in files, "ARTIFACT_TREE_DRIFT")
        mode = stat.S_IMODE(path.stat().st_mode)
        require(
            mode == modes[name],
            "ARTIFACT_TREE_PERMISSIONS_INVALID",
        )
        require(file_hash(path) == files[name]["sha256"], "ARTIFACT_TREE_DRIFT")
        present.add(name)
    require(present == set(files), "ARTIFACT_TREE_DRIFT")
    require(directories == expected_directories, "ARTIFACT_TREE_DRIFT")


def receive(request: dict[str, Any], stream: BinaryIO) -> dict[str, Any]:
    root, files = validate(request, inspect=True)
    module_hash = (
        globals()["__receiver_sha256__"]
        if "__receiver_sha256__" in globals()
        else file_hash(Path(__file__))
    )
    require(module_hash == request["receiver_sha256"], "ARTIFACT_RECEIVER_MISMATCH")
    created = [p for p in [root, *root.parents] if not p.exists()]
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(
        root.stat().st_uid == os.getuid()
        and stat.S_IMODE(root.stat().st_mode) == 0o700,
        "ARTIFACT_PRIVATE_ROOT_REQUIRED",
    )
    for path in created:
        sync(path)
        sync(path.parent)
    lock = root / ".delivery.lock"
    require(not lock.is_symlink(), "ARTIFACT_SYMLINK_REFUSED")
    with lock.open("ab") as held:
        os.chmod(lock, 0o600)
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise DeliveryError("ARTIFACT_DELIVERY_BUSY") from error
        attempt = Path(tempfile.mkdtemp(prefix=".attempt-", dir=root))
        sync(root)
        archive = attempt / "archive.zip"
        remaining = request["archive_size"]
        with archive.open("xb") as output:
            os.chmod(archive, 0o600)
            while remaining:
                data = stream.read(min(remaining, 1024 * 1024))
                require(bool(data), "ARTIFACT_STREAM_TRUNCATED")
                output.write(data)
                remaining -= len(data)
            require(stream.read(1) == b"", "ARTIFACT_STREAM_TRAILING_BYTES")
            output.flush()
            os.fsync(output.fileno())
        require(
            file_hash(archive) == request["manifest"]["archive_sha256"],
            "ARTIFACT_ARCHIVE_MISMATCH",
        )
        modes = archive_modes(archive, files, request["kind"])
        destination = root / (request["scope"] + "-" + request["manifest_sha256"])
        receipt = destination / "receipt.json"
        require(not destination.is_symlink(), "ARTIFACT_SYMLINK_REFUSED")
        result = {
            "schema_version": 1,
            "status": "ARTIFACT_MATERIALIZED",
            "kind": request["kind"],
            "scope": request["scope"],
            "request_sha256": digest(encode(request)),
            "manifest_sha256": request["manifest_sha256"],
            "archive_sha256": request["manifest"]["archive_sha256"],
            "files": len(files),
            "path": str(destination / "payload"),
            "owner_readiness_verified": False,
            "physical_distribution_verified": False,
            "queue_resume_issued": False,
        }
        if destination.exists():
            require(
                receipt.is_file() and not receipt.is_symlink(),
                "ARTIFACT_GENERATION_CONFLICT",
            )
            prior = load(receipt)
            require(
                set(prior) == set(result)
                and all(
                    prior[key] == value
                    for key, value in result.items()
                    if key != "request_sha256"
                ),
                "ARTIFACT_GENERATION_CONFLICT",
            )
            verify_tree(destination / "payload", files, request["kind"], modes)
            save(attempt / "receipt.json", {**result, "repeat_verified": True})
            return result
        payload = attempt / "payload"
        payload.mkdir(mode=0o700)
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            for member in members:
                name = relative(member.filename)
                permissions = modes[name]
                data = canonical(bundle.read(member), files[name], request["kind"])
                require(
                    digest(data) == files[name]["sha256"], "ARTIFACT_MEMBER_MISMATCH"
                )
                path = payload / name
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with path.open("xb") as output:
                    os.chmod(path, permissions)
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
        for path in sorted(
            [p for p in payload.rglob("*") if p.is_dir()],
            key=lambda p: len(p.parts),
            reverse=True,
        ):
            os.chmod(path, 0o755 if request["kind"] == "source" else 0o700)
            sync(path)
        os.chmod(payload, 0o755 if request["kind"] == "source" else 0o700)
        sync(payload)
        verify_tree(payload, files, request["kind"], modes)
        save(attempt / "receipt.json", result)
        os.rename(attempt, destination)
        sync(root)
        return result


def prepare_request(
    kind: str, scope: str, root: str, manifest: dict[str, Any], archive: Path
) -> dict[str, Any]:
    request = {
        "schema_version": 1,
        "scope": scope,
        "root": root,
        "kind": kind,
        "manifest": manifest,
        "manifest_sha256": digest(encode(manifest)),
        "archive_size": archive.stat().st_size,
        "receiver_sha256": file_hash(Path(__file__)),
    }
    validate(request, inspect=False)
    require(
        file_hash(archive) == manifest["archive_sha256"],
        "ARTIFACT_LOCAL_ARCHIVE_MISMATCH",
    )
    return request


def pack_inputs(
    root: Path, names: list[str], scope: str, output: Path
) -> dict[str, Any]:
    absolute(str(root.absolute()))
    absolute(str(output.absolute()))
    require(
        not output.absolute().is_relative_to(Path(__file__).resolve().parents[1]),
        "ARTIFACT_PRIVATE_ARCHIVE_OUTSIDE_SOURCE_REQUIRED",
    )
    require(
        root.is_dir()
        and root.stat().st_uid == os.getuid()
        and stat.S_IMODE(root.stat().st_mode) == 0o700,
        "ARTIFACT_PRIVATE_ROOT_REQUIRED",
    )
    require(
        isinstance(names, list)
        and 0 < len(names) <= 128
        and len(set(names)) == len(names),
        "ARTIFACT_FILES_INVALID",
    )
    require(
        isinstance(scope, str)
        and re.fullmatch(r"[a-z][a-z0-9_.-]{0,95}", scope) is not None,
        "ARTIFACT_SCOPE_INVALID",
    )
    records = {}
    for name in names:
        path = absolute(str(root / relative(name)))
        mode = path.stat().st_mode
        require(
            stat.S_ISREG(mode)
            and path.stat().st_uid == os.getuid()
            and stat.S_IMODE(mode) == 0o600,
            "ARTIFACT_PRIVATE_INPUT_REQUIRED",
        )
        require(path.stat().st_size <= 65536, "ARTIFACT_MEMBER_TOO_LARGE")
        body = path.read_bytes()
        require(len(body) <= 65536, "ARTIFACT_MEMBER_TOO_LARGE")
        records[name] = body
    descriptor = os.open(output, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w+b") as stream:
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, body in sorted(records.items()):
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                archive.writestr(info, body)
    sync(output)
    sync(output.parent)
    return {
        "schema_version": 1,
        "scope": scope,
        "archive_sha256": file_hash(output),
        "files": {name: {"sha256": digest(body)} for name, body in records.items()},
    }


def deliver(
    binding: dict[str, Any], request: dict[str, Any], archive: Path
) -> dict[str, Any]:
    from tools import node_execution as execution

    validate(request, inspect=binding == {"kind": "local"})
    absolute(str(archive.absolute()))
    if request["kind"] == "owner-input":
        execution.private(archive)
    require(
        archive.is_file()
        and not archive.is_symlink()
        and archive.stat().st_size == request["archive_size"]
        and file_hash(archive) == request["manifest"]["archive_sha256"],
        "ARTIFACT_LOCAL_ARCHIVE_MISMATCH",
    )
    if binding == {"kind": "local"}:
        with archive.open("rb") as stream:
            return receive(request, stream)
    command = execution.ssh_command(binding, program=BOOTSTRAP)
    evidence = absolute(binding["evidence_root"])
    evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    execution.private(evidence, directory=True)
    attempt = Path(tempfile.mkdtemp(prefix="artifact-", dir=evidence))
    wire = attempt / "request.bin"
    header = (
        encode(
            {
                "request": request,
                "code": base64.b64encode(Path(__file__).read_bytes()).decode(),
            }
        )
        + b"\n"
    )
    require(len(header) <= HEADER_LIMIT, "ARTIFACT_MANIFEST_TOO_LARGE")
    with wire.open("xb") as output, archive.open("rb") as source:
        os.chmod(wire, 0o600)
        output.write(header)
        for part in iter(lambda: source.read(1024 * 1024), b""):
            output.write(part)
        output.flush()
        os.fsync(output.fileno())
    try:
        with (
            wire.open("rb") as source,
            (attempt / "stdout").open("xb") as output,
            (attempt / "stderr").open("xb") as error,
        ):
            os.chmod(attempt / "stdout", 0o600)
            os.chmod(attempt / "stderr", 0o600)
            result = subprocess.run(
                command,
                stdin=source,
                stdout=output,
                stderr=error,
                timeout=600,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        save(attempt / "receipt.json", {"status": "ARTIFACT_TRANSPORT_RESULT_UNKNOWN"})
        raise DeliveryError("ARTIFACT_TRANSPORT_RESULT_UNKNOWN") from error
    save(
        attempt / "receipt.json",
        {
            "status": "TRANSPORT_EXITED",
            "exit_code": result.returncode,
            "request_sha256": digest(encode(request)),
        },
    )
    require(result.returncode == 0, "ARTIFACT_TRANSPORT_RESULT_UNKNOWN")
    require((attempt / "stdout").stat().st_size <= 65536, "ARTIFACT_REPLY_INVALID")
    reply = load(attempt / "stdout")
    require(
        reply.get("request_sha256") == digest(encode(request)), "ARTIFACT_REPLY_INVALID"
    )
    return reply


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        for path in (args.binding, args.request):
            absolute(str(path.absolute()))
            require(
                path.stat().st_uid == os.getuid()
                and stat.S_IMODE(path.stat().st_mode) == 0o600,
                "ARTIFACT_PRIVATE_CONTROLLER_INPUT_REQUIRED",
            )
        result = deliver(load(args.binding), load(args.request), args.archive)
    except DeliveryError as error:
        print(json.dumps({"status": "BLOCKED", "code": str(error)}))
        return 2
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
        print(
            json.dumps(
                {"status": "BLOCKED", "code": "ARTIFACT_INPUT_OR_ENVIRONMENT_INVALID"}
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "ARTIFACT_MATERIALIZED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
