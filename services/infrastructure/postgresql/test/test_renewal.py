"""Exercise the operator with real X.509 material, never production credentials."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any
from unittest.mock import Mock
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import pytest

import renewal as owner

SQL_VERIFY = owner.sql_verify


def test_delivery_is_public_only_and_revocation_sends_no_leaf(
    bundle: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config, _ = bundle
    owner.enroll(root, config)
    module = Mock()
    module.DeliveryError = ValueError
    monkeypatch.setattr(owner, "delivery_operator", lambda: module)
    spec = {"consumer_owner": "fixture"}
    state_before = (root / "state.json").read_bytes()
    owner.bind_delivery(root, spec)
    assert (root / "state.json").read_bytes() == state_before
    module.bind.assert_called_once()
    (root / "delivery.json").write_text(json.dumps({"spec": spec}))
    assert owner.once(root)["healthy"]
    sent = module.send.call_args.args[-1]
    assert len(sent) == 1 and set(sent[0]) == {
        "id",
        "certificate_base64",
        "issuer_certificate_base64",
    }
    owner.revoke(root)
    assert module.send.call_args.args[-1] == []
    with pytest.raises(owner.RenewalError):
        owner.bind_delivery(root, spec)


def test_failed_volume_delivery_marks_existing_host_report_negative(
    bundle: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config, _ = bundle
    owner.enroll(root, config)
    module = Mock()
    module.DeliveryError = ValueError
    module.send.side_effect = ValueError("safe isolated failure")
    monkeypatch.setattr(owner, "delivery_operator", lambda: module)
    (root / "delivery.json").write_text(
        json.dumps({"spec": {"consumer_owner": "fixture"}})
    )
    result = owner.once(root)
    assert result["healthy"] is False and result["code"] == "PG_RENEWAL_DELIVERY_FAILED"
    assert json.loads(Path(config["report"]).read_bytes())["healthy"] is False


def test_timed_out_probe_removes_only_its_exact_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CLI timeout is not evidence that its ephemeral container stopped."""
    key = tmp_path / "client.key"
    key.write_bytes(b"isolated-fixture-not-a-key")
    daemon = "e889855b-666a-40ce-b1e0-df1ccc3c24d1"
    config: dict[str, Any] = {
        "client_root": str(tmp_path),
        "role": "fixture_role",
        "client_key": key.name,
        "client_ca": "ca.crt",
        "url_file": "url",
        "docker": "/isolated/docker",
        "docker_endpoint": "unix:///isolated.sock",
        "daemon_id": daemon,
        "probe_image": "sha256:" + "a" * 64,
        "network": "isolated-network",
    }
    calls: list[list[str]] = []

    def execute(arguments: list[str], *, timeout: int = 30) -> bytes:
        calls.append(arguments)
        if "info" in arguments:
            return daemon.encode()
        if "run" in arguments:
            assert timeout == 150
            raise owner.RenewalError("PG_RENEWAL_NATIVE_UNAVAILABLE")
        assert arguments[-3:] == ["rm", "--force", "b" * 64]
        return b""

    inspect = Mock(
        return_value=subprocess.CompletedProcess(
            [],
            0,
            stdout=("b" * 64 + " certificate-renewal-check\n").encode(),
            stderr=b"",
        )
    )
    monkeypatch.setattr(owner, "execute", execute)
    monkeypatch.setattr(subprocess, "run", inspect)
    with pytest.raises(owner.RenewalError, match="PG_RENEWAL_SQL_VERIFY_FAILED"):
        SQL_VERIFY(config, "candidate.crt")
    created = calls[1][calls[1].index("--name") + 1]
    assert created.startswith("aelev-postgresql-client-check-")
    assert inspect.call_args.args[0][-1] == created
    assert len(calls) == 3


