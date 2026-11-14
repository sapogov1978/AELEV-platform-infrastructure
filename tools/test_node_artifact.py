"""Actual archive materialization, corruption and retry boundaries."""

import base64
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

from tools import node_artifact as artifact


class NodeArtifactTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def package(self, *, kind="source", members=None, compression=zipfile.ZIP_STORED):
        if members is None:
            members = (
                [
                    (
                        "owner/bootstrap.py",
                        b'print("technical owner")\n',
                        stat.S_IFREG | 0o644,
                    )
                ]
                if kind == "source"
                else [
                    (
                        "identity/client.key",
                        b"synthetic-private-input\n",
                        stat.S_IFREG | 0o600,
                    )
                ]
            )
        data = io.BytesIO()
        files = {}
        with zipfile.ZipFile(data, "w") as archive:
            for name, body, mode in members:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = mode << 16
                info.compress_type = compression
                archive.writestr(info, body)
                files[name] = {
                    "sha256": artifact.digest(
                        body.replace(b"\r\n", b"\n") if kind == "source" else body
                    )
                }
                if kind == "source":
                    files[name]["text"] = True
        payload = data.getvalue()
        manifest = (
            {
                "schema_version": 2,
                "repository": "technical/source",
                "source_commit": "a" * 40,
                "version": "1.0.0",
                "archive_sha256": artifact.digest(payload),
                "files": files,
            }
            if kind == "source"
            else {
                "schema_version": 1,
                "scope": "owner-one",
                "archive_sha256": artifact.digest(payload),
                "files": files,
            }
        )
        request = {
            "schema_version": 1,
            "kind": kind,
            "scope": "owner-one",
            "root": str(self.root / "installed"),
            "manifest": manifest,
            "manifest_sha256": artifact.digest(artifact.encode(manifest)),
            "archive_size": len(payload),
            "receiver_sha256": artifact.file_hash(Path(artifact.__file__)),
        }
        return request, payload

    def receive(self, request, payload):
        return artifact.receive(request, io.BytesIO(payload))

    def test_source_is_written_and_repeat_preserves_payload_and_receipt(self):
        request, payload = self.package()
        first = self.receive(request, payload)
        file = Path(first["path"]) / "owner/bootstrap.py"
        receipt = file.parents[2] / "receipt.json"
        original = (
            file.stat().st_ino,
            file.stat().st_mtime_ns,
            receipt.read_bytes(),
            receipt.stat().st_mtime_ns,
        )
        second = self.receive(request, payload)
        self.assertEqual(first, second)
        self.assertEqual(
            original,
            (
                file.stat().st_ino,
                file.stat().st_mtime_ns,
                receipt.read_bytes(),
                receipt.stat().st_mtime_ns,
            ),
        )
        self.assertEqual(file.read_bytes(), b'print("technical owner")\n')
        self.assertFalse(first["owner_readiness_verified"])

    def test_private_inputs_have_only_private_modes_and_no_content_in_reply(self):
        request, payload = self.package(kind="owner-input")
        result = self.receive(request, payload)
        root = Path(result["path"])
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((root / "identity").stat().st_mode & 0o777, 0o700)
        self.assertEqual((root / "identity/client.key").stat().st_mode & 0o777, 0o600)
        self.assertNotIn("synthetic-private-input", json.dumps(result))

    def test_new_generation_does_not_change_old(self):
        one, body = self.package()
        first = self.receive(one, body)
        two, body2 = self.package(
            members=[("owner/bootstrap.py", b"new release\n", stat.S_IFREG | 0o644)]
        )
        second = self.receive(two, body2)
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(
            (Path(first["path"]) / "owner/bootstrap.py").read_bytes(),
            b'print("technical owner")\n',
        )

    def test_canonical_source_text_uses_declared_lf_hash(self):
        request, payload = self.package(
            members=[
                ("owner/entry.sh", b"#!/bin/sh\r\necho ok\r\n", stat.S_IFREG | 0o755)
            ]
        )
        result = self.receive(request, payload)
        self.assertEqual(
            (Path(result["path"]) / "owner/entry.sh").read_bytes(),
            b"#!/bin/sh\necho ok\n",
        )
        self.assertEqual(
            (Path(result["path"]) / "owner/entry.sh").stat().st_mode & 0o777, 0o755
        )

    def test_receipt_from_older_receiver_is_preserved_after_tree_verification(self):
        request, payload = self.package()
        result = self.receive(request, payload)
        path = Path(result["path"]).parent / "receipt.json"
        original = json.loads(path.read_text())
        original["request_sha256"] = "b" * 64
        path.write_bytes(artifact.encode(original) + b"\n")
        original_bytes = path.read_bytes()
        self.assertEqual(self.receive(request, payload), result)
        self.assertEqual(path.read_bytes(), original_bytes)

    def test_changed_file_is_refused_without_overwriting_it(self):
        request, payload = self.package()
        result = self.receive(request, payload)
        file = Path(result["path"]) / "owner/bootstrap.py"
        file.write_text("operator change")
        with self.assertRaisesRegex(artifact.DeliveryError, "ARTIFACT_TREE_DRIFT"):
            self.receive(request, payload)
        self.assertEqual(file.read_text(), "operator change")

    def test_changed_modes_and_empty_directories_are_drift(self):
        request, payload = self.package()
        result = self.receive(request, payload)
        path = Path(result["path"]) / "owner/bootstrap.py"
        path.chmod(0o755)
        with self.assertRaisesRegex(
            artifact.DeliveryError, "ARTIFACT_TREE_PERMISSIONS_INVALID"
        ):
            self.receive(request, payload)
        path.chmod(0o644)
        (Path(result["path"]) / "extra-directory").mkdir(mode=0o755)
        with self.assertRaisesRegex(artifact.DeliveryError, "ARTIFACT_TREE_DRIFT"):
            self.receive(request, payload)

    def test_binary_carrier_executes_and_reports_corruption_without_raw_traceback(self):
        request, payload = self.package()
        header = (
            artifact.encode(
                {
                    "request": request,
                    "code": base64.b64encode(
                        Path(artifact.__file__).read_bytes()
                    ).decode(),
                }
            )
            + b"\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", artifact.BOOTSTRAP],
            input=header + payload,
            capture_output=True,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout)["status"], "ARTIFACT_MATERIALIZED")
        result = subprocess.run(
            [sys.executable, "-c", artifact.BOOTSTRAP],
            input=header + payload[:-1] + bytes([payload[-1] ^ 1]),
            capture_output=True,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout)["code"], "ARTIFACT_ARCHIVE_MISMATCH")
        self.assertEqual(result.stderr, b"")

    def test_extra_file_in_installed_generation_is_not_silently_accepted(self):
        request, payload = self.package()
        result = self.receive(request, payload)
        (Path(result["path"]) / "extra.py").write_text("unexpected")
        with self.assertRaisesRegex(artifact.DeliveryError, "ARTIFACT_TREE_DRIFT"):
            self.receive(request, payload)

    def test_corrupt_archive_has_no_published_generation(self):
        request, payload = self.package()
        with self.assertRaisesRegex(
            artifact.DeliveryError, "ARTIFACT_ARCHIVE_MISMATCH"
        ):
            self.receive(request, payload[:-1] + bytes([payload[-1] ^ 1]))
        self.assertFalse(list((self.root / "installed").glob("owner-one-*")))

    def test_truncated_and_trailing_streams_are_refused(self):
        for suffix, code in (
            (b"", "ARTIFACT_STREAM_TRUNCATED"),
            (b"extra", "ARTIFACT_STREAM_TRAILING_BYTES"),
        ):
            request, payload = self.package()
            body = payload[:-1] if not suffix else payload + suffix
            with (
                self.subTest(code=code),
                self.assertRaisesRegex(artifact.DeliveryError, code),
            ):
                self.receive(request, body)

    def test_manifest_with_unsafe_paths_is_refused_before_target_creation(self):
        for name in (
            "../escape",
            "/absolute",
            "a/../../b",
            "a\\b",
            "a//b",
            ".",
            ".git/config",
        ):
            request, payload = self.package(
                members=[(name, b"data", stat.S_IFREG | 0o644)]
            )
            with self.subTest(name=name), self.assertRaises(artifact.DeliveryError):
                self.receive(request, payload)
        self.assertFalse((self.root / "installed").exists())

    def test_symlink_and_compressed_members_are_refused(self):
        variants = [
            self.package(members=[("link", b"outside", stat.S_IFLNK | 0o777)]),
            self.package(compression=zipfile.ZIP_DEFLATED),
        ]
        for request, payload in variants:
            with self.assertRaisesRegex(
                artifact.DeliveryError, "ARTIFACT_MEMBER_TYPE_INVALID"
            ):
                self.receive(request, payload)
        self.assertFalse(list((self.root / "installed").glob("owner-one-*")))

    def test_duplicate_zip_names_are_refused(self):
        with self.assertWarns(UserWarning):
            request, payload = self.package(
                members=[
                    ("same", b"one", stat.S_IFREG | 0o644),
                    ("same", b"two", stat.S_IFREG | 0o644),
                ]
            )
        with self.assertRaisesRegex(
            artifact.DeliveryError, "ARTIFACT_MEMBER_SET_INVALID"
        ):
            self.receive(request, payload)

    def test_manifest_member_hash_and_file_set_are_enforced(self):
        for change in ("hash", "extra"):
            request, payload = self.package()
            if change == "hash":
                request["manifest"]["files"]["owner/bootstrap.py"]["sha256"] = "b" * 64
            else:
                request["manifest"]["files"]["not-in-zip"] = {
                    "sha256": "b" * 64,
                    "text": True,
                }
            request["manifest_sha256"] = artifact.digest(
                artifact.encode(request["manifest"])
            )
            with self.subTest(change=change), self.assertRaises(artifact.DeliveryError):
                self.receive(request, payload)

    def test_cross_owner_private_scope_is_refused(self):
        request, payload = self.package(kind="owner-input")
        request["scope"] = "another-owner"
        with self.assertRaisesRegex(
            artifact.DeliveryError, "ARTIFACT_INPUT_SCOPE_INVALID"
        ):
            self.receive(request, payload)

    def test_private_archive_cannot_relax_file_permissions(self):
        request, payload = self.package(
            kind="owner-input", members=[("client.key", b"test", stat.S_IFREG | 0o644)]
        )
        with self.assertRaisesRegex(
            artifact.DeliveryError, "ARTIFACT_MEMBER_PERMISSIONS_INVALID"
        ):
            self.receive(request, payload)

    def test_source_collision_and_source_root_symlink_are_refused(self):
        request, payload = self.package(
            members=[
                ("file", b"a", stat.S_IFREG | 0o644),
                ("file/child", b"b", stat.S_IFREG | 0o644),
            ]
        )
        with self.assertRaisesRegex(
            artifact.DeliveryError, "ARTIFACT_MEMBER_COLLISION"
        ):
            self.receive(request, payload)
        request, payload = self.package()
        (self.root / "installed").symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(artifact.DeliveryError, "ARTIFACT_SYMLINK_REFUSED"):
            self.receive(request, payload)

    def test_sync_failure_never_publishes_generation(self):
        request, payload = self.package()
        with mock.patch.object(
            artifact.os, "fsync", side_effect=OSError("synthetic failure")
        ):
            with self.assertRaises(OSError):
                self.receive(request, payload)
        self.assertFalse(list((self.root / "installed").glob("owner-one-*")))

    def test_local_controller_checks_archive_before_any_target_write(self):
        request, payload = self.package()
        archive = self.root / "input.zip"
        archive.write_bytes(payload[:-1])
        with self.assertRaisesRegex(
            artifact.DeliveryError, "ARTIFACT_LOCAL_ARCHIVE_MISMATCH"
        ):
            artifact.deliver({"kind": "local"}, request, archive)
        self.assertFalse((self.root / "installed").exists())

    def test_boolean_sizes_and_changed_receiver_are_refused(self):
        request, payload = self.package()
        request["archive_size"] = True
        with self.assertRaisesRegex(artifact.DeliveryError, "ARTIFACT_SIZE_INVALID"):
            self.receive(request, payload)
        request, payload = self.package()
        request["receiver_sha256"] = "b" * 64
        with self.assertRaisesRegex(
            artifact.DeliveryError, "ARTIFACT_RECEIVER_MISMATCH"
        ):
            self.receive(request, payload)

    def test_pack_inputs_selects_only_declared_private_files(self):
        inputs = self.root / "private"
        inputs.mkdir(mode=0o700)
        for name in ("selected.key", "not-selected.key"):
            path = inputs / name
            path.write_bytes(b"synthetic scoped input")
            path.chmod(0o600)
        output = self.root / "inputs.zip"
        manifest = artifact.pack_inputs(inputs, ["selected.key"], "owner-one", output)
        self.assertEqual(set(manifest["files"]), {"selected.key"})
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        request = artifact.prepare_request(
            "owner-input", "owner-one", str(self.root / "remote"), manifest, output
        )
        result = artifact.deliver({"kind": "local"}, request, output)
        self.assertEqual(
            (Path(result["path"]) / "selected.key").read_bytes(),
            b"synthetic scoped input",
        )
        self.assertFalse((Path(result["path"]) / "not-selected.key").exists())
        with self.assertRaises(FileExistsError):
            artifact.pack_inputs(inputs, ["selected.key"], "owner-one", output)

    def test_packer_rejects_public_permissions_and_symlink_before_archive_creation(
        self,
    ):
        root = self.root / "inputs"
        root.mkdir(mode=0o700)
        path = root / "client.key"
        path.write_bytes(b"synthetic input")
        path.chmod(0o644)
        output = self.root / "inputs.zip"
        with self.assertRaisesRegex(
            artifact.DeliveryError, "ARTIFACT_PRIVATE_INPUT_REQUIRED"
        ):
            artifact.pack_inputs(root, ["client.key"], "owner-one", output)
        self.assertFalse(output.exists())
        path.chmod(0o600)
        link = root / "linked.key"
        link.symlink_to(path)
        with self.assertRaisesRegex(artifact.DeliveryError, "ARTIFACT_SYMLINK_REFUSED"):
            artifact.pack_inputs(root, ["linked.key"], "owner-one", output)
        self.assertFalse(output.exists())

    def test_request_factory_preserves_target_path_without_probing_local_filesystem(
        self,
    ):
        request, payload = self.package()
        archive = self.root / "source.zip"
        archive.write_bytes(payload)
        remote = self.root / "remote-prefix"
        remote.symlink_to(self.root, target_is_directory=True)
        result = artifact.prepare_request(
            "source",
            "owner-one",
            str(remote / "node-only"),
            request["manifest"],
            archive,
        )
        self.assertEqual(result["root"], str(remote / "node-only"))

    def test_receiver_hash_check_survives_python_optimization(self):
        request, payload = self.package()
        marker = self.root / "unexpected-effect"
        code = (
            "from pathlib import Path;Path(" + repr(str(marker)) + ").touch()"
        ).encode()
        wire = (
            artifact.encode(
                {"request": request, "code": base64.b64encode(code).decode()}
            )
            + b"\n"
            + payload
        )
        result = subprocess.run(
            [sys.executable, "-O", "-c", artifact.BOOTSTRAP],
            input=wire,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
