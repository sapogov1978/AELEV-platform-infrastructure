"""Real host filesystem preservation; Docker effects use an explicit test adapter."""

from __future__ import annotations

import base64
import copy
import json
import os
import struct
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from postgresql import fencing_node_install as operation
from tools import node_execution as node
from tools import owner_compose

ROOT = Path(__file__).resolve().parents[2]


def key(number: int) -> bytes:
    raw = (
        struct.pack(">I", 11)
        + b"ssh-ed25519"
        + struct.pack(">I", 32)
        + bytes([number]) * 32
    )
    return b"ssh-ed25519 " + base64.b64encode(raw) + b" fixture\n"


@pytest.fixture(scope="session")
def certificates() -> dict[str, bytes]:
    now = datetime.now(UTC)
    ca_key = ed25519.Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "owner node unit CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(ca_key.public_key())
        .serial_number(1)
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
        .sign(ca_key, None)
    )
    client_key = ed25519.Ed25519PrivateKey.generate()
    client = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name(
                [x509.NameAttribute(NameOID.COMMON_NAME, "fencing-node-postgresql-1")]
            )
        )
        .issuer_name(name)
        .public_key(client_key.public_key())
        .serial_number(2)
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), False)
        .sign(ca_key, None)
    )
    return {
        "etcd-ca.crt": ca.public_bytes(serialization.Encoding.PEM),
        "etcd-client.crt": client.public_bytes(serialization.Encoding.PEM),
        "etcd-client.key": client_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    }


@pytest.fixture
def prepared(tmp_path: Path, certificates: dict[str, bytes]) -> dict[str, Any]:
    root = tmp_path.resolve()
    source = root / "source"
    source.mkdir()
    names = [
        operation.ENTRYPOINT,
        operation.LAUNCHER,
        operation.DESCRIPTOR,
        "tools/node_execution.py",
        "tools/owner_compose.py",
    ]
    for name in names:
        destination = source / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / name).read_bytes())
    descriptor_sha = operation.sha((source / operation.DESCRIPTOR).read_bytes())
    catalog = {
        "product": "platform-infrastructure",
        "sources": {operation.DESCRIPTOR: descriptor_sha},
        "roles": [
            {
                "id": "postgresql-fencing-node-install",
                "owner": "postgresql",
                "kind": "operation",
                "recipes": [operation.DESCRIPTOR],
            }
        ],
    }
    (source / "deployment-catalog.json").write_text(json.dumps(catalog))
    names.append("deployment-catalog.json")
    source_files = {name: operation.sha((source / name).read_bytes()) for name in names}
    inputs = root / "inputs"
    inputs.mkdir(mode=0o700)
    parent = root / "owned-target"
    parent.mkdir(mode=0o700)
    ssh = parent / "ssh"
    ssh.mkdir(mode=0o700)
    authorization = ssh / "authorized_keys"
    authorization.write_bytes(key(1))
    authorization.chmod(0o600)
    configuration = {
        "schema": 1,
        "cluster": "owned",
        "node": "postgresql-1",
        "daemon_id": "owned-daemon-id",
        "image": operation.IMAGE,
        "state_volume": "owned-node-1-state",
        "secrets_volume": "owned-node-1-credentials",
        "etcd": [{"host": "etcd.test", "port": 12379 + n * 10000} for n in range(3)],
        "timeout_seconds": 20,
    }
    capability = {
        "schema_version": 1,
        **{name: configuration[name] for name in ("cluster", "node", "daemon_id")},
        "platform": "linux/arm64",
        "installation_root": str(parent / "installation"),
        "ssh_user": "root",
        "authorized_keys_file": str(authorization),
        "authorized_keys_sha256": operation.sha(authorization.read_bytes()),
        "host_key_sha256": "b" * 64,
    }
    files = {
        "node.json": json.dumps(configuration).encode(),
        "capability.json": json.dumps(capability).encode(),
        "runtime.pub": key(2),
        **certificates,
    }
    for name, body in files.items():
        path = inputs / name
        path.write_bytes(body)
        path.chmod(0o600)
    definition = {
        "schema_version": 1,
        "executor_root": str(source),
        "executor_files": source_files,
        "owner_root": str(source),
        "owner_files": source_files,
        "catalog": "deployment-catalog.json",
        "role": operation.ROLE,
        "image": operation.IMAGE,
        "node_configuration_file": str(inputs / "node.json"),
        "capability_file": str(inputs / "capability.json"),
        "credentials": {name: str(inputs / name) for name in operation.CREDENTIALS},
        "runtime_public_keys": [str(inputs / "runtime.pub")],
        "inputs": {
            str(inputs / name): operation.sha(body) for name, body in files.items()
        },
    }
    return {
        "definition": definition,
        "configuration": configuration,
        "capability": capability,
        "source": source,
        "inputs": inputs,
        "authorization": authorization,
        "parent": parent,
    }


