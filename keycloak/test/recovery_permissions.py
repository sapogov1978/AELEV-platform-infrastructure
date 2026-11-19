"""Disposable Linux DAC probe; run as root with no live files or network."""

from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recovery  # noqa: E402


def readable(path: Path, uid: int, gid: int, groups: list[int]) -> bool:
    child = os.fork()
    if child == 0:
        try:
            os.setgroups(groups)
            os.setgid(gid)
            os.setuid(uid)
            path.read_bytes()
        except PermissionError:
            os._exit(1)
        except BaseException:
            os._exit(2)
        os._exit(0)
    _, status = os.waitpid(child, 0)
    code = os.waitstatus_to_exitcode(status)
    if code not in (0, 1):
        raise RuntimeError("permission probe failed unexpectedly")
    return code == 0


def main() -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("requires an isolated Linux root process")
    with tempfile.TemporaryDirectory(prefix="keycloak-recovery-dac-") as directory:
        root = Path(directory)
        root.chmod(0o755)
        old_umask = os.umask(0o077)
        try:
            old_tls = root / "old-tls/tls.key"
            recovery.write_private(old_tls, b"synthetic", mode=0o640, group=1000)
            old_status = root / "old-status"
            old_status.mkdir(mode=0o750)
            old_status.chmod(0o750)
            recovery.write_private(
                old_status / "credential", b"synthetic", mode=0o640, group=1000
            )
            for name in ("https", "status"):
                recovery.runtime_directory(root / name, 1000, create=True)
            tls = root / "https/tls.key"
            status = root / "status/credential"
            for target in (tls, status):
                recovery.write_private(target, b"synthetic", mode=0o640, group=1000)
            archive = root / "archive/secret"
            recovery.write_private(archive, b"synthetic-backup")
            old_password = root / "old-password"
            recovery.write_private(old_password, b"synthetic")
            passwords = [
                root / name for name in ("admin-password", "database-password")
            ]
            for target in passwords:
                recovery.write_private(target, b"synthetic", mode=0o640, group=1000)
        finally:
            os.umask(old_umask)
        checks = {
            "old_tls_directory_denies_uid_1000": not readable(
                old_tls, 1000, 1000, [1000]
            ),
            "old_status_directory_denies_uid_101": not readable(
                old_status / "credential", 101, 101, [1000]
            ),
            "restored_tls_readable_uid_1000": readable(tls, 1000, 1000, [1000]),
            "restored_status_readable_uid_101": readable(status, 101, 101, [1000]),
            "status_denied_without_additional_group": not readable(
                status, 101, 101, []
            ),
            "tls_denied_foreign_group": not readable(tls, 1002, 1002, [1002]),
            "archive_denied_runtime_uid": not readable(archive, 1000, 1000, [1000]),
            "archive_directory_private": archive.parent.stat().st_mode & 0o777 == 0o700,
            "archive_file_private": archive.stat().st_mode & 0o777 == 0o600,
            "old_password_denied_runtime_uid": not readable(
                old_password, 1000, 1000, [1000]
            ),
            "passwords_readable_runtime_uid": all(
                readable(target, 1000, 1000, [1000]) for target in passwords
            ),
            "password_readable_provisioner_different_primary_group": readable(
                passwords[0], 1001, 2000, [1000]
            ),
            "password_denied_provisioner_without_secret_group": not readable(
                passwords[0], 1001, 2000, []
            ),
        }
        result = {
            "python": platform.python_version(),
            "system": platform.system(),
            "umask_during_restore": "0077",
            "checks": checks,
            "runtime_directories": {
                name: {
                    "mode": oct((root / name).stat().st_mode & 0o777),
                    "gid": (root / name).stat().st_gid,
                }
                for name in ("https", "status")
            },
            "live_credentials_used": False,
            "network_used": False,
        }
        print(json.dumps(result, sort_keys=True))
        if not all(checks.values()):
            raise RuntimeError("Keycloak restore permission checks failed")


if __name__ == "__main__":
    main()
