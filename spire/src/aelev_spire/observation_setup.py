"""Compile this provider's observation recipe from initial owner inputs."""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import re
import socket
import ssl
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

from . import desktop_host
from .files import atomic, read, reserve
from .public_state import principal

if TYPE_CHECKING:
    from .installation import Installer

OWNER = "infrastructure-spire"
PREFIX = "AELEV_INFRASTRUCTURE_SPIRE_"
SECRET_FILES = {
    role: set(paths)
    for role, paths in cast(
        dict[str, list[str]],
        json.loads(Path(__file__).with_name("observation-inputs.json").read_text()),
    ).items()
}


def origin(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("SPIRE_OBSERVATION_HTTPS_REQUIRED")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or "\\" in value
        or any(c.isspace() or ord(c) < 32 for c in value)
    ):
        raise ValueError("SPIRE_OBSERVATION_HTTPS_REQUIRED")
    return value.rstrip("/")


def documents(plan: dict[str, Any]) -> dict[str, bytes]:
    if (
        plan.get("owner") != OWNER
        or plan.get("schema_version") != 1
        or set(plan) - {"host_gateway_aliases", "network"}
        != {
            "owner",
            "schema_version",
            "sender_image",
            "central_url",
            "presentation",
            "grafana_url",
            "datasource_uid",
        }
    ):
        raise ValueError("SPIRE_OBSERVATION_PLAN_INVALID")
    if not isinstance(plan["presentation"], dict):
        raise ValueError("SPIRE_OBSERVATION_PLAN_INVALID")
    presentation = cast(dict[str, Any], plan["presentation"])
    if set(presentation) != {
        "url",
        "identity_url",
        "realm",
        "client_id",
        "audience",
        "module_id",
    }:
        raise ValueError("SPIRE_OBSERVATION_PLAN_INVALID")
    for field in ("url", "identity_url"):
        origin(presentation[field])
    if str(uuid.UUID(presentation["module_id"])) != presentation["module_id"]:
        raise ValueError("SPIRE_OBSERVATION_MODULE_ID_INVALID")
    for field in ("realm", "client_id", "audience"):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", presentation[field]):
            raise ValueError("SPIRE_OBSERVATION_PRESENTATION_INVALID")
    central = origin(plan["central_url"])
    network = network_endpoints(plan)
    origin(plan["grafana_url"])
    gateway_aliases(plan)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", plan["datasource_uid"]):
        raise ValueError("SPIRE_OBSERVATION_DATASOURCE_INVALID")
    tls = {
        "ca_file": "/run/secrets/telemetry/server-ca.crt",
        "cert_file": "/run/secrets/telemetry/local-write.crt",
        "key_file": "/run/secrets/telemetry/local-write.key",
    }
    query_tls = {
        "ca_file": "/run/secrets/telemetry/server-ca.crt",
        "cert_file": "/run/secrets/telemetry/local-query.crt",
        "key_file": "/run/secrets/telemetry/local-query.key",
    }
    prometheus: dict[str, Any] = {
        "global": {
            "scrape_interval": "15s",
            "scrape_timeout": "2s",
            "external_labels": {
                "owner": OWNER,
                "owner_instance": presentation["module_id"],
            },
        },
        "rule_files": ["/etc/aelev/rules.json"],
        "scrape_configs": [
            {
                "job_name": "infrastructure-spire",
                "scheme": "https",
                "static_configs": [
                    {"targets": [urlsplit(network["observer_url"]).netloc]}
                ],
                "tls_config": query_tls,
            }
        ],
        "remote_write": [
            {
                "url": "https://localhost:4318/api/v1/write",
                "protobuf_message": "prometheus.WriteRequest",
                "remote_timeout": "2s",
                "tls_config": tls,
                "queue_config": {
                    "min_shards": 1,
                    "max_shards": 1,
                    "capacity": 1000,
                    "max_samples_per_send": 100,
                    "batch_send_deadline": "2s",
                },
            }
        ],
    }
    prometheus["scrape_configs"].append(
        {
            "job_name": "infrastructure-spire-delivery",
            "scheme": "https",
            "static_configs": [{"targets": ["localhost:13134"]}],
            "tls_config": {
                "ca_file": "/run/secrets/telemetry/server-ca.crt",
                "cert_file": "/run/secrets/telemetry/local-query.crt",
                "key_file": "/run/secrets/telemetry/local-query.key",
            },
        }
    )
    sender: dict[str, Any] = {
        "mode": "sender",
        "listen_host": "0.0.0.0",
        "listen_port": 4318,
        "health_port": 13134,
        "journal": "/var/lib/delivery",
        "capacity_bytes": 1024**3,
        "request_bytes": 4 * 1024**2,
        "retry_seconds": 2,
        "timeout_seconds": 2,
        "server_cert": "/run/secrets/telemetry/sender.crt",
        "server_key": "/run/secrets/telemetry/sender.key",
        "client_ca": "/run/secrets/telemetry/local-write-ca.crt",
        "ca": "/run/secrets/telemetry/central-ca.crt",
        "client_cert": "/run/secrets/telemetry/central-write.crt",
        "client_key": "/run/secrets/telemetry/central-write.key",
        "health_ca": "/run/secrets/telemetry/local-query-ca.crt",
        "health_cert": "/run/secrets/telemetry/local-query.crt",
        "health_key": "/run/secrets/telemetry/local-query.key",
        "destinations": {
            kind: central + route
            for kind, route in {
                "logs": "/v1/logs",
                "traces": "/v1/traces",
                "metrics": "/api/v1/write",
                "otlp_metrics": "/v1/metrics",
            }.items()
        },
        "prometheus_command": [
            "/bin/prometheus",
            "--config.file=/etc/aelev/prometheus.json",
            "--web.config.file=/etc/aelev/web.json",
            "--web.listen-address=127.0.0.1:9090",
            "--storage.tsdb.path=/var/lib/delivery/prometheus",
            "--storage.tsdb.retention.time=30d",
            "--log.format=json",
        ],
    }
    if "network" in plan:
        sender["certificate_renewal"] = {
            "consumer_owner": OWNER,
            "report": "/run/certificate-renewal/sender.json",
            "certificates": {
                name: "/run/secrets/telemetry/" + filename
                for name, filename in {
                    "sender": "sender.crt",
                    "local-write": "local-write.crt",
                    "local-query": "local-query.crt",
                    "central-write": "central-write.crt",
                }.items()
            },
        }
    web = {
        "tls_server_config": {
            "cert_file": "/run/secrets/telemetry/sender.crt",
            "key_file": "/run/secrets/telemetry/sender.key",
            "client_auth_type": "RequireAndVerifyClientCert",
            "client_ca_file": "/run/secrets/telemetry/local-query-ca.crt",
            "min_version": "TLS12",
        }
    }
    rules = {
        "groups": [
            {
                "name": "infrastructure-spire",
                "rules": [
                    {
                        "alert": "SpireNotReady",
                        "expr": "aelev_spire_native_ready == 0",
                        "for": "1m",
                        "labels": {"severity": "critical", "service": OWNER},
                        "annotations": {
                            "summary": "SPIRE не готов выдавать удостоверения"
                        },
                    },
                    {
                        "alert": "SpireObservationMissing",
                        "expr": "absent_over_time(aelev_spire_source_fresh[2m]) "
                        "or aelev_spire_source_fresh == 0",
                        "for": "1m",
                        "labels": {"severity": "critical", "service": OWNER},
                        "annotations": {"summary": "Нет свежего наблюдения SPIRE"},
                    },
                    {
                        "alert": "SpireCertificateRenewalUnhealthy",
                        "expr": "aelev_spire_certificate_renewal_ready == 0",
                        "for": "1m",
                        "labels": {"severity": "critical", "service": OWNER},
                        "annotations": {
                            "summary": (
                                "Автоматическое продление сертификатов SPIRE не готово"
                            )
                        },
                    },
                    {
                        "alert": "SpireCertificateExpiryWindow",
                        "expr": (
                            "min(aelev_spire_certificate_expires_at) - time() "
                            "< 2592000 or min(aelev_spire_certificate_ca_expires_at) "
                            "- time() < 2592000"
                        ),
                        "for": "1m",
                        "labels": {"severity": "critical", "service": OWNER},
                        "annotations": {
                            "summary": (
                                "Сертификат SPIRE вошёл в 30-дневное окно продления"
                            )
                        },
                    },
                ],
            }
        ]
    }
    dashboard: dict[str, Any] = {
        "uid": "aelev-infrastructure-spire",
        "title": "SPIRE",
        "schemaVersion": 41,
        "tags": [OWNER, "installation:" + presentation["module_id"]],
        "panels": [
            {
                "id": index,
                "type": "stat",
                "title": title,
                "gridPos": {"x": (index - 1) * 8, "y": 0, "w": 8, "h": 5},
                "datasource": {"type": "prometheus", "uid": plan["datasource_uid"]},
                "targets": [{"expr": query, "refId": "A"}],
            }
            for index, title, query in [
                (1, "Готовность выдачи", "aelev_spire_native_ready"),
                (2, "Свежесть наблюдения", "aelev_spire_source_fresh"),
                (3, "Возраст наблюдения, с", "aelev_spire_source_age_seconds"),
            ]
        ],
    }
    selector = '{owner_instance="' + presentation["module_id"] + '"}'
    for panel in dashboard["panels"]:
        panel["targets"][0]["expr"] += selector
    alert: dict[str, Any] = {
        "uid": "aelev-spire-readiness",
        "title": "SPIRE: нет подтверждённой готовности",
        "folderUID": "infrastructure-spire",
        "ruleGroup": "SPIRE",
        "condition": "B",
        "noDataState": "Alerting",
        "execErrState": "Alerting",
        "for": "1m",
        "labels": {
            "service": OWNER,
            "owner_instance": presentation["module_id"],
            "severity": "critical",
        },
        "annotations": {
            "summary": "SPIRE не готов либо прервана доставка его наблюдений"
        },
        "data": [
            {
                "refId": "A",
                "relativeTimeRange": {"from": 180, "to": 0},
                "datasourceUid": plan["datasource_uid"],
                "model": {
                    "refId": "A",
                    "expr": "min_over_time(aelev_spire_native_ready"
                    + selector
                    + "[1m])",
                    "instant": True,
                    "intervalMs": 15000,
                    "maxDataPoints": 1000,
                },
            },
            {
                "refId": "B",
                "relativeTimeRange": {"from": 0, "to": 0},
                "datasourceUid": "__expr__",
                "model": {
                    "refId": "B",
                    "type": "classic_conditions",
                    "conditions": [
                        {
                            "type": "query",
                            "evaluator": {"type": "lt", "params": [1]},
                            "operator": {"type": "and"},
                            "query": {"params": ["A"]},
                            "reducer": {"type": "last", "params": []},
                        }
                    ],
                },
            },
        ],
    }
    return {
        name: json.dumps(value, sort_keys=True).encode()
        for name, value in {
            "observation.json": plan,
            "sender.json": sender,
            "prometheus.json": prometheus,
            "web.json": web,
            "rules.json": rules,
            "dashboard.json": dashboard,
            "alert.json": alert,
        }.items()
    }


