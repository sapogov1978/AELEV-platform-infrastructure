"""No live Docker socket or working credentials in delivery transport tests."""

from __future__ import annotations

import base64
import copy
import json
import plistlib
import py_compile
from pathlib import Path
import subprocess
from typing import Any

import pytest
import renewal_delivery as delivery


@pytest.mark.parametrize("foreign", [False, True])
def test_artifact_repeat_with_previous_private_bytecode(
    tmp_path: Path, foreign: bool
) -> None:
    def atomic(path: Path, raw: bytes) -> None:
        path.write_bytes(raw)
        path.chmod(0o600)

    script = b"VALUE = 1\n"
    path = delivery.artifact(tmp_path, script, atomic)
    cache = Path(py_compile.compile(str(path), doraise=True))
    cache.parent.chmod(0o700)
    cache.chmod(0o600)
    if foreign:
        (cache.parent / "unrelated.pyc").write_bytes(b"not operator bytecode")
        with pytest.raises(delivery.DeliveryError):
            delivery.artifact(tmp_path, script, atomic)
    else:
        assert delivery.artifact(tmp_path, script, atomic) == path


@pytest.mark.parametrize(
    "suffix",
    [
        b"private-key",
        b"-----BEGIN PRIVATE KEY-----\naGVsbG8=\n-----END PRIVATE KEY-----\n",
    ],
)
def test_only_public_certificate_bytes_can_leave_operator(suffix: bytes) -> None:
    certificate = b"-----BEGIN CERTIFICATE-----\naGVsbG8=\n-----END CERTIFICATE-----\n"
    assert delivery.public_certificate(certificate)
    with pytest.raises(delivery.DeliveryError, match="PUBLIC_CERTIFICATE_REQUIRED"):
        delivery.public_certificate(certificate + suffix)


def test_interpreter_alias_is_preserved_but_change_is_rejected(tmp_path: Path) -> None:
    binary = tmp_path / "python-real"
    binary.write_bytes(b"isolated executable")
    alias = tmp_path / "python-alias"
    alias.symlink_to(binary)
    task = tmp_path / "task.plist"
    task.write_bytes(
        plistlib.dumps(
            {"ProgramArguments": [str(alias), "script", "once", "--state", "state"]}
        )
    )
    assert delivery.preserved_python(task, str(binary)) == str(alias)
    foreign = tmp_path / "different-python"
    foreign.write_bytes(binary.read_bytes())
    with pytest.raises(delivery.DeliveryError, match="INTERPRETER_CHANGED"):
        delivery.preserved_python(task, str(foreign))


