"""Publish the same deterministic operator helper, never runtime credentials."""

from __future__ import annotations

import hashlib
from pathlib import Path
import zipfile


def main() -> None:
    root = Path(__file__).resolve().parent
    source = root / "renewal_delivery.py"
    data = source.read_bytes()
    for owner in (root / "postgresql", root / "observability/delivery"):
        target = owner / "vendor/aelev-renewal-delivery-1.0.0.zip"
        info = zipfile.ZipInfo("renewal_delivery.py", date_time=(2026, 10, 24, 0, 0, 0))
        info.external_attr = 0o100644 << 16
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(info, data)
        print(owner.relative_to(root), hashlib.sha256(target.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
