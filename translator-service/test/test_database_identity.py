"""Host shell regressions with UID/stat/sync adapters; native DAC is separate."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import suppress
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
POSTGRES_UID = 999
SOURCE_ROOT = "/run/secrets/translator-database-server"
TARGET_ROOT = "/var/lib/translator-database-tls"

# Real shell and filesystem operations execute. Only Linux-only tools and UID
# ownership are adapted for a non-root macOS host. Native acceptance remains required.
SHIM = r"""
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import time

tool = Path(sys.argv[0]).name
args = sys.argv[1:]
state = Path(os.environ["IDENTITY_TEST_STATE"])
metadata = state / "owners.json"
owners = json.loads(metadata.read_text()) if metadata.exists() else {}
with (state / "commands.jsonl").open("a") as stream:
    stream.write(json.dumps({"tool": tool, "args": args}) + "\n")
if tool == "id":
    print(os.environ["IDENTITY_TEST_POSTGRES_UID"] if args[-1] == "postgres" else "0")
elif tool == "stat":
    path = Path(args[-1])
    info = path.lstat()
    uid, gid = owners.get(str(path), [0, 0])
    value = args[1]
    fields = {"u": str(uid), "g": str(gid),
        "a": format(stat.S_IMODE(info.st_mode), "o"), "h": str(info.st_nlink)}
    for name, data in fields.items():
        value = value.replace("%" + name, data)
    print(value)
elif tool == "chown":
    owners[args[1]] = [int(value) for value in args[0].split(":")]
    metadata.write_text(json.dumps(owners))
elif tool == "find":
    for path in sorted(Path(args[0]).iterdir()):
        if path.name != ".prepare-lock":
            print(path)
elif tool == "cp":
    gate = os.environ.get("IDENTITY_TEST_CP_GATE")
    if gate and not Path(gate).exists():
        Path(gate).touch()
        deadline = time.monotonic() + 8
        while not Path(os.environ["IDENTITY_TEST_CP_RELEASE"]).exists():
            if time.monotonic() >= deadline:
                sys.exit(72)
            time.sleep(0.01)
    shutil.copyfile(args[0], args[1])
elif tool == "mv":
    source, target = args
    Path(source).rename(target)
    if source in owners:
        owners[target] = owners.pop(source)
        metadata.write_text(json.dumps(owners))
elif tool == "sync":
    # Native run must exercise GNU sync -f; this adapter checks control flow only.
    assert args[0] == "-f"
else:
    raise AssertionError(tool)
