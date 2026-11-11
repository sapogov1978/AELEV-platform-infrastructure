"""Linux recovery must keep the installer and runtime private states usable."""

import os
import sys
import tempfile
from pathlib import Path

import pytest

from aelev_spire.backup import create, restore
from aelev_spire.files import atomic


@pytest.mark.skipif(
    sys.platform != "linux" or os.geteuid() != 0,
    reason="requires the separate Linux root CHOWN/SETUID/SETGID recovery check",
)
def test_restore_keeps_distinct_runtime_and_installer_owners() -> None:
    with tempfile.TemporaryDirectory(prefix="spire-restore-owners-") as temporary:
        root = Path(temporary)
        root.chmod(0o711)
        original, target = root / "original", root / "target"
        roles = ["server", "operator", "installation"]
        for role in roles:
            atomic(original / role / "owner.json", {"owner": "infrastructure-spire"})
        atomic(original / "server/native/private-key", b"runtime-private-fixture")
        atomic(original / "operator/operations/revoked.json", {"phase": "REVOKED"})
        atomic(original / "installation/deployment.json", {"role": "server"})
        key, archive = root / "backup.key", root / "state.enc"
        atomic(key, os.urandom(32))
        create(original, roles, archive, key)
        target.mkdir(mode=0o711)
        for role in roles:
            path = target / role
            path.mkdir(mode=0o700)
            os.chown(path, 10001, 10001)
        receipt = root / "state.enc.receipt.json"
        with pytest.raises(ValueError, match="INSTALLER_OWNER_REQUIRED"):
            restore(target, roles, archive, key, receipt)
        assert all(not list((target / role).iterdir()) for role in roles)
        os.chown(target / "installation", 0, 0)
        restore(target, roles, archive, key, receipt)
        for role in roles:
            expected = (0, 0) if role == "installation" else (10001, 10001)
            for path in [target / role, *(target / role).rglob("*")]:
                info = path.stat()
                assert (info.st_uid, info.st_gid) == expected
                assert info.st_mode & 0o077 == 0
        assert not list(target.rglob(".restore-incomplete"))
        # A real serving process must read its own restored private files and
        # still be denied access to the operator's installation state.
        child = os.fork()
        if child == 0:
            try:
                os.setgroups([])
                os.setgid(10001)
                os.setuid(10001)
                assert (
                    target / "server/native/private-key"
                ).read_bytes() == b"runtime-private-fixture"
                assert (
                    b"REVOKED"
                    in (target / "operator/operations/revoked.json").read_bytes()
                )
                with pytest.raises(PermissionError):
                    (target / "installation/deployment.json").read_bytes()
            except BaseException:
                os._exit(1)
            os._exit(0)
        _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0
