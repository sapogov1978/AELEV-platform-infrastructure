"""Owner read-only admission for one enrolled Docker restart, never leader election."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
from typing import Any

# Run with this owner's existing TLS material inside one surviving member.
PROBE = r"""
import base64, json, ssl, sys, urllib.request
from aelev_postgresql.__main__ import SECRETS, setting, tls_context
target = sys.argv[1]
def get(url, context, data=None):
    request = urllib.request.Request(url, data=None if data is None else json.dumps(data).encode(), headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request, context=context, timeout=3) as response:
        if response.status != 200:
            raise ValueError('NATIVE_NOT_READY')
        payload = response.read(65537)
        if len(payload) > 65536:
            raise ValueError('RESPONSE_TOO_LARGE')
        return json.loads(payload)
context = tls_context()
dcs = ssl.create_default_context(cafile=str(SECRETS / 'etcd-ca.crt'))
dcs.load_cert_chain(SECRETS / 'etcd-client.crt', SECRETS / 'etcd-client.key')
key = base64.b64encode(('/aelev-postgresql/' + setting('CLUSTER') + '/leader').encode()).decode()
leases = []
for node in (1, 2, 3):
    if target == 'etcd-' + str(node):
        continue
    try:
        url = 'https://' + setting('NODE_%d_ETCD_HOST' % node) + ':' + setting('NODE_%d_ETCD_CLIENT_PORT' % node)
        record = get(url + '/v3/kv/range', dcs, {'key':key, 'serializable':False})['kvs'][0]
        lease = (base64.b64decode(record['value']).decode(), record['lease'], record['create_revision'])
        if int(lease[1]) <= 0:
            raise ValueError('LEASE_MISSING')
        leases.append(lease)
    except (OSError, ValueError, KeyError, IndexError):
        pass
if len(leases) < 2 or len(set(leases)) != 1:
    raise ValueError('QUORUM_UNAVAILABLE')
leader = leases[0][0]
if target == leader:
    raise ValueError('PRIMARY_REQUIRES_SWITCHOVER')
healthy = 0
for node in (1, 2, 3):
    member = 'postgresql-' + str(node)
    if member == target:
        continue
    url = 'https://' + setting('NODE_%d_HOST' % node) + ':' + setting('NODE_%d_REST_PORT' % node)
    try:
        get(url + ('/primary' if member == leader else '/health'), context)
        healthy += 1
    except (OSError, ValueError):
        pass
if healthy < 2:
    raise ValueError('TWO_SQL_MEMBERS_REQUIRED')
if target != 'postgresql-fencer':
    get(setting('NODE_1_FENCING_URL') + '/health', context)
print(json.dumps({'allowed':True, 'code':'QUORUM_AND_FENCING_VERIFIED'}))
"""


def command(*args: str) -> bytes:
    """Keep credentials and native failure output inside this owner boundary."""
    result = subprocess.run(
        ["docker", *args], capture_output=True, timeout=40, check=False
    )
    if result.returncode:
        raise ValueError("NATIVE_CHECK_FAILED")
    return result.stdout


def check(identity: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise ValueError("EXACT_ID_REQUIRED")
    target = json.loads(command("inspect", identity))[0]
    labels = target["Config"]["Labels"]
    project, service = (
        labels["com.docker.compose." + key] for key in ("project", "service")
    )
    if not re.fullmatch(r"postgresql-[123]|etcd-[123]|postgresql-fencer", service):
        raise ValueError("FOREIGN_TARGET")
    # Select an already live peer, never start anything to satisfy a guard.
    for number in (1, 2, 3):
        member = "postgresql-" + str(number)
        if member == service:
            continue
        peers = (
            command(
                "ps",
                "-q",
                "--no-trunc",
                "--filter",
                "label=com.docker.compose.project=" + project,
                "--filter",
                "label=com.docker.compose.service=" + member,
            )
            .decode()
            .split()
        )
        if len(peers) == 1:
            safe = (
                "try:\n"
                + textwrap.indent(PROBE, "    ")
                + "\nexcept Exception as error:\n    code = str(error) if str(error) in ('PRIMARY_REQUIRES_SWITCHOVER','QUORUM_UNAVAILABLE','TWO_SQL_MEMBERS_REQUIRED') else 'NATIVE_QUORUM_OR_FENCING_UNCONFIRMED'\n    print(json.dumps({'allowed':False,'code':code}))\n"
            )
            result = command("exec", peers[0], "python", "-c", safe, service)
            answer: dict[str, Any] = json.loads(result)
            return answer
    raise ValueError("NO_SURVIVING_PEER")


if __name__ == "__main__":
    try:
        answer = check(sys.argv[1])
    except (OSError, ValueError, KeyError, IndexError, subprocess.TimeoutExpired):
        # No exception text, addresses or credentials cross the operator boundary.
        answer = {"allowed": False, "code": "NATIVE_QUORUM_OR_FENCING_UNCONFIRMED"}
    sys.stdout.write(json.dumps(answer) + "\n")
