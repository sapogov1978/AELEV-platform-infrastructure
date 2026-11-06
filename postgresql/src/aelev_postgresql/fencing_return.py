"""Finish only durable Docker return intents, independently of promotion RPCs.

This worker never stops a process, elects a leader, creates a lease, promotes
PostgreSQL or changes its data. Patroni remains the only HA coordinator.
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import ssl
import threading
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

from aelev_postgresql.configuration import SECRETS, setting
from aelev_postgresql.fencing import (
    CLUSTER_LABEL,
    NODE_LABEL,
    NODES,
    Claim,
    Docker,
    FencingError,
    request_json,
)
from aelev_postgresql.fencing_intent import RETURN_DELAYS, IntentStore

INTERVAL_SECONDS = 5
Record = Callable[[str, dict[str, Any], str, list[str], str | None], None]


def confirmed_claim() -> Claim | None:
    """Confirm a lease OR its absence from two distinct native DCS members.

    None is positive absence evidence, never an unreachable/malformed response.
    This admission is for restarting Patroni only, not a promotion receipt.
    """
    key = base64.b64encode(
        f"/aelev-postgresql/{setting('CLUSTER')}/leader".encode()
    ).decode()
    context = ssl.create_default_context(cafile=str(SECRETS / "etcd-ca.crt"))
    context.load_cert_chain(SECRETS / "etcd-client.crt", SECRETS / "etcd-client.key")
    claims: list[Claim | None] = []
    endpoints: set[str] = set()
    members: set[str] = set()
    cluster: str | None = None
    revision = 0
    for number in range(1, 4):
        endpoint = (
            f"https://{setting(f'NODE_{number}_ETCD_HOST')}:"
            f"{setting(f'NODE_{number}_ETCD_CLIENT_PORT')}/v3/kv/range"
        )
        if endpoint in endpoints:
            continue
        endpoints.add(endpoint)
        try:
            data = request_json(endpoint, {"key": key, "serializable": False}, context)
        except OSError:
            continue
        if not isinstance(data, dict) or not isinstance(data.get("header"), dict):
            raise FencingError("Invalid native DCS header", "DCS_RESPONSE_INVALID")
        header = data["header"]
        cluster_id = str(header.get("cluster_id", ""))
        member_id = str(header.get("member_id", ""))
        current_revision = str(header.get("revision", ""))
        if not all(
            value.isdecimal() and int(value) > 0
            for value in (cluster_id, member_id, current_revision)
        ):
            raise FencingError("Invalid native DCS response", "DCS_RESPONSE_INVALID")
        if cluster is not None and (
            cluster_id != cluster or int(current_revision) < revision
        ):
            raise FencingError(
                "DCS cluster or revision changed", "DCS_RESPONSE_CHANGED"
            )
        cluster, revision = cluster_id, int(current_revision)
        rows = data.get("kvs", [])
        if type(rows) is not list:
            raise FencingError("Invalid native DCS range", "DCS_RESPONSE_INVALID")
        count = str(data.get("count", len(rows)))
        if (
            len(rows) > 1
            or not count.isdecimal()
            or int(count) != len(rows)
            or data.get("more", False) is not False
        ):
            raise FencingError("Invalid native DCS range", "DCS_RESPONSE_INVALID")
        claim = None
        if rows:
            row = rows[0]
            candidate = base64.b64decode(row["value"], validate=True).decode()
            lease, created = str(row.get("lease", "0")), str(row["create_revision"])
            if (
                candidate not in NODES
                or not lease.isdecimal()
                or int(lease) <= 0
                or not created.isdecimal()
                or not 0 < int(created) <= revision
            ):
                raise FencingError("Invalid Patroni lease", "INVALID_LEADER_LEASE")
            claim = Claim(candidate, lease, created)
        if claims and claim != claims[0]:
            raise FencingError("DCS reads disagree", "LEASE_CHANGED")
        if member_id in members:
            continue
        members.add(member_id)
        claims.append(claim)
        if len(claims) == 2:
            return claim
    raise FencingError("Two DCS confirmations required", "DCS_QUORUM_UNCONFIRMED")


class Returns:
    """Serial bounded resumption under the same per-node/file locks as fencing."""

    def __init__(
        self,
        backend: Docker,
        locks: dict[str, Any],
        record: Record,
    ) -> None:
        self.backend = backend
        self.locks = locks
        self.record = record
        self.stop = threading.Event()
        self.dispatched = False

    def inspect(self, node: str, intent: dict[str, Any]) -> dict[str, Any]:
        """Inspect the stored exact ID; do not discover or adopt replacements."""
        if intent["cluster"] != setting("CLUSTER"):
            raise FencingError("Return cluster changed", "TARGET_IDENTITY_CHANGED")
        filters = urllib.parse.quote(
            json.dumps(
                {
                    "label": [
                        f"{CLUSTER_LABEL}={setting('CLUSTER')}",
                        f"{NODE_LABEL}={node}",
                    ]
                }
            )
        )
        rows = self.backend.call("GET", f"/containers/json?all=true&filters={filters}")
        if len(rows) != 1 or rows[0].get("Id") != intent["container_id"]:
            raise FencingError("Return target is not unique", "TARGET_IDENTITY_CHANGED")
        data: dict[str, Any] = self.backend.call(
            "GET", f"/containers/{intent['container_id']}/json"
        )
        labels = data["Config"].get("Labels", {})
        if (
            data.get("Id") != intent["container_id"]
            or labels.get(CLUSTER_LABEL) != setting("CLUSTER")
            or labels.get(NODE_LABEL) != node
            or data["State"].get("Restarting") is not False
        ):
            raise FencingError("Return target changed", "TARGET_IDENTITY_CHANGED")
        return data["State"]  # type: ignore[no-any-return]

    def resume(self, node: str, store: IntentStore, intent: dict[str, Any]) -> bool:
        """Reconcile a lost start ACK or dispatch at most one persisted start."""
        state = self.inspect(node, intent)
        claim = confirmed_claim()
        if state.get("StartedAt") != intent["started_at"]:
            if (
                intent["phase"] == "START_REQUESTED"
                and intent["start_attempts"] > 0
                and state.get("Running") is True
                and state.get("Pid", 0) > 0
            ):
                intent["phase"] = "COMPLETED"
                intent.pop("last_return_code", None)
                store.save(intent)
                self.record(
                    node, intent, "return_reconciled", [intent["container_id"]], None
                )
                return False
            raise FencingError(
                "Return incarnation changed", "PROCESS_INCARNATION_CHANGED"
            )
        if claim is not None and claim.candidate == node:
            raise FencingError(
                "Do not restart the claimant", "RETURN_TARGET_IS_CLAIMANT"
            )
        if state.get("Running") is not False or state.get("Pid") != 0:
            raise FencingError("Return termination unknown", "TERMINATION_UNCONFIRMED")
        if intent["start_attempts"] >= len(RETURN_DELAYS):
            raise FencingError("Return budget exhausted", "RETURN_ATTEMPTS_EXHAUSTED")
        if time.time() < intent.get("next_attempt_at", 0):
            return False
        # Reconfirm DCS and exact stopped process just before dispatch. No stop
        # command is ever sent by this worker, even if the inspection disagrees.
        if self.inspect(node, intent) != state:
            raise FencingError("Return target changed", "PROCESS_INCARNATION_CHANGED")
        if confirmed_claim() != claim:
            raise FencingError("Return lease changed", "LEASE_CHANGED")
        intent.update(
            phase="START_REQUESTED",
            start_attempts=intent["start_attempts"] + 1,
            next_attempt_at=time.time() + RETURN_DELAYS[intent["start_attempts"]],
            return_admission=(
                "LEASE_ABSENCE_CONFIRMED" if claim is None else "LEASE_CONFIRMED"
            ),
        )
        store.save(intent)
        self.record(node, intent, "return_requested", [intent["container_id"]], None)
        try:
            self.dispatched = True
            self.backend.call("POST", f"/containers/{intent['container_id']}/start")
        except (OSError, http.client.HTTPException):
            pass  # Inspect the actual result; do not refund an unknown command.
        after = self.inspect(node, intent)
        if (
            after.get("Running") is not True
            or after.get("Pid", 0) <= 0
            or after.get("StartedAt") == intent["started_at"]
        ):
            raise FencingError("Return not confirmed", "RETURN_UNCONFIRMED")
        # A start is not a promotion receipt, and completion does not depend on
        # the old lease still existing: only the old process exit is reconciled.
        intent["phase"] = "COMPLETED"
        intent.pop("last_return_code", None)
        store.save(intent)
        self.record(node, intent, "returned", [intent["container_id"]], None)
        return True

    def once(self) -> None:
        """Resume at most one target per pass; blocked targets do not starve peers."""
        self.dispatched = False
        allowed = setting("FENCING_DOCKER_LOCAL_NODES").split(",")
        if not allowed or set(allowed) - NODES:
            raise FencingError("Invalid return allowlist")
        for node in sorted(set(allowed)):
            if not self.locks[node].acquire(blocking=False):
                continue
            try:
                store = IntentStore(node)
                with store.locked():
                    intent = store.load()
                    if (
                        intent is None
                        or intent["phase"] in {"COMPLETED", "CANCELLED"}
                        or not intent["restart_required"]
                    ):
                        continue
                    attempts = intent["start_attempts"]
                    try:
                        if self.resume(node, store, intent):
                            return
                    except (
                        OSError,
                        ValueError,
                        KeyError,
                        TypeError,
                        http.client.HTTPException,
                    ) as error:
                        # A failed save must not accidentally persist its mutated
                        # in-memory COMPLETED phase while recording the failure.
                        intent = store.load()
                        if intent is None:
                            raise FencingError("Return intent vanished") from error
                        code = (
                            error.code
                            if isinstance(error, FencingError)
                            else "RETURN_CHECK_UNCONFIRMED"
                        )
                        if intent.get("last_return_code") != code:
                            intent["last_return_code"] = code
                            store.save(intent)
                            self.record(node, intent, "return_blocked", [], code)
                            logging.warning(
                                "fencing_return node=%s reason_code=%s", node, code
                            )
                        if intent["start_attempts"] != attempts:
                            return  # One command, including an unknown result.
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                http.client.HTTPException,
            ):
                logging.error(
                    "fencing_return node=%s reason_code=RETURN_STATE_UNAVAILABLE", node
                )
            finally:
                self.locks[node].release()
            if self.dispatched:
                return

    def run(self) -> None:
        """Retry pending intent checks, not unlimited container restarts."""
        while not self.stop.is_set():
            try:
                self.once()
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                http.client.HTTPException,
            ):
                logging.error("fencing_return reason_code=RETURN_PASS_UNAVAILABLE")
            self.stop.wait(INTERVAL_SECONDS)