def change(prepared: dict[str, Any], name: str, data: bytes) -> None:
    path = prepared["inputs"] / name
    path.write_bytes(data)
    prepared["definition"]["inputs"][str(path)] = operation.sha(data)


class FakeBackend(operation.Backend):
    """Explicit logical Engine adapter, never a claim of native UID/image admission."""

    def __init__(self) -> None:
        self.volumes: dict[str, Any] = {}
        self.contents: dict[str, Any] = {}
        self.effects: list[str] = []
        self.failure: str | None = None
        self.history = b"existing budget is never reset"

    def validate(self, value: operation.Definition) -> dict[str, Any]:
        if self.failure == "engine":
            raise node.ExecutionError("OWNER_POSTGRESQL_NODE_DAEMON_CHANGED")
        return {
            "daemon_id": value.configuration["daemon_id"],
            "native_image_id": "sha256:" + "c" * 64,
            "platform": "linux/arm64",
        }

    def volume(self, name: str) -> dict[str, Any] | None:
        return self.volumes.get(name)

    def create(self, value: operation.Definition, name: str, purpose: str) -> None:
        self.effects.append("create:" + purpose)
        if self.failure == purpose:
            raise OSError("injected volume result unknown")
        self.volumes[name] = {
            "Labels": {
                operation.LABEL + k: v
                for k, v in {
                    "cluster": value.configuration["cluster"],
                    "node": value.configuration["node"],
                    "fencing-resource": purpose,
                    "installation": value.configuration_sha256,
                }.items()
            }
        }

    def materialize(self, value: operation.Definition) -> None:
        self.effects.append("materialize")
        if self.failure == "materialize":
            raise OSError("injected partial delivery")
        self.contents[value.configuration["secrets_volume"]] = {
            "uid": 0,
            "mode": 0o700,
            "entries": sorted(operation.CREDENTIALS),
            "files": {
                name: {"sha256": operation.sha(body), "uid": 0, "gid": 0, "mode": 0o600}
                for name, body in value.credentials.items()
            },
        }

    def metadata(self, volume: str) -> dict[str, Any]:
        return copy.deepcopy(self.contents[volume])

    def launcher(
        self, value: operation.Definition, *, initialize: bool
    ) -> dict[str, Any]:
        if self.failure == "initialize":
            raise OSError("injected unknown initialize")
        if initialize:
            self.effects.append("initialize")
            self.contents[value.configuration["state_volume"]] = {
                "files": {
                    "node-binding.json": {
                        "sha256": "d" * 64,
                        "uid": 0,
                        "gid": 0,
                        "mode": 0o600,
                    }
                }
            }
            return {"initialize_exit_code": 0}
        return {
            "node_binding": {
                name: value.configuration[name]
                for name in ("cluster", "node", "daemon_id")
            },
            "current_intent_sha256": operation.sha(self.history),
        }


def snapshot(root: Path) -> dict[str, Any]:
    return {
        str(path.relative_to(root)): (
            path.lstat().st_mode,
            path.lstat().st_mtime_ns,
            path.read_bytes() if path.is_file() and not path.is_symlink() else None,
        )
        for path in [root, *root.rglob("*")]
    }


def test_fresh_install_real_files_initialize_and_repeat_preserve_foreign_keys(
    prepared: dict[str, Any],
) -> None:
    value = operation.definition(prepared["definition"])
    backend = FakeBackend()
    before = prepared["authorization"].read_bytes()
    previous_umask = os.umask(0o077)
    try:
        result = operation.execute(value, "task", backend)
    finally:
        os.umask(previous_umask)
    assert result == {
        "status": "OWNER_TASK_COMPLETED",
        "configuration_sha256": value.configuration_sha256,
    }
    assert backend.effects == [
        "create:state",
        "create:credentials",
        "materialize",
        "initialize",
    ]
    assert prepared["authorization"].read_bytes().startswith(before)
    assert prepared["authorization"].read_bytes().count(b"restrict,command=") == 1
    assert (value.installation / "fencing-node.py").stat().st_mode & 0o777 == 0o755
    installed = snapshot(prepared["parent"])
    effects = backend.effects.copy()
    history = backend.history
    for action in ("plan", "task", "status"):
        assert operation.execute(value, action, backend)["status"] == (
            "OWNER_NATIVE_PLANNED" if action == "plan" else "OWNER_TASK_COMPLETED"
        )
        assert snapshot(prepared["parent"]) == installed
        assert backend.effects == effects and backend.history == history


