"""Check transport commit ordering and failed-publication recovery."""

import json
import sys
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy.exc import OperationalError

from translator_service import presence, runtime, topic_provisioner
from translator_service.repository import Repository


@pytest.mark.parametrize("fail", [False, True])
def test_compute_has_a_bounded_model_phase_and_returns_to_loop(
    monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    progress = Mock()
    monkeypatch.setattr(runtime.health_heartbeat, "progress", progress)
    monkeypatch.setenv("AELEV_TRANSLATOR_SOCKET_PATH", "/tmp/unused.sock")
    payload = {
        "schema_version": "1.0.0",
        "source_language": "en",
        "target_language": "de",
        "translator_ref": "test",
        "text": "sample",
        "source_digest": "unused",
    }

    class Client:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *_args: Any) -> None:
            pass

        def post(self, _route: str, *, json: dict[str, Any]) -> httpx.Response:
            assert json == payload
            assert progress.call_args.args == ("worker", "model")
            if fail:
                raise httpx.ReadTimeout("isolated timeout")
            return httpx.Response(
                200,
                json={**payload, "translated_text": "Beispiel"},
                request=httpx.Request("POST", "http://private-model"),
            )

    monkeypatch.setattr(runtime.httpx, "Client", Client)
    if fail:
        with pytest.raises(httpx.ReadTimeout):
            runtime.compute(payload)
    else:
        assert runtime.compute(payload)["status"] == "SUCCEEDED"
    assert [call.args for call in progress.call_args_list] == [
        ("worker", "model"),
        ("worker",),
    ]


def test_production_repository_requires_verify_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AELEV_ENVIRONMENT", "production")
    monkeypatch.setattr(runtime, "database_sslmode", lambda: "verify-ca")
    with pytest.raises(RuntimeError, match="TRANSLATOR_DATABASE_TLS_REQUIRED"):
        runtime.repository()


@pytest.mark.parametrize(
    "environment,sslmode", [("local", "disable"), ("production", "verify-full")]
)
def test_repository_accepts_only_supported_environment_transport(
    monkeypatch: pytest.MonkeyPatch, environment: str, sslmode: str
) -> None:
    sentinel = object()
    monkeypatch.setenv("AELEV_ENVIRONMENT", environment)
    monkeypatch.setenv("AELEV_TRANSLATOR_LOCK_PATH", "/test/translator.lock.json")
    monkeypatch.setattr(runtime, "database_sslmode", lambda: sslmode)
    monkeypatch.setattr(runtime, "database_url", lambda: "postgresql://test")

    def fake_lock(_path: object) -> tuple[str, dict[str, object]]:
        return "execution-ref", {}

    def fake_repository(url: object, *, execution_ref: str | None) -> object | None:
        if (url, execution_ref) == ("postgresql://test", "execution-ref"):
            return sentinel
        return None

    monkeypatch.setattr(runtime, "read_translator_lock", fake_lock)
    monkeypatch.setattr(runtime, "Repository", fake_repository)
    assert runtime.repository() is sentinel


