"""Real shell and Compose config checks with synthetic files and an offline mc stub."""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from typing import TypedDict, cast

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "AELEV_INFRASTRUCTURE_MINIO_PROVISION_"
ACCESS = "fixture-bucket-user"
SECRET = ' FIXTURE_PROVISION_PASSWORD_NEVER_PUBLIC "quoted"\\tail '
ROOT_SECRET = "FIXTURE_ROOT_PASSWORD_NEVER_PUBLIC"


class McCall(TypedDict):
    argv: list[str]
    stdin: str
    leaked_env: dict[str, str]


class FileCredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="minio-file-secret-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        self.log = self.directory / "mc-calls.jsonl"
        self.root_user = self.file("root-user", b"fixture-root")
        self.root_password = self.file("root-password", ROOT_SECRET.encode())
        self.ca = self.file("ca.crt", b"fixture-public-ca")
        self.access = self.file("access-key", ACCESS.encode())
        self.secret = self.file("secret-key", SECRET.encode())
        stub = self.bin / "mc"
        stub.write_text(
            "#!" + sys.executable + "\n"
            "import json, os, sys\n"
            "a=sys.argv[1:]\n"
            "alias=a[:2]==['alias','set']; user=a[:3]==['admin','user','add']\n"
            "p=sys.stdin.read() if ((alias or user) and len(a)==4) else ''\n"
            "v={k:x for k,x in os.environ.items() if 'PASSWORD_NEVER_PUBLIC' in x}\n"
            "fd=os.open(os.environ['FIXTURE_LOG'],os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600)\n"
            "with os.fdopen(fd,'a') as f: f.write(json.dumps({'argv':a,'stdin':p,'leaked_env':v})+'\\n')\n"
            "if (alias and os.environ.get('FIXTURE_FAIL')=='alias') or (user and os.environ.get('FIXTURE_FAIL')=='user'):\n"
            " print('RAW_FIXTURE_PASSWORD_NEVER_PUBLIC'); print('RAW_FIXTURE_PASSWORD_NEVER_PUBLIC', file=sys.stderr); sys.exit(17)\n"
        )
        stub.chmod(0o700)

    def file(self, name: str, body: bytes) -> Path:
        path = self.directory / name
        path.write_bytes(body)
        path.chmod(0o600)
        return path

    def run_script(
        self, *, files: bool = True, changes: Mapping[str, str | None] | None = None
    ) -> subprocess.CompletedProcess[str]:
        text = (ROOT / "provision-bucket.sh").read_text()
        # Only public hardcoded filesystem locations are projected into the fixture.
        for original, target in (
            ("/run/secrets/minio-root-user", self.root_user),
            ("/run/secrets/minio-root-password", self.root_password),
            ("/run/secrets/minio-ca.crt", self.ca),
            ("/tmp/policy.json", self.directory / "legacy-policy.json"),
        ):
            self.assertIn(original, text)
            text = text.replace(original, str(target))
        script = self.file("provision.sh", text.encode())
        environment = {
            "PATH": str(self.bin) + os.pathsep + os.defpath,
            "LANG": "C",
            "FIXTURE_LOG": str(self.log),
            "MC_CONFIG_DIR": str(self.directory / "mc"),
            "AELEV_INFRASTRUCTURE_MINIO_ENDPOINT": "https://minio.owner.test:19000",
            PREFIX + "BUCKET": "fixture-bucket",
            PREFIX + "POLICY": "fixture-policy",
        }
        if files:
            environment.update(
                {
                    PREFIX + "ACCESS_KEY_FILE": str(self.access),
                    PREFIX + "SECRET_KEY_FILE": str(self.secret),
                }
            )
        else:
            environment.update(
                {PREFIX + "ACCESS_KEY": ACCESS, PREFIX + "SECRET_KEY": SECRET}
            )
        for key, value in (changes or {}).items():
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = value
        return subprocess.run(
            ["/bin/sh", str(script)],
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def calls(self) -> list[McCall]:
        if not self.log.exists():
            return []
        return [
            cast(McCall, json.loads(line)) for line in self.log.read_text().splitlines()
        ]

    def test_file_pairs_reach_stdin_without_password_argv_env_or_output(self) -> None:
        self.access.write_bytes((ACCESS + "\n").encode())
        self.root_password.write_bytes((ROOT_SECRET + "\n").encode())
        result = self.run_script(
            changes={
                PREFIX + "ACCESS_KEY": "",
                PREFIX + "SECRET_KEY": "",
                "file_root_password": ROOT_SECRET,
                "file_secret_key": SECRET,
                "credential_value": SECRET,
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.calls()
        self.assertEqual(len(rows), 6)
        self.assertEqual(
            rows[0]["argv"],
            ["alias", "set", "infrastructure", "https://minio.owner.test:19000"],
        )
        self.assertEqual(rows[0]["stdin"], "fixture-root\n" + ROOT_SECRET + "\n")
        self.assertEqual(rows[3]["argv"], ["admin", "user", "add", "infrastructure"])
        self.assertEqual(rows[3]["stdin"], ACCESS + "\n" + SECRET + "\n")
        self.assertEqual(
            rows[-1]["argv"],
            [
                "admin",
                "policy",
                "attach",
                "infrastructure",
                "fixture-policy",
                "--user",
                ACCESS,
            ],
        )
        for row in rows:
            self.assertFalse(row["leaked_env"])
            self.assertNotIn(SECRET, str(row["argv"]))
            self.assertNotIn(ROOT_SECRET, str(row["argv"]))
        self.assertNotIn("PASSWORD_NEVER_PUBLIC", result.stdout + result.stderr)
        self.assertEqual(stat.S_IMODE((self.directory / "mc").stat().st_mode), 0o700)

    def test_partial_empty_or_mixed_mode_fails_before_mc(self) -> None:
        for changes in (
            {PREFIX + "ACCESS_KEY_FILE": None},
            {PREFIX + "SECRET_KEY_FILE": None},
            {PREFIX + "ACCESS_KEY_FILE": ""},
            {PREFIX + "SECRET_KEY_FILE": ""},
            {PREFIX + "ACCESS_KEY": ACCESS},
            {PREFIX + "SECRET_KEY": SECRET},
        ):
            with self.subTest(fields=list(changes)):
                result = self.run_script(changes=changes)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls(), [])
                self.assertNotIn("PASSWORD_NEVER_PUBLIC", result.stdout + result.stderr)

    def test_bad_byte_framing_and_bounds_fail_before_mc(self) -> None:
        for body in (
            b"",
            b"has\x00nul",
            b"has\rreturn",
            b"line\r\n",
            b"two\nlines",
            b"two\n\n",
            b"\xff",
            b"\x80",
            "valid-unicode-ñ".encode(),
            b"x" * 4096,
            b"x" * 4097,
        ):
            with self.subTest(length=len(body)):
                self.secret.write_bytes(body)
                result = self.run_script()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls(), [])

    def test_root_password_pair_is_validated_before_first_mc(self) -> None:
        self.root_password.write_bytes(b"root\nsecond")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_missing_nonregular_symlink_and_read_failure_do_not_fallback(self) -> None:
        for path in (self.directory / "missing", self.directory):
            with self.subTest(path=path.name):
                result = self.run_script(
                    changes={PREFIX + "SECRET_KEY_FILE": str(path)}
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls(), [])
        link = self.directory / "secret-link"
        link.symlink_to(self.secret)
        self.assertNotEqual(
            self.run_script(changes={PREFIX + "SECRET_KEY_FILE": str(link)}).returncode,
            0,
        )
        self.assertEqual(self.calls(), [])
        self.secret.chmod(0)
        if os.getuid() != 0:
            self.assertNotEqual(self.run_script().returncode, 0)
            self.assertEqual(self.calls(), [])

    def test_decoded_4095_byte_line_with_optional_final_lf_is_accepted(self) -> None:
        self.secret.write_bytes(b"x" * 4095 + b"\n")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[3]["stdin"], ACCESS + "\n" + "x" * 4095 + "\n")

    def test_mc_failures_are_sanitized_and_new_user_failure_stops_attach(self) -> None:
        for failure in ("alias", "user"):
            with self.subTest(failure=failure):
                self.log.unlink(missing_ok=True)
                result = self.run_script(changes={"FIXTURE_FAIL": failure})
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("PASSWORD_NEVER_PUBLIC", result.stdout + result.stderr)
                self.assertFalse(
                    any(
                        r["argv"][:3] == ["admin", "policy", "attach"]
                        for r in self.calls()
                    )
                )
                private = list((self.directory / "mc").glob("*.log"))
                self.assertTrue(private)
                self.assertTrue(
                    any(
                        "RAW_FIXTURE_PASSWORD_NEVER_PUBLIC" in p.read_text()
                        for p in private
                    )
                )
                self.assertTrue(
                    all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in private)
                )

    def test_legacy_inline_sequence_and_user_failure_behavior_remain(self) -> None:
        result = self.run_script(files=False, changes={"FIXTURE_FAIL": "user"})
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.calls()
        self.assertEqual(rows[0]["argv"][-2:], ["fixture-root", ROOT_SECRET])
        self.assertEqual(rows[3]["argv"][-2:], [ACCESS, SECRET])
        self.assertEqual(rows[-1]["argv"][:3], ["admin", "policy", "attach"])


