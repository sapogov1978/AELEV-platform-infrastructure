#!/usr/bin/env python3
"""Render only the owner's Kubernetes executor, never consumer resources."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

PREFIX = "AELEV_INFRASTRUCTURE_POSTGRESQL_"


def render(values: dict[str, str]) -> dict[str, Any]:
    """Require explicit deployment settings and keep Kubernetes rights narrow."""

    def required(name: str) -> str:
        value = values.get(PREFIX + name, "")
        if not value:
            raise ValueError("Required configuration: " + PREFIX + name)
        return value

    if required("FENCING_MODE") != "kubernetes":
        raise ValueError("This manifest requires FENCING_MODE=kubernetes")
    namespace = required("FENCING_KUBERNETES_NAMESPACE")
    names = [required(f"NODE_{number}_KUBERNETES_POD") for number in range(1, 4)]
    if len(set(names)) != 3 or any(
        not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", name) for name in [namespace, *names]
    ):
        raise ValueError("Three distinct Pod names and a valid namespace are required")
    name = "postgresql-fencer"
    labels = {
        "app.kubernetes.io/name": name,
        "io.aelev.postgresql.cluster": required("CLUSTER"),
    }
    metadata = {"name": name, "namespace": namespace}
    suffixes = [
        "CLUSTER",
        "ADMIN_USER",
        "FENCING_MODE",
        "FENCING_TIMEOUT_SECONDS",
        "FENCING_PORT",
        "FENCING_KUBERNETES_API",
        "FENCING_KUBERNETES_NAMESPACE",
    ]
    for number in range(1, 4):
        suffixes.extend(
            [
                f"NODE_{number}_KUBERNETES_POD",
                f"NODE_{number}_ETCD_HOST",
                f"NODE_{number}_ETCD_CLIENT_PORT",
            ]
        )
    environment = [
        {"name": PREFIX + suffix, "value": required(suffix)} for suffix in suffixes
    ]
    port = int(required("FENCING_PORT"))
    items = [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": metadata},
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": metadata,
            "rules": [
                {
                    "apiGroups": [""],
                    "resources": ["pods"],
                    "resourceNames": names,
                    "verbs": ["get", "list", "watch", "delete"],
                }
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": metadata,
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": name,
            },
            "subjects": [
                {"kind": "ServiceAccount", "name": name, "namespace": namespace}
            ],
        },
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": metadata,
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": required("FENCING_KUBERNETES_STORAGE_CLASS"),
                "resources": {"requests": {"storage": "1Gi"}},
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": metadata,
            "spec": {
                "selector": labels,
                "ports": [{"name": "https", "port": port, "targetPort": port}],
            },
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": metadata,
            "spec": {
                "replicas": 1,
                "strategy": {"type": "Recreate"},
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "serviceAccountName": name,
                        "automountServiceAccountToken": True,
                        "containers": [
                            {
                                "name": name,
                                "image": required("IMAGE"),
                                "command": [
                                    "python",
                                    "-m",
                                    "aelev_postgresql",
                                    "fencing-serve",
                                ],
                                "env": environment,
                                "ports": [{"name": "https", "containerPort": port}],
                                "resources": {
                                    "requests": {
                                        "cpu": "50m",
                                        "memory": required("FENCING_MEMORY"),
                                    },
                                    "limits": {"memory": required("FENCING_MEMORY")},
                                },
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "readinessProbe": {
                                    "exec": {
                                        "command": [
                                            "python",
                                            "-m",
                                            "aelev_postgresql",
                                            "fencing-health",
                                        ]
                                    },
                                    "periodSeconds": 5,
                                    "timeoutSeconds": 5,
                                },
                                "volumeMounts": [
                                    {
                                        "name": "identity",
                                        "mountPath": "/run/aelev/secrets",
                                        "readOnly": True,
                                    },
                                    {
                                        "name": "audit",
                                        "mountPath": "/var/lib/aelev-fencing",
                                    },
                                    {"name": "tmp", "mountPath": "/tmp"},
                                ],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "identity",
                                "csi": {
                                    "driver": "secrets-store.csi.k8s.io",
                                    "readOnly": True,
                                    "volumeAttributes": {
                                        "secretProviderClass": required(
                                            "FENCING_KUBERNETES_SECRET_PROVIDER_CLASS"
                                        )
                                    },
                                },
                            },
                            {
                                "name": "audit",
                                "persistentVolumeClaim": {"claimName": name},
                            },
                            {
                                "name": "tmp",
                                "emptyDir": {"medium": "Memory", "sizeLimit": "8Mi"},
                            },
                        ],
                    },
                },
            },
        },
    ]
    return {"apiVersion": "v1", "kind": "List", "items": items}


def main() -> None:
    """Read explicit nonsecret env files; later overlays override earlier values."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, action="append", required=True)
    args = parser.parse_args()
    values: dict[str, str] = {}
    for path in args.env_file:
        for line in path.read_text().splitlines():
            if line and not line.startswith("#"):
                key, value = line.split("=", 1)
                if not key.startswith(PREFIX):
                    raise ValueError("Only PostgreSQL configuration is accepted")
                values[key] = value
    sys.stdout.write(json.dumps(render(values), indent=2) + "\n")


if __name__ == "__main__":
    main()
