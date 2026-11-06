#!/usr/bin/env python3
"""Fail closed when the standalone audit boundary contains excluded material."""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

from prepare_ci_assets import ASSET


TEXT_SUFFIXES = {
    "",
    ".css",
    ".env",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".mmd",
    ".py",
    ".sh",
    ".sql",
    ".svg",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}

EXCLUDED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "__pycache__",
}

_ENGLISH = [
    "mar" + "ket(?:s|place)?",
    "finan" + "cial",
    "fin" + "ance",
    "trad" + "ing",
    "port" + "folio",
    "cap" + "ital",
    "invest" + "ment",
    "invest" + "or",
    "ven" + "ue",
    "securit" + "ies",
    "stock[ -]+" + "exchange",
    "broker" + "(?:age|dealer)",
    "order[ -]+" + "execution",
    "central[ -]+" + "bank",
    "monet" + "ary",
    "interest[ -]+" + "rates?",
    "government[ -]+" + "bonds?",
    "fisc" + "al",
    "corporate[ -]+" + "filings?",
    "earnings?[ -]+" + "releases?",
    "commodit" + "(?:y|ies)",
    "trade[ -]+" + "policy",
    "banking[ -]+" + "regulation",
    "liquid" + "ity",
]
_RUSSIAN = [
    "фин" + "анс",
    "рын" + "ок",
    "рыноч" + "н",
    "торг" + "ов",
    "инв" + "ест",
    "кап" + "итал",
    "порт" + "фел",
    "бир" + "ж",
]
FORBIDDEN = re.compile(
    r"(?i)(?<![a-z])(?:" + "|".join(_ENGLISH) + r")(?![a-z])|" + "|".join(_RUSSIAN)
)
NEGATED_BOUNDARY_DECLARATION = re.compile(
    r"(?i)(?:\bis not\b|\bcontains no\b|\brejects?\b|\bpurpose-neutral\b|"
    r"не является|не содержит|\bнет\b|отсутств(?:ует|уют)|целенезавис)"
)
NEGATED_BOUNDARY_FILES = {
    "CHANGELOG.md",
    "README.md",
    "docs/00-vision.md",
    "docs/01-axioms.md",
    "docs/03-system-overview.md",
    "docs/audit/SCOPE.md",
}


def candidates(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and not any(part in EXCLUDED_DIRECTORIES for part in path.parts)
        and not ("graphify-out" in path.parts and "cache" in path.parts)
        and path.suffix.lower() in TEXT_SUFFIXES
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    findings: list[str] = []
    for path in candidates(root):
        relative = path.relative_to(root).as_posix()
        if path.parent.name == "swagger-assets":
            dockerfile = path.parent.parent / "Dockerfile"
            pins = (
                {
                    match.group(3): match.group(1)
                    for line in dockerfile.read_text(encoding="utf-8").splitlines()
                    if (match := ASSET.fullmatch(line))
                }
                if dockerfile.is_file()
                else {}
            )
            if pins.get(path.name) != hashlib.sha256(path.read_bytes()).hexdigest():
                findings.append(f"{relative}:unverified-third-party-asset")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            allowed_declaration = (
                relative in NEGATED_BOUNDARY_FILES
                and NEGATED_BOUNDARY_DECLARATION.search(line)
            )
            if FORBIDDEN.search(line) and not allowed_declaration:
                findings.append(f"{relative}:{line_number}")
        if FORBIDDEN.search(relative):
            findings.append(f"{relative}:path")
    if findings:
        print("Excluded-domain material detected:")
        print("\n".join(findings))
        return 1
    print(f"Boundary scan passed: {len(candidates(root))} text files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
