"""Failure-path checks for fresh owner scope and public runtime acceptance."""

from __future__ import annotations

import copy
import http.client
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

import lifecycle
import root_routes
from resources import Inventory, cleanup_plan, inventory, verify_preserved
from spire_fixture import Fixture


def fixture(tmp_path: Path) -> Fixture:
    (tmp_path / "spire").mkdir()
    (tmp_path / "spire/.env").write_text(
        "AELEV_INFRASTRUCTURE_SPIRE_COMPOSE_PROJECT=owned-spire\n"
    )
    (tmp_path / ".env").write_text("EXISTING=kept\n")
    return Fixture(tmp_path)


def empty() -> Inventory:
    return {kind: {} for kind in ("containers", "networks", "volumes", "images")}


def test_owner_scope_covers_partial_native_resources_without_adopting_peers() -> None:
    before = empty()
    before["volumes"]["original"] = {"project": "core"}
    after = copy.deepcopy(before)
    after["volumes"].update(
        {
            "native": {
                "project": "",
                "owner": "infrastructure-spire",
                "installation": "new-spire",
            },
            "peer": {
                "project": "",
                "owner": "infrastructure-spire",
                "installation": "other-spire",
            },
            "foreign": {
                "project": "",
                "owner": "another-owner",
                "installation": "new-spire",
            },
        }
    )
    owners = frozenset({("infrastructure-spire", "new-spire")})
    assert cleanup_plan(before, after, "core", set(), owners)["volumes"] == ["native"]
    with pytest.raises(RuntimeError, match="created project resources remain"):
        verify_preserved(before, after, "core", owners)
    del after["volumes"]["native"]
    verify_preserved(before, after, "core", owners)
    del after["volumes"]["original"]
    with pytest.raises(RuntimeError, match="pre-existing volumes changed"):
        verify_preserved(before, after, "core", owners)


def test_inventory_keeps_native_owner_labels() -> None:
    def execute(args: list[str]) -> str:
        if args[1:3] == ["volume", "ls"]:
            return json.dumps(
                {
                    "Name": "native",
                    "Labels": "io.aelev.owner=infrastructure-spire,io.aelev.installation=isolated",
                }
            )
        return ""

    assert inventory(execute)["volumes"]["native"]["installation"] == "isolated"


@pytest.mark.parametrize("kind", ["containers", "networks", "volumes"])
def test_fresh_preflight_rejects_prior_native_owner_state(
    tmp_path: Path, kind: str
) -> None:
    owner = fixture(tmp_path)
    before = empty()
    before[kind]["existing"] = {
        "owner": "infrastructure-spire",
        "installation": owner.project,
    }
    with pytest.raises(RuntimeError, match="EXISTING_RESOURCES_REFUSED"):
        owner.preflight(before)
    assert not owner.directory.exists()


def test_fresh_preflight_rejects_existing_public_binding(tmp_path: Path) -> None:
    owner = fixture(tmp_path)
    (tmp_path / ".env").write_text(
        "AELEV_INFRASTRUCTURE_SPIRE_INSTALLED_COMPOSE=/existing/compose.json\n"
    )
    with pytest.raises(RuntimeError, match="EXISTING_BINDING_REFUSED"):
        owner.preflight(empty())


def test_partial_bootstrap_cleanup_removes_only_new_exact_owner_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = empty()
    baseline["containers"]["protected"] = {"project": "other", "state": "running"}
    current = copy.deepcopy(baseline)
    labels = {
        "owner": "infrastructure-spire",
        "installation": "new",
        "project": "native",
    }
    current["containers"]["native-container"] = labels
    current["volumes"]["native-state"] = labels | {"project": ""}
    current["networks"]["native-network"] = labels
    commands: list[list[str]] = []

    def execute(args: list[str], **kwargs: Any) -> str:
        commands.append(args)
        return ""

    monkeypatch.setattr(lifecycle, "execute", execute)
    monkeypatch.setattr(lifecycle, "mounted_volumes", lambda *_: {"native-state"})
    monkeypatch.setattr(
        lifecycle, "inventory", Mock(side_effect=[current, current, baseline])
    )
    lifecycle.cleanup(
        baseline,
        "core",
        set(),
        ["docker", "compose", "-p", "core"],
        frozenset({("infrastructure-spire", "new")}),
    )
    assert ["docker", "container", "rm", "-f", "native-container"] in commands
    assert ["docker", "volume", "rm", "native-state"] in commands
    assert not any("protected" in command for command in commands)


