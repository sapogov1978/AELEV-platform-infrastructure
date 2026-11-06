"""Regressions for the security of this directory's operator tools."""

from __future__ import annotations

import ast
from pathlib import Path

import black
import black.cache
import pytest
import _pytest.tmpdir


def test_black_cache_cannot_escape_its_directory(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    mode = black.Mode(python_cell_magics={"/../../../../outside"})

    cache_file = black.cache.get_cache_file(mode, cache_root)

    assert cache_file.resolve().is_relative_to(cache_root.resolve())


def test_black_preserves_python_semantics() -> None:
    source = "result=1+2\n"

    formatted = black.format_str(source, mode=black.Mode())

    assert formatted == "result = 1 + 2\n"
    assert ast.dump(ast.parse(formatted)) == ast.dump(ast.parse(source))


def test_pytest_rejects_a_symlink_as_its_temporary_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "unrelated"
    target.mkdir()
    target_mode = target.stat().st_mode
    (tmp_path / "pytest-of-security-regression").symlink_to(target)
    monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", str(tmp_path))
    monkeypatch.setattr(_pytest.tmpdir, "get_user", lambda: "security-regression")
    factory = pytest.TempPathFactory(
        given_basetemp=None,
        retention_count=3,
        retention_policy="all",
        trace=lambda *args: None,
        _ispytest=True,
    )

    with pytest.raises(OSError, match="symbolic link"):
        factory.getbasetemp()

    assert target.stat().st_mode == target_mode
    assert not list(target.iterdir())
