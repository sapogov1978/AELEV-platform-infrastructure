"""Runtime-scoped fencing for Patroni's standard pre_promote hook.

This module never elects a leader or renews a lease. Patroni owns both. A
promotion fences both peer incarnations, including apparently healthy peers:
an old cached role or a lost heartbeat is not evidence of a stopped writer.
"""

from __future__ import annotations

import base64
import concurrent.futures
import http.client
import json
import logging
import os
import queue
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from aelev_postgresql.__main__ import SECRETS, safety_config, setting, tls_context
from aelev_postgresql.fencing_intent import IntentStore

NODES = {f"postgresql-{number}" for number in range(1, 4)}
DCS_SLOTS = {number: threading.BoundedSemaphore(1) for number in range(1, 4)}
CLUSTER_LABEL = "io.aelev.postgresql.cluster"
NODE_LABEL = "io.aelev.postgresql.node"
KUBERNETES_NAMESPACE = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")


class FencingError(ValueError):
    """A positive, current fencing acknowledgement could not be established."""

    def __init__(self, message: str, code: str = "FENCING_UNCONFIRMED") -> None:
        super().__init__(message)
        self.code = code


def mode() -> str:
    """Require an explicit supported mechanism, never silently disable fencing."""
    value = setting("FENCING_MODE")
    if value not in {"docker", "kubernetes", "watchdog"}:
        raise FencingError("FENCING_MODE must be docker, kubernetes or watchdog")
    return value


def timeout() -> float:
    """Bound a fencing operation independently from Patroni's lease settings."""
    value = float(setting("FENCING_TIMEOUT_SECONDS"))
    if not 5 <= value <= 60:
        raise FencingError("FENCING_TIMEOUT_SECONDS must be between 5 and 60")
    return value


def validate_runtime() -> None:
    """Never fence Kubernetes Pods on behalf of a writer running in Docker."""
    if mode() == "kubernetes":
        node = setting("NODE_ID")
        if (
            not KUBERNETES_NAMESPACE.is_file()
            or KUBERNETES_NAMESPACE.read_text().strip()
            != setting("FENCING_KUBERNETES_NAMESPACE")
            or os.environ.get("HOSTNAME") != setting(f"NODE_{node}_KUBERNETES_POD")
        ):
            raise FencingError(
                "Kubernetes fencing requires the exact native Pod runtime"
            )


