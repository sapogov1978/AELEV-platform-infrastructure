"""Resolve only Docker Desktop's reserved host from an unprivileged helper."""

from __future__ import annotations

import ipaddress
import socket
import sys

CODE = "SPIRE_OBSERVATION_DESKTOP_HOST_UNAVAILABLE"


def private_address(value: str) -> str:
    address = ipaddress.IPv4Address(value)
    if not address.is_private or address.is_loopback or address.is_unspecified:
        raise ValueError(CODE)
    return str(address)


def resolve() -> str:
    try:
        addresses = {
            ipaddress.IPv4Address(str(row[4][0]))
            for row in socket.getaddrinfo(
                "host.docker.internal", None, socket.AF_INET, socket.SOCK_STREAM
            )
        }
        if len(addresses) != 1:
            raise ValueError(CODE)
        return private_address(str(addresses.pop()))
    except (OSError, ValueError, IndexError, TypeError):
        raise ValueError(CODE) from None


def main() -> None:
    try:
        print(resolve())
    except ValueError:
        sys.stderr.write(CODE + "\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