def network_endpoints(plan: dict[str, Any]) -> dict[str, str]:
    """Explicit public addresses; legacy defaults belong only to file mode."""
    selected = plan.get("network")
    if selected is None:
        return {
            "observer_url": "https://spire-observer:8083",
            "sender_otlp_url": "https://spire-telemetry-sender:4318",
            "sender_health_url": "https://spire-telemetry-sender:13134",
        }
    required = {
        "source_url",
        "source_identity",
        "observer_url",
        "sender_otlp_url",
        "sender_health_url",
    }
    if not isinstance(selected, dict):
        raise ValueError("SPIRE_OBSERVATION_NETWORK_INVALID")
    selected = cast(dict[str, Any], selected)
    if set(selected) != required:
        raise ValueError("SPIRE_OBSERVATION_NETWORK_INVALID")
    result: dict[str, str] = {}
    for key in required - {"source_identity"}:
        result[key] = origin(selected[key])
    result["source_identity"] = principal(selected["source_identity"])
    return result


def bind_network(model: dict[str, Any], plan: dict[str, Any]) -> None:
    """Remove cross-role source mounts when HTTPS is explicitly selected."""
    if "network" not in plan:
        return
    selected = network_endpoints(plan)
    services = model["services"]
    services["spire-observer"]["environment"][PREFIX + "OTLP_ENDPOINT"] = selected[
        "sender_otlp_url"
    ]
    publisher = services["spire-status-publisher"]["environment"]
    publisher[PREFIX + "OBSERVER_URL"] = selected["observer_url"]
    publisher[PREFIX + "SENDER_HEALTH_URL"] = selected["sender_health_url"]
    for name in ("spire-observer", "spire-status-publisher"):
        service = services[name]
        service["environment"].update(
            {
                PREFIX + "SOURCE_MODE": "https",
                PREFIX + "SOURCE_URL": selected["source_url"],
                PREFIX + "SOURCE_IDENTITY": selected["source_identity"],
            }
        )
        service["volumes"] = [
            mount
            for mount in service["volumes"]
            if mount.get("target") != "/run/observation"
        ]
    sender = services.get("spire-telemetry-sender")
    if sender is not None:
        sender["volumes"].append(
            {
                "type": "volume",
                "source": "spire-certificate-renewal-status",
                "target": "/run/certificate-renewal",
                "read_only": True,
            }
        )
    # Prune only the now-unused public source/renewal projections. Secret and
    # queue volumes remain local to their original owning roles.
    for name in ("spire-observation-public-source",):
        model["volumes"].pop(name, None)