def request_json(
    url: str, payload: dict[str, Any] | None, context: ssl.SSLContext
) -> dict[str, Any]:
    """Use authenticated HTTPS without accepting redirects to another endpoint."""
    if urllib.parse.urlsplit(url).scheme != "https":
        raise FencingError("Fencing and DCS endpoints require HTTPS")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(
            self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
        ) -> None:
            return None

    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context), NoRedirect()
    )
    request = urllib.request.Request(
        url,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with opener.open(request, timeout=timeout()) as response:
        value: dict[str, Any] = json.load(response)
        return value


def read_dcs(
    context: ssl.SSLContext,
    payload: dict[str, Any],
    excluded: set[str] | None = None,
    reader: Any = None,
) -> tuple[str, dict[str, Any]]:
    """Race fresh linearizable reads within the native retry budget.

    DNS resolution is not covered by urllib's socket timeout. Daemon workers
    and one in-flight network request per endpoint bound that uncertainty;
    delayed responses belong only to this call and are never reused.
    """
    deadline = time.monotonic() + float(safety_config()["retry_timeout"])
    responses: queue.Queue[tuple[str, dict[str, Any] | Exception]] = queue.Queue()
    finished = threading.Event()
    read = request_json if reader is None else reader

    def worker(number: int, endpoint: str) -> None:
        remaining = max(0.0, deadline - time.monotonic())
        slot = DCS_SLOTS[number]
        if not slot.acquire(timeout=remaining):
            responses.put((endpoint, OSError("DCS endpoint busy")))
            return
        try:
            if finished.is_set() or time.monotonic() >= deadline:
                raise TimeoutError("DCS read budget exhausted")
            data = read(endpoint, payload, context)
            responses.put((endpoint, data))
        except Exception as error:
            responses.put((endpoint, error))
        finally:
            slot.release()

    endpoints: set[str] = set()
    for number in range(1, 4):
        endpoint = (
            f"https://{setting(f'NODE_{number}_ETCD_HOST')}:"
            f"{setting(f'NODE_{number}_ETCD_CLIENT_PORT')}/v3/kv/range"
        )
        if endpoint in endpoints or endpoint in (excluded or set()):
            continue
        endpoints.add(endpoint)
        threading.Thread(target=worker, args=(number, endpoint), daemon=True).start()
    for _ in endpoints:
        try:
            endpoint, data = responses.get(
                timeout=max(0.0, deadline - time.monotonic())
            )
        except queue.Empty:
            break
        if isinstance(data, OSError):
            continue
        if isinstance(data, Exception):
            finished.set()
            raise data
        finished.set()
        return endpoint, data
    finished.set()
    raise FencingError("No reachable linearizable DCS endpoint", "DCS_UNAVAILABLE")


@dataclass(frozen=True)
class Claim:
    """Identity of Patroni's existing etcd leader-key incarnation, not a new lock."""

    candidate: str
    lease: str
    revision: str

    @classmethod
    def current(cls) -> Claim:
        """Read the existing leader key using an etcd linearizable range request."""
        key = f"/aelev-postgresql/{setting('CLUSTER')}/leader"
        context = ssl.create_default_context(cafile=str(SECRETS / "etcd-ca.crt"))
        context.load_cert_chain(
            SECRETS / "etcd-client.crt", SECRETS / "etcd-client.key"
        )
        _, data = read_dcs(
            context,
            {
                "key": base64.b64encode(key.encode()).decode(),
                "serializable": False,
            },
        )
        rows = data.get("kvs", [])
        if len(rows) != 1:
            raise FencingError("No current Patroni leader lease", "NO_LEADER_LEASE")
        row = rows[0]
        name = base64.b64decode(row["value"], validate=True).decode()
        if name not in NODES or str(row.get("lease", "0")) == "0":
            raise FencingError("Invalid Patroni leader lease", "INVALID_LEADER_LEASE")
        return cls(name, str(row["lease"]), str(row["create_revision"]))

    def verify(self) -> None:
        """Reject a changed, expired, or unreachable Patroni leader lease."""
        if self != self.current():
            raise FencingError(
                "Patroni leader lease changed during fencing", "LEASE_CHANGED"
            )


class UnixConnection(http.client.HTTPConnection):
    """Access the local Docker socket only inside the trusted executor."""

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout())
        self.sock.connect("/run/docker.sock")


