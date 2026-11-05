"""Compile and exercise the actual bounded C probe against loopback fixtures."""

from __future__ import annotations

import shutil
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.fail("C compiler is required to verify the native health probe")
    target = tmp_path_factory.mktemp("kafka-probe") / "health"
    subprocess.run(
        [
            compiler,
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-O1",
            str(ROOT / "health.c"),
            "-o",
            str(target),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return target


def packet(payload: bytes) -> bytes:
    return struct.pack(">i", len(payload)) + payload


def versions(
    *,
    correlation: int = 1,
    error: int = 0,
    keys: list[tuple[int, int, int]] | None = None,
) -> bytes:
    selected = keys if keys is not None else [(3, 0, 12), (18, 0, 4)]
    return struct.pack(">ihi", correlation, error, len(selected)) + b"".join(
        struct.pack(">hhh", *value) for value in selected
    )


def metadata(
    *,
    controller: int = 1,
    topics: int = 0,
    brokers: list[int] | None = None,
    host: bytes = b"broker",
    rack: bytes | None = None,
    port: int = 9092,
) -> bytes:
    ids = brokers if brokers is not None else [1]
    raw = struct.pack(">ii", 2, len(ids))
    for identifier in ids:
        raw += (
            struct.pack(">ih", identifier, len(host)) + host + struct.pack(">i", port)
        )
        raw += (
            struct.pack(">h", -1)
            if rack is None
            else struct.pack(">h", len(rack)) + rack
        )
    return raw + struct.pack(">ii", controller, topics)


def receive(connection: socket.socket, size: int) -> bytes:
    raw = bytearray()
    while len(raw) < size:
        value = connection.recv(size - len(raw))
        if not value:
            raise EOFError
        raw.extend(value)
    return bytes(raw)


@contextmanager
def server(
    responses: list[bytes], *, fragmented: bool = False, stall: bool = False
) -> Iterator[tuple[int, list[bytes]]]:
    requests: list[bytes] = []
    failures: list[Exception] = []
    done = threading.Event()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(8)

    def serve() -> None:
        try:
            with listener.accept()[0] as connection:
                connection.settimeout(8)
                for response in responses:
                    size = struct.unpack(">i", receive(connection, 4))[0]
                    requests.append(receive(connection, size))
                    if stall:
                        done.wait(7)
                        return
                    if fragmented:
                        for byte in response:
                            connection.sendall(bytes([byte]))
                    else:
                        connection.sendall(response)
        except (EOFError, BrokenPipeError, ConnectionResetError):
            pass  # A rejected response must close the fixture connection.
        except Exception as error:
            failures.append(error)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1], requests
    finally:
        done.set()
        thread.join(timeout=9)
        listener.close()
        assert not thread.is_alive()
        assert not failures


def probe(binary: Path, port: int) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [str(binary), str(port)], capture_output=True, check=False, timeout=6
    )


@pytest.mark.parametrize("fragmented", [False, True])
def test_real_binary_validates_protocol_and_empty_topic_request(
    binary: Path, fragmented: bool
) -> None:
    with server([packet(versions()), packet(metadata())], fragmented=fragmented) as (
        port,
        requests,
    ):
        result = probe(binary, port)
    assert result.returncode == 0 and result.stdout == b"" and result.stderr == b""
    assert len(requests) == 2
    assert [struct.unpack(">hhi", value[:8]) for value in requests] == [
        (18, 0, 1),
        (3, 1, 2),
    ]
    assert requests[0][10:] == b"aelev-kafka-health"
    assert requests[1][10:] == b"aelev-kafka-health" + bytes(4)


@pytest.mark.parametrize(
    "response",
    [
        versions(correlation=9),
        versions(error=35),
        versions(keys=[]),
        versions(keys=[(3, 0, 12)]),
        versions(keys=[(18, 0, 4)]),
        versions(keys=[(3, 0, 12), (3, 0, 12)]),
        versions(keys=[(3, 2, 12), (18, 0, 4)]),
        versions(keys=[(3, 0, 12), (18, 1, 4)]),
        versions(keys=[(3, 12, 0), (18, 0, 4)]),
        versions(keys=[(-1, 0, 12), (18, 0, 4)]),
        versions(keys=[(3, 0, 12), (18, -1, 4)]),
        versions() + b"trailing",
        versions()[:-1],
        struct.pack(">ihi", 1, 0, 2147483647),
    ],
)
def test_malformed_api_response_fails_closed_before_metadata(
    binary: Path, response: bytes
) -> None:
    with server([packet(response)]) as (port, requests):
        result = probe(binary, port)
    expected = (
        b"KAFKA_HEALTH_FRAME\n"
        if response[:4] != struct.pack(">i", 1)
        else b"KAFKA_HEALTH_API_VERSIONS\n"
    )
    assert result.returncode == 1 and result.stderr == expected
    assert len(requests) == 1


@pytest.mark.parametrize(
    "response",
    [
        metadata(controller=-1),
        metadata(controller=99),
        metadata(brokers=[]),
        metadata(brokers=[1, 1]),
        metadata(brokers=[-1]),
        metadata(host=b""),
        metadata(port=0),
        metadata(port=65536),
        metadata(topics=1),
        metadata()[:-1],
        metadata() + b"trailing",
    ],
)
def test_absent_controller_or_malformed_metadata_does_not_look_healthy(
    binary: Path, response: bytes
) -> None:
    with server([packet(versions()), packet(response)]) as (port, _):
        result = probe(binary, port)
    assert result.returncode == 1 and result.stderr == b"KAFKA_HEALTH_METADATA\n"


def test_nullable_and_present_rack_are_accepted(binary: Path) -> None:
    with server(
        [packet(versions()), packet(metadata(rack=b"rack-a", brokers=[1, 2]))]
    ) as (port, _):
        assert probe(binary, port).returncode == 0


@pytest.mark.parametrize(
    "response",
    [struct.pack(">i", -1), struct.pack(">i", 65537), packet(b""), packet(b"\x00")],
)
def test_frame_length_is_bounded(binary: Path, response: bytes) -> None:
    with server([response]) as (port, _):
        result = probe(binary, port)
    assert result.returncode == 1 and result.stderr == b"KAFKA_HEALTH_FRAME\n"


def test_stalled_socket_has_one_four_second_deadline_not_per_read_timeouts(
    binary: Path,
) -> None:
    with server([b""], stall=True) as (port, _):
        start = time.monotonic()
        result = probe(binary, port)
        elapsed = time.monotonic() - start
    assert result.returncode == 1 and result.stderr == b"KAFKA_HEALTH_DEADLINE\n"
    assert 3.5 < elapsed < 5


@pytest.mark.parametrize("value", ["", "0", "65536", "-1", "x", "9092garbage"])
def test_argument_cannot_select_any_host_or_invalid_endpoint(
    binary: Path, value: str
) -> None:
    result = subprocess.run(
        [str(binary), value], capture_output=True, check=False, timeout=2
    )
    assert result.returncode == 2 and not result.stdout


def test_closed_port_is_not_healthy(binary: Path) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    result = probe(binary, port)
    assert result.returncode == 1 and result.stderr == b"KAFKA_HEALTH_CONNECT\n"
