"""Opt-in real CLI/volume delivery; this fixture does not claim native bootstrap."""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
import test_local_inputs as inputs

configuration = inputs.configuration
local = inputs.local

IMAGE = os.environ.get("AELEV_INFRASTRUCTURE_SPIRE_RENEWAL_TEST_IMAGE", "")
pytestmark = pytest.mark.skipif(
    not IMAGE, reason="Explicit native helper image required"
)


@pytest.mark.parametrize(
    "runtime_variable",
    [
        "AELEV_INFRASTRUCTURE_SPIRE_TEST_IMAGE",
        "AELEV_INFRASTRUCTURE_SPIRE_SENDER_TEST_IMAGE",
    ],
)
def test_runtime_first_mount_is_writable_by_certificate_delivery(
    tmp_path: Path,
    runtime_variable: str,
) -> None:
    runtime = os.environ.get(runtime_variable, "")
    if not runtime:
        pytest.skip("Explicit native runtime image required")
    volume = "spire-renewal-mount-" + uuid.uuid4().hex
    subprocess.run(
        [
            "docker",
            "volume",
            "create",
            "--label",
            "io.aelev.owner=infrastructure-spire",
            "--label",
            "io.aelev.installation=" + volume,
            volume,
        ],
        capture_output=True,
        check=True,
        timeout=30,
    )
    try:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--user",
                "10001:10001",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--memory",
                "128m",
                "--pids-limit",
                "32",
                "--mount",
                "type=volume,src=" + volume + ",dst=/run/certificate-renewal",
                "--entrypoint",
                "python",
                runtime,
                "-S",
                "-c",
                "import os; from pathlib import Path; "
                "root=Path('/run/certificate-renewal'); "
                "assert root.stat().st_uid == root.stat().st_gid == 10001; "
                "assert root.stat().st_mode & 0o777 == 0o700; "
                "assert not list(root.iterdir()); "
                "probe=root/'.permission-probe'; "
                "fd=os.open(probe,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600); "
                "os.write(fd,b'probe'); os.fsync(fd); os.close(fd); "
                "probe.unlink(); assert not list(root.iterdir())",
            ],
            capture_output=True,
            timeout=60,
        )
        diagnostic = tmp_path / "first-mount-private.stderr"
        diagnostic.write_bytes(result.stderr)
        diagnostic.chmod(0o600)
        assert result.returncode == 0, "Native first mount is not writable by UID 10001"
    finally:
        subprocess.run(
            ["docker", "volume", "rm", volume],
            capture_output=True,
            check=True,
            timeout=30,
        )