@pytest.mark.parametrize(
    "defect",
    [
        "none",
        "absent",
        "disabled",
        "changed-script",
        "changed-interval",
        "lost-bootstrap",
    ],
)
def test_existing_timer_upgrade_and_exact_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    root = tmp_path / "operator"
    root.mkdir(mode=0o700)
    old = root / "old.py"
    old.write_bytes(b"old immutable fixture")
    old_sha = delivery.hashlib.sha256(old.read_bytes()).hexdigest()

    def atomic(path: Path, raw: bytes) -> None:
        path.write_bytes(raw)
        path.chmod(0o600)

    new = delivery.artifact(root, b"new immutable fixture", atomic)
    assert new.read_bytes() == b"new immutable fixture"
    assert delivery.artifact(root, b"new immutable fixture", atomic) == new
    path = tmp_path / "fixture.plist"
    label = "com.aelev.isolated.fixture"
    previous = {
        "Label": label,
        "ProgramArguments": [
            "/isolated/python",
            str(old),
            "once",
            "--state",
            str(root),
        ],
        "StartInterval": 60,
        "RunAtLoad": True,
    }
    path.write_bytes(plistlib.dumps(previous))
    current = {
        **previous,
        "ProgramArguments": [
            "/isolated/python",
            str(new),
            "once",
            "--state",
            str(root),
        ],
    }
    loaded: Path | None = None if defect == "absent" else old
    calls = []
    failed = False

    def native(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        nonlocal loaded, failed
        calls.append(args)
        assert args[0] == "/bin/launchctl"
        if args[1] == "print":
            return subprocess.CompletedProcess(
                args,
                113 if loaded is None else 0,
                stdout=b"" if loaded is None else str(loaded).encode(),
                stderr=b"",
            )
        if args[1] == "print-disabled":
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=(
                    ('"' + label + '" => true').encode()
                    if defect == "disabled"
                    else b"{}"
                ),
                stderr=b"",
            )
        if args[1] == "bootout":
            loaded = None
        else:
            assert args[1] == "bootstrap"
            if defect == "lost-bootstrap" and not failed:
                failed = True
                return subprocess.CompletedProcess(
                    args, 1, stdout=b"", stderr=b"isolated error"
                )
            loaded = new
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(delivery.subprocess, "run", native)
    if defect == "changed-script":
        old.write_bytes(b"foreign")
    if defect == "changed-interval":
        current["StartInterval"] = 30
    content = plistlib.dumps(current)
    if defect != "none":
        with pytest.raises(delivery.DeliveryError):
            delivery.upgrade_timer(root, old_sha, path, content, new, label, atomic)
        if defect != "lost-bootstrap":
            assert not any(args[1] in {"bootstrap", "bootout"} for args in calls)
            return
        intent = (root / "timer-delivery-intent.json").read_bytes()
        alternate = delivery.artifact(root, b"third generation fixture", atomic)
        changed = {
            **current,
            "ProgramArguments": [
                "/isolated/python",
                str(alternate),
                "once",
                "--state",
                str(root),
            ],
        }
        count = len(calls)
        with pytest.raises(delivery.DeliveryError, match="TIMER_"):
            delivery.upgrade_timer(
                root,
                delivery.hashlib.sha256(new.read_bytes()).hexdigest(),
                path,
                plistlib.dumps(changed),
                alternate,
                label,
                atomic,
            )
        assert (root / "timer-delivery-intent.json").read_bytes() == intent
        assert not any(args[1] in {"bootstrap", "bootout"} for args in calls[count:])
    delivery.upgrade_timer(root, old_sha, path, content, new, label, atomic)
    assert loaded == new and path.read_bytes() == content
    delivery.upgrade_timer(
        root,
        delivery.hashlib.sha256(new.read_bytes()).hexdigest(),
        path,
        content,
        new,
        label,
        atomic,
    )
    assert loaded == new
    assert sum(args[1] == "bootout" for args in calls) == 1
    if defect == "none":
        completed = (root / "timer-delivery-intent.json").read_bytes()
        previous_sha = delivery.hashlib.sha256(new.read_bytes()).hexdigest()
        new = delivery.artifact(root, b"next immutable fixture", atomic)
        current["ProgramArguments"][1] = str(new)
        content = plistlib.dumps(current)
        delivery.upgrade_timer(root, previous_sha, path, content, new, label, atomic)
        assert loaded == new and path.read_bytes() == content
        archived = list((root / "timer-delivery-history").glob("*.json"))
        assert len(archived) == 1 and archived[0].read_bytes() == completed
        delivery.upgrade_timer(
            root,
            delivery.hashlib.sha256(new.read_bytes()).hexdigest(),
            path,
            content,
            new,
            label,
            atomic,
        )
        assert sum(args[1] == "bootout" for args in calls) == 2


