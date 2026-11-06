"""Compare pinned models; optionally pause only explicitly selected compute roles."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any

ROOT = Path(__file__).resolve().parent
SERVICE = ROOT.parent.parent
CACHE = SERVICE / "runtime" / "model-comparison"
IMAGE = "aelev/translator-index-experiment:20261011"
DOWNLOAD_IMAGE = "aelev/translator-index-download:20261011"
LABEL = "org.aelev.translator.experiment"
OWNER = "index-translate-20261011"


def cache_directory() -> None:
    """Reserve only this experiment's directory; never claim existing user data."""
    marker = CACHE / ".experiment-owner"
    if CACHE.exists():
        if not marker.is_file() or marker.read_text().strip() != OWNER:
            raise RuntimeError("Experiment cache ownership mismatch")
    else:
        CACHE.mkdir(parents=True)
        marker.write_text(OWNER + "\n")


def docker(*args: str) -> str:
    return subprocess.check_output(["docker", *args], text=True).strip()


def historical_madlad_lock(image: str) -> dict[str, Any]:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise RuntimeError("MADLAD must be an immutable local image ID")
    value: dict[str, Any] = json.loads(
        docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--entrypoint",
            "python",
            image,
            "-c",
            "from pathlib import Path; "
            "print(Path('/opt/aelev/translator.lock.json').read_text())",
        )
    )
    if value.get("model") != "google/madlad400-3b-mt":
        raise RuntimeError("Historical comparison requires the MADLAD image")
    return value


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def verify(path: Path, spec: dict[str, Any]) -> bool:
    return (
        path.is_file()
        and path.stat().st_size == spec["size_bytes"]
        and digest(path) == spec["sha256"]
    )


def prepare(manifest: dict[str, Any]) -> None:
    subprocess.run(
        [
            "docker",
            "build",
            "--tag",
            DOWNLOAD_IMAGE,
            "--file",
            str(ROOT / "Downloader.Dockerfile"),
            str(ROOT),
        ],
        check=True,
    )
    for model in manifest["models"]:
        download(model)
    subprocess.run(
        [
            "docker",
            "build",
            "--tag",
            IMAGE,
            "--file",
            str(ROOT / "Dockerfile"),
            str(ROOT),
        ],
        check=True,
    )


def download(model: dict[str, Any]) -> None:
    """Use the official client, then publish only a complete verified artifact."""
    cache_directory()
    target = CACHE / model["file"]
    if verify(target, model):
        return
    if target.exists():
        raise RuntimeError("Refusing to overwrite an unverified artifact")
    stage = CACHE / f"hub-{model['id']}"
    stage.mkdir(exist_ok=True)
    image = json.loads(docker("image", "inspect", DOWNLOAD_IMAGE))[0]
    if image["Config"]["Labels"].get(LABEL) != OWNER:
        raise RuntimeError("Download image ownership mismatch")
    name = f"aelev-translator-download-{uuid.uuid4().hex[:12]}"
    docker(
        "create",
        "--name",
        name,
        "--label",
        f"{LABEL}={OWNER}",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--cpus",
        "2",
        "--memory",
        "1g",
        "--memory-swap",
        "1g",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        "--mount",
        f"type=bind,src={CACHE},dst=/downloads",
        image["Id"],
        model["repository"],
        model["file"],
        "--revision",
        model["revision"],
        "--local-dir",
        f"/downloads/hub-{model['id']}",
    )
    try:
        subprocess.run(["docker", "start", "--attach", name], check=True)
        state = json.loads(docker("inspect", name))[0]["State"]
        if state["ExitCode"] != 0 or state["OOMKilled"]:
            raise RuntimeError(f"Official download failed: {model['id']}")
    finally:
        remove_owned_container(name, OWNER)
    artifact = stage / model["file"]
    if not verify(artifact, model):
        raise RuntimeError(f"Artifact integrity failed: {model['id']}")
    artifact.rename(target)
    print(f"Verified SHA-256 {model['id']}", flush=True)


def inventory() -> dict[str, Any]:
    ids = docker("ps", "--all", "--quiet").splitlines()
    containers: list[dict[str, Any]] = (
        json.loads(docker("inspect", *ids)) if ids else []
    )
    return {
        "containers": {
            item["Id"]: {
                "name": item["Name"],
                "status": item["State"]["Status"],
                "started_at": item["State"]["StartedAt"],
                "restart_count": item["RestartCount"],
                "image": item["Image"],
            }
            for item in containers
        },
        "networks": sorted(
            docker("network", "ls", "--quiet", "--no-trunc").splitlines()
        ),
        "volumes": sorted(docker("volume", "ls", "--quiet").splitlines()),
    }