@pytest.mark.parametrize(
    "action,status", [("plan", "OWNER_NATIVE_PLANNED"), ("status", "OWNER_ROLE_ABSENT")]
)
def test_fresh_observation_has_no_files_or_engine_effects(
    prepared: dict[str, Any], action: str, status: str
) -> None:
    value = operation.definition(prepared["definition"])
    backend = FakeBackend()
    before = snapshot(prepared["parent"])
    assert operation.execute(value, action, backend)["status"] == status
    assert snapshot(prepared["parent"]) == before and backend.effects == []


@pytest.mark.parametrize(
    "field,bad",
    [
        ("role", "platform-infrastructure/another"),
        ("image", "sha256:" + "a" * 64),
        ("image", "image:latest"),
        ("catalog", "../deployment-catalog.json"),
        ("schema_version", True),
        ("command", "sh"),
    ],
)
def test_role_image_unknown_field_refusals_before_effect(
    prepared: dict[str, Any], field: str, bad: Any
) -> None:
    prepared["definition"][field] = bad
    before = snapshot(prepared["parent"])
    with pytest.raises(node.ExecutionError):
        operation.definition(prepared["definition"])
    assert snapshot(prepared["parent"]) == before


@pytest.mark.parametrize(
    "field,bad",
    [
        ("node", "postgresql-2"),
        ("daemon_id", "changed-daemon"),
        ("platform", "darwin/arm64"),
        ("authorized_keys_file", "/unapproved-key-file"),
        ("extra", "sh"),
    ],
)
def test_capability_refusals_preserve_destination(
    prepared: dict[str, Any], field: str, bad: Any
) -> None:
    data = prepared["capability"]
    data[field] = bad
    change(prepared, "capability.json", json.dumps(data).encode())
    before = snapshot(prepared["parent"])
    with pytest.raises(node.ExecutionError):
        operation.definition(prepared["definition"])
    assert snapshot(prepared["parent"]) == before


@pytest.mark.parametrize(
    "kind", ["mode", "symlink", "hardlink", "oversized", "empty", "hash", "key-format"]
)
def test_protected_input_refusals(prepared: dict[str, Any], kind: str) -> None:
    path = prepared["inputs"] / "runtime.pub"
    if kind == "mode":
        path.chmod(0o644)
    elif kind == "symlink":
        target = path.with_name("saved.pub")
        path.rename(target)
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(path, path.with_name("linked.pub"))
    elif kind == "oversized":
        change(prepared, "runtime.pub", b"x" * 65537)
    elif kind == "empty":
        change(prepared, "runtime.pub", b"")
    elif kind == "hash":
        path.write_bytes(key(3))
    else:
        change(prepared, "runtime.pub", b"command=sh ssh-ed25519 not-a-key")
    before = snapshot(prepared["parent"])
    with pytest.raises((node.ExecutionError, OSError)):
        operation.definition(prepared["definition"])
    assert snapshot(prepared["parent"]) == before


@pytest.mark.parametrize("which", ["state_volume", "secrets_volume"])
def test_old_volume_even_with_matching_owner_requires_handoff(
    prepared: dict[str, Any], which: str
) -> None:
    value = operation.definition(prepared["definition"])
    backend = FakeBackend()
    backend.volumes[value.configuration[which]] = {"Labels": {"owner": "previous"}}
    before = snapshot(prepared["parent"])
    with pytest.raises(node.ExecutionError, match="HISTORY_HANDOFF"):
        operation.execute(value, "task", backend)
    assert snapshot(prepared["parent"]) == before and backend.effects == []


@pytest.mark.parametrize("phase", ["state", "credentials", "materialize", "initialize"])
def test_unknown_effect_retains_partial_and_never_reinitializes(
    prepared: dict[str, Any], phase: str
) -> None:
    value = operation.definition(prepared["definition"])
    backend = FakeBackend()
    backend.failure = phase
    before = prepared["authorization"].read_bytes()
    with pytest.raises(node.ExecutionError, match="RESULT_UNKNOWN"):
        operation.execute(value, "task", backend)
    effects = backend.effects.copy()
    partial = snapshot(prepared["parent"])
    backend.failure = None
    for action in ("plan", "task", "status"):
        with pytest.raises(node.ExecutionError, match="RESULT_UNKNOWN"):
            operation.execute(value, action, backend)
        assert backend.effects == effects and snapshot(prepared["parent"]) == partial
    assert prepared["authorization"].read_bytes() == before