def test_initial_and_repeat_use_public_cli_with_explicit_owner_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    execute = Mock(return_value=subprocess.CompletedProcess([], 0, "{}", ""))
    monkeypatch.setattr("lifecycle.subprocess.run", execute)
    for phase in ("initial", "repeat"):
        lifecycle.execute_group_start(
            ["unused"],
            "core",
            {},
            tmp_path / "diagnostic.json",
            phase,
            runtime_inputs=("--spire-local-input", "/explicit/private.json"),
        )
        args = execute.call_args.args[0]
        assert "bootstrap.py" in args[1] and "--full-runtime" in args
        assert args[-2:] == ["--spire-local-input", "/explicit/private.json"]


def test_unknown_operator_outcome_is_not_repeated_or_disclosed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owner = fixture(tmp_path)
    execute = Mock(
        side_effect=subprocess.TimeoutExpired(
            ["operator"], 1, stderr="sensitive-output"
        )
    )
    monkeypatch.setattr("spire_fixture.subprocess.run", execute)
    with pytest.raises(
        RuntimeError, match="ROOT_SPIRE_OPERATION_RESULT_UNKNOWN"
    ) as failure:
        owner.invoke(["operator"])
    assert "sensitive-output" not in str(failure.value)
    assert execute.call_count == 1


def test_binding_restore_preserves_original_bytes_and_refuses_unrelated_changes(
    tmp_path: Path,
) -> None:
    owner = fixture(tmp_path)
    path = tmp_path / ".env"
    binding = (
        "AELEV_INFRASTRUCTURE_SPIRE_INSTALLED_COMPOSE="
        + str(tmp_path / "runtime/spire-installed/runtime/installed-composition")
        + "/"
        + "a" * 64
        + "/compose.json\n"
    )
    inode = path.stat().st_ino
    path.write_bytes(owner.original_env + binding.encode())
    owner.restore_binding()
    assert path.read_bytes() == owner.original_env and path.stat().st_ino == inode
    changed = owner.original_env + binding.encode() + b"OTHER=operator-change\n"
    path.write_bytes(changed)
    with pytest.raises(RuntimeError, match="ENV_CHANGED"):
        owner.restore_binding()
    assert path.read_bytes() == changed


def test_route_preparation_requires_explicit_ephemeral_ci(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(RuntimeError, match="EPHEMERAL_CI_REQUIRED"):
        root_routes.prepare(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_route_restore_checks_every_file_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(root_routes, "ensure_stopped", lambda _: None)
    record: dict[str, Any] = {"schema_version": 1, "files": {}}
    for name in root_routes.OWNERS:
        directory = tmp_path / "observability" / name
        directory.mkdir(parents=True)
        (directory / ".env").write_text("changed\n")
        record["files"][name] = {"original": "original\n", "changed": "changed\n"}
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime/root-spire-routes.json").write_text(json.dumps(record))
    last = tmp_path / "observability/grafana/.env"
    last.write_text("operator-change\n")
    with pytest.raises(RuntimeError, match="ENV_CHANGED"):
        root_routes.restore(tmp_path)
    assert (tmp_path / "observability/delivery/.env").read_text() == "changed\n"
    assert last.read_text() == "operator-change\n"


def test_core_restart_targets_exclude_delegated_spire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    execute = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr("lifecycle.subprocess.run", execute)
    lifecycle.execute_group_start(
        ["docker", "compose"],
        "core",
        {"kafka": {}, "keycloak": {}},
        tmp_path / "diagnostic.json",
        "stop-up",
    )
    assert execute.call_args.args[0][-2:] == ["kafka", "keycloak"]


@pytest.mark.parametrize("invalid", [False, True])
def test_complete_status_requires_every_role_and_both_coordinators(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, invalid: bool
) -> None:
    owner = fixture(tmp_path)
    expected = json.loads((lifecycle.ROOT / "full-local.json").read_bytes())[
        "root_compose"
    ]["required_services"]
    observed = {
        "status": "RUNTIME_HEALTHY",
        "services": expected[:-1] if invalid else expected,
        "owner_coordinators": {"core": "core", "spire": owner.project},
    }
    monkeypatch.setattr(lifecycle, "execute", lambda _: json.dumps(observed))
    if invalid:
        with pytest.raises(RuntimeError, match="ROOT_FULL_RUNTIME_NOT_VERIFIED"):
            lifecycle.verify_full_runtime(owner)
    else:
        lifecycle.verify_full_runtime(owner)


def test_issued_leaf_passes_actual_spire_crypto_and_private_file_contract(
    tmp_path: Path,
) -> None:
    owner = fixture(tmp_path)
    owner.directory.mkdir(mode=0o700, parents=True)
    issuer = owner.root / "operator-ca.crt"
    ca_key = issuer.with_suffix(".key")
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-sha256",
            "-days",
            "365",
            "-subj",
            "/CN=isolated operator",
            "-addext",
            "basicConstraints=critical,CA:TRUE,pathlen:0",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
            "-addext",
            "subjectKeyIdentifier=hash",
            "-addext",
            "authorityKeyIdentifier=keyid:always",
            "-keyout",
            str(ca_key),
            "-out",
            str(issuer),
        ],
        capture_output=True,
        check=True,
        timeout=30,
    )
    original_ca = issuer.read_bytes(), ca_key.read_bytes()
    issued = owner.issue_client("central-write", issuer)
    source = Path(__file__).resolve().parents[1] / "spire/local-inputs.py"
    spec = importlib.util.spec_from_file_location("owner_crypto_contract", source)
    assert spec and spec.loader
    inputs: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inputs)
    public = inputs.verify_pair(
        "openssl", Path(issued["certificate"]), Path(issued["key"]), issuer, "sslclient"
    )
    assert public and Path(issued["key"]).stat().st_mode & 0o777 == 0o600
    assert Path(issued["certificate"]).stat().st_mode & 0o777 == 0o600
    assert owner.directory.stat().st_mode & 0o777 == 0o700
    assert original_ca == (issuer.read_bytes(), ca_key.read_bytes())
    with pytest.raises(RuntimeError, match="ISSUED_IDENTITY_EXISTS"):
        owner.issue_client("central-write", issuer)