class Docker:
    """Terminate only explicitly labelled members of this PostgreSQL cluster."""

    def call(self, method: str, path: str) -> Any:
        connection = UnixConnection("localhost", timeout=timeout())
        try:
            connection.request(method, path)
            response = connection.getresponse()
            body = response.read()
            if response.status not in {200, 204, 304}:
                raise FencingError(
                    "Docker operation was not confirmed", "DOCKER_UNCONFIRMED"
                )
            return json.loads(body) if body else None
        finally:
            connection.close()

    def fence(self, node: str, claim: Claim, record: Any) -> list[str]:
        """Confirm process exit; the existing return loop resumes owned peers."""
        store = IntentStore(node)
        with store.locked():
            return self._fence(node, claim, record, store)

    def _fence(
        self, node: str, claim: Claim, record: Any, store: IntentStore
    ) -> list[str]:
        allowed = setting("FENCING_DOCKER_LOCAL_NODES").split(",")
        if node not in allowed or set(allowed) - NODES or node == claim.candidate:
            raise FencingError("Node is outside this executor's allowlist")
        labels = [f"{CLUSTER_LABEL}={setting('CLUSTER')}", f"{NODE_LABEL}={node}"]
        filters = urllib.parse.quote(json.dumps({"label": labels}))
        rows = self.call("GET", f"/containers/json?all=true&filters={filters}")
        if len(rows) != 1:
            raise FencingError("Expected exactly one owned PostgreSQL container")
        identifier = rows[0]["Id"]
        if not re.fullmatch(r"[a-f0-9]{64}", identifier):
            raise FencingError("Invalid Docker container identity")
        path = f"/containers/{identifier}"
        before = self.call("GET", path + "/json")
        actual_labels = before["Config"].get("Labels", {})
        if any(
            actual_labels.get(key) != value
            for key, value in ((CLUSTER_LABEL, setting("CLUSTER")), (NODE_LABEL, node))
        ):
            raise FencingError("Docker target ownership changed")
        if before["State"].get("Restarting"):
            raise FencingError("Container is restarting; termination is not confirmed")
        running = before["State"]["Running"]
        claim.verify()
        intent = store.load()
        active = intent is not None and intent["phase"] not in {
            "COMPLETED",
            "CANCELLED",
        }
        if active:
            assert intent is not None
            if intent["container_id"] != identifier or intent["cluster"] != setting(
                "CLUSTER"
            ):
                raise FencingError(
                    "Pending fencing target identity changed", "TARGET_IDENTITY_CHANGED"
                )
            if intent["started_at"] != before["State"]["StartedAt"]:
                if (
                    running
                    and intent["phase"] == "START_REQUESTED"
                    and before["State"].get("Pid", 0) > 0
                    and intent["restart_required"]
                    and intent["start_attempts"] > 0
                ):
                    claim.verify()
                    intent["phase"] = "COMPLETED"
                    store.save(intent)
                    record("restart_reconciled", [identifier])
                    if intent["claim"] == asdict(claim):
                        return [identifier]
                    # The old start succeeded, but its lease expired before
                    # completion was persisted. Never reuse its receipt for a
                    # new lease: fence the CURRENT process as a fresh intent.
                    active = False
                else:
                    raise FencingError(
                        "Pending fencing process incarnation changed",
                        "PROCESS_INCARNATION_CHANGED",
                    )
        if not active:
            intent = {
                "version": 1,
                "cluster": setting("CLUSTER"),
                "node": node,
                "container_id": identifier,
                "started_at": before["State"]["StartedAt"],
                "restart_required": running,
                "start_attempts": 0,
                "phase": "STOP_REQUESTED",
                "claim": asdict(claim),
            }
        assert intent is not None
        # Persist whether this exact incarnation was running BEFORE dispatch.
        # A stop timeout/crash must not turn our stopped peer into a manual stop.
        intent["claim"] = asdict(claim)
        store.save(intent)
        record("intent", [identifier])
        # Docker's stop operation escalates to SIGKILL after five seconds. It
        # does not reboot the host and suppresses unless-stopped auto-restart.
        if running:
            try:
                self.call("POST", path + "/stop?t=5")
            except (OSError, http.client.HTTPException):
                # Reconcile by inspection below; an unknown result is not failure
                # and never authorizes a start without positive exit evidence.
                pass
        after = self.call("GET", path + "/json")
        state = after["State"]
        if state.get("Running") or state.get("Restarting") or state.get("Pid") != 0:
            raise FencingError(
                "Docker did not confirm process termination", "TERMINATION_UNCONFIRMED"
            )
        record("terminated", [identifier])
        intent["phase"] = "STOP_CONFIRMED"
        store.save(intent)
        claim.verify()
        # A promotion receipt proves the OLD process exited, not that a new
        # Patroni process is already available. Leave durable return intent for
        # Returns under the same node/file locks and its existing DCS guards.
        # Return backoff/budget must neither delay the RPC nor be spent here.
        if not intent["restart_required"]:
            intent["phase"] = "COMPLETED"
            store.save(intent)
        return [identifier]


class Kubernetes:
    """Fence exact Pod UIDs using kubelet-confirmed terminal container status."""

    def open(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        token = (
            Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
            .read_text()
            .strip()
        )
        context = ssl.create_default_context(
            cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        )
        base = setting("FENCING_KUBERNETES_API").rstrip("/")
        if urllib.parse.urlsplit(base).scheme != "https":
            raise FencingError("Kubernetes API requires HTTPS")
        request = urllib.request.Request(
            base + path,
            method=method,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
        )
        # A redirect must never receive the projected service-account token.
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context), NoKubernetesRedirect()
        )
        return opener.open(request, timeout=timeout())

    @staticmethod
    def terminated(pod: dict[str, Any], uid: str) -> bool:
        """Do not accept deletion, NotReady, or a vanished Pod as termination."""
        status = pod.get("status", {})
        containers = status.get("containerStatuses", [])
        return (
            pod.get("metadata", {}).get("uid") == uid
            and status.get("phase") in {"Failed", "Succeeded"}
            and bool(containers)
            and len(containers) == len(pod.get("spec", {}).get("containers", []))
            and all("terminated" in row.get("state", {}) for row in containers)
            and all(
                "terminated" in row.get("state", {})
                for row in status.get("initContainerStatuses", [])
            )
        )

    def fence(self, node: str, claim: Claim, record: Any) -> list[str]:
        number = node.rsplit("-", 1)[1]
        name = setting(f"NODE_{number}_KUBERNETES_POD")
        namespace = setting("FENCING_KUBERNETES_NAMESPACE")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", name + namespace):
            raise FencingError("Invalid Kubernetes target")
        collection = f"/api/v1/namespaces/{namespace}/pods"
        with self.open("GET", collection + "/" + name) as response:
            pod = json.load(response)
        labels = pod["metadata"].get("labels", {})
        if (
            labels.get(CLUSTER_LABEL) != setting("CLUSTER")
            or labels.get(NODE_LABEL) != node
        ):
            raise FencingError("Kubernetes target is not owned by this cluster")
        uid = pod["metadata"]["uid"]
        resource_version = pod["metadata"]["resourceVersion"]
        claim.verify()
        record("intent", [uid])
        with self.open(
            "DELETE",
            collection + "/" + name,
            {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {"uid": uid},
                "gracePeriodSeconds": 15,
            },
        ) as response:
            response.read()
        query = urllib.parse.urlencode(
            {
                "watch": "true",
                "resourceVersion": resource_version,
                "fieldSelector": "metadata.name=" + name,
                "timeoutSeconds": max(1, int(timeout() - 1)),
            }
        )
        with self.open("GET", collection + "?" + query) as response:
            for line in response:
                event = json.loads(line)
                if event.get("type") == "ERROR":
                    raise FencingError("Kubernetes watch lost termination evidence")
                if self.terminated(event.get("object", {}), uid):
                    record("terminated", [uid])
                    claim.verify()
                    return [uid]
        raise FencingError("No kubelet-confirmed termination for the exact Pod UID")