@pytest.mark.parametrize(
    "change_kind",
    ["auth", "launcher", "configuration", "metadata", "credentials", "volume"],
)
def test_installed_drift_never_repairs(
    prepared: dict[str, Any], change_kind: str
) -> None:
    value = operation.definition(prepared["definition"])
    backend = FakeBackend()
    operation.execute(value, "task", backend)
    if change_kind == "auth":
        prepared["authorization"].write_bytes(key(9))
    elif change_kind == "launcher":
        (value.installation / "fencing-node.py").write_bytes(b"foreign")
    elif change_kind == "configuration":
        (value.installation / "node.json").write_bytes(b"{}")
    elif change_kind == "metadata":
        path = value.installation / "installation.json"
        data = json.loads(path.read_bytes())
        data["configuration_sha256"] = "0" * 64
        path.write_text(json.dumps(data))
    elif change_kind == "credentials":
        backend.contents[value.configuration["secrets_volume"]]["files"][
            "etcd-client.key"
        ]["mode"] = 0o644
    else:
        backend.volumes[value.configuration["state_volume"]]["Labels"][
            operation.LABEL + "cluster"
        ] = "foreign"
    before = snapshot(prepared["parent"])
    effects = backend.effects.copy()
    with pytest.raises(node.ExecutionError):
        operation.execute(value, "task", backend)
    assert snapshot(prepared["parent"]) == before and backend.effects == effects


def test_invalid_tls_pair_refused_before_installation(prepared: dict[str, Any]) -> None:
    change(prepared, "etcd-client.key", b"not-a-private-key")
    before = snapshot(prepared["parent"])
    with pytest.raises((ValueError, OSError)):
        operation.definition(prepared["definition"])
    assert snapshot(prepared["parent"]) == before


def test_source_change_and_changed_input_refused(prepared: dict[str, Any]) -> None:
    value = operation.definition(prepared["definition"])
    change(prepared, "runtime.pub", key(3))
    with pytest.raises(node.ExecutionError, match="INPUT_CHANGED"):
        value.unchanged()
    (prepared["source"] / operation.LAUNCHER).write_text("foreign source")
    with pytest.raises(node.ExecutionError, match="SOURCE_DRIFT"):
        operation.definition(prepared["definition"])


def test_real_copy_program_and_read_metadata_preserve_existing_files(
    tmp_path: Path, certificates: dict[str, bytes]
) -> None:
    target = tmp_path.resolve() / "credential-volume"
    target.mkdir()
    program = operation.COPY.replace("/prepared", str(target))
    body = json.dumps(
        {name: base64.b64encode(raw).decode() for name, raw in certificates.items()}
    ).encode()
    first = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", program],
        input=body,
        capture_output=True,
        check=False,
    )
    assert first.returncode == 0
    assert {path.name: path.read_bytes() for path in target.iterdir()} == certificates
    assert target.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in target.iterdir())
    before = snapshot(target)
    retry = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", program],
        input=body,
        capture_output=True,
        check=False,
    )
    assert retry.returncode != 0 and snapshot(target) == before
    metadata = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            operation.READ.replace("/prepared", str(target)),
        ],
        capture_output=True,
        check=True,
    )
    value = json.loads(metadata.stdout)
    assert set(value["entries"]) == operation.CREDENTIALS
    assert (
        value["uid"] == os.getuid()
    )  # Host adapter; actual native UID0 remains a gate.
    assert {name: row["sha256"] for name, row in value["files"].items()} == {
        name: operation.sha(raw) for name, raw in certificates.items()
    }


def test_directory_lock_survives_atomic_authorized_key_replacement(
    prepared: dict[str, Any],
) -> None:
    value = operation.definition(prepared["definition"])
    program = "import fcntl,os,sys;f=os.open(sys.argv[1],os.O_RDONLY|os.O_DIRECTORY)\ntry:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)\nexcept BlockingIOError:raise SystemExit(23)\n"
    command = [
        sys.executable,
        "-I",
        "-S",
        "-B",
        "-c",
        program,
        str(prepared["authorization"].parent),
    ]
    with operation.authorization_lock(value):
        assert (
            subprocess.run(command, capture_output=True, check=False).returncode == 23
        )
        operation.execute(value, "task", FakeBackend())
        assert (
            subprocess.run(command, capture_output=True, check=False).returncode == 23
        )
    assert subprocess.run(command, capture_output=True, check=False).returncode == 0


