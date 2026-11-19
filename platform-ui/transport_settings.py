"""Public transport inputs; usable by an operator with only Python's stdlib."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit


def spiffe_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("canonical proxy SPIFFE ID required")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "spiffe"
        or not re.fullmatch(r"[a-z0-9._-]{1,255}", parsed.netloc)
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
        or value != "spiffe://" + parsed.netloc + parsed.path
        or any(
            not re.fullmatch(r"[A-Za-z0-9._-]+", part) or part in {".", ".."}
            for part in parsed.path.split("/")[1:]
        )
    ):
        raise ValueError("canonical proxy SPIFFE ID required")
    return value


def hostname(value: str) -> str:
    if not value or len(value) > 253 or "%" in value:
        raise ValueError("unambiguous hostname required")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        if re.fullmatch(r"[0-9.]+", value) or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
            for part in value.lower().split(".")
        ):
            raise ValueError("unambiguous hostname required") from None
        return value.lower()


def https_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
        or parsed.path not in {"", "/"}
        or "\\" in value
        or parsed.netloc.endswith(":")
        or any(ord(char) < 33 or ord(char) == 127 for char in value)
    ):
        raise ValueError("unambiguous HTTPS origin required")
    host = hostname(parsed.hostname)
    port = parsed.port
    if port is not None and not 0 < port <= 65535:
        raise ValueError("valid HTTPS port required")
    authority = "[" + host + "]" if ":" in host else host
    if port is not None:
        authority += ":" + str(port)
    return "https://" + authority


def network_parameters(
    api_url: str | None, proxy_id: str | None, tls_name: str | None
) -> tuple[str, str, str] | None:
    if api_url is None and proxy_id is None and tls_name is None:
        return None
    if api_url is None or proxy_id is None:
        raise ValueError("network render requires API URL and proxy SPIFFE ID")
    endpoint = https_origin(api_url)
    name = hostname(
        tls_name if tls_name is not None else str(urlsplit(endpoint).hostname)
    )
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        raise ValueError(
            "Nginx upstream verification requires an explicit DNS TLS name for an IP endpoint"
        )
    return endpoint, name, spiffe_id(proxy_id)


def api_probe_url(value: str) -> str:
    if "\\" in value or any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise ValueError("exact HTTPS API readiness URL required")
    parsed = urlsplit(value)
    if (
        parsed.path != "/api/readyz"
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
    ):
        raise ValueError("exact HTTPS API readiness URL required")
    return https_origin(parsed._replace(path="").geturl()) + "/api/readyz"