@pytest.mark.parametrize(
    "code",
    [
        "SPIRE_LOCAL_INPUT_FILE_INVALID",
        "SPIRE_LOCAL_PKI_VALIDATION_FAILED",
        "GRAFANA_LOCAL_TLS_CERTIFICATE_REJECTED",
    ],
)
def test_known_preparation_failure_preserves_only_exact_public_code(code: str) -> None:
    message = "[infrastructure] FULL_RUNTIME_" + code
    assert lifecycle.classify_group_start_failure(message) == "FULL_RUNTIME_" + code
    assert (
        lifecycle.classify_group_start_failure(message + " arbitrary private text")
        == "COMPOSE_UP_FAILED"
    )


def test_generated_fixture_passes_actual_public_initial_bundle_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = fixture(tmp_path)
    root = owner.root
    paths = {
        "platform-ui/.env": "AELEV_INFRASTRUCTURE_PLATFORM_UI_LOCAL_BRIDGE_BIND=172.17.0.1\nAELEV_INFRASTRUCTURE_PLATFORM_UI_UI_URL=https://host.docker.internal:18082\nAELEV_INFRASTRUCTURE_PLATFORM_UI_UI_IDENTITY_URL=https://keycloak:18443\nAELEV_INFRASTRUCTURE_PLATFORM_UI_KEYCLOAK_REALM=aelev\n",
        "observability/delivery/.env": "AELEV_INFRASTRUCTURE_DELIVERY_BIND=172.17.0.1\nAELEV_INFRASTRUCTURE_DELIVERY_PORT=4318\n",
        "observability/grafana/.env": "AELEV_INFRASTRUCTURE_GRAFANA_BIND=172.17.0.1\nAELEV_INFRASTRUCTURE_GRAFANA_PORT=13000\n",
        "keycloak/.env": "AELEV_INFRASTRUCTURE_KEYCLOAK_ADMIN_USER=admin\n",
        "keycloak/secrets/admin-password": "offline-layout-only\n",
    }
    for name, value in paths.items():
        path = root / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(value)
        path.chmod(0o600)
    authorities = (
        root / "runtime/root-spire-publisher-authority/publisher.crt",
        root / "observability/runtime/local-issuer/internal-ca.crt",
        root / "observability/runtime/local-issuer/publisher-ca.crt",
    )
    for issuer in authorities:
        issuer.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-sha256",
                "-days",
                "365",
                "-subj",
                "/CN=" + issuer.stem,
                "-addext",
                "basicConstraints=critical,CA:TRUE,pathlen:0",
                "-addext",
                "keyUsage=critical,keyCertSign,cRLSign",
                "-addext",
                "subjectKeyIdentifier=hash",
                "-addext",
                "authorityKeyIdentifier=keyid:always",
                "-keyout",
                str(issuer.with_suffix(".key")),
                "-out",
                str(issuer),
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )
    for name in (
        "platform-ui/secrets/issuer/transport.crt",
        "keycloak/secrets/https/ca.crt",
    ):
        path = root / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(authorities[1].read_bytes())
    monkeypatch.setattr(owner, "image", lambda _: "sha256:" + "a" * 64)
    owner.prepare()
    cfg = json.loads(owner.source.read_bytes())
    wrong_client = owner.issue_client("rejected-central-write", authorities[1])
    boundary = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("spire_delivery_tls_check.py")),
            str(owner.source),
            wrong_client["certificate"],
        ],
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert boundary.returncode == 0, boundary.stderr
    assert json.loads(boundary.stdout) == {
        "accepted_packets": 1,
        "wrong_client_issuer_rejected": True,
        "durable_ack": True,
    }
    # Only file/PKI contracts are exercised here; no Grafana/API success is claimed.
    cfg["grafana_tokens"] = {}
    for name in ("publisher", "provisioner"):
        path = owner.directory / (name + ".token")
        path.write_text("offline-layout-only\n")
        path.chmod(0o600)
        cfg["grafana_tokens"][name] = str(path)
    owner.source.write_text(json.dumps(cfg))
    source = Path(__file__).resolve().parents[1]
    output = root / "prepared"
    result = subprocess.run(
        [
            sys.executable,
            str(source / "spire/local-inputs.py"),
            "--input",
            str(owner.source),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr
    checked = subprocess.run(
        [
            sys.executable,
            str(source / "spire/bootstrap.py"),
            "validate",
            "--initial-input",
            str(output / "initial-input"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["status"] == "INITIAL_BUNDLE_LAYOUT_VALID"
    requests = json.loads((output / "renewal-requests.json").read_bytes())
    assert len(requests["profiles"]) == 5
    assert sum(len(values) for values in requests["profiles"].values()) == 9
    assert set(p.stem for p in (output / "authorities").glob("*.key")) == {
        "server",
        "write",
        "query",
    }


def test_native_diagnostic_excludes_unknown_codes_success_and_private_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = fixture(tmp_path)
    source = tmp_path / "spire/src/aelev_spire"
    source.mkdir(parents=True)
    (source / "native.py").write_text(
        'raise ValueError("SPIRE_KNOWN_FAILURE")\nprint("SPIRE_SUCCESS_MARKER")\n'
    )
    (tmp_path / "spire/export.sh").write_text(
        "echo 'SPIRE_EXPORT_OUTPUT_PERMISSIONS_REQUIRED' >&2\n"
    )
    logs = tmp_path / "runtime/spire-installed/bootstrap-new"
    logs.mkdir(parents=True)
    (logs / "server.stderr").write_text(
        '{"code":"SPIRE_KNOWN_FAILURE"}\n{"code":"SPIRE_SUCCESS_MARKER"}\n{"code":"SPIRE_UNKNOWN_PRIVATE"}\nprivate-output-must-not-persist\n'
    )
    (logs / "export.stderr").write_text(
        "ValueError: SPIRE_KNOWN_FAILURE\nSPIRE_EXPORT_OUTPUT_PERMISSIONS_REQUIRED\nValueError: SPIRE_UNKNOWN_PRIVATE\n"
    )
    monkeypatch.setattr(
        "spire_fixture.full_runtime.containers",
        lambda *_: [
            {
                "Config": {
                    "Labels": {"com.docker.compose.service": "spire-server"},
                    "Env": ["private-env-must-not-persist"],
                },
                "State": {"Status": "exited", "Running": False, "ExitCode": 1},
            }
        ],
    )
    output = tmp_path / "public/diagnostic.json"
    owner.diagnostic(output)
    value = json.loads(output.read_text())
    assert sorted(value["operations"], key=lambda row: row["phase"]) == [
        {
            "phase": "export",
            "failure_codes": [
                "SPIRE_EXPORT_OUTPUT_PERMISSIONS_REQUIRED",
                "SPIRE_KNOWN_FAILURE",
            ],
        },
        {"phase": "server", "failure_codes": ["SPIRE_KNOWN_FAILURE"]},
    ]
    assert value["roles"] == [
        {
            "service": "spire-server",
            "status": "exited",
            "running": False,
            "exit_code": 1,
            "health": None,
        }
    ]
    assert "private" not in output.read_text().lower()
    assert "SPIRE_SUCCESS_MARKER" not in output.read_text()


def test_central_diagnostic_network_failure_does_not_publish_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = fixture(tmp_path)
    service = tmp_path / "observability/delivery"
    service.mkdir(parents=True)
    (service / ".env").write_text(
        "AELEV_INFRASTRUCTURE_DELIVERY_SECRET_ROOT=secrets/native\n"
        "AELEV_INFRASTRUCTURE_DELIVERY_HEALTH_PORT=13134\n"
    )
    (tmp_path / "spire/export.sh").write_text("")
    monkeypatch.setattr("spire_fixture.full_runtime.containers", lambda *_: [])
    monkeypatch.setattr(
        "sender_diagnostic.read_status",
        Mock(side_effect=http.client.RemoteDisconnected("private-must-not-leave")),
    )
    output = tmp_path / "public/diagnostic.json"
    owner.diagnostic(output)
    assert json.loads(output.read_text())["central_delivery"] == {
        "code": "ROOT_CENTRAL_DELIVERY_STATUS_UNAVAILABLE"
    }
    assert "private" not in output.read_text().lower()
