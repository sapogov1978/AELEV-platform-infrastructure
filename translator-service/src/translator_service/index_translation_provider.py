"""Selected offline Index-Translate provider behind the owner's Unix socket."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import stat
import subprocess
import threading
import time
from collections import OrderedDict, deque
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, cast

import httpx
import uvicorn
from fastapi import Body, FastAPI, HTTPException

from translator_service import health_heartbeat
from translator_service.index_languages import language_name
from translator_service.protocol import (
    EvidenceTranslationRequest,
    ProfileLocalizationRequest,
    TranslatorConfigurationError,
    parse_evidence_translation_request,
    parse_profile_localization_request,
)
from translator_service.retry import retry_code

_MODEL_FILE = "Index-Translate-9B.Q4_K_M.gguf"
_NATIVE_ORIGIN = "http://127.0.0.1:8080"
_FIELDS = ("subject_area", "positive_terms", "excluded_terms", "excluded_topics")
LOGGER = logging.getLogger(__name__)


class TranslatorRuntimeRequestError(RuntimeError):
    pass


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError("translator metadata is invalid")
    return cast(dict[str, Any], value)


def translator_reference(model_digest: str, runtime: dict[str, Any]) -> str:
    """Pin the weights and complete immutable inference profile together."""
    profile = json.dumps(
        {"model_sha256": model_digest, "runtime": runtime},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return (
        "IndexTeam/Index-Translate-9B-q4km@sha256:"
        + hashlib.sha256(profile).hexdigest()
    )


def read_translator_lock(path: Path) -> tuple[str, dict[str, tuple[str, int]]]:
    """Validate the selected model manifest without downloading artifacts."""
    value = _mapping(json.loads(path.read_text(encoding="utf-8")))
    if (
        value.get("schema_version") != "1.0.0"
        or value.get("status") != "SELECTED_PRODUCTION"
        or value.get("model") != "IndexTeam/Index-Translate-9B-GGUF"
        or value.get("architecture") != "qwen35"
        or value.get("license") != "Apache-2.0"
        or not isinstance(value.get("files"), list)
        or len(value["files"]) != 1
    ):
        raise RuntimeError("translator lock metadata is invalid")
    artifact = _mapping(value["files"][0])
    if (
        artifact.get("path") != _MODEL_FILE
        or not isinstance(artifact.get("size"), int)
        or isinstance(artifact["size"], bool)
        or artifact["size"] <= 0
        or not isinstance(artifact.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])
    ):
        raise RuntimeError("translator artifact metadata is invalid")
    runtime = _mapping(value.get("runtime"))
    if (
        runtime.get("repository") != "ggml-org/llama.cpp"
        or runtime.get("version") != "v0.6.0"
        or runtime.get("profile") != "index-plain-v1"
        or not re.fullmatch(r"[0-9a-f]{40}", str(runtime.get("revision", "")))
        or not re.fullmatch(r"[0-9a-f]{64}", str(runtime.get("source_sha256", "")))
        or runtime.get("enable_thinking") is not False
        or runtime.get("cache_prompt") is not False
        or type(runtime.get("temperature")) not in {int, float}
        or runtime.get("temperature") != 0
    ):
        raise RuntimeError("translator runtime metadata is invalid")
    for key, low, high in (
        ("threads", 1, 128),
        ("context_size", 2048, 32768),
        ("output_tokens", 192, 4096),
        ("seed", 0, 2**32 - 1),
    ):
        setting = runtime.get(key)
        if type(setting) is not int or not low <= setting <= high:
            raise RuntimeError("translator runtime parameter is invalid")
    if runtime["output_tokens"] + 64 >= runtime["context_size"]:
        raise RuntimeError("translator runtime context is invalid")
    if value.get("translator_ref") != translator_reference(artifact["sha256"], runtime):
        raise RuntimeError("translator execution profile failed verification")
    return value["translator_ref"], {
        _MODEL_FILE: (artifact["sha256"], artifact["size"])
    }


@dataclass(frozen=True, slots=True)
class TranslationSettings:
    model_root: Path
    lock_path: Path
    socket_path: Path
    translator_ref: str
    artifacts: dict[str, tuple[str, int]]
    batch_size: int
    request_timeout_seconds: float = 240
    segment_timeout_seconds: float = 180
    startup_timeout_seconds: float = 600

    @classmethod
    def from_environment(cls) -> TranslationSettings:
        lock_path = Path(os.environ["AELEV_TRANSLATOR_LOCK_PATH"])
        translator_ref, artifacts = read_translator_lock(lock_path)
        batch_size = int(os.environ["AELEV_TRANSLATOR_BATCH_SIZE"])
        request_timeout = float(os.environ["AELEV_TRANSLATOR_REQUEST_TIMEOUT_SECONDS"])
        segment_timeout = float(os.environ["AELEV_TRANSLATOR_SEGMENT_TIMEOUT_SECONDS"])
        startup_timeout = float(
            os.environ.get("AELEV_TRANSLATOR_STARTUP_TIMEOUT_SECONDS", "600")
        )
        if not 1 <= batch_size <= 32:
            raise RuntimeError("translator batch size must be between 1 and 32")
        if not 0 < segment_timeout <= request_timeout <= 240:
            raise RuntimeError(
                "translator deadlines must satisfy 0 < segment <= request <= 240"
            )
        if not 0 < startup_timeout <= 1800:
            raise RuntimeError(
                "translator startup deadline must be within 1800 seconds"
            )
        return cls(
            Path(os.environ["AELEV_TRANSLATOR_MODEL_ROOT"]),
            lock_path,
            Path(os.environ["AELEV_TRANSLATOR_SOCKET_PATH"]),
            translator_ref,
            artifacts,
            batch_size,
            request_timeout,
            segment_timeout,
            startup_timeout,
        )


def verify_model_artifacts(settings: TranslationSettings) -> None:
    selected = read_translator_lock(settings.lock_path)
    if selected != (settings.translator_ref, settings.artifacts):
        raise RuntimeError("translator lock changed after startup")
    for name, (expected, size) in settings.artifacts.items():
        path = settings.model_root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
            raise RuntimeError("translator model artifact has an invalid size or type")
        checksum = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                checksum.update(chunk)
        if checksum.hexdigest() != expected:
            raise RuntimeError("translator model artifact failed verification")


def translation_prompt(text: str, source: str, target: str) -> str:
    """Use the same plain prompt as the approved comparison, without constraints."""
    return (
        f"请将以下{source}文本翻译为{target}，直接输出翻译结果，不要进行任何解释。"
        f"\n\n{text.strip()}"
    )


class TranslationEngine:
    def __init__(self, settings: TranslationSettings) -> None:
        verify_model_artifacts(settings)
        self._settings = settings
        self._profile: dict[str, Any] = json.loads(settings.lock_path.read_text())[
            "runtime"
        ]
        self._binary = os.environ.get(
            "AELEV_TRANSLATOR_RUNTIME_PATH", "/opt/llama/llama-server"
        )
        self._process: subprocess.Popen[bytes] | None = None
        self._stop_requested = False
        self._runtime_loaded = False
        self._condition = threading.Condition()
        self._tickets: deque[object] = deque()
        self._cache: OrderedDict[str, list[str]] = OrderedDict()
        self._client = httpx.Client(base_url=_NATIVE_ORIGIN, trust_env=False)
        try:
            self._start_runtime()
        except Exception:
            self._client.close()
            raise

    @property
    def translator_ref(self) -> str:
        return self._settings.translator_ref

    @property
    def runtime_ready(self) -> bool:
        return (
            self._runtime_loaded
            and self._process is not None
            and self._process.poll() is None
        )

    @property
    def runtime_pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None and self.runtime_ready else None

    def _start_runtime(self, timeout_seconds: float | None = None) -> None:
        self._runtime_loaded = False
        if not self._stop_runtime():
            LOGGER.warning("translator_native_stop_pending")
            raise RuntimeError("TRANSLATOR_NATIVE_NOT_READY")
        duration = self._settings.startup_timeout_seconds
        if timeout_seconds is not None:
            duration = min(duration, timeout_seconds)
        if duration <= 0:
            raise TimeoutError("TRANSLATOR_DEADLINE_EXCEEDED")
        deadline = time.monotonic() + duration
        profile = self._profile
        try:
            version = subprocess.run(
                [self._binary, "--version"],
                capture_output=True,
                text=True,
                check=True,
                timeout=min(10, duration),
            )
        except subprocess.TimeoutExpired as error:
            LOGGER.warning("translator_native_startup_timeout")
            raise RuntimeError("TRANSLATOR_NATIVE_NOT_READY") from error
        except (OSError, subprocess.CalledProcessError) as error:
            raise RuntimeError("TRANSLATOR_NATIVE_UNAVAILABLE") from error
        if profile["revision"] not in version.stdout + version.stderr:
            raise RuntimeError("TRANSLATOR_NATIVE_VERSION_MISMATCH")
        try:
            self._process = subprocess.Popen(
                [
                    self._binary,
                    "--model",
                    str(self._settings.model_root / _MODEL_FILE),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8080",
                    "--alias",
                    "index-translate",
                    "--threads",
                    str(profile["threads"]),
                    "--threads-batch",
                    str(profile["threads"]),
                    "--ctx-size",
                    str(profile["context_size"]),
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
                    "--log-disable",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            raise RuntimeError("TRANSLATOR_NATIVE_UNAVAILABLE") from error
        try:
            while self._process.poll() is None and time.monotonic() < deadline:
                try:
                    ready = self._client.get(
                        "/health",
                        timeout=min(1, max(0.01, deadline - time.monotonic())),
                    )
                    if ready.status_code == 200 and ready.json() == {"status": "ok"}:
                        self._runtime_loaded = True
                        return
                except (httpx.HTTPError, ValueError):
                    pass
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            if self._process.poll() is None:
                LOGGER.warning("translator_native_startup_timeout")
                raise RuntimeError("TRANSLATOR_NATIVE_NOT_READY")
            raise RuntimeError("TRANSLATOR_NATIVE_UNAVAILABLE")
        except Exception:
            self._stop_runtime()
            raise

    def _stop_runtime(self) -> bool:
        """Retain ownership until reaped; cleanup never hides the request failure."""
        self._runtime_loaded = False
        process = self._process
        if process is None:
            return True
        if self._stop_requested and process.poll() is None:
            return False
        try:
            if process.poll() is None:
                self._stop_requested = True
                with suppress(ProcessLookupError):
                    process.kill()
            process.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            LOGGER.warning("translator_native_stop_pending")
            return False
        self._process = None
        self._stop_requested = False
        return True

    def close(self) -> None:
        self._stop_runtime()
        self._client.close()

    @contextmanager
    def _runtime_slot(self, deadline: float) -> Generator[None]:
        ticket = object()
        with self._condition:
            self._tickets.append(ticket)
            while self._tickets[0] is not ticket:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._tickets.remove(ticket)
                    raise TimeoutError("TRANSLATOR_DEADLINE_EXCEEDED")
                self._condition.wait(remaining)
        try:
            if time.monotonic() >= deadline:
                raise TimeoutError("TRANSLATOR_DEADLINE_EXCEEDED")
            yield
        finally:
            with self._condition:
                self._tickets.popleft()
                self._condition.notify_all()

    def _post(
        self, route: str, payload: dict[str, object], deadline: float
    ) -> dict[str, Any]:
        remaining = min(
            self._settings.segment_timeout_seconds, deadline - time.monotonic()
        )
        if remaining <= 0:
            raise TimeoutError("TRANSLATOR_DEADLINE_EXCEEDED")
        try:
            response = self._client.post(route, json=payload, timeout=remaining)
            response.raise_for_status()
            value: object = response.json()
            if not isinstance(value, dict):
                raise RuntimeError("TRANSLATOR_NATIVE_RESPONSE_INVALID")
            return cast(dict[str, Any], value)
        except httpx.TimeoutException as error:
            self._stop_runtime()
            raise TimeoutError("TRANSLATOR_DEADLINE_EXCEEDED") from error
        except (httpx.HTTPError, ValueError) as error:
            self._stop_runtime()
            raise RuntimeError("TRANSLATOR_NATIVE_UNAVAILABLE") from error

    def _unit(
        self, value: str, source: str, target: str, budget: int, deadline: float
    ) -> str:
        prompt = translation_prompt(value, source, target)
        tokens = self._post("/tokenize", {"content": prompt}, deadline).get("tokens")
        if not isinstance(tokens, list) or any(
            type(token) is not int for token in cast(list[object], tokens)
        ):
            raise RuntimeError("TRANSLATOR_NATIVE_TOKENIZATION_INVALID")
        if (
            len(cast(list[object], tokens)) + budget + 64
            >= self._profile["context_size"]
        ):
            return self._split_unit(value, source, target, budget, deadline)
        result = self._post(
            "/v1/chat/completions",
            {
                "model": "index-translate",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": self._profile["temperature"],
                "seed": self._profile["seed"],
                "max_tokens": budget,
                "cache_prompt": self._profile["cache_prompt"],
                "chat_template_kwargs": {
                    "enable_thinking": self._profile["enable_thinking"]
                },
            },
            deadline,
        )
        choices = result.get("choices")
        if not isinstance(choices, list):
            raise RuntimeError("TRANSLATOR_NATIVE_RESPONSE_INVALID")
        items = cast(list[object], choices)
        if len(items) != 1:
            raise RuntimeError("TRANSLATOR_NATIVE_RESPONSE_INVALID")
        choice = _mapping(items[0])
        if choice.get("finish_reason") == "length":
            return self._split_unit(value, source, target, budget, deadline)
        if choice.get("finish_reason") != "stop":
            raise RuntimeError("TRANSLATOR_NATIVE_RESPONSE_INVALID")
        message = _mapping(choice.get("message"))
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise TranslatorRuntimeRequestError("TRANSLATOR_EMPTY_OUTPUT")
        if message.get("reasoning_content") or re.search(
            r"<think>|<\|[^>]+\|>|^\s*(?:[a-z]{2,3})?>\s", content
        ):
            raise TranslatorRuntimeRequestError("TRANSLATOR_CONTROL_TOKEN_LEAK")
        return content.strip()

    def _split_unit(
        self, value: str, source: str, target: str, budget: int, deadline: float
    ) -> str:
        if len(value) < 2:
            raise RuntimeError("TRANSLATOR_OUTPUT_LIMIT_EXCEEDED")
        middle = len(value) // 2
        boundary = value.rfind(" ", middle // 2, middle + 1)
        if boundary < 1:
            boundary = middle
        parts = (value[:boundary].strip(), value[boundary:].strip())
        return "\n".join(
            self._unit(part, source, target, budget, deadline) for part in parts if part
        )

    def _translate(
        self,
        values: list[str],
        *,
        source: str,
        target: str,
        max_output_tokens: int | None = None,
    ) -> list[str]:
        source_name = language_name(source, source=True)
        target_name = language_name(target)
        budget = (
            self._profile["output_tokens"]
            if max_output_tokens is None
            else max_output_tokens
        )
        deadline = time.monotonic() + self._settings.request_timeout_seconds
        translated: list[str] = []
        for offset in range(0, len(values), self._settings.batch_size):
            with self._runtime_slot(deadline):
                if not self.runtime_ready:
                    self._start_runtime(
                        min(
                            self._settings.startup_timeout_seconds,
                            max(0, deadline - time.monotonic()),
                        )
                    )
                for value in values[offset : offset + self._settings.batch_size]:
                    key = json.dumps(
                        [source_name, target_name, value, budget], ensure_ascii=False
                    )
                    cached = self._cache.get(key)
                    if cached is not None:
                        self._cache.move_to_end(key)
                        translated.extend(cached)
                        continue
                    result = self._unit(
                        value, source_name, target_name, budget, deadline
                    )
                    self._cache[key] = [result]
                    if len(self._cache) > 2048:
                        self._cache.popitem(last=False)
                    translated.append(result)
        return translated

    def localize(self, request: ProfileLocalizationRequest) -> dict[str, object]:
        language_name(request.source_language, source=True)
        language_name(request.target_language)
        if request.source_language == request.target_language:
            return request.profile
        values = [cast(str, request.profile["subject_area"])]
        lengths: dict[str, int] = {}
        for field in _FIELDS[1:]:
            items = cast(list[str], request.profile[field])
            lengths[field] = len(items)
            values.extend(items)
        translated = self._translate(
            values, source=request.source_language, target=request.target_language
        )
        result: dict[str, object] = {"subject_area": translated[0]}
        offset = 1
        for field in _FIELDS[1:]:
            result[field] = translated[offset : offset + lengths[field]]
            offset += lengths[field]
        return result

    def translate_text(
        self, request: EvidenceTranslationRequest
    ) -> tuple[str, tuple[str, ...]]:
        language_name(request.source_language, source=True)
        language_name(request.target_language)
        if request.source_language == request.target_language:
            return request.text, ()
        translated = "\n".join(
            self._translate(
                list(_evidence_segments(request.text)),
                source=request.source_language,
                target=request.target_language,
            )
        ).strip()
        warnings = ["MACHINE_TRANSLATION_UNVERIFIED"]
        if translated.casefold() == request.text.strip().casefold():
            warnings.append("UNCHANGED_OUTPUT")
        return translated, tuple(warnings)

    def translate_evidence(
        self, request: EvidenceTranslationRequest
    ) -> tuple[str, tuple[str, ...]]:
        if request.source_language == request.target_language:
            return self.translate_text(request)
        text, excerpted = _operator_translation_evidence(request.text)
        bounded = EvidenceTranslationRequest(
            request.translator_ref,
            request.source_language,
            request.target_language,
            request.source_digest,
            text,
        )
        translated, warnings = self.translate_text(bounded)
        return translated, (
            (
                *warnings,
                "REPRESENTATIVE_EXCERPT_TRANSLATED",
            )
            if excerpted
            else warnings
        )


def _evidence_segments(text: str) -> tuple[str, ...]:
    segments: list[str] = []
    for paragraph in re.split(r"\n+", text):
        remaining = paragraph.strip()
        while remaining:
            if len(remaining) <= 800:
                segments.append(remaining)
                break
            boundary = remaining.rfind(" ", 0, 801)
            if boundary < 400:
                boundary = 800
            segments.append(remaining[:boundary].strip())
            remaining = remaining[boundary:].strip()
    if not segments:
        raise RuntimeError("translator evidence text is empty")
    return tuple(segments)


def _operator_translation_evidence(text: str) -> tuple[str, bool]:
    text = text.strip()
    if len(text) <= 768:
        return text, False
    middle = max(0, len(text) // 2 - 128)
    return (
        " … ".join(
            (
                text[:256].strip(),
                text[middle : middle + 256].strip(),
                text[-256:].strip(),
            )
        ),
        True,
    )


def create_app(engine: TranslationEngine) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        heartbeat = (
            asyncio.create_task(
                health_heartbeat.run(
                    "model", lambda: engine.runtime_ready, lambda: engine.runtime_pid
                )
            )
            if health_heartbeat.enabled("model")
            else None
        )
        try:
            yield
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            engine.close()

    app = FastAPI(
        title="DRP translator private model",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/runtimez")
    async def runtimez() -> dict[str, str]:
        if not engine.runtime_ready:
            raise HTTPException(status_code=503, detail="translator runtime stopped")
        return {"status": "ok", "translator_ref": engine.translator_ref}

    def dispatch(payload: object, operation: str) -> dict[str, object]:
        try:
            if operation == "profile":
                profile = parse_profile_localization_request(
                    payload, expected_translator_ref=engine.translator_ref
                )
                return {
                    "schema_version": "1.0.0",
                    "translator_ref": engine.translator_ref,
                    "source_language": profile.source_language,
                    "target_language": profile.target_language,
                    "profile": engine.localize(profile),
                }
            request = parse_evidence_translation_request(
                payload, expected_translator_ref=engine.translator_ref
            )
            translated, warnings = (
                engine.translate_evidence(request)
                if operation == "evidence"
                else engine.translate_text(request)
            )
            return {
                "schema_version": "1.0.0",
                "translator_ref": engine.translator_ref,
                "source_language": request.source_language,
                "target_language": request.target_language,
                "source_digest": request.source_digest,
                "translated_text": translated,
                "warnings": list(warnings),
            }
        except (TranslatorConfigurationError, TranslatorRuntimeRequestError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except (TimeoutError, RuntimeError) as error:
            raise HTTPException(status_code=503, detail=retry_code(error)) from error

    @app.post("/v1/localize-profile")
    def localize(payload: Annotated[object, Body()]) -> dict[str, object]:
        return dispatch(payload, "profile")

    @app.post("/v1/translate-evidence")
    def evidence(payload: Annotated[object, Body()]) -> dict[str, object]:
        return dispatch(payload, "evidence")

    @app.post("/v1/translate-text")
    def translate(payload: Annotated[object, Body()]) -> dict[str, object]:
        return dispatch(payload, "text")

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("serve", "verify-model"))
    args = parser.parse_args()
    if args.command == "verify-model":
        path = Path(os.environ["AELEV_TRANSLATOR_LOCK_PATH"])
        ref, artifacts = read_translator_lock(path)
        verify_model_artifacts(
            TranslationSettings(
                Path(os.environ["AELEV_TRANSLATOR_MODEL_ROOT"]),
                path,
                Path("/unused"),
                ref,
                artifacts,
                1,
            )
        )
        return
    settings = TranslationSettings.from_environment()
    settings.socket_path.parent.mkdir(parents=True, exist_ok=True)
    if settings.socket_path.exists():
        if not stat.S_ISSOCK(settings.socket_path.stat().st_mode):
            raise RuntimeError("translator socket path exists and is not a socket")
        settings.socket_path.unlink()
    engine = TranslationEngine(settings)
    try:
        uvicorn.run(create_app(engine), uds=str(settings.socket_path), access_log=False)
    finally:
        engine.close()


if __name__ == "__main__":
    main()