@pytest.fixture
def bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], list[tuple[tuple[str, ...], bytes]]]:
    executable = tmp_path / "docker"
    executable.write_text("isolated executable")
    executable.chmod(0o700)
    spec: dict[str, Any] = {
        "schema_version": 1,
        "consumer_owner": "fixture",
        "installation_id": "9853ebc3-0aaa-41d1-a32c-6be763af81a3",
        "project": "fixture",
        "docker": str(executable),
        "docker_endpoint": "unix:///isolated.sock",
        "daemon_id": "e889855b-666a-40ce-b1e0-df1ccc3c24d1",
        "image": "sha256:" + "a" * 64,
        "destinations": [
            {
                "id": "client",
                "volume": "fixture_client",
                "role": "client",
                "file": "client.crt",
            }
        ],
        "report": {"volume": "fixture_report", "role": "report", "file": "status.json"},
    }
    calls: list[tuple[tuple[str, ...], bytes]] = []

    def command(selected: dict[str, Any], *args: str, payload: bytes = b"") -> bytes:
        assert selected == spec
        calls.append((args, payload))
        if args[0] == "info":
            return spec["daemon_id"].encode()
        if args[:2] == ("image", "inspect"):
            return delivery.encoded(
                {
                    "Id": spec["image"],
                    "Config": {"Labels": {"io.aelev.certificate-delivery": "1"}},
                }
            )
        if args[:2] == ("volume", "inspect"):
            role = args[2].removeprefix(spec["project"] + "_")
            labels = {
                "io.aelev.owner": "fixture",
                "io.aelev.installation": spec["installation_id"],
                "io.aelev.role": role,
            }
            if spec["schema_version"] == 1:
                labels["io.aelev.project"] = "fixture"
            return delivery.encoded(
                {
                    "Name": args[2],
                    "Driver": "local",
                    "Scope": "local",
                    "Options": {},
                    "CreatedAt": "isolated-creation",
                    "Labels": labels,
                }
            )
        assert args[0] == "run"
        value = json.loads(payload)
        return delivery.encoded(
            {
                "code": "CERTIFICATE_DELIVERY_VERIFIED",
                "installation_id": spec["installation_id"],
                "report_sha256": delivery.hashlib.sha256(
                    delivery.encoded(value["report"])
                ).hexdigest(),
                "certificate_sha256": {
                    item["id"]: delivery.hashlib.sha256(
                        base64.b64decode(item["certificate_base64"])
                    ).hexdigest()
                    for item in value["certificates"]
                },
            }
        )

    monkeypatch.setattr(delivery, "command", command)
    return spec, calls


def bind(root: Path, spec: dict[str, Any]) -> None:
    def atomic(path: Path, raw: bytes) -> None:
        path.write_bytes(raw)
        path.chmod(0o600)

    delivery.bind(root, spec, "a" * 64, "fixture", {"client"}, atomic)


def test_round_trip_repeat_preserves_approval_and_sends_no_keys(
    tmp_path: Path, bound: Any
) -> None:
    spec, calls = bound
    bind(tmp_path, spec)
    original = (tmp_path / "delivery.json").read_bytes()
    bind(tmp_path, spec)
    report = {"owner": "fixture-renewal", "healthy": True}
    certificates = [
        {
            "id": "client",
            "certificate_base64": base64.b64encode(
                b"-----BEGIN CERTIFICATE-----\naGVsbG8=\n-----END CERTIFICATE-----\n"
            ).decode(),
            "issuer_certificate_base64": base64.b64encode(
                b"-----BEGIN CERTIFICATE-----\naGVsbG8=\n-----END CERTIFICATE-----\n"
            ).decode(),
        }
    ]
    delivery.send(tmp_path, "a" * 64, "fixture", report, certificates)
    assert (tmp_path / "delivery.json").read_bytes() == original
    args, payload = next((a, p) for a, p in calls if a[0] == "run")
    assert "--network" in args and args[args.index("--network") + 1] == "none"
    assert "--read-only" in args and "--cap-drop" in args
    assert (
        args[args.index("--tmpfs") + 1]
        == "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777"
    )
    assert all("socket" not in arg and "key" not in arg for arg in args)
    assert b"private" not in payload
    assert b"public-cert" not in delivery.encoded(args)


def test_owner_installation_profile_accepts_existing_owner_volumes(
    tmp_path: Path, bound: Any
) -> None:
    spec, calls = bound
    spec["schema_version"] = 2
    spec["installation_id"] = "fixture-installation"
    spec["project"] = "fixture-installation"
    for item in [*spec["destinations"], spec["report"]]:
        item["volume"] = "fixture-installation_" + item["role"]
    bind(tmp_path, spec)
    inspected = [args for args, _ in calls if args[:2] == ("volume", "inspect")]
    assert {args[2] for args in inspected} == {
        "fixture-installation_client",
        "fixture-installation_report",
    }


