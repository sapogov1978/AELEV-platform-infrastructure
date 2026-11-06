"""Execute the manual's examples; no publication to a running installation."""

from __future__ import annotations

import json
import re
import secrets
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from api import Description, Settings, Status, create_app
from test.test_api import Identity, headers

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "AELEV_FUNCTIONAL_EXAMPLE_UI_"


@pytest.fixture
def cfg(tmp_path: Path) -> Settings:
    return Settings(
        "https://shell.invalid",
        "https://identity.invalid/realms/aelev",
        "https://identity.invalid/certs",
        "shell",
        "unused-unit-ca",
        str(tmp_path / "metadata.sqlite"),
        120,
        300,
        10,
        65536,
    )


def example(name: str) -> str:
    manual = (ROOT / "USER_MANUAL.md").read_text()
    matches = re.findall(
        rf"<!-- example: {re.escape(name)} -->\n```\w+\n(.*?)\n```",
        manual,
        re.DOTALL,
    )
    assert len(matches) == 1, f"missing or duplicated example: {name}"
    return matches[0]


@pytest.mark.parametrize(
    "name", ["infrastructure-links-description", "functional-swagger-description"]
)
def test_manual_external_link_examples(cfg: Settings, name: str) -> None:
    value = json.loads(example(name))
    with TestClient(create_app(cfg, Identity())) as client:
        assert (
            client.put(
                "/api/modules/" + value["module_id"], json=value, headers=headers()
            ).status_code
            == 200
        )
        declared = client.get("/api/modules", headers=headers()).json()["modules"][0]
        assert declared["revision"] == value["revision"]
        if value["service_kind"] == "FUNCTIONAL":
            assert declared["swagger_url"] == value["swagger_url"]
            assert "ui_url" not in declared
        else:
            assert declared["ui_url"] == value["ui_url"]
            assert (
                declared["components"][1]["swagger_url"]
                == value["components"][1]["swagger_url"]
            )


