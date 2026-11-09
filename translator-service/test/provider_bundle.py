#!/usr/bin/env python3
"""Verify exact provider/worker images over the opaque UDS without inference."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

DIGEST_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")


def run(
    arguments: list[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-image", required=True)
    parser.add_argument("--worker-image", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    for reference in (args.provider_image, args.worker_image):
        if DIGEST_IMAGE.fullmatch(reference) is None:
            raise SystemExit("PROVIDER_BUNDLE_PINNED_IMAGE_REQUIRED")

    token = uuid.uuid4().hex[:12]
    provider = f"aelev-provider-bundle-{token}"
    volume = f"aelev-provider-bundle-{token}"
    started = False
    result: dict[str, object] = {
        "schema_version": 1,
        "provider_image": args.provider_image,
        "worker_image": args.worker_image,
        "inference_requested": False,
        "provider_network": "none",
    }
    try:
        run(["docker", "volume", "create", volume])
        with tempfile.TemporaryDirectory(prefix="aelev-provider-credential-") as temp:
            credential = Path(temp) / "credential.env"
            credential.write_text(
                "OPENAI_API_KEY=fixture-openai-key-not-secret\n", encoding="utf-8"
            )
            credential.chmod(0o640)
            run(
                [
                    "docker",
                    "run",
                    "--detach",
                    "--name",
                    provider,
                    "--network",
                    "none",
                    "--read-only",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,size=64m,mode=1777",
                    "--mount",
                    f"type=volume,src={volume},dst=/run/aelev-translator",
                    "--mount",
                    "type=bind,src="
                    + str(credential.resolve())
                    + ",dst=/run/secrets/provider/credential.env,readonly",
                    "--group-add",
                    str(os.getgid()),
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    args.provider_image,
                ]
            )
            started = True
            deadline = time.monotonic() + 60
            health = "starting"
            while time.monotonic() < deadline:
                inspected = run(
                    [
                        "docker",
                        "inspect",
                        "--format",
                        "{{.State.Status}} {{.State.Health.Status}}",
                        provider,
                    ]
                ).stdout.strip()
                status, _, health = inspected.partition(" ")
                if status != "running":
                    raise RuntimeError("PROVIDER_BUNDLE_PROVIDER_STOPPED")
                if health == "healthy":
                    break
                time.sleep(1)
            if health != "healthy":
                raise RuntimeError("PROVIDER_BUNDLE_HEALTH_TIMEOUT")
            probe = run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,size=16m,mode=1777",
                    "--mount",
                    f"type=volume,src={volume},dst=/run/aelev-translator,readonly",
                    "--env",
                    "AELEV_TRANSLATOR_SOCKET_PATH=/run/aelev-translator/translator.sock",
                    "--entrypoint",
                    "python",
                    args.worker_image,
                    "-m",
                    "translator_service.provider_healthcheck",
                ]
            )
            result.update(
                {
                    "provider_health": health,
                    "worker_uds_probe": "PASS" if not probe.stdout.strip() else "PASS",
                    "status": "PASS",
                }
            )
    finally:
        if started:
            run(["docker", "stop", "--timeout", "5", provider], check=False)
            run(["docker", "rm", provider], check=False)
        run(["docker", "volume", "rm", volume], check=False)
    leftovers = run(
        [
            "docker",
            "ps",
            "--all",
            "--filter",
            f"name=^{provider}$",
            "--format",
            "{{.Names}}",
        ]
    ).stdout.strip()
    if leftovers:
        raise RuntimeError("PROVIDER_BUNDLE_CLEANUP_FAILED")
    result["cleanup_preserved"] = True
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": result.get("status"), "report": str(args.report)}))
    return 0 if result.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
