"""Operational entry points for a PostgreSQL cluster."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from aelev_postgresql.configuration import (
    PREFIX as PREFIX,
    RUNTIME as RUNTIME,
    SECRETS as SECRETS,
    desired_cluster_config as desired_cluster_config,
    includes,
    safety_config as safety_config,
    setting as setting,
    tls_context as tls_context,
)

DATA = Path("/var/lib/postgresql/data")


def secret(name: str) -> str:
    """Read a nonempty secret without including its value in errors."""
    value = (SECRETS / name).read_text().strip()
    if not value:
        raise ValueError(f"Empty secret file: {name}")
    return value


def connection(
    database: str = "postgres", *, tls: bool = True
) -> psycopg.Connection[Any]:
    """Select the writable member using native libpq multi-host discovery."""
    parameters: dict[str, Any] = {
        "host": ",".join(setting(f"NODE_{n}_HOST") for n in range(1, 4)),
        "port": ",".join(setting(f"NODE_{n}_PORT") for n in range(1, 4)),
        "user": setting("ADMIN_USER"),
        "password": secret("admin-password"),
        "dbname": database,
        "target_session_attrs": "read-write",
        "connect_timeout": 3,
        "sslmode": "verify-full" if tls else "disable",
    }
    if tls:
        parameters.update(
            sslrootcert=str(SECRETS / "ca.crt"),
            sslcert=str(SECRETS / "admin.crt"),
            sslkey=str(SECRETS / "admin.key"),
        )
    return psycopg.connect(**parameters)


def node_config() -> dict[str, Any]:
    """Render Patroni configuration into private, ephemeral runtime storage."""
    node = setting("NODE_ID")
    if node not in {"1", "2", "3"}:
        raise ValueError("NODE_ID must be 1, 2 or 3")
    from aelev_postgresql.fencing import mode

    mechanism = mode()
    for name in (
        "server.crt",
        "server.key",
        "admin.crt",
        "admin.key",
        "replication.crt",
        "replication.key",
        "etcd-client.crt",
        "etcd-client.key",
        "ca.crt",
        "etcd-ca.crt",
    ):
        if not (SECRETS / name).is_file():
            raise ValueError(f"Required TLS file: {name}")
    host = setting(f"NODE_{node}_HOST")
    port = setting(f"NODE_{node}_PORT")
    rest_port = setting(f"NODE_{node}_REST_PORT")
    admin = setting("ADMIN_USER")
    replication = "aelev_replication"

    def authentication(user: str, filename: str) -> dict[str, str]:
        return {
            "username": user,
            "password": secret(filename),
            "sslmode": "verify-full",
            "sslrootcert": str(SECRETS / "ca.crt"),
            "sslcert": str(
                SECRETS / ("admin.crt" if user == admin else "replication.crt")
            ),
            "sslkey": str(
                SECRETS / ("admin.key" if user == admin else "replication.key")
            ),
        }

    return {
        "name": f"postgresql-{node}",
        "scope": setting("CLUSTER"),
        "namespace": "/aelev-postgresql/",
        "log": {"type": "json", "format": ["asctime", "levelname", "message"]},
        "bootstrap": {
            "dcs": desired_cluster_config(),
            "initdb": [
                "data-checksums",
                {"encoding": "UTF8"},
                {"auth-local": "peer"},
                {"auth-host": "scram-sha-256"},
            ],
        },
        "etcd3": {
            "hosts": [
                f"{setting(f'NODE_{n}_ETCD_HOST')}:{setting(f'NODE_{n}_ETCD_CLIENT_PORT')}"
                for n in range(1, 4)
            ],
            "protocol": "https",
            "cacert": str(SECRETS / "etcd-ca.crt"),
            "cert": str(SECRETS / "etcd-client.crt"),
            "key": str(SECRETS / "etcd-client.key"),
        },
        "restapi": {
            "listen": f"0.0.0.0:{rest_port}",
            "connect_address": f"{host}:{rest_port}",
            "certfile": str(SECRETS / "server.crt"),
            "keyfile": str(SECRETS / "server.key"),
            "cafile": str(SECRETS / "ca.crt"),
            "verify_client": "required",
            "authentication": {"username": admin, "password": secret("admin-password")},
        },
        "ctl": {
            "cacert": str(SECRETS / "ca.crt"),
            "certfile": str(SECRETS / "admin.crt"),
            "keyfile": str(SECRETS / "admin.key"),
        },
        "watchdog": {
            "mode": "required" if mechanism == "watchdog" else "off",
            "device": "/dev/watchdog",
            "safety_margin": -1,
        },
        "postgresql": {
            **(
                {"pre_promote": "python -m aelev_postgresql fence-pre-promote"}
                if mechanism != "watchdog"
                else {}
            ),
            "listen": f"0.0.0.0:{port}",
            "connect_address": f"{host}:{port}",
            "data_dir": str(DATA / "pgdata"),
            "pgpass": str(RUNTIME / "pgpass"),
            "authentication": {
                "superuser": authentication(admin, "admin-password"),
                "replication": authentication(replication, "replication-password"),
            },
            "parameters": {
                "ssl": "on",
                "ssl_min_protocol_version": "TLSv1.2",
                "ssl_cert_file": str(SECRETS / "server.crt"),
                "ssl_key_file": str(SECRETS / "server.key"),
                "ssl_ca_file": str(SECRETS / "ca.crt"),
                "unix_socket_directories": str(RUNTIME),
                "logging_collector": "off",
                "log_destination": "stderr",
            },
            "pg_hba": [
                "local all all peer",
                "hostssl replication all all scram-sha-256 clientcert=verify-full",
                "hostssl all all all scram-sha-256 clientcert=verify-full",
                "host all all all reject",
                "host replication all all reject",
            ],
            "remove_data_directory_on_rewind_failure": False,
            "remove_data_directory_on_diverged_timelines": False,
        },
    }


def start() -> None:
    """Start Patroni, rejecting unowned or mismatched data volumes."""
    marker = DATA / "aelev-cluster"
    scope = setting("CLUSTER")
    if marker.exists():
        if marker.read_text() != scope:
            raise ValueError("Data volume belongs to a different cluster")
    elif any(DATA.iterdir()):
        raise ValueError("Refusing to adopt a nonempty, unmanaged data volume")
    else:
        marker.write_text(scope)
    from aelev_postgresql.fencing import validate_runtime

    validate_runtime()
    config = node_config()
    path = RUNTIME / "patroni.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    os.execvp("patroni", ["patroni", str(path)])


def provision() -> None:
    """Provision exactly one isolated database without adopting foreign objects."""
    database = setting("PROVISION_DATABASE")
    role = setting("PROVISION_ROLE")
    password = (
        os.environ[PREFIX + "PROVISION_PASSWORD"]
        if PREFIX + "PROVISION_PASSWORD" in os.environ
        else secret("provision-password")
    )
    if not password:
        raise ValueError("Empty provisioning password")
    if database in {"postgres", "template0", "template1"} or role in {
        setting("ADMIN_USER"),
        "aelev_replication",
    }:
        raise ValueError("Reserved database or role")
    verify()
    with connection() as conn:
        conn.autocommit = True
        conn.execute("SELECT pg_advisory_lock(9817001)")
        row = conn.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname=%s",
            (role,),
        ).fetchone()
        owner = conn.execute(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s",
            (database,),
        ).fetchone()
        marker = f"aelev-postgresql:database:{database}"
        owned = conn.execute(
            "SELECT shobj_description(oid, 'pg_authid') FROM pg_roles WHERE rolname=%s",
            (role,),
        ).fetchone()
        if row and (any(row) or not owned or owned[0] != marker):
            raise ValueError("Refusing to adopt an existing or privileged role")
        if owner and owner[0] != role:
            raise ValueError("Refusing to change an existing database owner")
        if not row:
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}"
                ).format(sql.Identifier(role), sql.Literal(password))
            )
            conn.execute(
                sql.SQL("COMMENT ON ROLE {} IS {}").format(
                    sql.Identifier(role), sql.Literal(marker)
                )
            )
        else:
            conn.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )
        if not owner:
            conn.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(database), sql.Identifier(role)
                )
            )
        conn.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(database)
            )
        )
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(database), sql.Identifier(role)
            )
        )


def health() -> None:
    """Check local Patroni and PostgreSQL readiness over authenticated TLS."""
    from aelev_postgresql.health import check

    check()


def verify() -> None:
    """Reject a persisted DCS configuration that weakens fixed safety rules."""

    for node in range(1, 4):
        url = f"https://{setting(f'NODE_{node}_HOST')}:{setting(f'NODE_{node}_REST_PORT')}/config"
        try:
            with urllib.request.urlopen(
                url, context=tls_context(), timeout=3
            ) as response:
                actual = json.load(response)
        except OSError:
            continue
        if not includes(actual, desired_cluster_config()):
            raise ValueError(
                "Persisted cluster safety configuration does not match the delivery"
            )
        return
    raise ValueError("No reachable Patroni configuration endpoint")


def configure() -> None:
    """Explicitly apply canonical settings through Patroni's standard API."""
    authorization = base64.b64encode(
        f"{setting('ADMIN_USER')}:{secret('admin-password')}".encode()
    ).decode()
    for node in range(1, 4):
        url = f"https://{setting(f'NODE_{node}_HOST')}:{setting(f'NODE_{node}_REST_PORT')}/config"
        request = urllib.request.Request(
            url,
            data=json.dumps(desired_cluster_config()).encode(),
            method="PATCH",
            headers={
                "Authorization": "Basic " + authorization,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, context=tls_context(), timeout=5):
                verify()
                return
        except OSError:
            continue
    raise ValueError("No reachable authenticated Patroni configuration endpoint")


def main() -> None:
    """Dispatch operational commands and redact database failure details."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "operation",
        choices=[
            "start",
            "health",
            "provision",
            "verify",
            "configure",
            "fence-pre-promote",
            "fencing-serve",
            "fencing-health",
        ],
    )
    args = parser.parse_args()
    logging.basicConfig(
        format='{"level":"%(levelname)s","message":"%(message)s"}', level=logging.INFO
    )
    try:
        from aelev_postgresql import fencing

        {
            "start": start,
            "health": health,
            "provision": provision,
            "verify": verify,
            "configure": configure,
            "fence-pre-promote": fencing.pre_promote,
            "fencing-serve": fencing.serve,
            "fencing-health": fencing.health,
        }[args.operation]()
    except (OSError, ValueError, psycopg.Error) as error:
        logging.error("Operation failed: %s", type(error).__name__)
        sys.exit(1)


if __name__ == "__main__":
    main()