def publisher(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    for row in example("publisher-env").splitlines():
        key, value = row.split("=", 1)
        monkeypatch.setenv(key, value)
    secret = tmp_path / "publisher-secret"
    secret.write_text(secrets.token_urlsafe(32))
    monkeypatch.setenv(PREFIX + "PUBLISHER_SECRET_FILE", str(secret))
    scope: dict[str, Any] = {"__name__": "onboarding_example"}
    exec(
        compile(example("publisher-python"), "USER_MANUAL.md:publisher", "exec"), scope
    )
    return scope


@pytest.mark.parametrize(
    "name,kind",
    [
        ("functional-description", "FUNCTIONAL"),
        ("infrastructure-description", "INFRASTRUCTURE"),
    ],
)
def test_descriptions_match_canonical_model(name: str, kind: str) -> None:
    description = Description.model_validate_json(example(name))
    assert description.service_kind == kind
    assert bool(description.sections) == (kind == "FUNCTIONAL")


def test_status_example_matches_model(monkeypatch: pytest.MonkeyPatch) -> None:
    value = json.loads(example("owner-status"))
    observed = datetime.fromisoformat(value["observed_at"]).timestamp()
    monkeypatch.setattr("api.time.time", lambda: observed + 1)
    assert Status.model_validate(value).state == "ONLINE"


def test_component_examples_match_contract_and_persist(
    cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    description = json.loads(example("component-description"))
    report = json.loads(example("component-status"))
    observed = datetime.fromisoformat(report["observed_at"]).timestamp()
    monkeypatch.setattr("api.time.time", lambda: observed + 1)
    with TestClient(create_app(cfg, Identity())) as client:
        path = "/api/modules/" + description["module_id"]
        assert client.put(path, json=description, headers=headers()).status_code == 200
        assert (
            client.put(path + "/status", json=report, headers=headers()).status_code
            == 200
        )
        result = client.get("/api/modules", headers=headers()).json()["modules"][0]
    assert result["components"] == description["components"]
    assert {key: result["status"][key] for key in report if key != "components"} == {
        key: value for key, value in report.items() if key != "components"
    }
    assert result["status"]["components"] == [
        {**component, "reported_state": component["state"]}
        for component in report["components"]
    ]
    assert result["status"]["connection"] == "CONNECTED"


def test_withdrawal_example_and_return_preserve_owner(
    cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    description = json.loads(example("infrastructure-description"))
    withdrawal = json.loads(example("owner-withdrawal"))
    observed = datetime.fromisoformat(withdrawal["observed_at"]).timestamp()
    monkeypatch.setattr("api.time.time", lambda: observed + 1)
    assert Status.model_validate(withdrawal).active is False
    with TestClient(create_app(cfg, Identity())) as client:
        path = "/api/modules/" + description["module_id"]
        assert client.put(path, json=description, headers=headers()).status_code == 200
        for _ in range(2):
            assert (
                client.put(
                    path + "/status", json=withdrawal, headers=headers()
                ).status_code
                == 200
            )
        assert client.get("/api/modules", headers=headers()).json()["modules"] == []
        returning = {**withdrawal, "sequence": 4, "active": True, "state": "ONLINE"}
        assert (
            client.put(path + "/status", json=returning, headers=headers()).status_code
            == 200
        )
        result = client.get("/api/modules", headers=headers()).json()["modules"][0]
        assert result["revision"] == description["revision"]
        assert result["status"]["connection"] == "CONNECTED"


@pytest.mark.parametrize("pending", [False, True])
def test_journal_rollback_example_preserves_legacy_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pending: bool
) -> None:
    import sqlite3
    from aelev_platform_ui_client import Outbox

    module_id = "be36a9c0-fbe9-4d14-8b77-3b5fc5c5ca55"
    path = tmp_path / "outbox.sqlite"
    box = Outbox(path, module_id)
    report = box.prepare("UNKNOWN", "2026-10-10T10:01:00Z", active=False)
    box.begin_withdrawal()
    if not pending:
        box.acknowledge(report)
    monkeypatch.setattr(sys, "argv", ["downgrade_journal.py", str(path)])
    source = compile(
        example("journal-rollback-python"), "USER_MANUAL.md:rollback", "exec"
    )
    if pending:
        with pytest.raises(RuntimeError):
            exec(source, {"__name__": "rollback_example"})
        assert box.pending() == report and box.withdrawing()
    else:
        exec(source, {"__name__": "rollback_example"})
        with sqlite3.connect(path) as journal:
            # The original client's exact INSERT must remain usable after downgrade.
            journal.execute(
                "INSERT OR IGNORE INTO state VALUES(1,?,0,NULL,0)", (module_id,)
            )
            assert journal.execute(
                "SELECT module,sequence,pending FROM state"
            ).fetchone() == (module_id, 1, None)
        upgraded = Outbox(path, module_id)
        assert (
            json.loads(upgraded.prepare("ONLINE", "2026-10-10T10:02:00Z"))["sequence"]
            == 2
        )


def test_journal_rollback_rejects_receiver_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    path = tmp_path / "receiver.sqlite"
    with sqlite3.connect(path) as receiver:
        receiver.execute("CREATE TABLE modules(id TEXT PRIMARY KEY)")
    monkeypatch.setattr(sys, "argv", ["downgrade_journal.py", str(path)])
    with pytest.raises(ValueError):
        exec(
            compile(
                example("journal-rollback-python"), "USER_MANUAL.md:rollback", "exec"
            ),
            {"__name__": "rollback_example"},
        )


def test_publisher_sends_exact_messages_and_preserves_retries(
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scope = publisher(monkeypatch, tmp_path)
    context = Mock()
    ssl_context = Mock(return_value=context)
    monkeypatch.setattr(scope["ssl"], "create_default_context", ssl_context)
    description = json.loads(example("functional-description"))
    status = json.loads(example("owner-status"))
    monkeypatch.setattr(
        "api.time.time",
        lambda: datetime.fromisoformat(status["observed_at"]).timestamp() + 1,
    )
    requests: list[tuple[str, bytes, dict[str, str], str]] = []
    token = secrets.token_urlsafe(32)
    with TestClient(create_app(cfg, Identity())) as client:

        def send(
            context: Any,
            url: str,
            data: bytes,
            given: dict[str, str],
            method: str = "POST",
        ) -> bytes:
            requests.append((url, data, given, method))
            if method == "POST":
                form = parse_qs(data.decode())
                assert (
                    url
                    == "https://identity.invalid/realms/aelev/protocol/openid-connect/token"
                )
                assert form["grant_type"] == ["client_credentials"]
                assert form["client_id"] == ["example-ui-publisher"]
                assert form["client_secret"] == [
                    (tmp_path / "publisher-secret").read_text()
                ]
                return json.dumps({"access_token": token}).encode()
            assert given == {
                "Content-Type": "application/json",
                "Authorization": "Bearer " + token,
            }
            # JWT and nginx mTLS verification are separate tests; this is an in-memory API.
            response = client.put(
                urlsplit(url).path,
                content=data,
                headers={
                    **headers("example-owner"),
                    "Content-Type": given["Content-Type"],
                },
            )
            assert response.status_code == 200
            return response.content

        scope["send"] = send
        for kind, value in (("description", description), ("status", status)):
            path = tmp_path / (kind + ".json")
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2))
            monkeypatch.setattr(sys, "argv", ["publish.py", kind, str(path)])
            scope["main"]()
            scope["main"]()
            puts = [row for row in requests if row[3] == "PUT"]
            assert puts[-1][1] == puts[-2][1] == path.read_bytes()
        visible = client.get("/api/modules", headers=headers()).json()["modules"]
        assert visible[0]["title"] == description["title"]
        assert visible[0]["status"]["state"] == "ONLINE"
    assert context.load_cert_chain.call_count == 4
    context.load_cert_chain.assert_called_with(
        "./secrets/publisher.crt", "./secrets/publisher.key"
    )
    assert ssl_context.call_count == 8
    assert capsys.readouterr().out == "Publication confirmed\n" * 4
    assert token not in example("publisher-python")


@pytest.mark.parametrize(
    "url",
    [
        "http://owner.invalid",
        "https://user:password@owner.invalid",
        "https://owner.invalid/path",
    ],
)
def test_publisher_rejects_insecure_or_ambiguous_origin(
    url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scope = publisher(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        scope["https_origin"](url)
    assert (
        scope["NoRedirect"]().redirect_request(
            None, None, 302, "", {}, "https://other.invalid"
        )
        is None
    )


def test_publisher_rejects_uuid_mismatch_before_token_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scope = publisher(monkeypatch, tmp_path)
    path = tmp_path / "other-module.json"
    path.write_text(example("infrastructure-description"))
    scope["send"] = Mock()
    monkeypatch.setattr(sys, "argv", ["publish.py", "description", str(path)])
    with pytest.raises(ValueError):
        scope["main"]()
    scope["send"].assert_not_called()


def test_publisher_masks_error_body_and_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scope = publisher(monkeypatch, tmp_path)
    path = tmp_path / "module.json"
    path.write_text(example("functional-description"))
    monkeypatch.setattr(sys, "argv", ["publish.py", "description", str(path)])
    context = Mock()
    monkeypatch.setattr(
        scope["ssl"], "create_default_context", Mock(return_value=context)
    )
    scope["send"] = Mock(
        side_effect=HTTPError(
            "https://identity.invalid", 401, secrets.token_urlsafe(32), {}, None
        )
    )
    # Re-evaluate the real script entry point, retaining the already patched transport.
    source = example("publisher-python").split('if __name__ == "__main__":', 1)[1]
    with pytest.raises(SystemExit) as error:
        exec(compile("if True:" + source, "USER_MANUAL.md:entrypoint", "exec"), scope)
    assert error.value.code == 1
    assert capsys.readouterr().err == "Publication failed: HTTPError\n"


def test_public_config_is_generated_from_env_without_secrets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(PREFIX + "SHELL_URL", "https://shell.invalid")
    monkeypatch.setenv(PREFIX + "POLL_SECONDS", "60")
    snippet = example("public-config-python").split("\n", 1)[1].rsplit("\nPY", 1)[0]
    exec(compile(snippet, "USER_MANUAL.md:public-config", "exec"), {})
    assert json.loads(capsys.readouterr().out) == {
        "shell_origin": "https://shell.invalid",
        "poll_seconds": 60,
    }