def remove_owned_container(name: str, run_id: str) -> None:
    details = json.loads(docker("inspect", name))[0]
    if details["Config"]["Labels"].get(LABEL) != run_id:
        raise RuntimeError(f"Cleanup ownership mismatch: {name}")
    if details["State"]["Running"]:
        docker("stop", "--time", "20", name)
    docker("rm", name)


def compute_state(container_id: str, project: str, role: str) -> dict[str, Any]:
    """Keep only non-secret evidence and verify the exact Compose owner."""
    item = json.loads(docker("inspect", container_id))[0]
    labels = item["Config"]["Labels"]
    if (
        item["Id"] != container_id
        or labels.get("com.docker.compose.project") != project
        or labels.get("com.docker.compose.service") != role
    ):
        raise RuntimeError("Compute container identity or ownership changed")
    return {
        "id": item["Id"],
        "name": item["Name"],
        "image": item["Image"],
        "running": item["State"]["Running"],
        "health": item["State"].get("Health", {}).get("Status"),
        "started_at": item["State"]["StartedAt"],
        "restart_count": item["RestartCount"],
        "mounts": sorted(mount["Source"] for mount in item["Mounts"]),
    }


@contextmanager
def released_compute(project: str, image: str) -> Generator[None]:
    """Restore existing roles even when benchmarking fails or is interrupted."""
    if not project:
        yield
        return
    manifest = json.loads((ROOT / "models.json").read_text())
    model = next(item for item in manifest["models"] if item["id"] == "index-9b")
    if not verify(CACHE / model["file"], model):
        raise RuntimeError("Verify the complete 9B artifact before pausing compute")
    roles = ("translator-model", "translator-worker")
    before: dict[str, dict[str, Any]] = {}
    for role in roles:
        ids = docker(
            "ps",
            "--all",
            "--quiet",
            "--no-trunc",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            f"label=com.docker.compose.service={role}",
        ).splitlines()
        if len(ids) != 1:
            raise RuntimeError(f"Select exactly one existing {role}")
        state = compute_state(ids[0], project, role)
        if not state["running"] or state["image"] != image:
            raise RuntimeError("Compute must be running the selected MADLAD image")
        if role == "translator-model" and state["health"] != "healthy":
            raise RuntimeError("The original model must be healthy before pausing")
        before[role] = state
    evidence: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "project": project,
        "before": before,
        "after": {},
        "restored": False,
    }
    path = ROOT / "maintenance-result.json"
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    try:
        for role in reversed(roles):
            state = compute_state(before[role]["id"], project, role)
            if state["image"] != image:
                raise RuntimeError("Compute image changed before pause")
            print(f"Pausing {role} for the authorized experiment", flush=True)
            docker("stop", "--timeout", "300", state["id"])
        yield
    finally:
        errors: list[str] = []
        for role in roles:
            try:
                state = compute_state(before[role]["id"], project, role)
                if state["image"] != image or state["mounts"] != before[role]["mounts"]:
                    raise RuntimeError(
                        "Compute image or mounts changed; refusing start"
                    )
                if not state["running"]:
                    print(f"Restoring {role}", flush=True)
                    docker("start", state["id"])
                deadline = time.monotonic() + 600
                while True:
                    state = compute_state(before[role]["id"], project, role)
                    if state["running"] and (
                        role != "translator-model" or state["health"] == "healthy"
                    ):
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Compute recovery timed out: {role}")
                    time.sleep(3)
                evidence["after"][role] = state
            except (RuntimeError, subprocess.SubprocessError, TimeoutError) as error:
                errors.append(f"{role}: {error}")
        evidence["errors"] = errors
        evidence["restored"] = not errors
        evidence["finished_at"] = datetime.now(UTC).isoformat()
        path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
        if errors:
            raise RuntimeError("Compute recovery failed; see maintenance-result.json")


def memory_bytes(value: str) -> int:
    match = re.fullmatch(r"([0-9.]+)\s*([A-Za-z]+)", value.strip())
    if match is None:
        raise RuntimeError(f"Unrecognized memory unit: {value}")
    factors = {
        "B": 1,
        "kB": 1000,
        "KiB": 2**10,
        "MB": 10**6,
        "MiB": 2**20,
        "GB": 10**9,
        "GiB": 2**30,
    }
    return int(float(match[1]) * factors[match[2]])


def model_memory_limit(model: dict[str, Any]) -> str:
    """Use one memory limit for both admission and Docker execution."""
    return "4g" if model["id"] == "madlad" else str(model["memory_limit"])


