from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api import (
    Description,
    ExternalLinks,
    Section,
    Settings,
    Status,
    Store,
    canonical,
    create_app,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:18496/docs",
        "http://127.0.0.1:18496/docs",
        "http://[::1]:18496/docs",
    ],
)
def test_canonical_loopback_http_is_valid_for_links_and_embedded_sections(
    url: str,
) -> None:
    assert ExternalLinks(swagger_url=url).swagger_url == url
    assert (
        Section(id="page", title={"ru": "Раздел", "en": "Section"}, url=url).url == url
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://0.0.0.0/docs",
        "http://owner.invalid",
        "http://localhost.invalid",
        "http://127.1",
        "http://[::1]evil/docs",
        "http://localhost:/docs",
        "http://localhost:0/docs",
        "http://localhost/#",
        "http://2130706433",
        "http://user@localhost",
        "http://localhost:bad/docs",
        "http://localhost\\@owner.invalid",
    ],
)
def test_remote_or_ambiguous_http_links_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        ExternalLinks(swagger_url=url)
    with pytest.raises(ValueError):
        Section(id="page", title={"ru": "Раздел", "en": "Section"}, url=url)


def components() -> list[dict[str, Any]]:
    return [
        {"id": "process", "title": {"ru": "Процесс владельца", "en": "Owner process"}},
        {"id": "store", "title": {"ru": "Хранилище владельца", "en": "Owner store"}},
    ]


class Identity:
    async def verify(self, token: str) -> dict[str, Any]:
        if token == "bad":
            raise HTTPException(401)
        return {
            "sub": token,
            "realm_access": {
                "roles": (
                    ["platform-ui-publisher", "platform-ui-viewer"]
                    if token != "unprivileged"
                    else []
                )
            },
        }


@pytest.fixture
def cfg(tmp_path: Path) -> Settings:
    return Settings(
        "https://shell.invalid",
        "https://identity.invalid/realms/r",
        "https://identity.invalid/certs",
        "shell",
        "unused-unit-ca",
        str(tmp_path / "data.sqlite"),
        120,
        300,
        10,
        65536,
    )


@pytest.fixture
def description() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "module_id": str(uuid4()),
        "revision": 1,
        "title": {"ru": "Участник", "en": "Owner"},
        "sections": [
            {
                "id": "page",
                "title": {"ru": "Раздел", "en": "Section"},
                "url": "https://owner.invalid/page",
            }
        ],
    }


def headers(identity: str = "owner", certificate: bool = True) -> dict[str, str]:
    return {
        "Authorization": "Bearer " + identity,
        "X-Verified-Publisher": "SUCCESS" if certificate else "NONE",
    }


def test_health_and_readiness_have_canonical_responses(cfg: Settings) -> None:
    with TestClient(create_app(cfg, Identity())) as client:
        assert client.get("/api/healthz").json() == {"status": "ok"}
        assert client.get("/api/readyz").json() == {"status": "ready"}


def test_publish_accepts_loopback_http_section(
    cfg: Settings, description: dict[str, Any]
) -> None:
    description["sections"][0]["url"] = "http://127.0.0.1:18088/"
    with TestClient(create_app(cfg, Identity())) as client:
        assert (
            client.put(
                "/api/modules/" + description["module_id"],
                json=description,
                headers=headers(),
            ).status_code
            == 200
        )


def test_publish_idempotence_ownership_revisions(
    cfg: Settings, description: dict[str, Any]
) -> None:
    with TestClient(create_app(cfg, Identity())) as client:
        path = "/api/modules/" + description["module_id"]
        assert client.put(path, json=description, headers=headers()).status_code == 200
        assert client.put(path, json=description, headers=headers()).status_code == 200
        assert (
            client.put(path, json=description, headers=headers("another")).status_code
            == 403
        )
        description["title"]["ru"] = "Новое имя"
        assert client.put(path, json=description, headers=headers()).status_code == 409
        description["revision"] = 2
        assert client.put(path, json=description, headers=headers()).status_code == 200
        assert (
            client.get("/api/modules", headers=headers()).json()["modules"][0]["title"][
                "ru"
            ]
            == "Новое имя"
        )


