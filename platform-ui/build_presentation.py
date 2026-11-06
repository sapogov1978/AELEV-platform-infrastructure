"""Build the immutable current presentation and its exact standalone SDK projection."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCES = (
    ROOT / "presentation/v6-filter-layout.css",
    ROOT / "presentation/v6-components.css",
    ROOT / "public/presentation/v5.css",
)
TARGETS = (
    ROOT / "public/presentation/v6.css",
    ROOT / "operator-ui-kit/package/generated/presentation-v6.css",
)


def build() -> bytes:
    parts = []
    for source in SOURCES:
        content = source.read_text(encoding="utf-8").rstrip() + "\n"
        if "@import" in content:
            raise ValueError(f"presentation source must be self-contained: {source}")
        parts.append(content)
    result = "\n".join(parts).encode()
    for target in TARGETS:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(result)
    return result


if __name__ == "__main__":
    build()
