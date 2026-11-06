"""Run via stdin in the candidate Linux image, network none and no working mounts."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

import httpx
import uvicorn

from translator_service import api, api_server, local_health
from translator_service.index_translation_provider import TranslationEngine, create_app

PROBE = "/opt/aelev/src/translator_service/local_health.py"


class Engine:
    translator_ref = "isolated-health-fixture-not-a-model-release"

    def __init__(self) -> None:
        self.loaded = False
        self.child = subprocess.Popen(
            [sys.executable, "-S", "-c", "import time; time.sleep(180)"],
            stdin=subprocess.DEVNULL,
        )

    @property
    def runtime_ready(self) -> bool:
        return self.loaded and self.child.poll() is None

    @property
    def runtime_pid(self) -> int | None:
        return self.child.pid if self.runtime_ready else None

    def close(self) -> None:
        if self.child.poll() is None:
            self.child.terminate()
        self.child.wait(timeout=5)


def probe(expected: int, times: list[float]) -> None:
    start = time.monotonic()
    result = subprocess.run([sys.executable, "-S", PROBE], timeout=5, check=False)
    times.append(round((time.monotonic() - start) * 1000, 3))
    assert result.returncode == expected, result.returncode


async def started(servers: tuple[api_server.Server, ...]) -> None:
    for _ in range(500):
        if all(server.started for server in servers):
            return
        await asyncio.sleep(0.02)
    raise RuntimeError("SANDBOX_LISTENERS_NOT_STARTED")


async def exercise() -> None:
    times: list[float] = []
    os.environ["AELEV_TRANSLATOR_HEALTH_ROLE"] = "model"
    fixture = Engine()
    model = api_server.Server(
        uvicorn.Config(
            create_app(cast(TranslationEngine, cast(object, fixture))),
            uds="/tmp/model.sock",
            access_log=False,
            log_level="error",
        )
    )
    model_task = asyncio.create_task(model.serve())
    try:
        await started((model,))
        probe(1, times)  # Parent is alive, but native model loading is incomplete.
        fixture.loaded = True
        await asyncio.sleep(1.1)
        for _ in range(8):
            probe(0, times)
            await asyncio.sleep(0.02)
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds="/tmp/model.sock"),
            base_url="http://translator",
            timeout=3,
        ) as client:
            assert (await client.get("/runtimez")).json()["translator_ref"] == (
                fixture.translator_ref
            )
            fixture.close()
            probe(1, times)  # Native death fails immediately, before marker expiry.
            assert (await client.get("/healthz")).json() == {"status": "ok"}
            assert (await client.get("/runtimez")).status_code == 503
        model.should_exit = True
        await model_task
        probe(1, times)
    finally:
        model.should_exit = True
        await asyncio.gather(model_task, return_exceptions=True)
        fixture.close()

    os.environ["AELEV_TRANSLATOR_HEALTH_ROLE"] = "api"

    def unavailable() -> None:
        raise RuntimeError("ISOLATED_DEPENDENCY_UNAVAILABLE")

    api.verify_kafka = unavailable
    api.read_status = unavailable  # type: ignore[assignment]
    servers = (
        api_server.Server(
            uvicorn.Config(api.app, uds="/tmp/api.sock", log_level="error")
        ),
        api_server.Server(
            uvicorn.Config(api.app, uds="/tmp/operator.sock", log_level="error")
        ),
    )
    serving = asyncio.create_task(api_server.serve(servers))
    try:
        await started(servers)
        await asyncio.sleep(1.1)
        for _ in range(8):
            probe(0, times)
            await asyncio.sleep(0.02)
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds="/tmp/api.sock"),
            base_url="http://translator",
            timeout=3,
        ) as client:
            assert (await client.get("/healthz")).json() == {"status": "ok"}
            assert (await client.get("/readyz")).status_code == 503
        # Intentional event-loop stall: a separate probe must not renew the signal.
        time.sleep(local_health.MAX_AGE + 1)
        probe(1, times)
        await asyncio.sleep(1.1)
        probe(0, times)
        servers[1].should_exit = True
        try:
            await serving
        except RuntimeError as error:
            assert str(error) == "TRANSLATOR_API_LISTENER_STOPPED"
        probe(1, times)
        assert all(server.should_exit for server in servers)
    finally:
        for server in servers:
            server.should_exit = True
        await asyncio.gather(serving, return_exceptions=True)
    assert not list(Path(local_health.ROOT).glob("*.signal"))
    print(
        json.dumps(
            {
                "status": "LOCAL_HEALTH_SANDBOX_PASSED",
                "probe_ms": times,
                "real_model_loaded": False,
                "working_data_accessed": False,
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(exercise())