def test_owner_links_survive_restart_and_need_a_new_revision(
    cfg: Settings, description: dict[str, Any]
) -> None:
    description.update(
        service_kind="INFRASTRUCTURE",
        ui_url="https://console.owner.invalid/",
        swagger_url="https://api.owner.invalid/docs",
        components=[
            {**components()[0], "swagger_url": "https://api.owner.invalid/docs"}
        ],
    )
    path = "/api/modules/" + description["module_id"]
    with TestClient(create_app(cfg, Identity())) as client:
        assert client.put(path, json=description, headers=headers()).status_code == 200
        assert client.put(path, json=description, headers=headers()).status_code == 200
        description["ui_url"] = "https://new-console.owner.invalid/"
        assert client.put(path, json=description, headers=headers()).status_code == 409
        assert (
            client.put(path, json=description, headers=headers("another")).status_code
            == 403
        )
        description["revision"] = 2
        assert client.put(path, json=description, headers=headers()).status_code == 200
    with TestClient(create_app(cfg, Identity())) as client:
        value = client.get("/api/modules", headers=headers()).json()["modules"][0]
        assert value["ui_url"] == description["ui_url"]
        assert value["swagger_url"] == description["swagger_url"]
        assert value["components"][0]["swagger_url"] == description["swagger_url"]


@pytest.mark.parametrize("field", ["ui_url", "swagger_url"])
@pytest.mark.parametrize("child", [False, True])
@pytest.mark.parametrize(
    "url",
    [
        "http://owner.invalid",
        "javascript:alert(1)",
        "data:text/html,evil",
        "/docs",
        "https://user:password@owner.invalid",
        "https://@owner.invalid",
        "https://owner.invalid/\x7f",
        "https://owner.invalid/#fragment",
        "https://owner.invalid\\evil",
        "https://owner.invalid/ bad",
        "https://owner.invalid/\n",
        "https://owner.invalid:99999/",
        "https://shell.invalid/docs",
        "",
        True,
    ],
)
def test_external_link_validation(
    cfg: Settings, description: dict[str, Any], field: str, child: bool, url: Any
) -> None:
    if child:
        description["components"] = [{**components()[0], field: url}]
    else:
        description[field] = url
    with TestClient(create_app(cfg, Identity())) as client:
        assert (
            client.put(
                "/api/modules/" + description["module_id"],
                json=description,
                headers=headers(),
            ).status_code
            == 422
        )
        assert client.get("/api/modules", headers=headers()).json()["modules"] == []


@pytest.mark.parametrize("child", [False, True])
def test_embedded_functional_owner_publishes_only_swagger(
    cfg: Settings, description: dict[str, Any], child: bool
) -> None:
    description["service_kind"] = "FUNCTIONAL"
    if child:
        description["components"] = [
            {**components()[0], "ui_url": "https://ui.owner.invalid"}
        ]
    else:
        description["ui_url"] = "https://ui.owner.invalid"
    path = "/api/modules/" + description["module_id"]
    with TestClient(create_app(cfg, Identity())) as client:
        assert client.put(path, json=description, headers=headers()).status_code == 422
        if child:
            description["components"][0].pop("ui_url")
            description["components"][0][
                "swagger_url"
            ] = "https://api.owner.invalid/docs"
        else:
            description.pop("ui_url")
            description["swagger_url"] = "https://api.owner.invalid/docs"
        assert client.put(path, json=description, headers=headers()).status_code == 200


def test_legacy_component_links_keep_canonical_bytes(
    description: dict[str, Any],
) -> None:
    description["components"] = components()
    expected = canonical(description)
    description["ui_url"] = description["swagger_url"] = None
    for component in description["components"]:
        component["ui_url"] = component["swagger_url"] = None
    assert (
        canonical(Description.model_validate(description).model_dump(exclude_none=True))
        == expected
    )