class ComposeFileSecretTests(unittest.TestCase):
    def render(
        self, *, selected: bool, values: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        compose = shutil.which("docker-compose")
        self.assertIsNotNone(compose, "standalone Compose config plugin is required")
        with tempfile.TemporaryDirectory(prefix="minio-file-model-") as name:
            command = [
                str(compose),
                "--project-directory",
                str(ROOT),
                "--env-file",
                str(ROOT / ".env.example"),
                "-f",
                str(ROOT / "compose.yml"),
                "-f",
                str(ROOT / "compose.owner-image.yml"),
            ]
            if selected:
                command += ["-f", str(ROOT / "compose.bucket-secrets.yml")]
            command += [
                "--profile",
                "*",
                "config",
                "--no-env-resolution",
                "--format",
                "json",
            ]
            environment = {
                "PATH": os.defpath,
                "HOME": name,
                "DOCKER_CONFIG": name,
                "DOCKER_HOST": "unix://" + name + "/unavailable.sock",
                "AELEV_INFRASTRUCTURE_MINIO_IMAGE": "sha256:22a301efbe53bf4fe6a962118ac7eeb1770a9ca1e588eb268251b18a2d887e75",
                **values,
            }
            return subprocess.run(
                command,
                cwd=ROOT,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )

    def test_omission_is_byte_equal_and_selection_changes_only_bucket_two_files(
        self,
    ) -> None:
        values = {
            PREFIX + "ACCESS_KEY_FILE": "/fixture/secrets/provision/access-key",
            PREFIX + "SECRET_KEY_FILE": "/fixture/secrets/provision/secret-key",
        }
        old = self.render(selected=False, values={})
        omitted = self.render(selected=False, values=values)
        self.assertEqual(old.returncode, 0, old.stderr)
        self.assertEqual(omitted.returncode, 0, omitted.stderr)
        self.assertEqual(old.stdout, omitted.stdout)
        selected = self.render(selected=True, values=values)
        self.assertEqual(selected.returncode, 0, selected.stderr)
        model, before = json.loads(selected.stdout), json.loads(old.stdout)
        bucket = model["services"]["bucket-provisioner"]
        for suffix, name in (
            ("ACCESS_KEY_FILE", "access"),
            ("SECRET_KEY_FILE", "secret"),
        ):
            target = "/run/secrets/minio-provision-" + name + "-key"
            self.assertEqual(bucket["environment"].pop(PREFIX + suffix), target)
            mount = next(v for v in bucket["volumes"] if v["target"] == target)
            self.assertEqual(mount["source"], values[PREFIX + suffix])
            self.assertTrue(mount["read_only"])
            self.assertFalse(mount["bind"]["create_host_path"])
            bucket["volumes"].remove(mount)
        self.assertEqual(model, before)

    def test_selected_profile_requires_both_nonempty_host_file_inputs(self) -> None:
        for suffix in ("ACCESS_KEY_FILE", "SECRET_KEY_FILE"):
            for empty in (False, True):
                values = {
                    PREFIX + "ACCESS_KEY_FILE": "/fixture/access-key",
                    PREFIX + "SECRET_KEY_FILE": "/fixture/secret-key",
                }
                if empty:
                    values[PREFIX + suffix] = ""
                else:
                    del values[PREFIX + suffix]
                with self.subTest(field=suffix, empty=empty):
                    result = self.render(selected=True, values=values)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(PREFIX + suffix, result.stderr)


if __name__ == "__main__":
    unittest.main()