def gateway_aliases(plan: dict[str, Any]) -> dict[str, str]:
    """Resolve only explicitly selected local provider endpoint names."""
    aliases = plan.get("host_gateway_aliases", [])
    endpoints: set[str | None] = {
        urlsplit(str(value)).hostname
        for value in (
            plan["central_url"],
            plan["grafana_url"],
            plan["presentation"]["url"],
            plan["presentation"]["identity_url"],
        )
    }
    if "network" in plan:
        endpoints.update(
            urlsplit(value).hostname
            for key, value in network_endpoints(plan).items()
            if key != "source_identity"
        )
    if not isinstance(aliases, list):
        raise ValueError("SPIRE_OBSERVATION_GATEWAY_ALIASES_INVALID")
    selected: list[str] = []
    for name in cast(list[object], aliases):
        if (
            not isinstance(name, str)
            or name not in endpoints
            or not re.fullmatch(r"[a-z][a-z0-9.-]{0,252}", name)
            or name == "localhost"
            or name.startswith("spire-")
            or name in selected
        ):
            raise ValueError("SPIRE_OBSERVATION_GATEWAY_ALIASES_INVALID")
        selected.append(name)
    if len(selected) > 4:
        raise ValueError("SPIRE_OBSERVATION_GATEWAY_ALIASES_INVALID")
    return {name: "host-gateway" for name in selected}