@pytest.fixture
def bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, Any], Mock]:
    openssl = Path("/opt/homebrew/opt/openssl@3/bin/openssl")
    if not openssl.is_file():
        located = shutil.which("openssl")
        if located is None:
            pytest.skip("OpenSSL is required for genuine certificate checks")
        openssl = Path(located)
    issuer = tmp_path / "issuer"
    client = tmp_path / "client"
    reports = tmp_path / "reports"
    for path in (issuer, client, reports):
        path.mkdir(mode=0o700)

    def native(*args: str) -> None:
        subprocess.run([str(openssl), *args], capture_output=True, check=True)

    native(
        "req",
        "-new",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "365",
        "-subj",
        "/CN=isolated-renewal-test-ca",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
        "-keyout",
        str(issuer / "ca.key"),
        "-out",
        str(issuer / "ca.crt"),
    )
    native(
        "req",
        "-new",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-subj",
        "/CN=fixture_role",
        "-keyout",
        str(client / "client.key"),
        "-out",
        str(client / "client.csr"),
    )
    extensions = tmp_path / "extensions.cnf"
    extensions.write_text(
        "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=clientAuth\n"
    )
    native(
        "x509",
        "-req",
        "-in",
        str(client / "client.csr"),
        "-CA",
        str(issuer / "ca.crt"),
        "-CAkey",
        str(issuer / "ca.key"),
        "-set_serial",
        "1234",
        "-days",
        "1",
        "-sha256",
        "-extfile",
        str(extensions),
        "-out",
        str(client / "client.crt"),
    )
    shutil.copyfile(issuer / "ca.crt", client / "ca.crt")
    (client / "url").write_text("isolated-fixture-value-not-a-credential\n")
    os.chmod(client / "url", 0o600)
    os.chmod(issuer / "ca.key", 0o600)
    config: dict[str, Any] = {
        "schema_version": 1,
        "environment": "test",
        "role": "fixture_role",
        "client_root": str(client),
        "certificate": "client.crt",
        "client_key": "client.key",
        "client_ca": "ca.crt",
        "url_file": "url",
        "issuer_certificate": str(issuer / "ca.crt"),
        "issuer_key": str(issuer / "ca.key"),
        "report": str(reports / "status.json"),
        "openssl": str(openssl),
        "docker": "/usr/bin/true" if Path("/usr/bin/true").exists() else "/bin/true",
        "docker_endpoint": "unix:///isolated-test.sock",
        "daemon_id": "e889855b-666a-40ce-b1e0-df1ccc3c24d1",
        "probe_image": "sha256:" + "a" * 64,
        "network": "isolated-fixture-network",
    }
    probe = Mock()
    monkeypatch.setattr(owner, "sql_verify", probe)
    return tmp_path / "state", config, probe


def test_renew_preserves_key_scope_trust_and_credentials(
    bundle: tuple[Path, dict[str, Any], Mock],
) -> None:
    root, config, probe = bundle
    files = [
        Path(config["client_root"]) / name for name in ("client.key", "ca.crt", "url")
    ]
    previous = [path.read_bytes() for path in files]
    cert = Path(config["client_root"]) / "client.crt"
    initial = cert.read_bytes()
    owner.enroll(root, config)
    result = owner.once(root)
    state = owner.document(root / "state.json")
    assert result["healthy"] and result["code"] == "PG_RENEWAL_READY"
    assert state["issued"] == 1 and "intent" not in state
    assert cert.read_bytes() != initial
    assert [path.read_bytes() for path in files] == previous
    assert state["extensions_sha256"] == owner.digest(owner.extensions(config, cert))
    assert result["certificate_expires_at"] - result["checked_at"] > 89 * 86400
    assert probe.call_count == 3  # Enrollment, old authorization, candidate proof.
    owner.once(root)
    assert owner.document(root / "state.json")["issued"] == 1
    assert probe.call_count == 3


def test_revocation_is_durable_and_repeat_enrollment_cannot_restore_it(
    bundle: tuple[Path, dict[str, Any], Mock],
) -> None:
    root, config, probe = bundle
    owner.enroll(root, config)
    owner.revoke(root)
    result = owner.once(root, force=True)
    assert not result["healthy"] and result["code"] == "PG_RENEWAL_APPROVAL_REVOKED"
    assert owner.document(root / "state.json")["issued"] == 0
    assert probe.call_count == 1
    with pytest.raises(owner.RenewalError, match="REVOKED_OR_CHANGED"):
        owner.enroll(root, config)


def test_current_sql_authorization_required_before_issuance(
    bundle: tuple[Path, dict[str, Any], Mock],
) -> None:
    root, config, probe = bundle
    owner.enroll(root, config)
    probe.side_effect = owner.RenewalError("PG_RENEWAL_SQL_VERIFY_FAILED")
    result = owner.once(root)
    assert result["code"] == "PG_RENEWAL_SQL_VERIFY_FAILED"
    assert owner.document(root / "state.json")["issued"] == 0


