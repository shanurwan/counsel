"""Model adapters: NVIDIA NIM live, and recorded replay for CI.

The runtime only sees `complete(messages) -> Completion`. Which provider or
model answered is recorded with every call, so swapping providers is a config
change and every run can be reconstructed.

Models on the NVIDIA API are retired without much notice (410 Gone), so the
live adapter takes an ordered list and falls through to the next model on
404/410. Rate limits (429) and server errors back off and retry.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
# Probed 2026-11-03: meta/llama-3.3-70b-instruct and llama-3.1-8b are gone (410).
DEFAULT_MODELS = (
    "nvidia/nemotron-3-ultra-550b-a55b",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
)


class ModelError(Exception):
    pass


@dataclass(frozen=True)
class Completion:
    text: str
    provider: str
    model: str
    latency_ms: int
    prompt_tokens: int | None = None
    output_tokens: int | None = None


class Model(Protocol):
    def complete(self, messages: list[dict], *, max_tokens: int = 800) -> Completion: ...


def request_hash(messages: list[dict], max_tokens: int) -> str:
    """Identifies a request independent of which model ends up answering it."""
    canonical = json.dumps({"messages": messages, "max_tokens": max_tokens}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


# (status, body) for a POST; injectable so tests never touch the network.
Transport = Callable[[str, dict, dict, float], tuple[int, dict]]


def _urllib_transport(url: str, headers: dict, body: dict, timeout: float) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        # The URL is the fixed https NVIDIA_URL constant, never caller input.
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, TimeoutError) as e:
        return 599, {"error": str(e)}


@dataclass
class NvidiaModel:
    api_key: str
    models: tuple[str, ...] = DEFAULT_MODELS
    timeout_s: float = 45.0
    retries: int = 2
    # The free tier allows about 40 requests a minute; stay under it.
    min_interval_s: float = 1.6
    temperature: float = 0.2
    transport: Transport = _urllib_transport
    sleep: Callable[[float], None] = time.sleep
    _last_call: float = field(default=0.0, repr=False)

    def complete(self, messages: list[dict], *, max_tokens: int = 800) -> Completion:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        errors = []
        for model in self.models:
            for attempt in range(self.retries + 1):
                wait = self._last_call + self.min_interval_s - time.monotonic()
                if wait > 0:
                    self.sleep(wait)
                self._last_call = time.monotonic()

                # Nemotron 3 reasons before answering by default, and reasoning tokens
                # count against max_tokens; with it on, a digest was cut off mid-line.
                body = {"model": model, "messages": messages, "max_tokens": max_tokens,
                        "temperature": self.temperature,
                        "chat_template_kwargs": {"enable_thinking": False}}
                started = time.monotonic()
                status, data = self.transport(NVIDIA_URL, headers, body, self.timeout_s)
                latency = int((time.monotonic() - started) * 1000)

                if status == 200:
                    choice = data["choices"][0]
                    msg = choice["message"]
                    usage = data.get("usage") or {}
                    if choice.get("finish_reason") == "length":
                        # A truncated answer must never be written as if it were whole.
                        raise ModelError(f"{model}: output truncated at max_tokens={max_tokens}")
                    return Completion(
                        text=(msg.get("content") or "").strip(),
                        provider="nvidia",
                        model=model,
                        latency_ms=latency,
                        prompt_tokens=usage.get("prompt_tokens"),
                        output_tokens=usage.get("completion_tokens"),
                    )
                errors.append(f"{model}: {status} {str(data)[:200]}")
                if status in (404, 410):
                    break  # retired or not enabled for this key: next model
                if status == 429 or status >= 500:
                    self.sleep(min(2 ** attempt * 2, 10))
                    continue
                raise ModelError(errors[-1])
        raise ModelError("all models failed: " + " | ".join(errors))


class ReplayModel:
    """Answers from recordings keyed by request hash; refuses anything unrecorded."""

    def __init__(self, directory: Path):
        self.directory = directory

    def complete(self, messages: list[dict], *, max_tokens: int = 800) -> Completion:
        path = self.directory / f"{request_hash(messages, max_tokens)}.json"
        if not path.exists():
            raise ModelError(f"no recording for request {path.stem}; re-record with COUNSEL_MODEL=record")
        rec = json.loads(path.read_text(encoding="utf-8"))
        return Completion(**{**rec, "latency_ms": 0})


class RecordingModel:
    """Calls a live model and saves each completion for later replay."""

    def __init__(self, live: Model, directory: Path):
        self.live = live
        self.directory = directory

    def complete(self, messages: list[dict], *, max_tokens: int = 800) -> Completion:
        result = self.live.complete(messages, max_tokens=max_tokens)
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{request_hash(messages, max_tokens)}.json"
        path.write_text(json.dumps(result.__dict__, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return result
