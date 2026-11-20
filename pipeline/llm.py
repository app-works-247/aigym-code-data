"""Provider-neutral JSON generation (PLAN.md 6.6: summaries must not depend on one vendor).

A provider takes a system prompt, a user prompt and a JSON schema, and returns the parsed
object. Only Gemini exists today; an Anthropic provider is added when a Claude key exists.
Keys come from the environment and never appear in errors or logs.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Protocol

GEMINI_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
_MAX_ATTEMPTS = 6


class LLMError(Exception):
    pass


class Provider(Protocol):
    name: str
    model: str

    def generate_json(self, system: str, user: str, schema: dict) -> dict: ...


def _post(url: str, headers: dict, body: dict, timeout: int = 120) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.loads(err.read() or b"{}")
        except ValueError:
            return err.code, {}
    except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
        return 599, {"error": {"message": f"network: {err}"}}


def _retry_delay(reply: dict, attempt: int) -> float:
    """Google puts the wait in error.details[].retryDelay, e.g. "17s"."""
    for detail in (reply.get("error") or {}).get("details") or []:
        delay = detail.get("retryDelay")
        if delay:
            match = re.match(r"([\d.]+)s", delay)
            if match:
                return min(float(match.group(1)) + 1, 120.0)
    return float(min(2 ** (attempt + 2), 60))


class GeminiProvider:
    name = "google"

    def __init__(
        self,
        api_key: str,
        model: str,
        min_interval: float = 0.0,
        post: Callable = _post,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_key:
            raise LLMError("GEMINI_API_KEY is not set")
        self._key = api_key
        self.model = model
        self._min_interval = min_interval
        self._post = post
        self._sleep = sleep
        self._clock = clock
        self._last_call = None
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def generate_json(self, system: str, user: str, schema: dict) -> dict:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": schema,
                "temperature": 0.2,
            },
        }
        url = f"{GEMINI_ROOT}/{self.model}:generateContent"
        headers = {"x-goog-api-key": self._key, "Content-Type": "application/json"}
        for attempt in range(_MAX_ATTEMPTS):
            self._pace()
            status, reply = self._post(url, headers, body)
            self.calls += 1
            if status == 200:
                usage = reply.get("usageMetadata") or {}
                self.input_tokens += int(usage.get("promptTokenCount") or 0)
                self.output_tokens += int(usage.get("candidatesTokenCount") or 0)
                return _parse_candidate(reply)
            if status == 429 or status >= 500:
                self._sleep(_retry_delay(reply, attempt))
                continue
            message = str((reply.get("error") or {}).get("message", ""))[:300]
            raise LLMError(f"Gemini {status}: {message}")
        raise LLMError(f"Gemini: still failing after {_MAX_ATTEMPTS} attempts")

    def _pace(self) -> None:
        if self._last_call is not None:
            wait = self._min_interval - (self._clock() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._clock()


def _parse_candidate(reply: dict) -> dict:
    candidates = reply.get("candidates") or []
    if not candidates:
        reason = (reply.get("promptFeedback") or {}).get("blockReason", "no candidates")
        raise LLMError(f"Gemini returned nothing ({reason})")
    first = candidates[0]
    parts = (first.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not text:
        raise LLMError(f"Gemini returned no text (finishReason {first.get('finishReason')})")
    try:
        return json.loads(text)
    except ValueError as err:
        raise LLMError(f"Gemini returned invalid JSON: {err}") from err


def make_provider(config: dict, env: dict) -> Provider:
    if config["provider"] == "gemini":
        return GeminiProvider(
            env.get("GEMINI_API_KEY", ""),
            config["model"],
            min_interval=float(config.get("min_seconds_between_calls", 0)),
        )
    raise LLMError(f"unknown provider {config['provider']!r}")
