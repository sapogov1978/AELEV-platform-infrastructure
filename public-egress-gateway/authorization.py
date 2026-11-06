"""Strict operator-approved grants; no consumer catalogue or self-registration."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

MAX_GRANT_BYTES = 65536
MAX_IDENTITIES = 1024
FIELDS = frozenset(
    {
        "schema_version",
        "identity",
        "expires_at",
        "mode",
        "max_response_bytes",
        "timeout_seconds",
        "max_concurrency",
        "per_host_interval_seconds",
        "credential_scopes",
        "post_scopes",
    }
)


class GrantRejected(ValueError):
    """Missing, revoked, expired or malformed authorization."""


def identity_filename(identity: str) -> str:
    validate_identity(identity)
    return hashlib.sha256(identity.encode()).hexdigest() + ".json"


def validate_identity(identity: str) -> None:
    try:
        parsed = urlsplit(identity)
    except ValueError as error:
        raise GrantRejected("EGRESS_IDENTITY_BLOCKED") from error
    if (
        parsed.scheme != "spiffe"
        or not identity.startswith("spiffe://")
        or not parsed.hostname
        or re.fullmatch(r"[a-z0-9._-]{1,255}", parsed.hostname) is None
        or parsed.netloc != parsed.hostname
        or not parsed.path.startswith("/")
        or parsed.path == "/"
        or re.fullmatch(r"/[A-Za-z0-9._/-]+", parsed.path) is None
        or parsed.query
        or parsed.fragment
        or "%" in identity
        or any(part in {"", ".", ".."} for part in parsed.path[1:].split("/"))
        or any(character.isspace() for character in identity)
        or len(identity) > 2048
    ):
        raise GrantRejected("EGRESS_IDENTITY_BLOCKED")


def strict_json(content: str) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise GrantRejected("EGRESS_GRANT_INVALID")
            result[key] = value
        return result

    def constant(value: str) -> object:
        del value
        raise GrantRejected("EGRESS_GRANT_INVALID")

    try:
        return cast(
            object,
            json.loads(content, object_pairs_hook=pairs, parse_constant=constant),
        )
    except (ValueError, RecursionError) as error:
        raise GrantRejected("EGRESS_GRANT_INVALID") from error


def positive_int(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise GrantRejected("EGRESS_GRANT_INVALID")
    return value


def finite_number(value: object, *, positive: bool) -> float:
    if type(value) not in {int, float}:
        raise GrantRejected("EGRESS_GRANT_INVALID")
    try:
        result = float(cast(float, value))
    except OverflowError as error:
        raise GrantRejected("EGRESS_GRANT_INVALID") from error
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise GrantRejected("EGRESS_GRANT_INVALID")
    return result


def hostname(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 253:
        raise GrantRejected("EGRESS_GRANT_INVALID")
    normalized = value.lower().rstrip(".")
    if normalized != value or any(
        c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in value
    ):
        raise GrantRejected("EGRESS_GRANT_INVALID")
    if any(
        not label or label.startswith("-") or label.endswith("-")
        for label in value.split(".")
    ):
        raise GrantRejected("EGRESS_GRANT_INVALID")
    return normalized


@dataclass(frozen=True)
class Grant:
    identity: str
    expires_at: datetime
    mode: str
    max_response_bytes: int
    timeout_seconds: float
    max_concurrency: int
    per_host_interval_seconds: float
    credential_scopes: dict[str, frozenset[str]]
    post_scopes: dict[str, tuple[str, ...]]

    @classmethod
    def parse(cls, value: object) -> Grant:
        if not isinstance(value, dict):
            raise GrantRejected("EGRESS_GRANT_INVALID")
        fields = cast(dict[str, object], value)
        if frozenset(fields) != FIELDS:
            raise GrantRejected("EGRESS_GRANT_INVALID")
        identity, expiry, mode = (
            fields["identity"],
            fields["expires_at"],
            fields["mode"],
        )
        if type(fields["schema_version"]) is not int or fields["schema_version"] != 1:
            raise GrantRejected("EGRESS_GRANT_INVALID")
        if (
            not isinstance(identity, str)
            or not isinstance(expiry, str)
            or not isinstance(mode, str)
            or mode not in {"EXPLORATION", "REGULAR"}
        ):
            raise GrantRejected("EGRESS_GRANT_INVALID")
        validate_identity(identity)
        try:
            expires_at = datetime.fromisoformat(expiry)
        except ValueError as error:
            raise GrantRejected("EGRESS_GRANT_INVALID") from error
        if expires_at.tzinfo is None:
            raise GrantRejected("EGRESS_GRANT_INVALID")
        if expires_at <= datetime.now(UTC):
            raise GrantRejected("EGRESS_GRANT_EXPIRED")
        raw_credentials, raw_post = fields["credential_scopes"], fields["post_scopes"]
        if not isinstance(raw_credentials, dict) or not isinstance(raw_post, dict):
            raise GrantRejected("EGRESS_GRANT_INVALID")
        credentials: dict[str, frozenset[str]] = {}
        post: dict[str, tuple[str, ...]] = {}
        for header, hosts in cast(dict[object, object], raw_credentials).items():
            if (
                header not in {"authorization", "x-subscription-token"}
                or not isinstance(hosts, list)
                or not hosts
            ):
                raise GrantRejected("EGRESS_GRANT_INVALID")
            credentials[cast(str, header)] = frozenset(
                hostname(host) for host in cast(list[object], hosts)
            )
        for host, paths in cast(dict[object, object], raw_post).items():
            if not isinstance(paths, list) or not paths:
                raise GrantRejected("EGRESS_GRANT_INVALID")
            validated_paths: list[str] = []
            for path in cast(list[object], paths):
                if (
                    not isinstance(path, str)
                    or not path.startswith("/")
                    or path.startswith("//")
                    or "?" in path
                    or "#" in path
                    or any(c.isspace() for c in path)
                ):
                    raise GrantRejected("EGRESS_GRANT_INVALID")
                validated_paths.append(path)
            post[hostname(host)] = tuple(validated_paths)
        if mode == "EXPLORATION" and (credentials or post):
            raise GrantRejected("EGRESS_GRANT_INVALID")
        return cls(
            identity,
            expires_at,
            mode,
            positive_int(fields["max_response_bytes"]),
            finite_number(fields["timeout_seconds"], positive=True),
            positive_int(fields["max_concurrency"]),
            finite_number(fields["per_host_interval_seconds"], positive=False),
            credentials,
            post,
        )


class GrantStore:
    """Read before each admission so expiry/revocation never require a restart."""

    def __init__(self, directory: Path) -> None:
        if not directory.is_dir():
            raise RuntimeError("grant directory is missing")
        self.directory = directory

    def load(self, identity: str) -> Grant:
        path = self.directory / identity_filename(identity)
        try:
            # Projected Kubernetes volumes use ..data symlinks. Trust only
            # regular files which resolve within the read-only grant mount.
            resolved = path.resolve(strict=True)
            if (
                not resolved.is_relative_to(self.directory.resolve())
                or not resolved.is_file()
            ):
                raise GrantRejected("EGRESS_IDENTITY_BLOCKED")
            with resolved.open("rb") as stream:
                content = stream.read(MAX_GRANT_BYTES + 1)
            if len(content) > MAX_GRANT_BYTES:
                raise GrantRejected("EGRESS_GRANT_INVALID")
            grant = Grant.parse(strict_json(content.decode()))
            if grant.identity != identity:
                raise GrantRejected("EGRESS_IDENTITY_BLOCKED")
            return grant
        except (OSError, UnicodeError) as error:
            raise GrantRejected("EGRESS_IDENTITY_BLOCKED") from error