def resource_preflight(model: dict[str, Any]) -> dict[str, int]:
    limit = model_memory_limit(model)
    match_limit = re.fullmatch(r"([1-9][0-9]*)([mg])", limit)
    if match_limit is None:
        raise RuntimeError("Unsupported experiment memory limit")
    required = int(match_limit[1]) * (2**30 if match_limit[2] == "g" else 2**20)
    info = json.loads(docker("info", "--format", "{{json .}}"))
    stats = docker("stats", "--no-stream", "--format", "{{json .}}")
    used = sum(
        memory_bytes(json.loads(line)["MemUsage"].split("/")[0])
        for line in stats.splitlines()
    )
    meminfo = docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--entrypoint",
        "cat",
        IMAGE,
        "/proc/meminfo",
    )
    match = re.search(r"^MemAvailable:\s+(\d+) kB$", meminfo, re.MULTILINE)
    if match is None:
        raise RuntimeError("Docker VM available memory is unknown")
    available = int(match[1]) * 1024
    if (
        used + required + 512 * 2**20 > info["MemTotal"]
        or required + 512 * 2**20 > available
    ):
        raise RuntimeError(
            "Insufficient VM headroom; live services will not be stopped"
        )
    return {
        "vm_bytes": info["MemTotal"],
        "current_container_bytes": used,
        "vm_available_bytes": available,
        "model_limit_bytes": required,
        "reserved_headroom_bytes": 512 * 2**20,
    }


