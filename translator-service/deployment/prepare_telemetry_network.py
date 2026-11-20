"""Offline, owner-local preparation of explicit telemetry network configuration."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "AELEV_INFRASTRUCTURE_TRANSLATOR_"
ORIGINAL_TARGET = "translator-api:8096"
QUERY_MTLS = True
SUFFIXES = (
    "TELEMETRY_BIND_ADDRESS",
    "TELEMETRY_HTTP_PORT",
    "TELEMETRY_CONFIGURATION_ROOT",
    "TELEMETRY_DELIVERY_ORIGIN",
    "TELEMETRY_SCRAPE_ORIGIN",
    "TELEMETRY_SCRAPE_SERVER_NAME",
)
PATHS = {
    "logs": "/v1/logs",
    "traces": "/v1/traces",
    "metrics": "/api/v1/write",
    "otlp_metrics": "/v1/metrics",
}


def name(value: str) -> str:
    if not value or not value.isascii() or len(value) > 253 or "%" in value:
        raise ValueError("TELEMETRY_NETWORK_NAME_INVALID")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        if not all(
            re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
            for label in value.split(".")
        ):
            raise ValueError("TELEMETRY_NETWORK_NAME_INVALID") from None
        return value.lower()


def origin(value: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(value)
        host = name(parsed.hostname or "")
        port = parsed.port
        if (
            not value.isascii()
            or len(value) > 512
            or any(ord(c) <= 32 or ord(c) == 127 for c in value)
            or "#" in value
            or "?" in value
            or parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.netloc.endswith(":")
            or port is not None
            and not 1 <= port <= 65535
        ):
            raise ValueError
    except ValueError:
        raise ValueError("TELEMETRY_NETWORK_HTTPS_ORIGIN_REQUIRED") from None
    authority = "[" + host + "]" if ":" in host else host
    if port is not None:
        authority += ":" + str(port)
    return "https://" + authority, authority


def bounded(path: Path, *, private: bool = False) -> bytes:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or not 0 < info.st_size <= 1024**2
        or private
        and (
            info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) not in {0o600, 0o640}
        )
    ):
        raise ValueError("TELEMETRY_NETWORK_FILE_REFUSED")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        fields = (
            "st_dev",
            "st_ino",
            "st_uid",
            "st_gid",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(
            getattr(os.fstat(stream.fileno()), key) != getattr(info, key)
            for key in fields
        ):
            raise ValueError("TELEMETRY_NETWORK_FILE_CHANGED")
        value = stream.read(1024**2 + 1)
        if len(value) != info.st_size or any(
            getattr(os.fstat(stream.fileno()), key) != getattr(info, key)
            for key in fields
        ):
            raise ValueError("TELEMETRY_NETWORK_FILE_CHANGED")
    return value


def replace_once(value: str, previous: str, current: str) -> str:
    if value.count(previous) != 1:
        raise ValueError("TELEMETRY_NETWORK_TEMPLATE_CHANGED")
    return value.replace(previous, current)


def render_configuration(
    delivery: str, scrape: str, server_name: str
) -> dict[str, bytes]:
    delivery_origin, _ = origin(delivery)
    _, target = origin(scrape)
    identity = name(server_name)
    sender = json.loads(bounded(ROOT / "observability/sender.local.json"))
    if not isinstance(sender, dict) or sender.get("destinations") != {
        kind: "https://delivery:4318" + path for kind, path in PATHS.items()
    }:
        raise ValueError("TELEMETRY_NETWORK_TEMPLATE_CHANGED")
    sender["destinations"] = {
        kind: delivery_origin + path for kind, path in PATHS.items()
    }
    prometheus = bounded(ROOT / "observability/prometheus.yml").decode("utf-8")
    prometheus = replace_once(
        prometheus,
        "      - targets: [" + ORIGINAL_TARGET + "]",
        "      - targets: [" + json.dumps(target) + "]",
    )
    if QUERY_MTLS:
        prometheus = replace_once(
            prometheus,
            "    metrics_path: /metrics\n",
            "    metrics_path: /metrics\n    scheme: https\n    tls_config:\n"
            "      ca_file: /run/secrets/telemetry/sender/api-ca.crt\n"
            "      cert_file: /run/secrets/telemetry/sender/api-query.crt\n"
            "      key_file: /run/secrets/telemetry/sender/api-query.key\n"
            "      min_version: TLS12\n      server_name: "
            + json.dumps(identity)
            + "\n",
        )
    else:
        prometheus = replace_once(
            prometheus,
            "      server_name: public-egress-gateway",
            "      server_name: " + json.dumps(identity),
        )
    prometheus = replace_once(
        prometheus,
        "    scheme: https\n",
        "    scheme: https\n    follow_redirects: false\n",
    )
    return {
        "sender.local.json": (json.dumps(sender, indent=2) + "\n").encode(),
        "prometheus.yml": prometheus.encode(),
    }


def environment(path: Path) -> dict[str, str]:
    selected: dict[str, str] = {}
    seen: set[str] = set()
    for line in bounded(path, private=True).decode("utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key in seen:
            raise ValueError("TELEMETRY_NETWORK_ENV_INVALID")
        seen.add(key)
        if key in {PREFIX + suffix for suffix in SUFFIXES}:
            selected[key.removeprefix(PREFIX)] = value
    return selected


def render_environment(values: dict[str, str]) -> dict[str, bytes]:
    if set(values) != set(SUFFIXES) or any(not value for value in values.values()):
        raise ValueError("TELEMETRY_NETWORK_INPUTS_REQUIRED")
    ipaddress.ip_address(values["TELEMETRY_BIND_ADDRESS"])
    port = values["TELEMETRY_HTTP_PORT"]
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("TELEMETRY_NETWORK_PORT_INVALID")
    configuration = Path(values["TELEMETRY_CONFIGURATION_ROOT"])
    if not configuration.is_absolute() or ".." in configuration.parts:
        raise ValueError("TELEMETRY_NETWORK_ABSOLUTE_ROOT_REQUIRED")
    return render_configuration(
        values["TELEMETRY_DELIVERY_ORIGIN"],
        values["TELEMETRY_SCRAPE_ORIGIN"],
        values["TELEMETRY_SCRAPE_SERVER_NAME"],
    )


def publish(output: Path, files: dict[str, bytes]) -> str:
    if (
        not output.is_absolute()
        or ".." in output.parts
        or any(path.is_symlink() for path in (output, *output.parents))
    ):
        raise ValueError("TELEMETRY_NETWORK_OUTPUT_REFUSED")
    if output.exists():
        info = output.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or {path.name for path in output.iterdir()} != set(files)
        ):
            raise ValueError("TELEMETRY_NETWORK_OUTPUT_CONFLICT")
        for filename, value in files.items():
            path = output / filename
            if (
                stat.S_IMODE(path.lstat().st_mode) != 0o600
                or bounded(path, private=True) != value
            ):
                raise ValueError("TELEMETRY_NETWORK_OUTPUT_CONFLICT")
        return "UNCHANGED"
    # Exclusive directory creation is the ownership boundary. A failed partial
    # publication is retained for handoff, never repaired/deleted on a retry.
    output.mkdir(mode=0o700)
    for filename, value in files.items():
        descriptor = os.open(
            output / filename,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
    descriptor = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    descriptor = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return "PREPARED"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment-file", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    options = parser.parse_args()
    os.umask(0o077)
    try:
        files = render_environment(environment(options.environment_file))
        result = publish(options.output_directory, files)
        print(
            json.dumps(
                {
                    "status": "TELEMETRY_NETWORK_CONFIGURATION_" + result,
                    "sha256": {
                        filename: hashlib.sha256(value).hexdigest()
                        for filename, value in files.items()
                    },
                }
            )
        )
    except (ValueError, OSError, UnicodeError, KeyError):
        print(
            json.dumps(
                {"status": "BLOCKED", "code": "TELEMETRY_NETWORK_CONFIGURATION_REFUSED"}
            )
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
