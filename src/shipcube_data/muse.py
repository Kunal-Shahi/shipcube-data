"""Muse Spark client for migration drafting (Meta Model API, OpenAI-compatible chat completions).

Configured from the environment:

* `META_API_KEY`        required to enable Muse; in production, mounted from Secret Manager
* `META_API_BASE_URL`   default https://api.meta.ai/v1
* `META_MUSE_MODEL`     default muse-spark-1.3
* `META_MUSE_TIMEOUT_S` default 60

Muse Spark is a reasoning model: reasoning tokens count against `max_tokens`, so we ask for
low reasoning effort and leave plenty of room. Transient failures (429, 5xx, network) are
retried with backoff; anything else raises `MuseError`, which `MuseDrafter` logs before
falling back to rules.
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from typing import Optional

from .obs import Timer, log_event

DEFAULT_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_MODEL = "muse-spark-1.3"


class MuseError(RuntimeError):
    pass


class MetaMuseClient:
    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL, model: str = DEFAULT_MODEL,
                 timeout_s: float = 60.0, max_tokens: int = 6000, reasoning_effort: str = "low", attempts: int = 3):
        if not api_key:
            raise ValueError("api_key is required")
        self._key = api_key
        self.base_url, self.model = base_url.rstrip("/"), model
        self.timeout_s, self.max_tokens, self.reasoning_effort, self.attempts = timeout_s, max_tokens, reasoning_effort, attempts

    def __repr__(self) -> str:  # never print the key
        return f"MetaMuseClient(model={self.model!r}, base_url={self.base_url!r})"

    def __call__(self, system: str, user: str) -> str:
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": self.max_tokens,
            "reasoning_effort": self.reasoning_effort,
            "temperature": 0,
        }).encode()
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body, method="POST",
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
        )
        last: Optional[str] = None
        for attempt in range(1, self.attempts + 1):
            with Timer() as t:
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                        payload = json.loads(resp.read())
                    content = (payload.get("choices") or [{}])[0].get("message", {}).get("content")
                    if not content:
                        raise MuseError("Muse returned an empty message (reasoning may have used all max_tokens)")
                    usage = payload.get("usage") or {}
                    log_event("muse.ok", model=self.model, attempt=attempt, elapsed_ms=t.elapsed_ms,
                              prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"))
                    return content
                except urllib.error.HTTPError as e:
                    last = f"HTTP {e.code}"
                    if e.code not in (408, 429) and e.code < 500:
                        raise MuseError(f"Muse request failed: {last}") from None
                except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                    last = type(e).__name__
            log_event("muse.retry", level=30, model=self.model, attempt=attempt, error=last)
            if attempt < self.attempts:
                time.sleep(min(8.0, 0.5 * 2**attempt) + random.random() / 4)
        raise MuseError(f"Muse unavailable after {self.attempts} attempts: {last}")


def muse_from_env() -> Optional[MetaMuseClient]:
    key = os.environ.get("META_API_KEY")
    if not key:
        return None
    return MetaMuseClient(
        key,
        base_url=os.environ.get("META_API_BASE_URL", DEFAULT_BASE_URL),
        model=os.environ.get("META_MUSE_MODEL", DEFAULT_MODEL),
        timeout_s=float(os.environ.get("META_MUSE_TIMEOUT_S", "60")),
    )
