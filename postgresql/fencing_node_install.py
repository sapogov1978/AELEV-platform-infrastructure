"""Install one fixed PostgreSQL node operation; never reset its fencing history."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shlex
import ssl
import stat
import struct
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from tools import node_execution as node
from tools import owner_compose as owner

ROLE = "platform-infrastructure/postgresql-fencing-node-install"
DESCRIPTOR = "postgresql/fencing-node-installation.json"
ENTRYPOINT = "postgresql/fencing_node_install.py"
LAUNCHER = "postgresql/fencing-node.py"
IMAGE = "ghcr.io/sapogov1978/aelev-platform-infrastructure-postgresql-fencing@sha256:cef0751b964641f9563c9b7b4ca61bf1cae616deea5b85f857a3b5d39a573995"
CREDENTIALS = {"etcd-ca.crt", "etcd-client.crt", "etcd-client.key"}
FIELDS = {
    "schema_version",
    "executor_root",
    "executor_files",
    "owner_root",
    "owner_files",
    "catalog",
    "role",
    "image",
    "node_configuration_file",
    "capability_file",
    "credentials",
    "runtime_public_keys",
    "inputs",
}
CAPABILITY_FIELDS = {
    "schema_version",
    "cluster",
    "node",
    "daemon_id",
    "platform",
    "installation_root",
    "ssh_user",
    "authorized_keys_file",
    "authorized_keys_sha256",
    "host_key_sha256",
}
LABEL = "io.aelev.postgresql."
DOCKER = ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock"]
ENVIRONMENT = {"PATH": "/usr/bin:/bin", "LANG": "C", "DOCKER_CONFIG": "/nonexistent"}
sys.dont_write_bytecode = True


def require(value: bool, code: str) -> None:
    node.require(value, "OWNER_POSTGRESQL_NODE_" + code)


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def protected(path: Path, *, limit: int = 65536, empty: bool = False) -> bytes:
    node.absolute(str(path))
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "REGULAR_INPUT_REQUIRED")
    require(
        info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600,
        "PRIVATE_INPUT_REQUIRED",
    )
    require((empty or info.st_size > 0) and info.st_size <= limit, "INPUT_SIZE_INVALID")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        body = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_uid",
        "st_gid",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    require(
        all(
            getattr(info, key) == getattr(before, key) == getattr(after, key)
            for key in fields
        ),
        "INPUT_CHANGED",
    )
    require(len(body) == info.st_size, "INPUT_CHANGED")
    return body


def directory(path: Path) -> None:
    node.absolute(str(path))
    node.private(path, directory=True)
    require(stat.S_IMODE(path.stat().st_mode) == 0o700, "PRIVATE_DIRECTORY_REQUIRED")


@contextmanager
def authorization_lock(value: Definition) -> Iterator[None]:
    parent = Path(value.capability["authorized_keys_file"]).parent
    directory(parent)
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def public_key(body: bytes) -> str:
    try:
        text = body.decode("ascii").strip()
        words = text.split()
        require(
            "\n" not in text and "\r" not in text and len(words) in {2, 3},
            "PUBLIC_KEY_INVALID",
        )
        require(words[0] == "ssh-ed25519", "PUBLIC_KEY_INVALID")
        raw = base64.b64decode(words[1], validate=True)
        require(
            len(raw) == 51
            and raw[:4] == struct.pack(">I", 11)
            and raw[4:15] == b"ssh-ed25519"
            and raw[15:19] == struct.pack(">I", 32),
            "PUBLIC_KEY_INVALID",
        )
    except (UnicodeError, ValueError, IndexError) as error:
        raise node.ExecutionError("OWNER_POSTGRESQL_NODE_PUBLIC_KEY_INVALID") from error
    return "ssh-ed25519 " + words[1]


def descriptor(value: dict[str, Any]) -> None:
    require(
        type(value.get("schema_version")) is int
        and value
        == {
            "schema_version": 1,
            "id": "postgresql-fencing-node-install",
            "owner": "postgresql",
            "kind": "operation",
            "procedure": "postgresql-fencing-node-install",
            "entrypoint": ENTRYPOINT,
            "actions": ["plan", "task", "status"],
            "helper_image": IMAGE,
        },
        "DESCRIPTOR_INVALID",
    )


@dataclass(frozen=True)
class Definition:
    value: dict[str, Any]
    root: Path
    launcher: Path
    configuration: dict[str, Any]
    configuration_bytes: bytes
    capability: dict[str, Any]
    installation: Path
    keys: tuple[str, ...]
    credentials: dict[str, bytes]
    configuration_sha256: str
    launcher_sha256: str
    inputs: dict[str, bytes]

    def unchanged(self) -> None:
        for name, body in self.inputs.items():
            require(protected(Path(name)) == body, "INPUT_CHANGED")
        require(
            sha(self.launcher.read_bytes()) == self.launcher_sha256, "SOURCE_CHANGED"
        )

    def forced_lines(self) -> bytes:
        argv = [
            sys.executable,
            str(self.installation / "fencing-node.py"),
            "--config",
            str(self.installation / "node.json"),
        ]
        command = shlex.join(argv).replace("\\", "\\\\").replace('"', '\\"')
        return (
            "\n".join('restrict,command="' + command + '" ' + key for key in self.keys)
            + "\n"
        ).encode()


def definition(value: dict[str, Any]) -> Definition:
    require(
        set(value) == FIELDS
        and type(value["schema_version"]) is int
        and value["schema_version"] == 1,
        "REQUEST_INVALID",
    )
    executor, executor_files = owner.source_tree(
        value["executor_root"], value["executor_files"]
    )
    root, files = owner.source_tree(value["owner_root"], value["owner_files"])
    require(
        all(
            name in executor_files
            for name in ("tools/node_execution.py", "tools/owner_compose.py")
        ),
        "EXECUTOR_REQUIRED",
    )
    require(
        value["catalog"] == "deployment-catalog.json"
        and all(
            name in files
            for name in (ENTRYPOINT, LAUNCHER, DESCRIPTOR, value["catalog"])
        ),
        "SOURCE_REQUIRED",
    )
    require(sha(Path(__file__).read_bytes()) == files[ENTRYPOINT], "EXECUTOR_MISMATCH")
    require(value["role"] == ROLE and value["image"] == IMAGE, "ROLE_OR_IMAGE_INVALID")
    descriptor(node.read(root / DESCRIPTOR))
    catalog = node.read(root / value["catalog"])
    roles = [
        row for row in catalog["roles"] if catalog["product"] + "/" + row["id"] == ROLE
    ]
    require(
        len(roles) == 1
        and roles[0]["owner"] == "postgresql"
        and roles[0]["kind"] == "operation"
        and roles[0]["recipes"] == [DESCRIPTOR]
        and catalog["sources"].get(DESCRIPTOR) == files[DESCRIPTOR],
        "CATALOG_INVALID",
    )
    require(
        isinstance(value["credentials"], dict)
        and set(value["credentials"]) == CREDENTIALS,
        "CREDENTIALS_INVALID",
    )
    key_files = value["runtime_public_keys"]
    require(
        isinstance(key_files, list)
        and 1 <= len(key_files) <= 16
        and all(isinstance(name, str) for name in key_files)
        and len(set(key_files)) == len(key_files),
        "PUBLIC_KEYS_INVALID",
    )
    names = [
        value["node_configuration_file"],
        value["capability_file"],
        *value["credentials"].values(),
        *key_files,
    ]
    require(
        all(isinstance(name, str) for name in names) and len(set(names)) == len(names),
        "INPUT_SET_INVALID",
    )
    require(
        isinstance(value["inputs"], dict) and set(value["inputs"]) == set(names),
        "INPUT_SET_INVALID",
    )
    inputs = {name: protected(node.absolute(name)) for name in names}
    require(
        all(sha(body) == value["inputs"][name] for name, body in inputs.items()),
        "INPUT_HASH_MISMATCH",
    )
    context = ssl.create_default_context(cafile=value["credentials"]["etcd-ca.crt"])
    context.load_cert_chain(
        value["credentials"]["etcd-client.crt"], value["credentials"]["etcd-client.key"]
    )
    spec = importlib.util.spec_from_file_location(
        "postgresql_node_launcher", root / LAUNCHER
    )
    require(spec is not None and spec.loader is not None, "LAUNCHER_REQUIRED")
    assert spec is not None and spec.loader is not None
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    configuration = launcher.configuration(Path(value["node_configuration_file"]))
    require(
        configuration.get("image") == IMAGE and "secrets_volume" in configuration,
        "CONFIGURATION_INVALID",
    )
    capability = node.read(Path(value["capability_file"]))
    require(
        set(capability) == CAPABILITY_FIELDS
        and type(capability["schema_version"]) is int
        and capability["schema_version"] == 1,
        "CAPABILITY_INVALID",
    )
    require(
        all(
            capability[key] == configuration[key]
            for key in ("cluster", "node", "daemon_id")
        ),
        "CAPABILITY_BINDING_MISMATCH",
    )
    require(
        capability["platform"] in {"linux/amd64", "linux/arm64"}
        and isinstance(capability["ssh_user"], str)
        and re.fullmatch(r"[a-z_][a-z0-9_-]{0,63}", capability["ssh_user"]) is not None,
        "CAPABILITY_INVALID",
    )
    require(
        all(
            isinstance(capability[key], str)
            and re.fullmatch(r"[0-9a-f]{64}", capability[key]) is not None
            for key in ("authorized_keys_sha256", "host_key_sha256")
        ),
        "CAPABILITY_INVALID",
    )
    installation = node.absolute(capability["installation_root"])
    authorized = node.absolute(capability["authorized_keys_file"])
    require(
        not any(
            ord(character) < 32 or ord(character) == 127
            for path in (installation, authorized)
            for character in str(path)
        ),
        "PATH_CONTROL_CHARACTER",
    )
    require(authorized.name == "authorized_keys", "KEY_DESTINATION_INVALID")
    directory(installation.parent)
    directory(authorized.parent)
    protected(authorized, limit=1024**2, empty=True)
    for path in [executor, root, *(Path(name) for name in names), authorized.parent]:
        require(
            not installation.is_relative_to(path)
            and not path.is_relative_to(installation),
            "INSTALLATION_SCOPE_CONFLICT",
        )
    keys = tuple(public_key(inputs[name]) for name in key_files)
    require(len(set(keys)) == len(keys), "PUBLIC_KEYS_INVALID")
    result = Definition(
        value,
        root,
        root / LAUNCHER,
        configuration,
        inputs[value["node_configuration_file"]],
        capability,
        installation,
        keys,
        {name: inputs[path] for name, path in value["credentials"].items()},
        sha(canonical(value)),
        files[LAUNCHER],
        inputs,
    )
    result.unchanged()
    return result


COPY = """import base64,json,os
from pathlib import Path
v=json.load(__import__('sys').stdin);r=Path('/prepared')
assert set(v)=={'etcd-ca.crt','etcd-client.crt','etcd-client.key'} and not any(r.iterdir())
os.chmod(r,0o700)
for name,encoded in v.items():
 data=base64.b64decode(encoded,validate=True);assert 0<len(data)<=65536
 fd=os.open(r/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
fd=os.open(r,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
"""
READ = """import hashlib,json,os,stat
from pathlib import Path
r=Path('/prepared');v={}
for p in r.iterdir():
 s=p.lstat()
 if not stat.S_ISREG(s.st_mode):continue
 if p.name not in {'etcd-ca.crt','etcd-client.crt','etcd-client.key','node-binding.json'}:continue
 assert s.st_nlink==1 and s.st_size<=65536
 v[p.name]={'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'uid':s.st_uid,'gid':s.st_gid,'mode':stat.S_IMODE(s.st_mode)}
print(json.dumps({'mode':stat.S_IMODE(r.stat().st_mode),'uid':r.stat().st_uid,'entries':sorted(p.name for p in r.iterdir()),'files':v}))
"""


class Backend:
    def command(
        self,
        arguments: list[str],
        *,
        body: bytes | None = None,
        effectful: bool = False,
    ) -> bytes:
        try:
            result = subprocess.run(
                [*DOCKER, *arguments],
                input=body,
                capture_output=True,
                timeout=90,
                env=ENVIRONMENT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise node.ExecutionError(
                "OWNER_POSTGRESQL_NODE_DOCKER_UNCONFIRMED"
            ) from error
        require(
            result.returncode == 0
            and len(result.stdout) <= 1024**2
            and len(result.stderr) <= 1024**2,
            "DOCKER_UNCONFIRMED",
        )
        return result.stdout

    def validate(self, value: Definition) -> dict[str, Any]:
        engine = json.loads(self.command(["info", "--format", "{{json .}}"]))
        image = json.loads(self.command(["image", "inspect", IMAGE]))[0]
        architecture = {"aarch64": "arm64", "x86_64": "amd64"}.get(
            engine["Architecture"], engine["Architecture"]
        )
        require(engine["ID"] == value.configuration["daemon_id"], "DAEMON_CHANGED")
        require(
            image["Os"] == "linux"
            and image["Architecture"] == architecture
            and value.capability["platform"] == "linux/" + architecture,
            "NATIVE_IMAGE_REQUIRED",
        )
        return {
            "daemon_id": engine["ID"],
            "native_image_id": image["Id"],
            "platform": "linux/" + architecture,
        }

    def volume(self, name: str) -> dict[str, Any] | None:
        names = self.command(["volume", "ls", "--quiet"]).decode().splitlines()
        return (
            json.loads(self.command(["volume", "inspect", name]))[0]
            if name in names
            else None
        )

    def create(self, value: Definition, name: str, purpose: str) -> None:
        require(self.volume(name) is None, "VOLUME_ALREADY_EXISTS")
        arguments = ["volume", "create"]
        for key, content in {
            "cluster": value.configuration["cluster"],
            "node": value.configuration["node"],
            "fencing-resource": purpose,
            "installation": value.configuration_sha256,
        }.items():
            arguments += ["--label", LABEL + key + "=" + content]
        self.command([*arguments, name], effectful=True)

    def materialize(self, value: Definition) -> None:
        body = json.dumps(
            {
                name: base64.b64encode(raw).decode()
                for name, raw in value.credentials.items()
            }
        ).encode()
        self.command(
            [
                "run",
                "--rm",
                "--interactive",
                "--pull",
                "never",
                "--network",
                "none",
                "--label",
                "io.aelev.postgresql.operation=node-install-credentials",
                "--label",
                "io.aelev.postgresql.fencing-cluster=" + value.configuration["cluster"],
                "--label",
                "io.aelev.postgresql.fencing-target=" + value.configuration["node"],
                "--label",
                "io.aelev.postgresql.installation=" + value.configuration_sha256,
                "--read-only",
                "--cap-drop",
                "ALL",
                "--user",
                "0:0",
                "--pids-limit",
                "32",
                "--memory",
                "64m",
                "--security-opt",
                "no-new-privileges:true",
                "--mount",
                "type=volume,source="
                + value.configuration["secrets_volume"]
                + ",target=/prepared",
                "--entrypoint",
                "python",
                IMAGE,
                "-I",
                "-S",
                "-B",
                "-c",
                COPY,
            ],
            body=body,
            effectful=True,
        )

    def metadata(self, volume: str) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            json.loads(
                self.command(
                    [
                        "run",
                        "--rm",
                        "--pull",
                        "never",
                        "--network",
                        "none",
                        "--label",
                        "io.aelev.postgresql.operation=node-install-observe",
                        "--label",
                        "io.aelev.postgresql.fencing-volume=" + volume,
                        "--read-only",
                        "--cap-drop",
                        "ALL",
                        "--user",
                        "0:0",
                        "--pids-limit",
                        "32",
                        "--memory",
                        "64m",
                        "--security-opt",
                        "no-new-privileges:true",
                        "--mount",
                        "type=volume,source=" + volume + ",target=/prepared,readonly",
                        "--entrypoint",
                        "python",
                        IMAGE,
                        "-I",
                        "-S",
                        "-B",
                        "-c",
                        READ,
                    ]
                )
            ),
        )

    def launcher(self, value: Definition, *, initialize: bool) -> dict[str, Any]:
        spec = importlib.util.spec_from_file_location(
            "postgresql_installed_launcher", value.installation / "fencing-node.py"
        )
        assert spec is not None and spec.loader is not None
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        command = launcher.command(
            launcher.configuration(value.installation / "node.json"),
            initialize=initialize,
        )
        request = {
            "schema": 1,
            **{
                key: value.configuration[key]
                for key in ("cluster", "node", "daemon_id")
            },
            "action": "status",
            "request_id": str(uuid.uuid4()),
        }
        result = subprocess.run(
            command,
            input=b"" if initialize else canonical(request),
            capture_output=True,
            timeout=90,
            env=ENVIRONMENT,
            check=False,
        )
        require(
            result.returncode == 0
            and len(result.stdout) <= 65536
            and len(result.stderr) <= 65536,
            "INITIALIZATION_UNCONFIRMED",
        )
        if initialize:
            return {"initialize_exit_code": 0}
        response = json.loads(result.stdout)
        require(
            response.get("ok") is True and response.get("request") == request,
            "BINDING_UNCONFIRMED",
        )
        return {
            "node_binding": {
                key: request[key] for key in ("schema", "cluster", "node", "daemon_id")
            },
            "current_intent_sha256": sha(canonical(response.get("intent"))),
        }


def save(path: Path, value: dict[str, Any]) -> None:
    node.save(path, value)


def write_file(path: Path, body: bytes, mode: int) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode
    )
    os.fchmod(descriptor, mode)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())


def check_volumes(value: Definition, backend: Backend) -> None:
    for key, purpose in (("state_volume", "state"), ("secrets_volume", "credentials")):
        volume = backend.volume(value.configuration[key])
        require(
            volume is not None
            and all(
                (volume.get("Labels") or {}).get(LABEL + name) == content
                for name, content in {
                    "cluster": value.configuration["cluster"],
                    "node": value.configuration["node"],
                    "fencing-resource": purpose,
                    "installation": value.configuration_sha256,
                }.items()
            ),
            "VOLUME_BINDING_CHANGED",
        )
    metadata = backend.metadata(value.configuration["secrets_volume"])
    require(
        metadata["uid"] == 0
        and metadata["mode"] == 0o700
        and set(metadata["entries"]) == CREDENTIALS
        and set(metadata["files"]) == CREDENTIALS,
        "CREDENTIAL_DELIVERY_INVALID",
    )
    for name, row in metadata["files"].items():
        require(
            row
            == {
                "sha256": sha(value.credentials[name]),
                "uid": 0,
                "gid": 0,
                "mode": 0o600,
            },
            "CREDENTIAL_DELIVERY_INVALID",
        )


def verify(value: Definition, backend: Backend) -> dict[str, Any]:
    directory(value.installation)
    if (value.installation / "installation.json").exists():
        protected(value.installation / "installation.json")
        previous = node.read(value.installation / "installation.json")
        require(
            not (
                previous.get("phase") == "INTENT"
                and previous.get("configuration_sha256") == value.configuration_sha256
            ),
            "RESULT_UNKNOWN",
        )
    require(
        {p.name for p in value.installation.iterdir()}
        == {"fencing-node.py", "node.json", "installation.json"},
        "INSTALLATION_PARTIAL",
    )
    receipt = node.read(value.installation / "installation.json")
    require(
        receipt.get("phase") == "COMPLETED"
        and receipt.get("configuration_sha256") == value.configuration_sha256,
        "INSTALLATION_RECONCILIATION_REQUIRED",
    )
    launcher = value.installation / "fencing-node.py"
    info = launcher.lstat()
    require(
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o755
        and sha(launcher.read_bytes()) == value.launcher_sha256,
        "LAUNCHER_CHANGED",
    )
    require(
        protected(value.installation / "node.json") == value.configuration_bytes,
        "CONFIGURATION_CHANGED",
    )
    actual = protected(
        Path(value.capability["authorized_keys_file"]), limit=1024**2, empty=True
    )
    require(
        sha(actual) == receipt["authorized_keys_after_sha256"]
        and all(
            actual.splitlines().count(line) == 1
            for line in value.forced_lines().splitlines()
        ),
        "AUTHORIZATION_CHANGED",
    )
    check_volumes(value, backend)
    current = backend.launcher(value, initialize=False)
    binding = backend.metadata(value.configuration["state_volume"])["files"].get(
        "node-binding.json"
    )
    require(binding == receipt["node_binding_metadata"], "BINDING_CHANGED")
    value.unchanged()
    return {"receipt": receipt, "current": current}


def execute(value: Definition, action: str, backend: Backend) -> dict[str, Any]:
    require(action in {"plan", "task", "status"}, "ACTION_INVALID")
    native = backend.validate(value)
    result = {"configuration_sha256": value.configuration_sha256}
    if value.installation.exists():
        verify(value, backend)
        return {
            "status": (
                "OWNER_NATIVE_PLANNED" if action == "plan" else "OWNER_TASK_COMPLETED"
            ),
            **result,
        }
    require(
        all(
            backend.volume(value.configuration[key]) is None
            for key in ("state_volume", "secrets_volume")
        ),
        "HISTORY_HANDOFF_REQUIRED",
    )
    authorized = Path(value.capability["authorized_keys_file"])
    before = protected(authorized, limit=1024**2, empty=True)
    require(
        sha(before) == value.capability["authorized_keys_sha256"]
        and not any(key.encode() in before for key in value.keys),
        "AUTHORIZATION_HANDOFF_REQUIRED",
    )
    if action != "task":
        return {
            "status": (
                "OWNER_ROLE_ABSENT" if action == "status" else "OWNER_NATIVE_PLANNED"
            ),
            **result,
        }
    value.unchanged()
    value.installation.mkdir(mode=0o700)
    intent = {
        "schema_version": 1,
        "phase": "INTENT",
        **result,
        "native": native,
        "host_key_sha256": value.capability["host_key_sha256"],
    }
    try:
        save(value.installation / "installation.json", intent)
        write_file(
            value.installation / "fencing-node.py", value.launcher.read_bytes(), 0o755
        )
        write_file(value.installation / "node.json", value.configuration_bytes, 0o600)
        backend.create(value, value.configuration["state_volume"], "state")
        backend.create(value, value.configuration["secrets_volume"], "credentials")
        backend.materialize(value)
        check_volumes(value, backend)
        initialization = backend.launcher(value, initialize=True)
        current = backend.launcher(value, initialize=False)
        binding = backend.metadata(value.configuration["state_volume"])["files"].get(
            "node-binding.json"
        )
        require(
            binding is not None and binding["uid"] == 0 and binding["mode"] == 0o600,
            "BINDING_UNCONFIRMED",
        )
        value.unchanged()
        require(
            protected(authorized, limit=1024**2, empty=True) == before,
            "AUTHORIZATION_CHANGED",
        )
        after = (
            before
            + (b"\n" if before and not before.endswith(b"\n") else b"")
            + value.forced_lines()
        )
        descriptor, pending = tempfile.mkstemp(
            prefix=".aelev-fencing-", dir=authorized.parent
        )
        pending_identity = os.fstat(descriptor)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(after)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, authorized)
        finally:
            temporary = Path(pending)
            if temporary.exists() or temporary.is_symlink():
                actual = temporary.lstat()
                require(
                    (actual.st_dev, actual.st_ino)
                    == (pending_identity.st_dev, pending_identity.st_ino),
                    "STAGING_CHANGED",
                )
                temporary.unlink()
        directory_fd = os.open(authorized.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        completed = {
            **intent,
            "phase": "COMPLETED",
            "authorized_keys_before_sha256": sha(before),
            "authorized_keys_after_sha256": sha(after),
            "launcher_sha256": value.launcher_sha256,
            "node_configuration_sha256": sha(value.configuration_bytes),
            "node_binding_metadata": binding,
            "initialization": initialization,
            "current": current,
        }
        save(value.installation / "installation.json", completed)
        verify(value, backend)
    except BaseException as error:
        raise node.ExecutionError("OWNER_POSTGRESQL_NODE_RESULT_UNKNOWN") from error
    return {"status": "OWNER_TASK_COMPLETED", **result}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "task", "status"])
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--request-sha256", required=True)
    options = parser.parse_args()
    os.umask(0o077)
    value: Definition | None = None
    try:
        body = protected(node.absolute(str(options.request)), limit=1024**2)
        require(sha(body) == options.request_sha256, "REQUEST_HASH_MISMATCH")
        value = definition(node.read(options.request))
        require(
            sys.platform == "linux" and (options.action != "task" or os.geteuid() == 0),
            "LINUX_AUTHORITY_REQUIRED",
        )
        # One authorization destination serializes all target installations.
        with authorization_lock(value):
            result = execute(value, options.action, Backend())
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        unknown = (
            isinstance(error, node.ExecutionError)
            and str(error) == "OWNER_POSTGRESQL_NODE_RESULT_UNKNOWN"
        )
        code = (
            str(error)
            if isinstance(error, node.ExecutionError)
            else "OWNER_POSTGRESQL_NODE_INSTALLATION_REFUSED"
        )
        print(
            json.dumps(
                {
                    "status": "RESULT_UNKNOWN" if unknown else "BLOCKED",
                    "code": code,
                    **(
                        {"configuration_sha256": value.configuration_sha256}
                        if value
                        else {}
                    ),
                }
            )
        )
        return 3 if unknown else 2


if __name__ == "__main__":
    raise SystemExit(main())