@pytest.mark.parametrize("explicit_null", [False, True])
def test_legacy_description_keeps_bytes_and_idempotence(
    cfg: Settings, description: dict[str, Any], explicit_null: bool
) -> None:
    store = Store(cfg)
    body, digest = canonical(description)
    with store.connect() as db:
        db.execute(
            "INSERT INTO modules(id,owner,revision,body,digest) VALUES(?,?,?,?,?)",
            (description["module_id"], "owner", 1, body, digest),
        )
    if explicit_null:
        description["service_kind"] = None
        description["ui_url"] = None
        description["swagger_url"] = None
    with TestClient(create_app(cfg, Identity())) as client:
        assert (
            client.put(
                "/api/modules/" + description["module_id"],
                json=description,
                headers=headers(),
            ).status_code
            == 200
        )
        assert client.get("/api/modules", headers=headers()).json()["modules"] == []
    with store.connect() as db:
        row = db.execute("SELECT body,digest FROM modules").fetchone()
        assert (row["body"], row["digest"]) == (body, digest)
        assert db.execute("SELECT count(*) FROM audit").fetchone()[0] == 0


def test_graceful_withdrawal_and_return_preserve_identity(
    cfg: Settings, description: dict[str, Any]
) -> None:
    store = Store(cfg)
    declaration = Description.model_validate(description)
    store.publish(declaration, "owner")
    report = {
        "sequence": 1,
        "state": "ONLINE",
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    store.status(declaration.module_id, Status.model_validate(report), "owner")
    assert store.list()[0]["status"]["connection"] == "CONNECTED"
    withdrawal = Status.model_validate({**report, "sequence": 2, "active": False})
    store.status(declaration.module_id, withdrawal, "owner")
    store.status(declaration.module_id, withdrawal, "owner")
    store.publish(declaration, "owner")
    assert store.list() == []
    with pytest.raises(HTTPException) as error:
        store.status(declaration.module_id, Status.model_validate(report), "owner")
    assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        store.status(
            declaration.module_id,
            Status.model_validate({**report, "sequence": 3}),
            "foreign",
        )
    assert error.value.status_code == 403
    store.status(
        declaration.module_id,
        Status.model_validate({**report, "sequence": 3, "active": True}),
        "owner",
    )
    returned = store.list()[0]
    assert (
        returned["module_id"],
        returned["revision"],
        returned["status"]["sequence"],
    ) == (declaration.module_id, 1, 3)


def test_connection_timeout_is_not_an_unsubscribe_or_observation(
    cfg: Settings, description: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(timezone.utc).timestamp()
    monkeypatch.setattr("api.time.time", lambda: now)
    store = Store(cfg)
    declaration = Description.model_validate(description)
    store.publish(declaration, "owner")
    initial = store.list()[0]["status"]
    assert initial["state"] == "UNKNOWN" and initial["connection"] == "CONNECTED"
    old_time = datetime.fromtimestamp(now - 300, timezone.utc).isoformat()
    report = Status(sequence=1, state="ONLINE", observed_at=old_time)
    store.status(declaration.module_id, report, "owner")
    current = store.list()[0]["status"]
    assert current["state"] == "UNKNOWN" and current["connection"] == "CONNECTED"
    now += cfg.status_ttl_seconds + 1
    store.publish(declaration, "owner")
    store.status(declaration.module_id, report, "owner")
    lost = store.list()[0]["status"]
    assert lost["connection"] == "LOST"
    assert lost["state"] == "UNKNOWN" and lost["reported_state"] == "ONLINE"
    assert lost["received_at"] == current["received_at"]
    assert lost["observed_at"] == old_time
    store.status(
        declaration.module_id,
        Status(sequence=2, state="UNKNOWN", observed_at=old_time),
        "owner",
    )
    assert store.list()[0]["status"]["connection"] == "CONNECTED"


@pytest.mark.parametrize("active", ["false", 0, 1, [], {}])
def test_presence_requires_a_real_boolean(
    cfg: Settings, description: dict[str, Any], active: Any
) -> None:
    with TestClient(create_app(cfg, Identity())) as client:
        path = "/api/modules/" + description["module_id"]
        assert client.put(path, json=description, headers=headers()).status_code == 200
        assert (
            client.put(
                path + "/status",
                json={
                    "sequence": 1,
                    "state": "UNKNOWN",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "active": active,
                },
                headers=headers(),
            ).status_code
            == 422
        )


def test_legacy_status_hash_survives_optional_presence() -> None:
    value = {
        "sequence": 1,
        "state": "UNKNOWN",
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    assert canonical(
        Status.model_validate(value).model_dump(exclude_none=True)
    ) == canonical(value)
    assert canonical(
        Status.model_validate({**value, "active": None}).model_dump(exclude_none=True)
    ) == canonical(value)


def test_legacy_description_hash_survives_optional_navigation_order(
    description: dict[str, Any],
) -> None:
    assert canonical(
        Description.model_validate(description).model_dump(exclude_none=True)
    ) == canonical(description)
    assert canonical(
        Description.model_validate(
            {**description, "navigation_order": None}
        ).model_dump(exclude_none=True)
    ) == canonical(description)


def test_navigation_order_persists(cfg: Settings, description: dict[str, Any]) -> None:
    description["navigation_order"] = 2147483647
    with TestClient(create_app(cfg, Identity())) as client:
        path = "/api/modules/" + description["module_id"]
        assert client.put(path, json=description, headers=headers()).status_code == 200
        assert client.put(path, json=description, headers=headers()).status_code == 200
        assert (
            client.get("/api/modules", headers=headers()).json()["modules"][0][
                "navigation_order"
            ]
            == 2147483647
        )
    assert Store(cfg).list()[0]["navigation_order"] == 2147483647


@pytest.mark.parametrize("order", [-1, 2147483648, 1.5, "last", True, {}, []])
def test_invalid_navigation_order(
    cfg: Settings, description: dict[str, Any], order: Any
) -> None:
    description["navigation_order"] = order
    with TestClient(create_app(cfg, Identity())) as client:
        assert (
            client.put(
                "/api/modules/" + description["module_id"],
                json=description,
                headers=headers(),
            ).status_code
            == 422
        )


@pytest.mark.parametrize("kind", ["FUNCTIONAL", "INFRASTRUCTURE"])
def test_service_kind_persists(
    cfg: Settings, description: dict[str, Any], kind: str
) -> None:
    description["service_kind"] = kind
    with TestClient(create_app(cfg, Identity())) as client:
        path = "/api/modules/" + description["module_id"]
        assert client.put(path, json=description, headers=headers()).status_code == 200
        assert client.put(path, json=description, headers=headers()).status_code == 200
        assert (
            client.get("/api/modules", headers=headers()).json()["modules"][0][
                "service_kind"
            ]
            == kind
        )
    assert Store(cfg).list()[0]["service_kind"] == kind


@pytest.mark.parametrize("kind", ["functional", "OTHER", "", True, 0, {}, []])
def test_invalid_service_kind(
    cfg: Settings, description: dict[str, Any], kind: Any
) -> None:
    description["service_kind"] = kind
    with TestClient(create_app(cfg, Identity())) as client:
        assert (
            client.put(
                "/api/modules/" + description["module_id"],
                json=description,
                headers=headers(),
            ).status_code
            == 422
        )


def test_service_kind_change_requires_owner_and_new_revision(
    cfg: Settings, description: dict[str, Any]
) -> None:
    with TestClient(create_app(cfg, Identity())) as client:
        path = "/api/modules/" + description["module_id"]
        assert client.put(path, json=description, headers=headers()).status_code == 200
        report = {
            "sequence": 1,
            "state": "ONLINE",
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
        assert (
            client.put(path + "/status", json=report, headers=headers()).status_code
            == 200
        )
        description["service_kind"] = "FUNCTIONAL"
        assert client.put(path, json=description, headers=headers()).status_code == 409
        description["revision"] = 2
        assert (
            client.put(path, json=description, headers=headers("another")).status_code
            == 403
        )
        assert client.put(path, json=description, headers=headers()).status_code == 200
        description["service_kind"] = "INFRASTRUCTURE"
        assert client.put(path, json=description, headers=headers()).status_code == 409
        description["revision"] = 3
        assert client.put(path, json=description, headers=headers()).status_code == 200
        assert client.put(path, json=description, headers=headers()).status_code == 200
        result = client.get("/api/modules", headers=headers()).json()["modules"][0]
        assert result["service_kind"] == "INFRASTRUCTURE"
        assert {key: result["status"][key] for key in report} == report
        assert result["status"]["connection"] == "CONNECTED"


@pytest.mark.parametrize(
    "identity,certificate,expected",
    [("bad", True, 401), ("unprivileged", True, 403), ("owner", False, 403)],
)
def test_publish_security(
    cfg: Settings,
    description: dict[str, Any],
    identity: str,
    certificate: bool,
    expected: int,
) -> None:
    with TestClient(create_app(cfg, Identity())) as client:
        assert (
            client.put(
                "/api/modules/" + description["module_id"],
                json=description,
                headers=headers(identity, certificate),
            ).status_code
            == expected
        )
        assert client.get("/api/modules").status_code == 401
        assert (
            client.get("/api/modules", headers=headers("unprivileged")).status_code
            == 403
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://owner.invalid",
        "https://shell.invalid/page",
        "https://user:password@owner.invalid",
        "https://owner.invalid\\attack",
        "javascript:alert(1)",
    ],
)
def test_invalid_urls(cfg: Settings, description: dict[str, Any], url: str) -> None:
    description["sections"][0]["url"] = url
    with TestClient(create_app(cfg, Identity())) as client:
        assert (
            client.put(
                "/api/modules/" + description["module_id"],
                json=description,
                headers=headers(),
            ).status_code
            == 422
        )


def test_status_and_restart(cfg: Settings, description: dict[str, Any]) -> None:
    store = Store(cfg)
    store.publish(Description(**description), "owner")
    module_id = description["module_id"]
    assert store.list()[0]["status"]["state"] == "UNKNOWN"
    report = Status(
        sequence=1, state="ONLINE", observed_at=datetime.now(timezone.utc).isoformat()
    )
    store.status(module_id, report, "owner")
    store.status(module_id, report, "owner")
    assert Store(cfg).list()[0]["status"]["state"] == "ONLINE"
    with pytest.raises(HTTPException) as error:
        store.status(module_id, report.model_copy(update={"state": "OFFLINE"}), "owner")
    assert error.value.status_code == 409
    old = Status(
        sequence=2,
        state="OFFLINE",
        observed_at=(datetime.now(timezone.utc) - timedelta(seconds=121)).isoformat(),
    )
    store.status(module_id, old, "owner")
    assert store.list()[0]["status"]["state"] == "UNKNOWN"
    store.delete(module_id, "owner")
    store.delete(module_id, "owner")
    assert Store(cfg).list() == []
    with pytest.raises(HTTPException):
        store.publish(Description(**description), "owner")
    description["revision"] = 2
    store.publish(Description(**description), "owner")
    assert store.list()[0]["status"]["state"] == "UNKNOWN"


def test_capacity_and_body_limit(cfg: Settings, description: dict[str, Any]) -> None:
    store = Store(replace(cfg, max_modules=1))
    store.publish(Description(**description), "owner")
    description["module_id"] = str(uuid4())
    with pytest.raises(HTTPException) as error:
        store.publish(Description(**description), "owner")
    assert error.value.status_code == 503
    with TestClient(create_app(replace(cfg, max_body_bytes=16), Identity())) as client:
        assert (
            client.put(
                "/api/modules/" + description["module_id"],
                json=description,
                headers=headers(),
            ).status_code
            == 413
        )


def test_component_status_is_owned_durable_and_idempotent(
    cfg: Settings, description: dict[str, Any]
) -> None:
    description["components"] = components()
    now = datetime.now(timezone.utc).isoformat()
    report = {
        "sequence": 1,
        "state": "OFFLINE",
        "observed_at": now,
        "components": [
            {"id": "process", "state": "ONLINE", "observed_at": now},
            {"id": "store", "state": "OFFLINE", "observed_at": now},
        ],
    }
    with TestClient(create_app(cfg, Identity())) as client:
        path = "/api/modules/" + description["module_id"]
        assert client.put(path, json=description, headers=headers()).status_code == 200
        for _ in range(2):
            assert (
                client.put(path + "/status", json=report, headers=headers()).status_code
                == 200
            )
        assert (
            client.put(
                path + "/status", json=report, headers=headers("other")
            ).status_code
            == 403
        )
        changed = {
            **report,
            "components": [{**report["components"][0], "state": "OFFLINE"}],
        }
        assert (
            client.put(path + "/status", json=changed, headers=headers()).status_code
            == 409
        )
    result = Store(cfg).list()[0]
    assert result["components"] == components()
    assert {key: result["status"][key] for key in report if key != "components"} == {
        key: value for key, value in report.items() if key != "components"
    }
    assert result["status"]["components"] == [
        {**component, "reported_state": component["state"]}
        for component in report["components"]
    ]
    assert result["status"]["connection"] == "CONNECTED"


def test_component_freshness_does_not_inherit_parent(
    cfg: Settings, description: dict[str, Any]
) -> None:
    description["components"] = components()
    store = Store(cfg)
    store.publish(Description(**description), "owner")
    now = datetime.now(timezone.utc).isoformat()
    old = (
        datetime.now(timezone.utc) - timedelta(seconds=cfg.status_ttl_seconds + 1)
    ).isoformat()
    store.status(
        description["module_id"],
        Status(
            sequence=1,
            state="ONLINE",
            observed_at=now,
            components=[{"id": "process", "state": "ONLINE", "observed_at": old}],
        ),
        "owner",
    )
    result = store.list()[0]["status"]
    assert result["state"] == "ONLINE"
    assert result["components"] == [
        {
            "id": "process",
            "state": "UNKNOWN",
            "reported_state": "ONLINE",
            "observed_at": old,
        },
        {
            "id": "store",
            "state": "UNKNOWN",
            "reported_state": "UNKNOWN",
            "observed_at": None,
        },
    ]
    store.status(
        description["module_id"],
        Status(
            sequence=2,
            state="ONLINE",
            observed_at=old,
            components=[{"id": "process", "state": "ONLINE", "observed_at": now}],
        ),
        "owner",
    )
    assert {item["state"] for item in store.list()[0]["status"]["components"]} == {
        "UNKNOWN"
    }


def test_component_description_change_and_reactivation_need_new_reports(
    cfg: Settings, description: dict[str, Any]
) -> None:
    description["components"] = components()
    store = Store(cfg)
    store.publish(Description(**description), "owner")
    report = Status(
        sequence=1,
        state="ONLINE",
        observed_at=datetime.now(timezone.utc).isoformat(),
        components=[
            {
                "id": "process",
                "state": "ONLINE",
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }
        ],
    )
    store.status(description["module_id"], report, "owner")
    description["revision"] = 2
    description["components"][0]["title"]["ru"] = "Новое название"
    store.publish(Description(**description), "owner")
    assert store.list()[0]["status"]["components"][0]["state"] == "UNKNOWN"
    store.status(
        description["module_id"], report.model_copy(update={"sequence": 2}), "owner"
    )
    store.delete(description["module_id"], "owner")
    description["revision"] = 3
    store.publish(Description(**description), "owner")
    result = store.list()[0]["status"]
    assert result["sequence"] == 2
    assert result["state"] == "UNKNOWN"
    assert all(item["observed_at"] is None for item in result["components"])


@pytest.mark.parametrize(
    "value",
    [
        [{"id": "process", "title": {"ru": "Процесс", "en": "Process"}}] * 2,
        [{"id": "invalid id", "title": {"ru": "Процесс", "en": "Process"}}],
        [
            {"id": str(index), "title": {"ru": "Процесс", "en": "Process"}}
            for index in range(33)
        ],
    ],
)
def test_invalid_component_catalog(
    cfg: Settings, description: dict[str, Any], value: Any
) -> None:
    description["components"] = value
    with TestClient(create_app(cfg, Identity())) as client:
        assert (
            client.put(
                "/api/modules/" + description["module_id"],
                json=description,
                headers=headers(),
            ).status_code
            == 422
        )
    assert Store(cfg).list() == []


@pytest.mark.parametrize(
    "kind", ["undeclared", "duplicate", "future", "invalid", "too_many"]
)
def test_invalid_component_report_does_not_advance_sequence(
    cfg: Settings, description: dict[str, Any], kind: str
) -> None:
    description["components"] = components()
    now = datetime.now(timezone.utc).isoformat()
    child = {"id": "process", "state": "ONLINE", "observed_at": now}
    if kind == "undeclared":
        child["id"] = "other"
    elif kind == "future":
        child["observed_at"] = (
            datetime.now(timezone.utc) + timedelta(minutes=2)
        ).isoformat()
    elif kind == "invalid":
        child["state"] = "UP"
    children = [child] * (2 if kind == "duplicate" else 33 if kind == "too_many" else 1)
    with TestClient(create_app(cfg, Identity())) as client:
        path = "/api/modules/" + description["module_id"]
        assert client.put(path, json=description, headers=headers()).status_code == 200
        assert (
            client.put(
                path + "/status",
                json={
                    "sequence": 1,
                    "state": "ONLINE",
                    "observed_at": now,
                    "components": children,
                },
                headers=headers(),
            ).status_code
            == 422
        )
        assert (
            client.get("/api/modules", headers=headers()).json()["modules"][0][
                "status"
            ]["sequence"]
            == 0
        )


def test_legacy_status_keeps_canonical_bytes(
    cfg: Settings, description: dict[str, Any]
) -> None:
    store = Store(cfg)
    store.publish(Description(**description), "owner")
    report = {
        "sequence": 1,
        "state": "ONLINE",
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    store.status(description["module_id"], Status(**report), "owner")
    with store.connect() as db:
        assert (
            db.execute("SELECT status_digest FROM modules").fetchone()[0]
            == canonical(report)[1]
        )
    store.status(description["module_id"], Status(**report, components=None), "owner")
    assert {key: store.list()[0]["status"][key] for key in report} == report


def test_legacy_database_migration_preserves_description_and_status(
    cfg: Settings, description: dict[str, Any]
) -> None:
    store = Store(cfg)
    store.publish(Description(**description), "owner")
    report = Status(
        sequence=1, state="ONLINE", observed_at=datetime.now(timezone.utc).isoformat()
    )
    store.status(description["module_id"], report, "owner")
    with store.connect() as db:
        db.execute("ALTER TABLE modules DROP COLUMN component_status")
    restored = Store(cfg)
    result = restored.list()[0]
    assert result["title"] == description["title"]
    expected = report.model_dump(exclude_none=True)
    assert {key: result["status"][key] for key in expected} == expected
    restored.status(description["module_id"], report, "owner")


def test_presence_migration_keeps_ownership_but_requires_a_new_report(
    cfg: Settings, description: dict[str, Any]
) -> None:
    store = Store(cfg)
    declaration = Description(**description)
    store.publish(declaration, "owner")
    observed = datetime.now(timezone.utc).isoformat()
    report = Status(sequence=1, state="ONLINE", observed_at=observed)
    store.status(declaration.module_id, report, "owner")
    with store.connect() as db:
        db.execute("ALTER TABLE modules DROP COLUMN present")
        db.execute("ALTER TABLE modules DROP COLUMN presence_received_at")
    restored = Store(cfg)
    assert restored.list() == []
    restored.publish(declaration, "owner")
    restored.status(declaration.module_id, report, "owner")
    assert restored.list() == []
    with pytest.raises(HTTPException) as failure:
        restored.status(declaration.module_id, report, "foreign")
    assert failure.value.status_code == 403
    restored.status(
        declaration.module_id,
        Status(sequence=2, state="ONLINE", observed_at=observed, active=True),
        "owner",
    )
    row = restored.list()[0]
    assert row["module_id"] == declaration.module_id
    assert row["revision"] == declaration.revision
    assert row["status"]["sequence"] == 2
    assert row["status"]["connection"] == "CONNECTED"


@pytest.mark.parametrize("value", [True, 9223372036854775808, 0])
def test_invalid_revision(
    cfg: Settings, description: dict[str, Any], value: Any
) -> None:
    description["revision"] = value
    with TestClient(create_app(cfg, Identity())) as client:
        assert (
            client.put(
                "/api/modules/" + description["module_id"],
                json=description,
                headers=headers(),
            ).status_code
            == 422
        )
