"""Offline, sequential inference inside an experiment-only container."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import selectors
import subprocess
import sys
import time
import urllib.error
import urllib.request
from importlib import import_module
from pathlib import Path
from typing import Any

LANGUAGE_NAMES = {
    "en": "英语",
    "ru": "俄语",
    "de": "德语",
    "fr": "法语",
    "es": "西班牙语",
    "zh": "中文",
}


def emit(document: dict[str, Any]) -> None:
    print(json.dumps(document, ensure_ascii=False), flush=True)


def memory(pid: int | None = None) -> dict[str, int]:
    result: dict[str, int] = {}
    for name in ("current", "peak"):
        path = Path(f"/sys/fs/cgroup/memory.{name}")
        if path.exists():
            result[f"cgroup_{name}_bytes"] = int(path.read_text())
    if pid is not None:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith(("VmRSS:", "VmHWM:")):
                key = (
                    "process_rss_bytes"
                    if line.startswith("VmRSS:")
                    else "process_peak_rss_bytes"
                )
                result[key] = int(line.split()[1]) * 1024
    return result


def read_line(process: subprocess.Popen[str], timeout: float) -> dict[str, Any]:
    assert process.stdout is not None
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        if not selector.select(timeout):
            raise TimeoutError("Model response deadline exceeded")
        line = process.stdout.readline()
    if not line:
        raise RuntimeError(f"Model process exited: {process.poll()}")
    return json.loads(line)


def request(path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:8080{path}",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=240) as response:
        return json.load(response)


def index_prompt(case: dict[str, Any]) -> str:
    return (
        f"请将以下{LANGUAGE_NAMES[case['source']]}文本翻译为"
        f"{LANGUAGE_NAMES[case['target']]}，直接输出翻译结果，不要进行任何解释。"
        f"\n\n{case['text'].strip()}"
    )


def literal_checks(case: dict[str, Any], translated: str) -> list[str]:
    checks = case.get("checks", {})
    failures = [
        f"missing:{value}"
        for value in checks.get("required", [])
        if value not in translated
    ]
    failures.extend(
        f"added:{value}"
        for value in checks.get("forbidden", [])
        if value.casefold() in translated.casefold()
    )
    if not translated.strip():
        failures.append("empty_output")
    return failures


def translate(
    kind: str,
    process: subprocess.Popen[str],
    case: dict[str, Any],
) -> dict[str, Any]:
    if kind == "madlad":
        # Match TRANSLATE_TEXT's paragraph split, target token and 192-token cap.
        # Do not use the provider result cache: every repetition must compute.
        assert process.stdin is not None
        translations: list[str] = []
        raw: list[dict[str, Any]] = []
        for paragraph in case["text"].splitlines():
            if not paragraph.strip():
                continue
            process.stdin.write(
                json.dumps(
                    {
                        "texts": [f"<2{case['target']}> {paragraph.strip()}"],
                        "max_output_tokens": 192,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            process.stdin.flush()
            response = read_line(process, 180)
            raw.append(response)
            if "error" in response:
                raise RuntimeError(response["error"])
            translations.extend(response["translations"])
        return {"translated_text": "\n".join(translations), "raw_response": raw}
    response = request(
        "/v1/chat/completions",
        {
            "model": "index-translate",
            "messages": [
                {"role": "user", "content": index_prompt(case)},
            ],
            "temperature": 0,
            "seed": 299792458,
            "max_tokens": 192,
            "cache_prompt": False,
            "chat_template_kwargs": {"enable_thinking": False},
        },
    )
    choice = response["choices"][0]
    return {
        "translated_text": choice["message"]["content"],
        "finish_reason": choice["finish_reason"],
        "usage": response.get("usage"),
        "timings": response.get("timings"),
        "raw_response": response,
    }


def run(kind: str, model: Path, corpus: Path, repeats: int) -> None:
    cases = json.loads(corpus.read_text())
    if kind == "madlad":
        # This branch runs only inside the historical immutable MADLAD image.
        historical = import_module("translator_service.madlad_translation_provider")
        historical.verify_model_artifacts(
            historical.MadladTranslationSettings.from_environment()
        )
        command = ["/usr/local/bin/aelev-madlad-runtime", str(model)]
    else:
        manifest = json.loads((corpus.parent / "models.json").read_text())
        spec = next(item for item in manifest["models"] if item["file"] == model.name)
        checksum = hashlib.sha256()
        with model.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                checksum.update(chunk)
        if (
            model.stat().st_size != spec["size_bytes"]
            or checksum.hexdigest() != spec["sha256"]
        ):
            raise RuntimeError("Index model integrity check failed")
        command = [
            "/opt/llama/llama-server",
            "--model",
            str(model),
            "--host",
            "127.0.0.1",
            "--port",
            "8080",
            "--alias",
            "index-translate",
            "--threads",
            "2",
            "--threads-batch",
            "2",
            "--ctx-size",
            "2048",
            "--parallel",
            "1",
            "--cache-ram",
            "0",
            "--fit",
            "off",
            "--n-gpu-layers",
            "0",
            "--jinja",
            "--chat-template-kwargs",
            '{"enable_thinking":false}',
            "--no-webui",
            "--no-warmup",
        ]
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE if kind == "madlad" else subprocess.DEVNULL,
        stderr=sys.stderr,
        text=True,
        bufsize=1,
        env={**os.environ, "RAYON_NUM_THREADS": "2"},
    )
    try:
        if kind == "madlad":
            if read_line(process, 240) != {"status": "ready"}:
                raise RuntimeError("MADLAD readiness handshake failed")
        else:
            deadline = started + 240
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f"llama-server failed: {process.returncode}")
                try:
                    if request("/health").get("status") == "ok":
                        break
                except (urllib.error.URLError, TimeoutError):
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError("llama-server readiness deadline exceeded")
                time.sleep(1)
        emit(
            {
                "event": "ready",
                "seconds": time.monotonic() - started,
                "command": command,
                "python": sys.version,
                "memory": memory(process.pid),
            }
        )
        warmup = {"source": "en", "target": "ru", "text": "The document is ready."}
        started = time.monotonic()
        emit(
            {
                "event": "warmup",
                "seconds": None,
                "result": translate(kind, process, warmup),
                "elapsed_seconds": time.monotonic() - started,
                "memory": memory(process.pid),
            }
        )
        for repetition in range(1, repeats + 1):
            for case in cases:
                started = time.monotonic()
                result = translate(kind, process, case)
                failures = literal_checks(case, result["translated_text"])
                if result.get("finish_reason") == "length":
                    failures.append("generation_length_limit")
                emit(
                    {
                        "event": "translation",
                        "case_id": case["id"],
                        "repetition": repetition,
                        "seconds": time.monotonic() - started,
                        **result,
                        "literal_failures": failures,
                        "memory": memory(process.pid),
                    }
                )
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=("madlad", "index"), required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args()
    run(args.kind, args.model, args.corpus, args.repeats)