class NoKubernetesRedirect(urllib.request.HTTPRedirectHandler):
    """Reject redirects without disclosing a Kubernetes bearer token."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def pre_promote() -> None:
    """Fence peer incarnations while Patroni owns its existing leader lease."""
    mechanism = mode()
    validate_runtime()
    if mechanism == "watchdog":
        raise FencingError("Watchdog mode must not invoke the external fencing hook")
    candidate = f"postgresql-{setting('NODE_ID')}"
    claim = Claim.current()
    if claim.candidate != candidate:
        raise FencingError("This member does not own Patroni's leader lease")
    request_id = str(uuid.uuid4())

    def peer(node: str) -> None:
        number = node.rsplit("-", 1)[1]
        endpoint = setting(f"NODE_{number}_FENCING_URL").rstrip("/")
        receipt = request_json(
            endpoint + "/fence",
            {
                **asdict(claim),
                "node": node,
                "request_id": request_id,
            },
            tls_context(),
        )
        expected = {
            **asdict(claim),
            "node": node,
            "request_id": request_id,
            "mechanism": mechanism,
        }
        if (
            any(receipt.get(key) != value for key, value in expected.items())
            or receipt.get("confirmed") is not True
            or not receipt.get("identities")
        ):
            raise FencingError("Fencing receipt is missing or mismatched")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(peer, node) for node in sorted(NODES - {candidate})]
        for future in futures:
            future.result()
    claim.verify()


def serve() -> None:
    """Expose a narrow mTLS executor, never a general Docker/Kubernetes proxy."""
    mechanism = mode()
    if mechanism == "watchdog":
        raise FencingError("Watchdog has no runtime-control executor")
    backend = Docker() if mechanism == "docker" else Kubernetes()
    locks = {node: threading.Lock() for node in NODES}
    journal = Path("/var/lib/aelev-fencing/audit.jsonl")
    journal_lock = threading.Lock()

    def return_record(
        node: str,
        intent: dict[str, Any],
        phase: str,
        identities: list[str],
        reason_code: str | None,
    ) -> None:
        row = {
            "node": node,
            "phase": phase,
            "identities": identities,
            "mechanism": mechanism,
            "start_attempts": intent["start_attempts"],
            "recorded_at": time.time(),
        }
        if "return_admission" in intent:
            row["return_admission"] = intent["return_admission"]
        if reason_code is not None:
            row["reason_code"] = reason_code
        with journal_lock, journal.open("a") as stream:
            stream.write(json.dumps(row) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    # Import only after this module is initialized; the return worker shares
    # the same native Claim and Docker adapter, not a second election protocol.
    from aelev_postgresql.fencing_return import Returns

    returns = (
        Returns(backend, locks, return_record) if isinstance(backend, Docker) else None
    )
    return_thread = (
        threading.Thread(target=returns.run, name="fencing-return", daemon=True)
        if returns is not None
        else None
    )

    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            # TLS negotiation and body reads are bounded in the request thread,
            # not the server's accept loop; a stalled peer cannot block health.
            self.request.settimeout(timeout())
            self.request.do_handshake()
            super().setup()

        def log_message(self, format: str, *args: Any) -> None:
            logging.info(
                "fencing_http status=%s", args[1] if len(args) > 1 else "unknown"
            )

        def respond(self, status: int, data: dict[str, Any]) -> None:
            body = json.dumps(data).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (OSError, http.client.HTTPException) as error:
                # Delivery uncertainty cannot undo or relabel a durable result.
                # Never expose exception text, request bodies or credentials.
                logging.warning(
                    "fencing_response_delivery_failed status=%s error_type=%s",
                    status,
                    type(error).__name__,
                )

        def authorized(self, *, mutate: bool = False) -> bool:
            certificate = self.connection.getpeercert()
            names = (
                {setting("ADMIN_USER")}
                if mutate
                else {setting("ADMIN_USER"), "postgresql-probe"}
            )
            return any(
                key == "commonName" and value in names
                for entry in certificate.get("subject", ())
                for key, value in entry
            )

        def do_GET(self) -> None:
            if self.path != "/health" or not self.authorized():
                self.respond(403, {"error": "denied"})
                return
            if return_thread is not None and not return_thread.is_alive():
                self.respond(503, {"error": "return_loop_stopped"})
                return
            self.respond(200, {"mechanism": mechanism})

        def do_POST(self) -> None:
            failure_record: Any = None
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if (
                    self.path != "/fence"
                    or not self.authorized(mutate=True)
                    or not 0 < length <= 2048
                ):
                    self.respond(403, {"error": "denied"})
                    return
                data = json.loads(self.rfile.read(length))
                if set(data) != {
                    "candidate",
                    "lease",
                    "revision",
                    "node",
                    "request_id",
                }:
                    raise FencingError("Invalid fencing request")
                node = data["node"]
                if (
                    node not in NODES
                    or data["candidate"] not in NODES
                    or node == data["candidate"]
                ):
                    raise FencingError("Invalid fencing target")
                uuid.UUID(data["request_id"])
                claim = Claim(data["candidate"], data["lease"], data["revision"])

                def record(
                    phase: str,
                    identities: list[str],
                    reason_code: str | None = None,
                ) -> None:
                    row = {
                        **data,
                        "phase": phase,
                        "identities": identities,
                        "mechanism": mechanism,
                        "recorded_at": time.time(),
                    }
                    if reason_code is not None:
                        row["reason_code"] = reason_code
                    with journal_lock, journal.open("a") as stream:
                        stream.write(json.dumps(row) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())

                failure_record = record
                with locks[node]:
                    claim.verify()
                    identities = backend.fence(node, claim, record)
                    claim.verify()
                    record("confirmed", identities)
                status = 200
                response = {
                    **data,
                    "mechanism": mechanism,
                    "confirmed": True,
                    "identities": identities,
                }
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                http.client.HTTPException,
            ) as error:
                reason_code = (
                    error.code
                    if isinstance(error, FencingError)
                    else "EXECUTOR_" + type(error).__name__.upper()
                )
                if failure_record is not None:
                    try:
                        failure_record("failed", [], reason_code)
                    except OSError:
                        logging.error("fencing_failure_audit_unavailable")
                logging.error(
                    "fencing_request failed error_type=%s reason_code=%s",
                    type(error).__name__,
                    reason_code,
                )
                status = 409
                response = {
                    "error": "termination_not_confirmed",
                    "reason_code": reason_code,
                }
            # Exactly one delivery attempt, outside the operation failure scope.
            self.respond(status, response)

    context = ssl.create_default_context(
        ssl.Purpose.CLIENT_AUTH, cafile=str(SECRETS / "ca.crt")
    )
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(SECRETS / "server.crt", SECRETS / "server.key")
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    server = ThreadingHTTPServer(("0.0.0.0", int(setting("FENCING_PORT"))), Handler)
    server.socket = context.wrap_socket(
        server.socket, server_side=True, do_handshake_on_connect=False
    )
    if return_thread is not None:
        return_thread.start()
    try:
        server.serve_forever()
    finally:
        if returns is not None and return_thread is not None:
            returns.stop.set()
            return_thread.join(timeout=1)


def health() -> None:
    """Check the executor's authenticated endpoint without arming a watchdog."""
    request_json(
        f"https://localhost:{setting('FENCING_PORT')}/health", None, tls_context()
    )