def gateway_routes(
    aliases: dict[str, str], host_os: str, *, confirmed_address: str | None = None
) -> dict[str, str]:
    """Docker Desktop's host is not the Linux VM's bridge gateway."""
    if not aliases or host_os == "Linux":
        return aliases
    if host_os not in {"Darwin", "Windows"}:
        raise ValueError("SPIRE_OBSERVATION_HOST_OS_INVALID")
    try:
        if confirmed_address is None:
            records = socket.getaddrinfo(
                "host.docker.internal", None, socket.AF_INET, socket.SOCK_STREAM
            )
            confirmed_address = str(records[0][4][0])
        address = desktop_host.private_address(confirmed_address)
    except (OSError, ValueError, IndexError):
        raise ValueError("SPIRE_OBSERVATION_DESKTOP_HOST_UNAVAILABLE") from None
    return {name: str(address) for name in aliases}


def installation_routes(
    installer: Installer, aliases: dict[str, str]
) -> dict[str, str]:
    """Keep the socket-bearing operator offline; resolve only declared aliases."""
    address = None
    if aliases and installer.host_os in {"Darwin", "Windows"}:
        try:
            raw = installer.docker(
                "run",
                "--rm",
                "--user",
                "10001:10001",
                "--network",
                "bridge",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--pids-limit",
                "32",
                "--memory",
                "64m",
                "--env",
                "RES_OPTIONS=timeout:2 attempts:1",
                "--entrypoint",
                "python",
                installer.image,
                "-S",
                "-P",
                "/opt/aelev/src/aelev_spire/desktop_host.py",
            )
            if not 1 <= len(raw) <= 16:
                raise ValueError(desktop_host.CODE)
            address = raw.decode("ascii").strip()
        except (OSError, UnicodeError, ValueError):
            raise ValueError(desktop_host.CODE) from None
    return gateway_routes(aliases, installer.host_os, confirmed_address=address)


