"""One desktop lookup path serves both public installers without private input."""

from __future__ import annotations

import socket
from typing import Any

import pytest

from aelev_spire import desktop_host
from aelev_spire.installation import Installer
from aelev_spire.observation_setup import installation_routes


@pytest.mark.parametrize(
    "addresses",
    [[], ["127.0.0.1"], ["0.0.0.0"], ["8.8.8.8"], ["::1"], ["10.0.0.1", "10.0.0.2"]],
)
def test_ambiguous_or_unusable_desktop_address_is_rejected(
    addresses: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def lookup(*args: object) -> Any:
        assert args == (
            "host.docker.internal",
            None,
            socket.AF_INET,
            socket.SOCK_STREAM,
        )
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0)) for a in addresses]

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    with pytest.raises(ValueError, match=desktop_host.CODE):
        desktop_host.resolve()


def test_duplicate_records_preserve_one_confirmed_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def lookup(*_args: object) -> Any:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.65.254", 0))] * 2

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    assert desktop_host.resolve() == "192.168.65.254"


def test_resolution_failure_has_only_a_safe_diagnostic(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unavailable(*_args: object) -> Any:
        raise OSError("untrusted native resolver diagnostic")

    monkeypatch.setattr(socket, "getaddrinfo", unavailable)
    with pytest.raises(SystemExit) as error:
        desktop_host.main()
    assert error.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == desktop_host.CODE + "\n"


def operator(
    system: str, answer: bytes = b"192.168.65.254\n"
) -> tuple[Installer, list[tuple[str, ...]]]:
    value = object.__new__(Installer)
    value.host_os, value.image = system, "fixture/runtime"
    calls: list[tuple[str, ...]] = []

    def docker(*args: str, payload: bytes | None = None) -> bytes:
        assert payload is None
        calls.append(args)
        return answer

    value.docker = docker
    return value, calls


@pytest.mark.parametrize("system", ["Darwin", "Windows"])
def test_closed_operator_uses_only_the_unprivileged_resolver_result(
    system: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    value, calls = operator(system)

    def unavailable(*_args: object) -> Any:
        raise AssertionError("The closed operator must not perform DNS")

    monkeypatch.setattr(socket, "getaddrinfo", unavailable)
    assert installation_routes(value, {"idp.example": "host-gateway"}) == {
        "idp.example": "192.168.65.254"
    }
    [helper] = calls
    assert helper[helper.index("--user") + 1] == "10001:10001"
    assert helper[helper.index("--network") + 1] == "bridge"
    assert "--read-only" in helper and helper[helper.index("--cap-drop") + 1] == "ALL"
    assert helper[helper.index("--pids-limit") + 1] == "32"
    assert helper[helper.index("--memory") + 1] == "64m"
    assert not any(a in helper for a in ("--mount", "-v", "--volume"))
    assert not any("docker.sock" in a for a in helper)
    assert helper[-5:] == (
        "python",
        "fixture/runtime",
        "-S",
        "-P",
        "/opt/aelev/src/aelev_spire/desktop_host.py",
    )


@pytest.mark.parametrize("system", ["Darwin", "Windows", "Linux"])
def test_no_aliases_never_require_desktop_dns(system: str) -> None:
    value, calls = operator(system)
    assert installation_routes(value, {}) == {} and not calls


def test_native_linux_keeps_the_engine_gateway_without_desktop_lookup() -> None:
    value, calls = operator("Linux")
    assert installation_routes(value, {"idp.example": "host-gateway"}) == {
        "idp.example": "host-gateway"
    }
    assert not calls


@pytest.mark.parametrize("system", ["Darwin", "Windows"])
@pytest.mark.parametrize(
    "answer", [b"", b"192.168.65.254 --mount hostile", b"8.8.8.8", b"\xff"]
)
def test_invalid_helper_result_is_never_compiled(system: str, answer: bytes) -> None:
    value, _calls = operator(system, answer)
    with pytest.raises(ValueError, match=desktop_host.CODE):
        installation_routes(value, {"idp.example": "host-gateway"})


def test_unavailable_helper_has_a_bounded_safe_failure() -> None:
    value, _calls = operator("Darwin")

    def unavailable(*_args: str, payload: bytes | None = None) -> bytes:
        raise ValueError("SPIRE_INSTALL_HELPER_FAILED")

    value.docker = unavailable
    with pytest.raises(ValueError, match=desktop_host.CODE):
        installation_routes(value, {"idp.example": "host-gateway"})