def benchmark(
    model: dict[str, Any], image: str, run_id: str, repeats: int
) -> dict[str, Any]:
    name = f"aelev-translator-experiment-{run_id}-{model['id']}"
    resources = resource_preflight(model)
    memory_limit = model_memory_limit(model)
    args = [
        "create",
        "--name",
        name,
        "--label",
        f"{LABEL}={run_id}",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--cpus",
        "2",
        "--memory",
        memory_limit,
        "--memory-swap",
        memory_limit,
        "--pids-limit",
        "128",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=32m",
        "--mount",
        f"type=bind,src={ROOT},dst=/experiment,readonly",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        "RAYON_NUM_THREADS=2",
        "--entrypoint",
        "python",
    ]
    if model["id"] == "madlad":
        kind, model_path = "madlad", "/opt/aelev/model"
        args.extend(
            [
                "--env",
                "AELEV_TRANSLATOR_MODEL_ROOT=/opt/aelev/model",
                "--env",
                "AELEV_TRANSLATOR_LOCK_PATH=/opt/aelev/translator.lock.json",
                "--env",
                "AELEV_TRANSLATOR_SOCKET_PATH=/tmp/unused.sock",
            ]
        )
    else:
        kind, model_path = "index", f"/weights/{model['file']}"
        args.extend(["--mount", f"type=bind,src={CACHE},dst=/weights,readonly"])
    args.extend(
        [
            image,
            "/experiment/worker.py",
            "--kind",
            kind,
            "--model",
            model_path,
            "--corpus",
            "/experiment/cases.json",
            "--repeats",
            str(repeats),
        ]
    )
    container_id = docker(*args)
    events: list[dict[str, Any]] = []
    log_path = ROOT / f"{model['id']}-engine.log"
    result: dict[str, Any] = {
        "model": model,
        "image": image,
        "container_id": container_id,
        "cpu_limit": 2,
        "memory_limit": memory_limit,
        "events": events,
        "resource_preflight": resources,
    }
    print(f"Running {model['id']} ({memory_limit}, 2 CPUs, network none)", flush=True)
    try:
        with log_path.open("w") as log:
            process = subprocess.Popen(
                ["docker", "start", "--attach", name],
                stdout=subprocess.PIPE,
                stderr=log,
                text=True,
                bufsize=1,
            )
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    event = json.loads(line)
                    events.append(event)
                    if event["event"] == "translation":
                        print(
                            f"{model['id']} {event['repetition']} {event['case_id']}: "
                            f"{event['seconds']:.2f}s; "
                            f"checks={event['literal_failures']}",
                            flush=True,
                        )
                    (ROOT / f"{model['id']}-result.json").write_text(
                        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                    )
                process.wait(timeout=30)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=30)
        state = json.loads(docker("inspect", name))[0]["State"]
        result["exit_state"] = {
            key: state[key] for key in ("ExitCode", "OOMKilled", "Error")
        }
        expected = len(json.loads((ROOT / "cases.json").read_text())) * repeats
        result["execution_pass"] = (
            state["ExitCode"] == 0
            and not state["OOMKilled"]
            and sum(event["event"] == "translation" for event in events) == expected
        )
        if not result["execution_pass"]:
            raise RuntimeError(f"{model['id']} incomplete; see {log_path.name}")
        return result
    finally:
        remove_owned_container(name, run_id)
        (ROOT / f"{model['id']}-result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        )


def report(results: list[dict[str, Any]], evidence: dict[str, Any]) -> None:
    lines = [
        "# Сравнение MADLAD и Index-Translate",
        "",
        f"Время: {evidence['started_at']}",
        "",
        "Операторский эксперимент, не смена рабочей модели. "
        "CPU-only, 2 CPU на вычислитель, одна модель эксперимента одновременно; "
        "рабочая модель не заменяется. Разрешённые временные остановки "
        "вычислителя и восстановление фиксируются отдельно в maintenance-result.json.",
        "",
        "| Модель | Переводы | p50, с | p95, с | "
        "Пиковая cgroup-память, ГиБ | Нарушения точных условий |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    if len(results) < 3:
        lines[4:4] = [
            "НЕПОЛНЫЙ ПРОГОН: выполнены только перечисленные модели. "
            "Отсутствие 9B в таблице означает, что он не проверен.",
            "",
        ]
    for result in results:
        events = [
            event for event in result["events"] if event["event"] == "translation"
        ]
        durations = sorted(event["seconds"] for event in events)
        peak = max(
            event["memory"].get("cgroup_peak_bytes", 0) for event in result["events"]
        )
        p95 = durations[math.ceil(len(durations) * 0.95) - 1]
        lines.append(
            f"| {result['model']['id']} | {len(events)} "
            f"| {statistics.median(durations):.2f} | {p95:.2f} | {peak / 2**30:.2f} "
            f"| {sum(bool(e['literal_failures']) for e in events)} |"
        )
    if evidence.get("pause_compute_project"):
        lines.extend(
            [
                "",
                "9B измерялся с разрешённой остановкой рабочего model/worker; "
                "результаты MADLAD/2B сохранены из предыдущего запуска без этой "
                "остановки. Лимит CPU одинаков, но фоновая нагрузка не идентична.",
                "",
            ]
        )
    lines.extend(
        [
            "",
            "Память cgroup включает процесс и файловый кэш, не только веса. "
            "p95 — ближайший верхний ранг небольшой выборки, не нагрузочный SLO. "
            "Точные условия проверяют заданные идентификаторы, числа и добавление "
            "страны, а не правильность всего смысла. Q4_K и Q4_K_M, "
            "архитектуры и токенизаторы различаются.",
            "",
            "## Исходные тексты и полученные переводы",
            "",
        ]
    )
    cases = json.loads((ROOT / "cases.json").read_text())
    for case in cases:
        lines.extend(
            [
                f"### {case['id']} ({case['source']} → {case['target']})",
                "",
                "```text",
                case["text"],
                "```",
                "",
                f"Что проверять: {case['meaning']}",
                "",
            ]
        )
        for result in results:
            matching = [
                event
                for event in result["events"]
                if event.get("case_id") == case["id"]
            ]
            lines.extend(
                [
                    f"{result['model']['id']} ({matching[0]['seconds']:.2f} с):",
                    "",
                    "```text",
                    matching[0]["translated_text"],
                    "```",
                    "",
                ]
            )
            if len({event["translated_text"] for event in matching}) > 1:
                lines.extend(
                    ["Повтор дал другие байты; варианты сохранены в JSON.", ""]
                )
    lines.extend(
        [
            "## Воспроизводимость и очистка",
            "",
            "Опись Docker на границах вычислительного этапа совпала: "
            f"`{evidence.get('baseline_preserved', False)}`. "
            "Временные контейнеры удалены; сценарий не создавал сети, тома, "
            "задания Kafka или записи рабочих БД. При разрешённой остановке "
            "эта сверка начинается уже после паузы: исходное состояние и "
            "восстановление вычислителя подтверждаются отдельно в "
            "maintenance-result.json.",
            "",
            "Версии, SHA-256, параметры запуска, сырые ответы "
            "и обе описи сохранены в JSON рядом. Загрузка весов, проверка SHA-256, "
            "запуск и прогрев не входят в задержку отдельных переводов.",
            "",
        ]
    )
    (ROOT / "result.md").write_text("\n".join(lines))


def run(args: argparse.Namespace, manifest: dict[str, Any]) -> None:
    madlad_lock = historical_madlad_lock(args.madlad_image)
    engine = json.loads(docker("image", "inspect", IMAGE))[0]
    if engine["Config"]["Labels"].get(LABEL) != OWNER:
        raise RuntimeError("Experiment image ownership mismatch")
    if (
        engine["Config"]["Labels"].get("org.opencontainers.image.revision")
        != manifest["engine"]["revision"]
    ):
        raise RuntimeError("Experiment engine revision mismatch")
    before = inventory()
    run_id = uuid.uuid4().hex[:12]
    info = json.loads(docker("info", "--format", "{{json .}}"))
    evidence: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "manifest": manifest,
        "corpus_sha256": digest(ROOT / "cases.json"),
        "madlad_lock": madlad_lock,
        "docker": {
            key: info[key]
            for key in (
                "Architecture",
                "MemTotal",
                "NCPU",
                "ServerVersion",
                "KernelVersion",
            )
        },
        "before": before,
        "results": [],
        "pause_compute_project": args.pause_compute_project or None,
    }
    completed: set[str] = set()
    if args.resume:
        previous = json.loads((ROOT / "result.json").read_text())
        if (
            previous["manifest"] != manifest
            or previous["corpus_sha256"] != evidence["corpus_sha256"]
            or previous["madlad_lock"] != evidence["madlad_lock"]
        ):
            raise RuntimeError("Previous experiment inputs do not match")
        for result in previous["results"]:
            expected_image = (
                args.madlad_image if result["model"]["id"] == "madlad" else engine["Id"]
            )
            translations = [
                event for event in result["events"] if event["event"] == "translation"
            ]
            expected = len(json.loads((ROOT / "cases.json").read_text())) * args.repeats
            if (
                not result.get("execution_pass")
                or result["image"] != expected_image
                or len(translations) != expected
            ):
                raise RuntimeError("Previous model run cannot be reused")
            completed.add(result["model"]["id"])
            evidence["results"].append(result)
        evidence["previous_run"] = previous
    try:
        for model in [{"id": "madlad"}, *manifest["models"]]:
            if model["id"] in completed:
                continue
            if model["id"] != "madlad":
                deadline = time.monotonic() + 8 * 3600
                path = CACHE / model["file"]
                while args.wait_for_artifacts and not path.exists():
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Artifact wait exceeded: {model['id']}")
                    print(f"Waiting for verified artifact: {model['id']}", flush=True)
                    time.sleep(30)
                if not verify(path, model):
                    raise RuntimeError(f"Missing or invalid artifact: {model['id']}")
            image = args.madlad_image if model["id"] == "madlad" else engine["Id"]
            evidence["results"].append(benchmark(model, image, run_id, args.repeats))
    finally:
        evidence["after"] = inventory()
        evidence["baseline_preserved"] = evidence["after"] == before
        evidence["finished_at"] = datetime.now(UTC).isoformat()
        (ROOT / "result.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
        )
        if evidence["results"]:
            report(evidence["results"], evidence)
    if not evidence["baseline_preserved"]:
        raise RuntimeError("Docker baseline changed during the experiment")


def terminate(_signum: int, _frame: FrameType | None) -> None:
    sys.exit(143)


def clean() -> None:
    """Remove only positively owned artifacts, never Docker's shared build cache."""
    containers = docker("ps", "--all", "--quiet", "--filter", f"label={LABEL}")
    if containers:
        raise RuntimeError("Finish the experiment containers before cleanup")
    if CACHE.exists():
        cache_directory()
        if (
            CACHE.is_symlink()
            or CACHE.resolve() != SERVICE / "runtime" / "model-comparison"
        ):
            raise RuntimeError("Unsafe experiment cache target")
        shutil.rmtree(CACHE)
    for image in (IMAGE, DOWNLOAD_IMAGE):
        if not docker("image", "ls", "--quiet", image):
            continue
        details = json.loads(docker("image", "inspect", image))[0]
        if details["Config"]["Labels"].get(LABEL) != OWNER:
            raise RuntimeError("Refusing to remove an unrelated image")
        docker("image", "rm", image)
    print(
        "Owned experiment weights/cache/images removed; "
        "reports and live stack preserved."
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "download", "run", "clean"))
    parser.add_argument("--model", choices=("index-2b", "index-9b"))
    parser.add_argument("--madlad-image", default="")
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=2)
    parser.add_argument("--wait-for-artifacts", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--pause-compute-project", default="")
    args = parser.parse_args()
    manifest = json.loads((ROOT / "models.json").read_text())
    signal.signal(signal.SIGTERM, terminate)
    if args.phase == "prepare":
        prepare(manifest)
    elif args.phase == "download":
        if not args.model:
            parser.error("download requires --model")
        download(
            next(model for model in manifest["models"] if model["id"] == args.model)
        )
    elif args.phase == "run":
        with released_compute(args.pause_compute_project, args.madlad_image):
            run(args, manifest)
    else:
        clean()


if __name__ == "__main__":
    main()