def seed(root: Path, value: dict[str, Any]) -> None:
    role = value["role"]
    allowed = (
        SECRET_FILES.get(role)
        if role != "configuration"
        else {
            "observation.json",
            "sender.json",
            "prometheus.json",
            "web.json",
            "rules.json",
            "dashboard.json",
            "alert.json",
        }
    )
    if allowed is None or set(value["files"]) != allowed:
        raise ValueError("SPIRE_OBSERVATION_FILES_INVALID")
    reserve(root, {"owner": OWNER, "role": "observation-" + role})
    for name, encoded in value["files"].items():
        raw = base64.b64decode(encoded, validate=True)
        if not 1 <= len(raw) <= 65536:
            raise ValueError("SPIRE_OBSERVATION_FILE_SIZE_INVALID")
        path = root / name
        if path.is_symlink():
            raise ValueError("SPIRE_STATE_SYMLINK_FORBIDDEN")
        if (
            path.exists()
            and path.read_bytes() != raw
            and value.get("update") is not True
        ):
            raise ValueError("SPIRE_EXISTING_CONFIGURATION_PRESERVED")
        if not path.exists() or path.read_bytes() != raw:
            atomic(path, raw)


def install(installer: Installer, plan: dict[str, Any], source: Path) -> None:
    from .sender_recovery import require_running

    require_running(installer)
    require_running(installer, role="observer")
    public = documents(plan)
    deployment = read(installer.root / "deployment.json")
    aliases = gateway_aliases(plan)
    if aliases and deployment["plan"]["environment"] == "production":
        raise ValueError("SPIRE_OBSERVATION_GATEWAY_ALIASES_LOCAL_ONLY")
    if deployment["role"] != "server" or deployment["image_id"] != installer.image_id:
        raise ValueError("SPIRE_OBSERVATION_SERVER_RELEASE_REQUIRED")
    routes = installation_routes(installer, aliases)
    if deployment["plan"]["environment"] == "production" and not re.fullmatch(
        r".+@sha256:[0-9a-f]{64}", plan["sender_image"]
    ):
        raise ValueError("SPIRE_OBSERVATION_SENDER_DIGEST_REQUIRED")
    try:
        installer.docker("image", "inspect", plan["sender_image"])
    except ValueError:
        try:
            installer.docker("pull", plan["sender_image"])
        except ValueError:
            raise ValueError("SPIRE_OBSERVATION_SENDER_IMAGE_UNAVAILABLE") from None
    previous = installer.root / "observation-deployment.json"
    if previous.exists() and read(previous) != plan:
        before = read(previous)
        if not installer.update or any(
            before["presentation"][key] != plan["presentation"][key]
            for key in ("module_id", "realm", "client_id", "audience")
        ):
            raise ValueError("SPIRE_EXISTING_CONFIGURATION_PRESERVED")
    values = {
        PREFIX + "COMPOSE_PROJECT": installer.project,
        PREFIX + "IMAGE": installer.image,
        PREFIX + "SENDER_IMAGE": plan["sender_image"],
        PREFIX + "ACCESS_NETWORK": installer.project + "-access",
        f"{PREFIX}OBSERVATION_VOLUME": installer.volume(
            "observation-source", ephemeral=True
        ),
    }
    for key, value in plan["presentation"].items():
        values[PREFIX + "UI_" + key.upper()] = value
    mappings = {
        "OBSERVATION_CONFIG": "observation-configuration",
        "OBSERVER_DATA": "observation",
        "STATUS_DATA": "status",
        "TELEMETRY_DATA": "telemetry",
        "OBSERVER_SECRETS": "observer-secrets",
        "STATUS_SECRETS": "status-secrets",
        "SENDER_SECRETS": "sender-secrets",
        "STATUS_ADMIN": "status-admin",
        "GRAFANA_SECRETS": "grafana-secrets",
        "GRAFANA_ADMIN": "grafana-admin",
        "CERTIFICATE_RENEWAL_STATUS": "certificate-renewal-status",
    }
    for key, role in mappings.items():
        values[PREFIX + key + "_VOLUME"] = installer.volume(role)

    def deliver(volume: str, role: str, files: dict[str, bytes]) -> None:
        installer.docker(
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "--read-only",
            "--entrypoint",
            "python",
            "-v",
            volume + ":/var/lib/spire",
            installer.image,
            "-m",
            "aelev_spire.observation_setup",
            "seed",
            payload=json.dumps(
                {
                    "role": role,
                    "update": installer.update,
                    "files": {
                        n: base64.b64encode(b).decode() for n, b in files.items()
                    },
                }
            ).encode(),
        )

    deliver(values[PREFIX + "OBSERVATION_CONFIG_VOLUME"], "configuration", public)
    for role, files in SECRET_FILES.items():
        selected = source / role
        target_key = {
            "observer": "OBSERVER_SECRETS",
            "sender": "SENDER_SECRETS",
            "status": "STATUS_SECRETS",
            "status-admin": "STATUS_ADMIN",
            "grafana": "GRAFANA_SECRETS",
            "grafana-admin": "GRAFANA_ADMIN",
        }[role]
        if not selected.exists() and previous.exists():
            installer.docker(
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--entrypoint",
                "python",
                "-v",
                values[PREFIX + target_key + "_VOLUME"] + ":/var/lib/spire:ro",
                installer.image,
                "-c",
                "from pathlib import Path; import sys; "
                "p=Path('/var/lib/spire'); "
                "assert all((p/n).is_file() and not (p/n).is_symlink() "
                "for n in sys.argv[1:])",
                *sorted(files),
            )
            continue
        if any(
            (selected / n).is_symlink()
            or not (selected / n).is_file()
            or (selected / n).stat().st_size > 65536
            for n in files
        ):
            raise ValueError("SPIRE_OBSERVATION_SECRET_INPUT_REQUIRED")
        target_key = {
            "observer": "OBSERVER_SECRETS",
            "sender": "SENDER_SECRETS",
            "status": "STATUS_SECRETS",
            "status-admin": "STATUS_ADMIN",
            "grafana": "GRAFANA_SECRETS",
            "grafana-admin": "GRAFANA_ADMIN",
        }[role]
        deliver(
            values[PREFIX + target_key + "_VOLUME"],
            role,
            {n: (selected / n).read_bytes() for n in files},
        )
    for key, role in [
        ("OBSERVER_DATA", "observation"),
        ("STATUS_DATA", "status"),
        ("TELEMETRY_DATA", "telemetry"),
    ]:
        installer.docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--entrypoint",
            "python",
            "-v",
            values[PREFIX + key + "_VOLUME"] + ":/var/lib/spire",
            installer.image,
            "-c",
            "from pathlib import Path; from aelev_spire.files import reserve; "
            "import sys; reserve(Path('/var/lib/spire'), "
            "{'owner':'infrastructure-spire','role':sys.argv[1]})",
            role,
        )
    model = installer.resolve("compose.observation.yml", values)
    bind_network(model, plan)
    from .sender_recovery import apply_override

    for key, definition in model["volumes"].items():
        model["volumes"][key] = {"name": definition["name"], "external": True}
    for service in model["services"].values():
        if routes:
            service["extra_hosts"] = routes
        service["labels"] = {
            "io.aelev.owner": OWNER,
            "io.aelev.installation": installer.project,
        }
    revision_path = installer.root / "observation-revision.json"
    if installer.update or not revision_path.exists():
        atomic(revision_path, {"revision": str(uuid.uuid4())})
    revision = read(revision_path)["revision"]
    for service in model["services"].values():
        service["labels"]["io.aelev.configuration-revision"] = revision
    apply_override(installer.root, model["services"]["spire-telemetry-sender"])
    apply_override(installer.root, model["services"]["spire-observer"], role="observer")
    installer.preserve_restart_policies(model)
    path = installer.root / "observation-compose.json"
    atomic(previous, plan)
    atomic(path, model)
    common = (
        "compose",
        "--env-file",
        os.devnull,
        "-p",
        installer.compose_project,
        "-f",
        str(path),
    )
    completed = installer.root / "observation-provision-result.json"
    if not completed.exists() or read(completed).get("revision") != revision:
        for task in (
            "spire-status-provision",
            "spire-dashboard-provision",
            "spire-alert-provision",
        ):
            installer.docker(*common, "run", "--rm", "--no-deps", task)
        atomic(completed, {"owner": OWNER, "revision": revision})
    installer.docker(
        *common,
        "up",
        "-d",
        "--no-build",
        "--pull",
        "never",
        "--wait",
        "--wait-timeout",
        "90",
        "spire-observer",
        "spire-status-publisher",
        "spire-telemetry-sender",
    )


