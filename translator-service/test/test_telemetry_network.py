"""Offline renderer preservation and actual opt-in Compose model checks."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "AELEV_INFRASTRUCTURE_TRANSLATOR_"
ROLE = "translator-telemetry-sender"
RECIPE = "compose.remote.yml"
spec = importlib.util.spec_from_file_location(
    "owner_telemetry_network", ROOT / "deployment/prepare_telemetry_network.py"
)
assert spec is not None and spec.loader is not None
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


def values() -> dict[str, str]:
    return {
        "TELEMETRY_BIND_ADDRESS": "127.0.0.1",
        "TELEMETRY_HTTP_PORT": "34318",
        "TELEMETRY_CONFIGURATION_ROOT": "/private/owner/secrets/telemetry-config",
        "TELEMETRY_DELIVERY_ORIGIN": "https://delivery.test:24418",
        "TELEMETRY_SCRAPE_ORIGIN": "https://192.0.2.10:24443",
        "TELEMETRY_SCRAPE_SERVER_NAME": "api.owner.test",
    }


def files() -> dict[str, bytes]:
    return renderer.render_environment(values())


def snapshot(root: Path) -> dict[str, tuple[bytes | None, int, int]]:
    return {
        str(path.relative_to(root)): (
            path.read_bytes() if path.is_file() and not path.is_symlink() else None,
            path.lstat().st_mode,
            path.lstat().st_mtime_ns,
        )
        for path in [root, *root.rglob("*")]
    }


def test_only_destinations_and_scrape_transport_change() -> None:
    output = files()
    before = json.loads((ROOT / "observability/sender.local.json").read_bytes())
    after = json.loads(output["sender.local.json"])
    destinations = after.pop("destinations")
    before.pop("destinations")
    assert after == before
    assert destinations == {
        "logs": "https://delivery.test:24418/v1/logs",
        "traces": "https://delivery.test:24418/v1/traces",
        "metrics": "https://delivery.test:24418/api/v1/write",
        "otlp_metrics": "https://delivery.test:24418/v1/metrics",
    }
    prometheus = output["prometheus.yml"].decode()
    assert 'targets: ["192.0.2.10:24443"]' in prometheus
    assert 'server_name: "api.owner.test"' in prometheus
    assert "scheme: https" in prometheus
    assert (
        prometheus[: prometheus.index("remote_write:")].count(
            "    follow_redirects: false\n"
        )
        == 1
    )
    assert (
        "follow_redirects: false"
        not in (ROOT / "observability/prometheus.yml").read_text()
    )
    old = (ROOT / "observability/prometheus.yml").read_text()
    assert (
        prometheus[prometheus.index("remote_write:") :]
        == old[old.index("remote_write:") :]
    )
    if ROOT.name == "translator-service":
        assert "api-query.crt" in prometheus and "api-query.key" in prometheus
        assert "api-ca.crt" in prometheus
    else:
        assert "/run/secrets/gateway/server-ca.pem" in prometheus
        assert "api-query" not in prometheus


@pytest.mark.parametrize(
    "bad",
    [
        "http://delivery.test",
        "https://user@delivery.test",
        "https://user:secret@delivery.test",
        "https://delivery.test/path",
        "https://delivery.test?query=1",
        "https://delivery.test#fragment",
        "https://delivery.test:0",
        "https://delivery.test:65536",
        "https://delivery.test:",
        "https://bad..test",
        "https://delivery.test\n",
        "https://delivery.тест",
        "https://delivery%2etest",
        "https://[not-ip]",
        "https://delivery.test#",
        "https://delivery.test?",
        "https://[fe80::1%25eth0]",
        "\x01https://delivery.test",
    ],
)
@pytest.mark.parametrize(
    "key", ["TELEMETRY_DELIVERY_ORIGIN", "TELEMETRY_SCRAPE_ORIGIN"]
)
def test_invalid_transport_input_refused_before_output(bad: str, key: str) -> None:
    source = values()
    source[key] = bad
    with pytest.raises(ValueError):
        renderer.render_environment(source)


@pytest.mark.parametrize(
    "key,bad",
    [
        ("TELEMETRY_SCRAPE_SERVER_NAME", ""),
        ("TELEMETRY_SCRAPE_SERVER_NAME", "bad\nname"),
        ("TELEMETRY_SCRAPE_SERVER_NAME", "*.owner.test"),
        ("TELEMETRY_SCRAPE_SERVER_NAME", "https://owner.test"),
        ("TELEMETRY_BIND_ADDRESS", "guess-host.test"),
        ("TELEMETRY_HTTP_PORT", "0"),
        ("TELEMETRY_HTTP_PORT", "65536"),
        ("TELEMETRY_HTTP_PORT", "port"),
        ("TELEMETRY_CONFIGURATION_ROOT", "relative/guess"),
        ("TELEMETRY_CONFIGURATION_ROOT", "/private/../guess"),
    ],
)
def test_explicit_binding_and_identity_inputs(key: str, bad: str) -> None:
    source = values()
    source[key] = bad
    with pytest.raises(ValueError):
        renderer.render_environment(source)


@pytest.mark.parametrize("missing", renderer.SUFFIXES)
def test_missing_renderer_input_refused(missing: str) -> None:
    source = values()
    source.pop(missing)
    with pytest.raises(ValueError):
        renderer.render_environment(source)


def test_explicit_ipv6_and_nondefault_ports() -> None:
    source = values()
    source["TELEMETRY_DELIVERY_ORIGIN"] = "https://[2001:db8::1]:34418/"
    source["TELEMETRY_SCRAPE_ORIGIN"] = "https://[2001:db8::2]:34443"
    result = renderer.render_environment(source)
    assert b"https://[2001:db8::1]:34418/v1/logs" in result["sender.local.json"]
    assert b'targets: ["[2001:db8::2]:34443"]' in result["prometheus.yml"]


def test_fresh_private_output_and_exact_repeat(tmp_path: Path) -> None:
    target = tmp_path.resolve() / "configuration"
    rendered = files()
    assert renderer.publish(target, rendered) == "PREPARED"
    assert target.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in target.iterdir())
    before = snapshot(target)
    assert renderer.publish(target, rendered) == "UNCHANGED"
    assert snapshot(target) == before


@pytest.mark.parametrize(
    "kind",
    [
        "partial",
        "foreign",
        "changed",
        "exposed-file",
        "exposed-directory",
        "symlink",
        "hardlink",
    ],
)
def test_existing_unknown_output_preserved(tmp_path: Path, kind: str) -> None:
    target = tmp_path.resolve() / "configuration"
    rendered = files()
    renderer.publish(target, rendered)
    if kind == "partial":
        (target / "prometheus.yml").unlink()
    elif kind == "foreign":
        (target / "foreign").write_bytes(b"preserve")
    elif kind == "changed":
        (target / "sender.local.json").write_bytes(b"preserve")
    elif kind == "exposed-file":
        (target / "sender.local.json").chmod(0o644)
    elif kind == "exposed-directory":
        target.chmod(0o755)
    elif kind == "symlink":
        (target / "sender.local.json").unlink()
        (target / "sender.local.json").symlink_to(target / "prometheus.yml")
    else:
        os.link(target / "sender.local.json", tmp_path / "hardlinked-input")
    before = snapshot(target)
    with pytest.raises(ValueError):
        renderer.publish(target, rendered)
    assert snapshot(target) == before


def test_output_link_and_template_drift_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path.resolve() / "real"
    real.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError):
        renderer.publish(link / "output", files())
    assert not (real / "output").exists()
    replacement = tmp_path / "source"
    shutil.copytree(ROOT / "observability", replacement / "observability")
    target = replacement / "observability/prometheus.yml"
    target.write_text(
        target.read_text().replace(renderer.ORIGINAL_TARGET, "unexpected-template:4318")
    )
    monkeypatch.setattr(renderer, "ROOT", replacement)
    with pytest.raises(ValueError, match="TEMPLATE_CHANGED"):
        files()


def test_cli_private_env_repeat_and_refusal(tmp_path: Path) -> None:
    folder = tmp_path.resolve()
    environment = folder / "input.env"
    environment.write_text(
        "\n".join(PREFIX + key + "=" + value for key, value in values().items()) + "\n"
    )
    environment.chmod(0o600)
    target = folder / "configuration"
    command = [
        sys.executable,
        "-B",
        str(ROOT / "deployment/prepare_telemetry_network.py"),
        "--environment-file",
        str(environment),
        "--output-directory",
        str(target),
    ]
    first = subprocess.run(command, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stdout + first.stderr
    assert json.loads(first.stdout)["status"].endswith("_PREPARED")
    before = snapshot(target)
    repeat = subprocess.run(command, capture_output=True, text=True, check=False)
    assert repeat.returncode == 0
    assert json.loads(repeat.stdout)["status"].endswith("_UNCHANGED")
    assert snapshot(target) == before
    environment.write_text(
        environment.read_text() + PREFIX + "TELEMETRY_HTTP_PORT=another\n"
    )
    failed = subprocess.run(command, capture_output=True, text=True, check=False)
    assert failed.returncode == 2
    assert (
        json.loads(failed.stdout)["code"] == "TELEMETRY_NETWORK_CONFIGURATION_REFUSED"
    )
    assert snapshot(target) == before
    assert "delivery.test" not in failed.stdout + failed.stderr


@pytest.mark.parametrize(
    "kind", ["exposed", "symlink", "hardlink", "empty", "oversized"]
)
def test_private_environment_file_refusals_preserve_input(
    tmp_path: Path, kind: str
) -> None:
    source = tmp_path / "input.env"
    source.write_text(
        "\n".join(PREFIX + key + "=" + value for key, value in values().items()) + "\n"
    )
    source.chmod(0o600)
    if kind == "exposed":
        source.chmod(0o644)
    elif kind == "symlink":
        target = tmp_path / "original"
        source.rename(target)
        source.symlink_to(target)
    elif kind == "hardlink":
        os.link(source, tmp_path / "another-link")
    elif kind == "empty":
        source.write_bytes(b"")
    else:
        source.write_bytes(b"x" * (1024**2 + 1))
    before = snapshot(tmp_path)
    with pytest.raises(ValueError):
        renderer.environment(source)
    assert snapshot(tmp_path) == before


def test_sender_template_drift_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replacement = tmp_path / "source"
    shutil.copytree(ROOT / "observability", replacement / "observability")
    target = replacement / "observability/sender.local.json"
    sender = json.loads(target.read_bytes())
    sender["destinations"]["logs"] = "https://unexpected.test/v1/logs"
    target.write_text(json.dumps(sender))
    monkeypatch.setattr(renderer, "ROOT", replacement)
    with pytest.raises(ValueError, match="TEMPLATE_CHANGED"):
        files()


def compiled(
    settings: dict[str, str], *, network: bool
) -> subprocess.CompletedProcess[str]:
    command = [
        "docker",
        "compose",
        "--env-file",
        ".env.example",
        "--profile",
        "*",
        "--file",
        RECIPE,
    ]
    if network:
        command += ["--file", "compose.telemetry-network.yml"]
    return subprocess.run(
        [*command, "config", "--no-env-resolution", "--format", "json"],
        cwd=ROOT,
        env={
            **{
                k: v
                for k, v in os.environ.items()
                if not k.startswith(("AELEV_", "COMPOSE_"))
            },
            **{PREFIX + k: v for k, v in settings.items()},
        },
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("address,port", [("127.0.0.1", "34318"), ("0.0.0.0", "35318")])
def test_actual_profile_changes_only_published_port_and_two_config_files(
    address: str, port: str
) -> None:
    source = values()
    source.update(TELEMETRY_BIND_ADDRESS=address, TELEMETRY_HTTP_PORT=port)
    previous = compiled(source, network=False)
    current = compiled(source, network=True)
    assert previous.returncode == current.returncode == 0, (
        previous.stderr + current.stderr
    )
    base, actual = json.loads(previous.stdout), json.loads(current.stdout)
    role = actual["services"][ROLE]
    ports = role.pop("ports")
    assert len(ports) == 1
    assert (ports[0]["host_ip"], ports[0]["published"], ports[0]["target"]) == (
        address,
        port,
        4318,
    )
    configured = role.pop("configs")
    assert {item["target"] for item in configured} == {
        "/etc/aelev/sender.local.json",
        "/etc/aelev/prometheus.yml",
    }
    for item in configured:
        config = actual["configs"].pop(item["source"])
        assert (
            config["file"]
            == source["TELEMETRY_CONFIGURATION_ROOT"] + "/" + Path(item["target"]).name
        )
    if not actual["configs"]:
        actual.pop("configs")
    assert actual == base


@pytest.mark.parametrize(
    "missing",
    ["TELEMETRY_BIND_ADDRESS", "TELEMETRY_HTTP_PORT", "TELEMETRY_CONFIGURATION_ROOT"],
)
def test_selected_network_recipe_requires_explicit_input(missing: str) -> None:
    source = values()
    source.pop(missing)
    result = compiled(source, network=True)
    assert result.returncode != 0
    assert PREFIX + missing in result.stderr
