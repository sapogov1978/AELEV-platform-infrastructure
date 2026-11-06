"""Restore verifier rejects corrupt or unsafe archives without touching originals."""

import hashlib
import io
import tarfile
from pathlib import Path

import pytest
from cold_backup_verify import offsets, verify


def sample(
    root: Path, *, name: str = "directory/message", kind: bytes = tarfile.REGTYPE
) -> str:
    with tarfile.open(root / "data.tar", "w") as target:
        member = tarfile.TarInfo(name)
        member.type = kind
        member.mode = 0o600
        member.size = 5 if kind == tarfile.REGTYPE else 0
        if kind == tarfile.SYMTYPE:
            member.linkname = "elsewhere"
        target.addfile(member, io.BytesIO(b"exact") if member.isfile() else None)
    return hashlib.sha256((root / "data.tar").read_bytes()).hexdigest()


def test_actual_archive_content_is_restored_and_verified(tmp_path: Path) -> None:
    sha = sample(tmp_path)
    destination = tmp_path / "restored"
    destination.mkdir()
    result = verify(tmp_path, "data", sha, destination)
    assert result["files"] == 1 and result["bytes"] == 5
    assert (destination / "directory/message").read_bytes() == b"exact"
    assert hashlib.sha256((tmp_path / "data.tar").read_bytes()).hexdigest() == sha


@pytest.mark.parametrize("name", ["../escape", "/absolute", "directory/../../escape"])
def test_untrusted_path_is_not_extracted(tmp_path: Path, name: str) -> None:
    sha = sample(tmp_path, name=name)
    with pytest.raises(ValueError, match="UNSAFE_MEMBER"):
        verify(tmp_path, "data", sha, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_symlink_is_not_followed(tmp_path: Path) -> None:
    sha = sample(tmp_path, kind=tarfile.SYMTYPE)
    with pytest.raises(ValueError, match="UNSAFE_MEMBER"):
        verify(tmp_path, "data", sha, tmp_path / "restored")


def test_changed_archive_is_rejected_before_extraction(tmp_path: Path) -> None:
    sha = sample(tmp_path)
    with (tmp_path / "data.tar").open("ab") as target:
        target.write(b"changed")
    with pytest.raises(ValueError, match="ARCHIVE_CHANGED"):
        verify(tmp_path, "data", sha, tmp_path / "restored")


def test_duplicate_archive_member_is_not_silently_overwritten(tmp_path: Path) -> None:
    sample(tmp_path)
    with tarfile.open(tmp_path / "data.tar", "a") as target:
        member = tarfile.TarInfo("directory/message")
        target.addfile(member, io.BytesIO())
    sha = hashlib.sha256((tmp_path / "data.tar").read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="UNSAFE_MEMBER"):
        verify(tmp_path, "data", sha, tmp_path / "restored")


def test_offsets_accept_zero_and_preserve_exact_partition_keys() -> None:
    assert offsets(b"topic:0:0\ntopic:1:19\n") == {("topic", 0): 0, ("topic", 1): 19}


@pytest.mark.parametrize(
    "raw", [b"", b"topic:0:1\ntopic:0:2", b"topic:-1:2", b"topic:0:-1", b"../topic:0:2"]
)
def test_bad_offset_evidence_is_not_accepted(raw: bytes) -> None:
    with pytest.raises(ValueError, match="OFFSETS_"):
        offsets(raw)
