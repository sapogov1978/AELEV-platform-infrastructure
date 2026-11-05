"""The build supplier cannot publish truncated or unverified weights."""

import hashlib
import io
import json
import runpy
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

SUPPLY = runpy.run_path(str(Path(__file__).resolve().parents[1] / "download-model.py"))


class Reply(io.BytesIO):
    status = 206

    def __init__(self, content: bytes, span: str) -> None:
        super().__init__(content)
        self.headers = {"Content-Range": span}


def install(monkeypatch: pytest.MonkeyPatch, body: bytes, span: str) -> Mock:
    def respond(request: Any, timeout: int) -> Reply:
        assert timeout == 90
        assert request.headers["Accept-encoding"] == "identity"
        return Reply(body, span)

    mocked = Mock(side_effect=respond)
    monkeypatch.setattr(SUPPLY["urllib"].request, "urlopen", mocked)
    monkeypatch.setattr(SUPPLY["time"], "sleep", Mock())
    return mocked


def test_incomplete_range_resumes_without_discarding_downloaded_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    opened = Mock(
        side_effect=[Reply(b"ABCD", "bytes 0-7/8"), Reply(b"EFGH", "bytes 4-7/8")]
    )
    monkeypatch.setattr(SUPPLY["urllib"].request, "urlopen", opened)
    monkeypatch.setattr(SUPPLY["time"], "sleep", Mock())
    path = tmp_path / "part"
    SUPPLY["fetch_part"]("https://example.invalid", 0, 7, 8, path)
    assert path.read_bytes() == b"ABCDEFGH"
    assert opened.call_args_list[1].args[0].headers["Range"] == "bytes=4-7"


def test_valid_range_is_cached_without_redownload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    opened = install(monkeypatch, b"part", "bytes 4-7/8")
    path = tmp_path / "part"
    SUPPLY["fetch_part"]("https://example.invalid", 4, 7, 8, path)
    assert path.read_bytes() == b"part"
    SUPPLY["fetch_part"]("https://example.invalid", 4, 7, 8, path)
    assert opened.call_count == 1


@pytest.mark.parametrize(
    ("body", "span"),
    [
        (b"short", "bytes 0-7/8"),
        (b"too large", "bytes 0-7/8"),
        (b"12345678", "bytes 1-8/9"),
    ],
)
def test_wrong_range_or_size_never_promoted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: bytes, span: str
) -> None:
    opened = install(monkeypatch, body, span)
    path = tmp_path / "part"
    with pytest.raises(RuntimeError, match="MODEL_DOWNLOAD_FAILED"):
        SUPPLY["fetch_part"]("https://example.invalid", 0, 7, 8, path)
    assert opened.call_count == 5 and not path.exists()


@pytest.mark.parametrize("matching", [True, False])
def test_final_sha256_required_before_weights_are_published(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, matching: bool
) -> None:
    content = b"verified weights"
    install(monkeypatch, content, f"bytes 0-{len(content) - 1}/{len(content)}")
    lock: dict[str, Any] = {
        "status": "SELECTED_PRODUCTION",
        "model": "IndexTeam/Index-Translate-9B-GGUF",
        "revision": "a" * 40,
        "files": [
            {
                "path": "Index-Translate-9B.Q4_K_M.gguf",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest() if matching else "0" * 64,
                "official_mirror_revision": "a" * 40,
            }
        ],
    }
    path = tmp_path / "lock.json"
    path.write_text(json.dumps(lock))
    destination = tmp_path / "model"
    args = (path, destination, tmp_path / "cache")
    if matching:
        SUPPLY["download"](*args)
        assert (destination / lock["files"][0]["path"]).read_bytes() == content
    else:
        with pytest.raises(RuntimeError, match="INTEGRITY_FAILED"):
            SUPPLY["download"](*args)
        assert list(destination.iterdir()) == []


def test_http_200_is_not_a_range(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(SUPPLY["time"], "sleep", Mock())
    # A server ignoring Range cannot silently become a valid cached part.
    response = Mock(status=200)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(
        SUPPLY["urllib"].request, "urlopen", Mock(return_value=response)
    )
    with pytest.raises(RuntimeError, match="MODEL_DOWNLOAD_FAILED"):
        SUPPLY["fetch_part"]("https://example.invalid", 0, 7, 8, tmp_path / "part")
