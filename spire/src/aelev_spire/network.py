"""Explicit node-local bridge IPAM, never an inter-role endpoint substitute."""

import ipaddress
from typing import Any


def subnet(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("SPIRE_INSTALL_ACCESS_SUBNET_INVALID")
    try:
        network = ipaddress.IPv4Network(value, strict=True)
    except ValueError:
        raise ValueError("SPIRE_INSTALL_ACCESS_SUBNET_INVALID") from None
    private = [
        ipaddress.IPv4Network(prefix)
        for prefix in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    ]
    if not 24 <= network.prefixlen <= 28 or not any(
        network.subnet_of(parent) for parent in private
    ):
        raise ValueError("SPIRE_INSTALL_ACCESS_SUBNET_INVALID")
    return str(network)


def configure(model: dict[str, Any], name: str, value: object) -> None:
    selected = subnet(value)
    if selected is not None:
        network = model["networks"][name]
        if network.get("external"):
            raise ValueError("SPIRE_INSTALL_EXTERNAL_NETWORK_IPAM_FORBIDDEN")
        network["ipam"] = {"config": [{"subnet": selected}]}