def test_failed_candidate_not_published_and_retry_reuses_it(
    bundle: tuple[Path, dict[str, Any], Mock],
) -> None:
    root, config, probe = bundle
    owner.enroll(root, config)
    cert = Path(config["client_root"]) / "client.crt"
    original = cert.read_bytes()
    probe.side_effect = [None, owner.RenewalError("PG_RENEWAL_SQL_VERIFY_FAILED")]
    result = owner.once(root)
    state = owner.document(root / "state.json")
    assert not result["healthy"] and state["issued"] == 1 and state["intent"]
    assert cert.read_bytes() == original
    probe.side_effect = None
    assert owner.once(root)["healthy"]
    assert owner.document(root / "state.json")["issued"] == 1


def test_lost_publication_ack_reconciles_without_new_issuance(
    bundle: tuple[Path, dict[str, Any], Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config, _ = bundle
    owner.enroll(root, config)
    cert = Path(config["client_root"]) / "client.crt"
    original = os.replace

    def interrupt(source: Any, destination: Any) -> None:
        original(source, destination)
        if Path(destination) == cert:
            raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", interrupt)
    with pytest.raises(KeyboardInterrupt):
        owner.once(root)
    assert owner.document(root / "state.json")["intent"]
    monkeypatch.setattr(os, "replace", original)
    assert owner.once(root)["healthy"]
    state = owner.document(root / "state.json")
    assert state["issued"] == 1 and "intent" not in state
    assert state["events"][-1]["code"] == "PUBLICATION_RECONCILED"


@pytest.mark.parametrize("name", ["url", "ca.crt", "client.key"])
def test_bound_material_drift_blocks_renewal(
    bundle: tuple[Path, dict[str, Any], Mock], name: str
) -> None:
    root, config, _ = bundle
    owner.enroll(root, config)
    path = Path(config["client_root"]) / name
    path.write_bytes(path.read_bytes() + b"\n")
    result = owner.once(root, force=True)
    assert result["code"] == "PG_RENEWAL_BOUND_MATERIAL_CHANGED"
    assert owner.document(root / "state.json")["issued"] == 0


def test_public_report_excludes_operator_material(
    bundle: tuple[Path, dict[str, Any], Mock],
) -> None:
    root, config, _ = bundle
    owner.enroll(root, config)
    result = owner.once(root)
    encoded = json.dumps(result)
    assert str(root.parent) not in encoded
    assert "fixture_role" not in encoded
    assert "bindings" not in encoded and "issuer_key" not in encoded
    assert "isolated-fixture-value" not in encoded


def test_production_cannot_select_local_issuer(
    bundle: tuple[Path, dict[str, Any], Mock],
) -> None:
    root, config, _ = bundle
    config["environment"] = "production"
    with pytest.raises(owner.RenewalError, match="PRODUCTION_CA_FORBIDDEN"):
        owner.enroll(root, config)


def test_duplicate_input_fields_rejected(tmp_path: Path) -> None:
    path = tmp_path / "input.json"
    path.write_text('{"schema_version":1,"schema_version":1}')
    with pytest.raises(owner.RenewalError, match="DUPLICATE_FIELD"):
        owner.document(path)


def test_provider_probe_preserves_sqlalchemy_multihost_semantics() -> None:
    tree = ast.parse(owner.PROBE)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "parameters"
    )
    parser = Mock(return_value={"user": "fixture_role", "sslmode": "verify-full"})
    context: dict[str, Any] = {
        "urlsplit": urlsplit,
        "urlunsplit": urlunsplit,
        "parse_qs": parse_qs,
        "urlencode": urlencode,
        "conninfo_to_dict": parser,
    }
    exec(
        compile(
            ast.Module(body=[function], type_ignores=[]),
            "<canonical-read-only-probe>",
            "exec",
        ),
        context,
    )
    value = context["parameters"](
        "postgresql+psycopg://fixture_role@/fixture?host=node-1:15432&host=node-2:25432&host=node-3:35432&sslmode=verify-full"
    )
    assert value == {
        "user": "fixture_role",
        "sslmode": "verify-full",
        "host": "node-1,node-2,node-3",
        "port": "15432,25432,35432",
    }
    assert "host=" not in parser.call_args.args[0]