@pytest.mark.parametrize("version", ["1.0.0", "2.0.0"])
def test_execution_duration_measures_only_compute_and_preserves_v1(
    monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    compute = Mock(return_value={"status": "SUCCEEDED"})
    monkeypatch.setattr(runtime, "compute", compute)
    monkeypatch.setattr(
        runtime.time, "perf_counter", Mock(side_effect=[50000, 50001.25])
    )
    response, duration_ms = runtime.execute_translation(
        {
            "payload": json.dumps({"data": {"schema_version": version}}).encode(),
            "execution_ref": "owner-only",
        }
    )
    assert duration_ms == 1250
    compute.assert_called_once_with({"schema_version": version}, "owner-only")
    assert ("execution_duration_ms" in response) == (version == "2.0.0")
    if version == "2.0.0":
        assert response["execution_duration_ms"] == 1250


def test_long_translation_resumes_durable_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Repo:
        completed: list[tuple[int, str]] = []

        def prepare_segments(
            self,
            lease: dict[str, Any],
            segments: tuple[str, ...],
            execution_ref: str,
        ) -> dict[int, tuple[str, list[str]]]:
            assert lease["token"] == "lease"
            assert segments == ("first", "second")
            assert execution_ref == "owner-only"
            return {0: ("erste", ["MACHINE_TRANSLATION_UNVERIFIED"])}

        def complete_segment(
            self,
            lease: dict[str, Any],
            index: int,
            translated_text: str,
            warnings: list[str],
        ) -> bool:
            del lease, warnings
            self.completed.append((index, translated_text))
            return True

    compute = Mock(
        return_value={
            "status": "SUCCEEDED",
            "translated_text": "zweite",
            "warnings": ["MACHINE_TRANSLATION_UNVERIFIED"],
            "error_code": "",
        }
    )
    monkeypatch.setattr(runtime, "compute", compute)
    response, _ = runtime.execute_translation(
        {
            "payload": json.dumps(
                {
                    "data": {
                        "schema_version": "2.0.0",
                        "text": "first\nsecond",
                    }
                }
            ).encode(),
            "execution_ref": "owner-only",
            "token": "lease",
        },
        cast(Repository, Repo()),
    )
    assert response["translated_text"] == "erste\nzweite"
    assert response["warnings"] == ["MACHINE_TRANSLATION_UNVERIFIED"]
    compute.assert_called_once()


def test_topic_retention_is_declared_by_translator_not_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parameters = {
        "RETENTION_MS": "604800000",
        "RETENTION_BYTES": "-1",
        "MAX_MESSAGE_BYTES": "1048576",
        "MIN_INSYNC_REPLICAS": "1",
    }
    for key, value in parameters.items():
        monkeypatch.setenv("AELEV_TRANSLATOR_TOPIC_" + key, value)
    expected = {
        "cleanup.policy": "delete",
        "retention.ms": "604800000",
        "retention.bytes": "-1",
        "max.message.bytes": "1048576",
        "min.insync.replicas": "1",
    }
    topics = topic_provisioner.required_topics()
    assert set(topics) == {
        "translation.requests.priority.v1",
        "translation.requests.v1",
        "translation.results.v1",
        "translation.requests.v2",
        "translation.requests.priority.v2",
        "translation.results.v2",
    }
    assert all(configuration == expected for configuration in topics.values())


def test_database_outage_never_commits_or_polls_next_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions: list[str] = []

    class Engine:
        def dispose(self) -> None:
            actions.append("dispose")

    class Repo:
        engine = Engine()

        def accept(self, raw: bytes, priority: int) -> None:
            assert raw == b"request"
            assert priority == 0
            raise OperationalError("not logged", {}, Exception("not logged"))

    class Message:
        def error(self) -> None:
            return None

        def value(self) -> bytes:
            return b"request"

    class Consumer:
        def __init__(self, config: dict[str, Any]) -> None:
            del config

        def subscribe(self, topics: list[str]) -> None:
            assert topics == [
                "translation.requests.priority.v1",
                "translation.requests.priority.v2",
            ]

        def poll(self, timeout: float) -> Message:
            del timeout
            actions.append("poll")
            return Message()

        def commit(self, **kwargs: Any) -> None:
            del kwargs
            actions.append("commit")

        def close(self) -> None:
            actions.append("close")

    monkeypatch.setattr(runtime, "repository", lambda: Repo())
    monkeypatch.setattr(presence, "start", Mock(return_value=Mock()))
    monkeypatch.setattr(runtime, "Consumer", Consumer)

    def configuration() -> dict[str, Any]:
        return {}

    monkeypatch.setattr(runtime, "kafka_configuration", configuration)
    monkeypatch.setenv("AELEV_TRANSLATOR_KAFKA_GROUP", "test-priority")
    monkeypatch.setenv(
        "AELEV_TRANSLATOR_REQUEST_TOPIC", "translation.requests.priority.v1"
    )
    monkeypatch.setenv("AELEV_TRANSLATOR_REQUEST_PRIORITY", "0")
    monkeypatch.setenv("AELEV_ENVIRONMENT", "local")
    monkeypatch.setenv("AELEV_TRANSLATOR_OTLP_HTTP_ENDPOINT", "")
    monkeypatch.setattr(sys, "argv", ["test", "consumer"])
    with pytest.raises(RuntimeError, match="STORAGE_UNAVAILABLE"):
        runtime.main()
    assert actions == ["poll", "close", "dispose"]


def test_existing_topics_are_verified_without_empty_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = ["translation.requests.v1"]
    parameters = {"cleanup.policy": "delete", "retention.ms": "42"}
    checked: list[str] = []

    class Future:
        def result(self, timeout: float) -> dict[str, SimpleNamespace]:
            assert timeout > 0
            return {
                key: SimpleNamespace(value=value) for key, value in parameters.items()
            }

    class Admin:
        def list_topics(self, timeout: float) -> SimpleNamespace:
            assert timeout > 0
            return SimpleNamespace(
                topics={
                    name: SimpleNamespace(partitions={0: SimpleNamespace(replicas=[1])})
                    for name in names
                }
            )

        def create_topics(self, topics: list[object]) -> None:
            raise AssertionError(
                "existing topics must not call create_topics, even with []"
            )

        def describe_configs(self, resources: list[Any]) -> dict[Any, Future]:
            checked.extend(resource.name for resource in resources)
            return {resource: Future() for resource in resources}

    def admin_factory(config: dict[str, Any]) -> Admin:
        assert config == {}
        return Admin()

    def configuration() -> dict[str, Any]:
        return {}

    monkeypatch.setattr(topic_provisioner, "AdminClient", admin_factory)
    monkeypatch.setattr(topic_provisioner, "kafka_configuration", configuration)
    monkeypatch.setattr(
        topic_provisioner,
        "required_topics",
        lambda: {name: dict(parameters) for name in names},
    )
    monkeypatch.setenv("AELEV_TRANSLATOR_TOPIC_PARTITIONS", "1")
    monkeypatch.setenv("AELEV_TRANSLATOR_TOPIC_REPLICATION_FACTOR", "1")
    topic_provisioner.main()
    assert checked == names
