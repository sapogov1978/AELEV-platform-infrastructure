"""Run in the candidate image with no network, secrets or persistent mounts."""

from __future__ import annotations

import json
import os
import signal
import statistics
import subprocess
import sys
import time

PROBE = "/opt/aelev/src/translator_service/local_health.py"


def main() -> None:
    os.environ["AELEV_TRANSLATOR_HEALTH_ROLE"] = "worker"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from translator_service.health_heartbeat import progress; "
            "import time\nwhile True:\n progress('worker'); time.sleep(.2)",
        ]
    )
    timings: list[float] = []

    def probe(expected: int) -> None:
        started = time.monotonic()
        result = subprocess.run([sys.executable, "-S", PROBE], timeout=5, check=False)
        timings.append(1000 * (time.monotonic() - started))
        assert result.returncode == expected

    try:
        time.sleep(1)
        for _ in range(12):
            probe(0)
        child.send_signal(signal.SIGSTOP)
        time.sleep(31)
        probe(1)
        child.send_signal(signal.SIGCONT)
        time.sleep(1)
        probe(0)
        child.kill()
        child.wait(timeout=5)
        probe(1)
        sys.stdout.write(
            json.dumps(
                {
                    "healthy_stalled_resumed_dead": True,
                    "probes": len(timings),
                    "median_ms": round(statistics.median(timings), 3),
                    "max_ms": round(max(timings), 3),
                }
            )
            + "\n"
        )
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


if __name__ == "__main__":
    main()