@pytest.mark.parametrize(
    "field,bad",
    [
        ("schema_version", True),
        ("helper_image", "image:latest"),
        ("procedure", "arbitrary-shell"),
        ("extra", "sh"),
    ],
)
def test_descriptor_cannot_authorize_another_handler_or_image(
    prepared: dict[str, Any], field: str, bad: Any
) -> None:
    path = prepared["source"] / operation.DESCRIPTOR
    descriptor = json.loads(path.read_bytes())
    descriptor[field] = bad
    path.write_text(json.dumps(descriptor))
    prepared["definition"]["owner_files"][operation.DESCRIPTOR] = operation.sha(
        path.read_bytes()
    )
    with pytest.raises(node.ExecutionError, match="DESCRIPTOR_INVALID"):
        operation.definition(prepared["definition"])


def test_node_configuration_cannot_pick_an_unadmitted_helper(
    prepared: dict[str, Any],
) -> None:
    configuration = prepared["configuration"]
    configuration["image"] = "ghcr.io/unadmitted/helper@sha256:" + "e" * 64
    change(prepared, "node.json", json.dumps(configuration).encode())
    with pytest.raises(node.ExecutionError, match="CONFIGURATION_INVALID"):
        operation.definition(prepared["definition"])


def test_native_operation_is_not_executed_as_compose(
    prepared: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = prepared["definition"]
    request = {
        name: original[name]
        for name in (
            "schema_version",
            "executor_root",
            "executor_files",
            "owner_root",
            "owner_files",
            "catalog",
            "role",
            "image",
        )
    }
    request.update(
        recipes=[operation.DESCRIPTOR],
        project_directory=str(prepared["parent"]),
        project="fixed-node-operation",
        env_file=original["node_configuration_file"],
        inputs=original["inputs"],
        configuration_sha256=None,
    )

    def forbidden(*args: Any, **kwargs: Any) -> bytes:
        raise AssertionError("Compose must not be invoked for this native operation")

    monkeypatch.setattr(owner_compose, "call", forbidden)
    with pytest.raises(node.ExecutionError, match="NATIVE_OPERATION_REQUIRED"):
        owner_compose.definition(request)


@pytest.mark.parametrize("kind", ["foreign", "unsafe", "symlink"])
def test_preexisting_unknown_installation_is_preserved(
    prepared: dict[str, Any], kind: str
) -> None:
    value = operation.definition(prepared["definition"])
    target = value.installation
    if kind == "symlink":
        external = prepared["parent"] / "external"
        external.mkdir(mode=0o700)
        (external / "foreign").write_bytes(b"preserved")
        target.symlink_to(external, target_is_directory=True)
    else:
        target.mkdir(mode=0o700)
        (target / ".prepare-1").write_bytes(b"preserved")
        if kind == "unsafe":
            target.chmod(0o755)
    before = snapshot(prepared["parent"])
    backend = FakeBackend()
    with pytest.raises(node.ExecutionError):
        operation.execute(value, "task", backend)
    assert snapshot(prepared["parent"]) == before and backend.effects == []


def test_interrupt_keeps_owned_intent_and_foreign_authorization(
    prepared: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    value = operation.definition(prepared["definition"])
    backend = FakeBackend()

    def interrupted(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(backend, "materialize", interrupted)
    before = prepared["authorization"].read_bytes()
    with pytest.raises(node.ExecutionError, match="RESULT_UNKNOWN"):
        operation.execute(value, "task", backend)
    assert prepared["authorization"].read_bytes() == before
    assert (
        json.loads((value.installation / "installation.json").read_bytes())["phase"]
        == "INTENT"
    )


@pytest.mark.parametrize("character", ["\n", "\r", "\x7f"])
def test_forced_command_destination_refuses_control_characters(
    prepared: dict[str, Any], character: str
) -> None:
    capability = prepared["capability"].copy()
    capability["installation_root"] = str(
        prepared["parent"] / ("unsafe" + character + "path")
    )
    change(prepared, "capability.json", json.dumps(capability).encode())
    before = snapshot(prepared["parent"])
    with pytest.raises(node.ExecutionError, match="PATH_CONTROL_CHARACTER"):
        operation.definition(prepared["definition"])
    assert snapshot(prepared["parent"]) == before