@pytest.mark.parametrize("healthy", [False, True])
def test_negative_report_cannot_rotate(
    tmp_path: Path, bound: Any, healthy: bool
) -> None:
    spec, calls = bound
    bind(tmp_path, spec)
    if healthy:
        with pytest.raises(delivery.DeliveryError):
            delivery.send(tmp_path, "a" * 64, "fixture", {"healthy": True}, [])
    else:
        delivery.send(
            tmp_path,
            "a" * 64,
            "fixture",
            {"owner": "fixture-renewal", "healthy": False},
            [],
        )
        payload = json.loads(next(p for a, p in calls if a[0] == "run"))
        assert payload["certificates"] == []


@pytest.mark.parametrize("field", ["approval", "tool", "volume", "helper", "symlink"])
def test_changed_binding_never_dispatches(
    tmp_path: Path, bound: Any, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    spec, calls = bound
    bind(tmp_path, spec)
    approval = "a" * 64
    if field == "approval":
        approval = "b" * 64
    elif field == "tool":
        Path(spec["docker"]).write_text("changed")
    elif field == "symlink":
        original = tmp_path / "original"
        (tmp_path / "delivery.json").rename(original)
        (tmp_path / "delivery.json").symlink_to(original)
    else:
        original_command = delivery.command

        def changed(
            selected: dict[str, Any], *args: str, payload: bytes = b""
        ) -> bytes:
            raw = original_command(selected, *args, payload=payload)
            if field == "volume" and args[:2] == ("volume", "inspect"):
                value = json.loads(raw)
                value["CreatedAt"] = "replacement"
                return delivery.encoded(value)
            if field == "helper" and args[:2] == ("image", "inspect"):
                value = json.loads(raw)
                value["Config"]["Labels"] = {}
                return delivery.encoded(value)
            return raw

        monkeypatch.setattr(delivery, "command", changed)
    with pytest.raises(delivery.DeliveryError):
        delivery.send(
            tmp_path,
            approval,
            "fixture",
            {"owner": "fixture-renewal", "healthy": False},
            [],
        )
    assert not any(a[0] == "run" for a, p in calls)


def test_no_binding_keeps_existing_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        delivery, "command", lambda *a, **k: pytest.fail("unbound dispatch")
    )
    delivery.send(tmp_path, "", "", {}, [])


def test_rebinding_and_traversal_refused(tmp_path: Path, bound: Any) -> None:
    spec, _ = bound
    bind(tmp_path, spec)
    spec["report"]["file"] = "different.json"
    with pytest.raises(delivery.DeliveryError, match="BINDING_CHANGED"):
        bind(tmp_path, spec)
    spec["report"]["file"] = "../status.json"
    with pytest.raises(delivery.DeliveryError):
        bind(tmp_path, spec)


@pytest.mark.parametrize("defect", ["none", "expected", "destination"])
def test_only_explicit_same_scope_helper_update_preserves_full_history(
    tmp_path: Path, bound: Any, defect: str
) -> None:
    spec, _ = bound
    bind(tmp_path, spec)
    original = (tmp_path / "delivery.json").read_bytes()
    previous_image = spec["image"]
    spec["image"] = "sha256:" + "c" * 64
    if defect == "destination":
        spec["report"]["file"] = "changed.json"

    def atomic(path: Path, raw: bytes) -> None:
        path.write_bytes(raw)
        path.chmod(0o600)

    expected = "sha256:" + "d" * 64 if defect == "expected" else previous_image
    if defect != "none":
        with pytest.raises(delivery.DeliveryError, match="BINDING_CHANGED"):
            delivery.bind(
                tmp_path,
                spec,
                "a" * 64,
                "fixture",
                {"client"},
                atomic,
                expected_helper=expected,
            )
        assert (tmp_path / "delivery.json").read_bytes() == original
    else:
        delivery.bind(
            tmp_path,
            spec,
            "a" * 64,
            "fixture",
            {"client"},
            atomic,
            expected_helper=expected,
        )
        archives = list((tmp_path / "delivery-helper-history").glob("*.json"))
        assert len(archives) == 1 and archives[0].read_bytes() == original
        updated = (tmp_path / "delivery.json").read_bytes()
        delivery.bind(
            tmp_path,
            copy.deepcopy(spec),
            "a" * 64,
            "fixture",
            {"client"},
            atomic,
            expected_helper=expected,
        )
        assert (tmp_path / "delivery.json").read_bytes() == updated
        assert len(list((tmp_path / "delivery-helper-history").glob("*.json"))) == 1