"""


class DatabaseIdentityPreparationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="identity-shell-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / "source"
        self.target = self.base / "target"
        self.state = self.base / "state"
        self.bin = self.base / "bin"
        for path in (self.source, self.target, self.state, self.bin):
            path.mkdir(mode=0o700)
        for name in ("server.crt", "server.key", "client-ca.crt"):
            path = self.source / name
            path.write_bytes(("synthetic-" + name).encode())
            path.chmod(0o600)
        original = (ROOT / "database-identity-prepare.sh").read_text()
        for old, new in (
            ("source_root=" + SOURCE_ROOT, "source_root=" + str(self.source)),
            ("target_root=" + TARGET_ROOT, "target_root=" + str(self.target)),
        ):
            self.assertEqual(original.count(old), 1)
            original = original.replace(old, new)
        self.script = self.base / "identity.sh"
        self.script.write_text(original)
        self.script.chmod(0o700)
        shim = self.bin / "shim"
        shim.write_text("#!" + sys.executable + "\n" + SHIM)
        shim.chmod(0o700)
        for name in ("id", "stat", "chown", "find", "cp", "mv", "sync"):
            (self.bin / name).symlink_to("shim")
        self.environment = {
            "PATH": str(self.bin) + os.pathsep + "/usr/bin:/bin",
            "IDENTITY_TEST_STATE": str(self.state),
            "IDENTITY_TEST_POSTGRES_UID": str(POSTGRES_UID),
        }

    def snapshot(self) -> dict[str, Any]:
        owners_path = self.state / "owners.json"
        owners = json.loads(owners_path.read_text()) if owners_path.exists() else {}
        result: dict[str, Any] = {}
        for path in [self.target, *sorted(self.target.rglob("*"))]:
            info = path.lstat()
            result[str(path.relative_to(self.target))] = {
                "mode": stat.S_IMODE(info.st_mode),
                "owner": owners.get(str(path), [0, 0]),
                "link": os.readlink(path) if path.is_symlink() else None,
                "sha256": (
                    hashlib.sha256(path.read_bytes()).hexdigest()
                    if path.is_file() and not path.is_symlink()
                    else None
                ),
                "mtime_ns": info.st_mtime_ns if path.is_file() else None,
            }
        return result

    def start(self, **environment: str) -> subprocess.Popen[bytes]:
        # Stop before exec so a preexisting .prepare-PID can be created precisely.
        process = subprocess.Popen(
            [
                "/bin/sh",
                "-c",
                'kill -STOP $$; exec /bin/sh "$1"',
                "fixture",
                str(self.script),
            ],
            env={**self.environment, **environment},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.addCleanup(self.terminate, process)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            identity, status = os.waitpid(process.pid, os.WUNTRACED | os.WNOHANG)
            if identity:
                self.assertTrue(os.WIFSTOPPED(status))
                return process
            time.sleep(0.005)
        self.fail("Fixture shell did not stop before exec")

    @staticmethod
    def terminate(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)

    def finish(
        self, process: subprocess.Popen[bytes]
    ) -> subprocess.CompletedProcess[bytes]:
        stdout, stderr = process.communicate(timeout=10)
        return subprocess.CompletedProcess(
            process.args, process.returncode, stdout, stderr
        )

    def run_helper(self) -> subprocess.CompletedProcess[bytes]:
        process = self.start()
        process.send_signal(signal.SIGCONT)
        return self.finish(process)

    def wait_for_gate(self, gate: Path) -> None:
        deadline = time.monotonic() + 5
        while not gate.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(gate.exists(), "Fixture copy gate was not reached")

    def test_fresh_and_exact_repeat_preserve_private_pair(self) -> None:
        first = self.run_helper()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(
            set(path.name for path in self.target.iterdir()),
            {"server.crt", "server.key", "client-ca.crt"},
        )
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o700)
        for name in ("server.crt", "server.key", "client-ca.crt"):
            self.assertEqual(
                (self.target / name).read_bytes(), (self.source / name).read_bytes()
            )
            self.assertEqual(stat.S_IMODE((self.target / name).stat().st_mode), 0o600)
        before = self.snapshot()
        second = self.run_helper()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn(b"UNCHANGED", second.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_preexisting_predictable_stage_is_never_removed(self) -> None:
        process = self.start()
        stage = self.target / (".prepare-" + str(process.pid))
        stage.mkdir(mode=0o700)
        (stage / "foreign").write_bytes(b"must-survive-refusal")
        before = self.snapshot()
        process.send_signal(signal.SIGCONT)
        result = self.finish(process)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_invalid_source_does_not_remove_preexisting_stage(self) -> None:
        (self.source / "server.key").unlink()
        process = self.start()
        stage = self.target / (".prepare-" + str(process.pid))
        stage.mkdir(mode=0o700)
        (stage / "previous").write_bytes(b"preserve")
        before = self.snapshot()
        process.send_signal(signal.SIGCONT)
        self.assertNotEqual(self.finish(process).returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_partial_target_is_refused_without_change(self) -> None:
        (self.target / "server.crt").write_bytes(b"previous-partial-state")
        before = self.snapshot()
        self.assertNotEqual(self.run_helper().returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_foreign_target_ownership_is_preserved(self) -> None:
        (self.target / "foreign").write_bytes(b"foreign-state")
        (self.state / "owners.json").write_text(
            json.dumps({str(self.target): [12345, 12345]})
        )
        before = self.snapshot()
        self.assertNotEqual(self.run_helper().returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_stale_lock_and_stage_require_reconciliation(self) -> None:
        (self.target / ".prepare-lock").mkdir()
        (self.target / ".prepare-1").mkdir()
        (self.target / ".prepare-1" / "server.key").write_bytes(b"partial-key")
        before = self.snapshot()
        self.assertNotEqual(self.run_helper().returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_identity_drift_is_refused_without_overwrite(self) -> None:
        self.assertEqual(self.run_helper().returncode, 0)
        before = self.snapshot()
        (self.source / "server.crt").write_bytes(b"different-identity")
        self.assertNotEqual(self.run_helper().returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_empty_or_linked_source_is_refused_without_initialization(self) -> None:
        for case in ("empty", "symlink"):
            with self.subTest(case=case):
                key = self.source / "server.key"
                key.unlink()
                if case == "empty":
                    key.write_bytes(b"")
                    key.chmod(0o600)
                else:
                    key.symlink_to(self.source / "server.crt")
                before = self.snapshot()
                self.assertNotEqual(self.run_helper().returncode, 0)
                self.assertEqual(self.snapshot(), before)

    def test_concurrent_attempt_cannot_change_first_attempt_stage(self) -> None:
        gate, release = self.state / "copy-gate", self.state / "copy-release"
        first = self.start(
            IDENTITY_TEST_CP_GATE=str(gate), IDENTITY_TEST_CP_RELEASE=str(release)
        )
        first.send_signal(signal.SIGCONT)
        self.wait_for_gate(gate)
        before = self.snapshot()
        self.assertNotEqual(self.run_helper().returncode, 0)
        self.assertEqual(self.snapshot(), before)
        release.touch()
        self.assertEqual(self.finish(first).returncode, 0)

    def test_catchable_signal_stops_before_later_initialization(self) -> None:
        for sent in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=sent.name):
                gate = self.state / ("copy-gate-" + sent.name)
                release = self.state / ("copy-release-" + sent.name)
                (self.state / "commands.jsonl").unlink(missing_ok=True)
                process = self.start(
                    IDENTITY_TEST_CP_GATE=str(gate),
                    IDENTITY_TEST_CP_RELEASE=str(release),
                )
                process.send_signal(signal.SIGCONT)
                self.wait_for_gate(gate)
                process.send_signal(sent)
                release.touch()
                result = self.finish(process)
                self.assertEqual(result.returncode, 130, result.stderr)
                self.assertEqual(list(self.target.iterdir()), [])
                commands = [
                    json.loads(line)["tool"]
                    for line in (self.state / "commands.jsonl").read_text().splitlines()
                ]
                for forbidden in ("chown", "mv", "sync"):
                    self.assertNotIn(forbidden, commands)
                self.assertNotIn(b"PREPARED", result.stdout)

    def test_sigkill_preserves_unknown_stage_and_blocks_retry(self) -> None:
        gate, release = self.state / "kill-gate", self.state / "kill-release"
        process = self.start(
            IDENTITY_TEST_CP_GATE=str(gate), IDENTITY_TEST_CP_RELEASE=str(release)
        )
        process.send_signal(signal.SIGCONT)
        self.wait_for_gate(gate)
        os.killpg(process.pid, signal.SIGKILL)
        self.finish(process)
        before = self.snapshot()
        self.assertIn(".prepare-lock", before)
        self.assertNotEqual(self.run_helper().returncode, 0)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