def dashboard(
    configuration: Path = Path("/etc/aelev"),
    secret_root: Path = Path("/run/secrets/grafana"),
    *,
    alert_only: bool = False,
) -> None:
    plan = read(configuration / "observation.json")
    parsed = urlsplit(origin(plan["grafana_url"]))
    token = (secret_root / "token").read_text().strip()
    if not token or any(c.isspace() for c in token):
        raise ValueError("SPIRE_GRAFANA_TOKEN_INVALID")
    context = ssl.create_default_context(cafile=str(secret_root / "grafana-ca.crt"))

    def call(
        method: str, route: str, value: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        assert parsed.hostname is not None
        client = http.client.HTTPSConnection(
            parsed.hostname, parsed.port or 443, timeout=5, context=context
        )
        try:
            headers = {
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            }
            client.request(
                method,
                route,
                json.dumps(value).encode() if value else None,
                headers,
            )
            response = client.getresponse()
            raw = response.read(1048577)
            if len(raw) > 1048576:
                raise ValueError("SPIRE_GRAFANA_PUBLICATION_FAILED")
            if response.status not in {200, 201, 404}:
                raise ValueError("SPIRE_GRAFANA_HTTP_" + str(response.status))
            return response.status, json.loads(raw) if raw else {}
        finally:
            client.close()

    folder = "infrastructure-spire"
    code, _ = call("GET", "/api/folders/" + folder)
    if code != 200:
        raise ValueError("SPIRE_GRAFANA_OWN_FOLDER_REQUIRED")
    definition = read(configuration / "dashboard.json")
    if not alert_only:
        publish_dashboard(call, folder, definition)
        return
    alert = read(configuration / "alert.json")
    if (
        alert.get("uid") != "aelev-spire-readiness"
        or alert.get("folderUID") != folder
        or alert.get("labels", {}).get("service") != OWNER
        or alert.get("labels", {}).get("owner_instance")
        != plan["presentation"]["module_id"]
    ):
        raise ValueError("SPIRE_GRAFANA_FOREIGN_ALERT_REFUSED")
    route = "/api/v1/provisioning/alert-rules"
    code, current = call("GET", route + "/aelev-spire-readiness")
    if code == 200 and (
        current.get("folderUID") != folder
        or current.get("labels", {}).get("owner_instance")
        != alert["labels"]["owner_instance"]
    ):
        raise ValueError("SPIRE_GRAFANA_FOREIGN_ALERT_REFUSED")
    code, _ = call(
        "PUT" if code == 200 else "POST",
        route + "/aelev-spire-readiness" if code == 200 else route,
        alert,
    )
    if code not in {200, 201}:
        raise ValueError("SPIRE_GRAFANA_PUBLICATION_FAILED")


def publish_dashboard(call: Any, folder: str, definition: dict[str, Any]) -> None:
    if definition.get("uid") != "aelev-infrastructure-spire":
        raise ValueError("SPIRE_GRAFANA_FOREIGN_DASHBOARD_REFUSED")
    code, current = call("GET", "/api/dashboards/uid/aelev-infrastructure-spire")
    if code == 200:
        if current["meta"].get("folderUid") != folder or not set(
            definition.get("tags", [])
        ).issubset(set(current["dashboard"].get("tags", []))):
            raise ValueError("SPIRE_GRAFANA_FOREIGN_DASHBOARD_REFUSED")
        definition["id"], definition["version"] = (
            current["dashboard"]["id"],
            current["dashboard"]["version"],
        )
    code, _ = call(
        "POST",
        "/api/dashboards/db",
        {"folderUid": folder, "dashboard": definition, "overwrite": False},
    )
    if code != 200:
        raise ValueError("SPIRE_GRAFANA_PUBLICATION_FAILED")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("seed", "dashboard", "alert"))
    args = parser.parse_args()
    if args.action == "seed":
        raw = sys.stdin.buffer.read(2 * 1024**2 + 1)
        if len(raw) > 2 * 1024**2:
            raise ValueError("SPIRE_OBSERVATION_INPUT_TOO_LARGE")
        seed(Path("/var/lib/spire"), json.loads(raw))
    else:
        dashboard(alert_only=args.action == "alert")


if __name__ == "__main__":
    try:
        main()
    except (
        OSError,
        ValueError,
        RuntimeError,
        KeyError,
        TypeError,
        http.client.HTTPException,
    ) as error:
        code = str(error)
        print(
            json.dumps(
                {
                    "code": (
                        code
                        if re.fullmatch(r"SPIRE_[A-Z0-9_]{1,120}", code)
                        else "SPIRE_OBSERVATION_SETUP_FAILED"
                    )
                }
            ),
            flush=True,
        )
        raise SystemExit(1) from None
