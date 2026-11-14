"""Own server's read-only state listener; no consumer or foreign store access."""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from .public_state import PeerTLS, material

if TYPE_CHECKING:
    from .installation import Installer

PREFIX = "AELEV_INFRASTRUCTURE_SPIRE_PUBLIC_SOURCE_"
FILES = ("source.crt", "source.key", "source-client-ca.crt")


def specification(plan: dict[str, Any]) -> dict[str, Any] | None:
    value = plan.get("public_source")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("SPIRE_PUBLIC_SOURCE_CONFIGURATION_INVALID")
    selected = cast(dict[str, Any], value)
    if set(selected) != {
        "bind_address",
        "public_port",
        "identity",
        "client_identities",
    }:
        raise ValueError("SPIRE_PUBLIC_SOURCE_CONFIGURATION_INVALID")
    if not isinstance(selected["bind_address"], str):
        raise ValueError("SPIRE_PUBLIC_SOURCE_CONFIGURATION_INVALID")
    address = ipaddress.ip_address(selected["bind_address"])
    port = selected["public_port"]
    if (
        type(port) is not int
        or not 1 <= port <= 65535
        or port == plan.get("public_port", 18081)
    ):
        raise ValueError("SPIRE_PUBLIC_SOURCE_CONFIGURATION_INVALID")
    clients = selected["client_identities"]
    if (
        not isinstance(clients, list)
        or not clients
        or not all(
            isinstance(item, str) and "," not in item
            for item in cast(list[object], clients)
        )
    ):
        raise ValueError("SPIRE_PUBLIC_SOURCE_CONFIGURATION_INVALID")
    peers = cast(list[str], clients)
    if len(peers) != len(set(peers)) or len(peers) > 8:
        raise ValueError("SPIRE_PUBLIC_SOURCE_CONFIGURATION_INVALID")
    PeerTLS(
        Path("/run/secrets/public-source"),
        *FILES,
        selected["identity"],
        tuple(peers),
        server=True,
    )
    return {**selected, "bind_address": str(address)}


def configure(
    installer: Installer,
    model: dict[str, Any],
    plan: dict[str, Any],
    source: Path = Path("/run/input/public-source"),
) -> None:
    selected = specification(plan)
    if selected is None:
        return
    secrets = installer.volume("public-source-secrets")
    receipts = installer.volume("certificate-renewal-status")
    if source.exists():
        identity = PeerTLS(
            source,
            *FILES,
            selected["identity"],
            tuple(selected["client_identities"]),
            server=True,
        )
        identity.current()
        installer.seed(
            secrets,
            "public-source-secrets",
            {
                name: material(source / name, private=name.endswith(".key"))
                for name in FILES
            },
        )
    else:
        # Repeat installation validates the existing owned secret volume. It
        # never creates a new key or accepts an empty replacement volume.
        installer.docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "128m",
            "--pids-limit",
            "64",
            "--entrypoint",
            "python",
            "-v",
            secrets + ":/run/secrets/public-source:ro",
            installer.image,
            "-c",
            "from pathlib import Path; import sys; "
            "from aelev_spire.public_state import PeerTLS; "
            "PeerTLS(Path('/run/secrets/public-source'),'source.crt',"
            "'source.key','source-client-ca.crt',sys.argv[1],"
            "tuple(sys.argv[2].split(',')),server=True).current()",
            selected["identity"],
            ",".join(selected["client_identities"]),
        )
    service = model["services"]["spire-server"]
    environment = service.setdefault("environment", {})
    environment.pop("AELEV_INFRASTRUCTURE_SPIRE_OBSERVATION_ROOT", None)
    environment.update(
        {
            PREFIX + "MODE": "https",
            PREFIX + "TLS_ROOT": "/run/secrets/public-source",
            PREFIX + "IDENTITY": selected["identity"],
            PREFIX + "CLIENT_IDENTITIES": ",".join(selected["client_identities"]),
        }
    )
    service["volumes"] = [
        item for item in service["volumes"] if item.get("target") != "/run/observation"
    ]
    service["volumes"] += [
        {
            "type": "volume",
            "source": "spire-public-source-secrets",
            "target": "/run/secrets/public-source",
            "read_only": True,
        },
        {
            "type": "volume",
            "source": "spire-certificate-renewal-status",
            "target": "/run/certificate-renewal",
            "read_only": True,
        },
    ]
    service.setdefault("ports", []).append(
        {
            "target": 8084,
            "published": str(selected["public_port"]),
            "host_ip": selected["bind_address"],
            "protocol": "tcp",
        }
    )
    model["volumes"].pop("spire-observation-source", None)
    model["volumes"].update(
        {
            "spire-public-source-secrets": {"name": secrets},
            "spire-certificate-renewal-status": {"name": receipts},
        }
    )