@pytest.mark.parametrize("network", [False, True], ids=["legacy-three", "network-four"])
def test_three_real_approvals_deliver_and_revoke_exact_profiles(
    tmp_path: Path, local: Any, configuration: dict[str, Any], network: bool
) -> None:
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))

    def module(name: str, source: Path) -> Any:
        specification = importlib.util.spec_from_file_location(name, source)
        assert specification and specification.loader
        value: Any = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(value)
        return value

    spire_local = module("isolated_spire_local", root / "tools/spire_local.py")
    spire_renewal = module("isolated_spire_renewal", root / "tools/spire_renewal.py")

    work = tmp_path.resolve()
    work.chmod(0o700)
    material = work / "material"
    if network:
        inputs.network_configuration(configuration)
    local.prepare(configuration, material)
    requests = json.loads((material / "renewal-requests.json").read_bytes())
    profiles: dict[str, dict[str, str]] = {}
    for name, entries in requests["profiles"].items():
        operator = work / name.removesuffix(".json")
        operator.mkdir(mode=0o700)
        reports = operator / "reports"
        reports.mkdir(mode=0o700)
        approved: list[dict[str, Any]] = []
        for entry in entries:
            issuer = entry["issuer_scope"]
            issuer_key = (
                material / "authorities" / (issuer + ".key")
                if issuer in {"query", "server", "write"}
                else Path(
                    configuration["issued"][entry["id"]]["issuer_certificate"]
                ).with_suffix(".key")
            )
            approved.append(
                {
                    key: entry[key]
                    for key in ("id", "certificate", "key", "issuer_certificate")
                }
                | {"issuer_key": str(issuer_key)}
            )
        approval = operator / "approval.json"
        spire_local.save(
            approval,
            {
                "schema_version": 1,
                "environment": "test",
                "consumer_owner": spire_renewal.OWNER,
                "openssl": str(
                    Path(shutil.which("openssl") or "missing-openssl").resolve()
                ),
                "report": str(reports / name),
                "certificates": approved,
            },
        )
        profiles[name] = {"approval": str(approval), "state": str(operator / "state")}
    source = work / "renewal.json"
    timer_test = os.environ.get("AELEV_INFRASTRUCTURE_SPIRE_RENEWAL_TIMER_TEST") == "1"
    if timer_test:
        assert sys.platform == "darwin"
    spire_local.save(
        source,
        {
            "schema_version": 1,
            "environment": "test",
            "maintenance": "launchd" if timer_test else "operator",
            "helper_image": IMAGE,
            "profiles": profiles,
        },
    )
    project = "spire-renewal-test-" + uuid.uuid4().hex[:12]
    created: dict[str, dict[str, str]] = {}
    timers = (
        {
            "com.aelev.telemetry-renewal."
            + hashlib.sha256(profile["state"].encode()).hexdigest()[:16]: profile[
                "state"
            ]
            for profile in profiles.values()
        }
        if timer_test
        else {}
    )
    domain = "gui/" + str(os.getuid())

    def retire_timers() -> None:
        for label, state in timers.items():
            plist = Path.home() / "Library/LaunchAgents" / (label + ".plist")
            if plist.exists():
                assert not plist.is_symlink()
                value = plistlib.loads(plist.read_bytes())
                assert value["Label"] == label
                assert value["ProgramArguments"][-2:] == ["--state", state]
                with (Path(state) / ".lock").open("a") as lock:
                    deadline = time.monotonic() + 60
                    while True:
                        try:
                            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            assert time.monotonic() < deadline, (
                                "SPIRE_RENEWAL_TEST_TASK_STILL_ACTIVE"
                            )
                            time.sleep(1)
                    subprocess.run(
                        ["/bin/launchctl", "bootout", domain + "/" + label],
                        capture_output=True,
                        timeout=30,
                    )
                check = subprocess.run(
                    ["/bin/launchctl", "print", domain + "/" + label],
                    capture_output=True,
                    timeout=30,
                )
                assert check.returncode != 0
                plist.unlink()

    for label in timers:
        assert not (Path.home() / "Library/LaunchAgents" / (label + ".plist")).exists()
        existing = subprocess.run(
            ["/bin/launchctl", "print", domain + "/" + label],
            capture_output=True,
            timeout=30,
        )
        assert existing.returncode != 0

    def docker(*arguments: str, payload: bytes | None = None) -> bytes:
        result = subprocess.run(
            ["docker", *arguments], input=payload, capture_output=True, timeout=120
        )
        if result.returncode:
            diagnostic = work / ("docker-" + uuid.uuid4().hex + ".stderr")
            spire_local.private(diagnostic, result.stderr)
            raise AssertionError(
                "SPIRE_RENEWAL_NATIVE_TEST_FAILED private_diagnostic=" + str(diagnostic)
            )
        return result.stdout

    image = docker("image", "inspect", "--format", "{{.Id}}", IMAGE).decode().strip()
    roles = {
        "observer-secrets",
        "sender-secrets",
        "status-secrets",
        "certificate-renewal-status",
    }
    if network:
        roles.add("public-source-secrets")
    try:
        for role in sorted(roles):
            name = project + "_" + role
            labels = {
                "io.aelev.owner": spire_renewal.OWNER,
                "io.aelev.installation": project,
                "io.aelev.role": role,
            }
            # Unique names must be absent before creation; never adopt a volume.
            existing = (
                docker("volume", "ls", "--format", "{{.Name}}").decode().splitlines()
            )
            assert name not in existing
            docker(
                "volume",
                "create",
                *[
                    item
                    for key, value in labels.items()
                    for item in ("--label", key + "=" + value)
                ],
                name,
            )
            created[name] = labels
        mounts = [
            item
            for role in sorted(roles)
            for item in (
                "--mount",
                "type=volume,src=" + project + "_" + role + ",dst=/target/" + role,
            )
        ]
        # This isolated fixture seeds only this consumer's initial leaf/key/trust;
        # no issuer key or other owner's credentials are mounted in the helper.
        seed = """import json,os,shutil
from pathlib import Path
markers = {"observer":"observation-observer",
 "sender":"observation-sender", "status":"observation-status"}
if (Path("/input")/"public-source").is_dir():
 markers["public-source"]="public-source-secrets"
for role,marker in markers.items():
 target=Path("/target")/(role+"-secrets")
 for source in (Path("/input")/role).iterdir():
  shutil.copyfile(source,target/source.name)
 value={"owner":"infrastructure-spire","role":marker}
 if role=="public-source": value["project"]=os.environ["TEST_INSTALLATION"]
 (target/"owner.json").write_text(json.dumps(value))
for directory in Path("/target").iterdir():
 os.chown(directory,10001,10001)
 for file in directory.iterdir():
  file.chmod(0o600);os.chown(file,10001,10001)
"""
        docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "DAC_READ_SEARCH",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "256m",
            "--pids-limit",
            "32",
            "--env",
            "TEST_INSTALLATION=" + project,
            *mounts,
            *[
                item
                for role in (
                    ["observer", "sender", "status"]
                    + (["public-source"] if network else [])
                )
                for item in (
                    "--mount",
                    "type=bind,src="
                    + str(material / "initial-input" / role)
                    + ",dst=/input/"
                    + role
                    + ",readonly",
                )
            ],
            "--entrypoint",
            "python",
            image,
            "-c",
            seed,
        )
        native = {
            "spire-server": {
                "Image": image,
                "Config": {
                    "Labels": {
                        "io.aelev.owner": spire_renewal.OWNER,
                        "io.aelev.installation": project,
                    }
                },
            }
        }
        # Coordinate real checked-in CLI with evidence kept outside its checkout.
        (work / "observability").mkdir()
        (work / "observability/delivery").symlink_to(
            root / "observability/delivery", target_is_directory=True
        )
        result = spire_renewal.apply(work, source, native)
        assert result["status"] == "FRESH_RECEIPTS_VERIFIED"
        assert result["automatic_maintenance_verified"] is timer_test
        if timer_test:
            assert set(result["timer_labels"]) == set(timers)
            initial = {
                name: json.loads(
                    Path(
                        spire_renewal.read(Path(profile["approval"]))["report"]
                    ).read_bytes()
                )["checked_at"]
                for name, profile in profiles.items()
            }
            deadline = time.monotonic() + 100
            refreshed = False
            while time.monotonic() < deadline:
                current = {
                    name: json.loads(
                        Path(
                            spire_renewal.read(Path(profile["approval"]))["report"]
                        ).read_bytes()
                    )
                    for name, profile in profiles.items()
                }
                if all(
                    report["healthy"] is True
                    and report["checked_at"] >= initial[name] + 45
                    for name, report in current.items()
                ):
                    refreshed = True
                    break
                time.sleep(1)
            assert refreshed, "SPIRE_RENEWAL_TIMER_CYCLE_NOT_CONFIRMED"
        cli = [sys.executable, str(root / "observability/delivery/renewal.py")]
        for profile_name in ["status-query.json"] + (
            ["public-source.json"] if network else []
        ):
            selected = profiles[profile_name]
            leaf = spire_renewal.read(Path(selected["approval"]))["certificates"][0]
            previous_certificate = Path(leaf["certificate"]).read_bytes()
            previous_key = Path(leaf["key"]).read_bytes()
            spire_renewal.invoke(
                [*cli, "once", "--force", "--state", selected["state"]], work, work
            )
            assert Path(leaf["certificate"]).read_bytes() != previous_certificate
            assert Path(leaf["key"]).read_bytes() == previous_key
        before = {
            name: Path(profile["state"]).joinpath("approval.json").read_bytes()
            for name, profile in profiles.items()
        }
        repeated = spire_renewal.apply(work, source, native)
        assert repeated == result
        assert before == {
            name: Path(profile["state"]).joinpath("approval.json").read_bytes()
            for name, profile in profiles.items()
        }
        retire_timers()
        read_reports = (
            "import json;from pathlib import Path;"
            'folder=Path("/var/lib/aelev-renewal/certificate-renewal-status");'
            "print(json.dumps({p.name:json.loads(p.read_bytes()) "
            'for p in folder.glob("*.json")}))'
        )
        report_mount = (
            "type=volume,src=" + project + "_certificate-renewal-status,"
            "dst=/var/lib/aelev-renewal/certificate-renewal-status,readonly"
        )

        def received() -> dict[str, Any]:
            return json.loads(
                docker(
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--user",
                    "10001:10001",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--memory",
                    "256m",
                    "--pids-limit",
                    "32",
                    "--mount",
                    report_mount,
                    "--entrypoint",
                    "python",
                    image,
                    "-c",
                    read_reports,
                )
            )

        receipts = received()
        spire_local.save(work / "positive-receipts.json", receipts)
        assert set(receipts) == set(profiles)
        assert all(report["healthy"] is True for report in receipts.values())
        assert sum(len(report["certificates"]) for report in receipts.values()) == (
            9 if network else 8
        )
        for name, report in receipts.items():
            approval = spire_renewal.read(Path(profiles[name]["approval"]))
            expected = {
                entry["id"]: hashlib.sha256(
                    Path(entry["certificate"]).read_bytes()
                ).hexdigest()
                for entry in approval["certificates"]
            }
            assert {
                entry["id"]: entry["certificate_sha256"]
                for entry in report["certificates"]
            } == expected
        publisher_state = profiles["status-publisher.json"]["state"]
        spire_renewal.invoke([*cli, "revoke", "--state", publisher_state], work, work)
        revoked = received()
        assert revoked["status-publisher.json"]["healthy"] is False
        assert {
            name: value
            for name, value in revoked.items()
            if name != "status-publisher.json"
        } == {
            name: value
            for name, value in receipts.items()
            if name != "status-publisher.json"
        }
        with pytest.raises(
            spire_renewal.full_local.FullLocalError, match="RECEIPT_NOT_READY"
        ):
            spire_renewal.invoke(
                [*cli, "check", "--state", publisher_state], work, work
            )
    finally:
        retire_timers()
        for name, labels in created.items():
            deadline = time.monotonic() + 60
            while docker("ps", "-q", "--filter", "volume=" + name).strip():
                assert time.monotonic() < deadline, (
                    "SPIRE_RENEWAL_TEST_HELPER_STILL_ACTIVE"
                )
                time.sleep(1)
            for identifier in (
                docker("ps", "-aq", "--filter", "volume=" + name).decode().split()
            ):
                row = json.loads(docker("inspect", identifier))[0]
                assert row["State"]["Running"] is False
                assert row["Config"]["Image"] == image
                assert row["Config"]["Entrypoint"] == ["python"]
                assert row["Config"]["Cmd"] == ["/opt/aelev/certificate-delivery.py"]
                assert row["HostConfig"]["NetworkMode"] == "none"
                assert all(
                    mount["Type"] == "volume" and mount["Name"] in created
                    for mount in row["Mounts"]
                )
                docker("rm", identifier)
            rows = json.loads(docker("volume", "inspect", name))
            assert rows[0]["Labels"] == labels
            assert rows[0]["Name"] == name
            docker("volume", "rm", name)
    spire_local.save(
        work / "result.json",
        {
            "status": "PASS",
            "helper_image": image,
            "profiles_verified": len(profiles),
            "certificates_verified": 9 if network else 8,
            "launchd_cycle_verified": timer_test,
            "leaf_reissued_with_same_key": True,
            "repeat_preserved_approvals": True,
            "publisher_revocation_isolated": True,
            "public_source_reissued_and_delivered": network,
            "test_resources_removed": True,
            "native_bootstrap_claimed": False,
        },
    )
