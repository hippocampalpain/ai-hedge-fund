"""Disk cache, throttling and retry shared by the Korean sources.

Secrets never reach this module's output: callers pass a ``redact`` list
and every error message is scrubbed before it is raised or logged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from hedge_fund.data.client import FDClientError
from hedge_fund.paths import CACHE_DIR

logger = logging.getLogger(__name__)

KR_CACHE_DIR = CACHE_DIR / "kr"


class KRDataError(FDClientError):
    """A Korean source failed for infrastructure reasons (auth, network,
    rate limit). Subclasses FDClientError so every existing fail-loud
    handler (paper runner, TUI) treats it the same way."""


def scrub(text: str, secrets: list[str | None]) -> str:
    """Remove every secret value from *text* (URLs in exceptions carry keys)."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


class DiskCache:
    """JSON files under ~/.hedge-fund/cache/kr/<namespace>/."""

    def __init__(self, namespace: str, root: Path | str | None = None) -> None:
        self._dir = Path(root or KR_CACHE_DIR) / namespace

    @staticmethod
    def key(*parts: Any) -> str:
        canonical = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()[:24]

    def path(self, key: str, suffix: str = ".json") -> Path:
        return self._dir / f"{key}{suffix}"

    def get(self, key: str, max_age: float | None = None) -> Any | None:
        """The stored payload, or None when missing, corrupt or older than
        *max_age* seconds."""
        path = self.path(key)
        if not path.exists():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if max_age is not None:
            try:
                fetched = datetime.fromisoformat(entry["fetched_at"])
            except (KeyError, TypeError, ValueError):
                return None
            if (datetime.now(timezone.utc) - fetched).total_seconds() > max_age:
                return None
        return entry.get("data")

    def put(self, key: str, data: Any) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self.path(key)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), "data": data},
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


class Throttle:
    """At least *interval* seconds between calls, across threads."""

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            delay = self._last + self._interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


def with_retry(
    fn: Callable[[], Any],
    *,
    what: str,
    retries: int = 3,
    base_delay: float = 2.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    secrets: list[str | None] = (),
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Call *fn*; on a retryable exception wait base, 2×base, 4×base … and
    try again. The final failure is raised as KRDataError with secrets
    scrubbed from the message (the cause is not chained: it may carry them)."""
    for attempt in range(retries + 1):
        try:
            return fn()
        except KRDataError:
            raise
        except retry_on as exc:
            message = scrub(f"{type(exc).__name__}: {exc}", list(secrets))
            if attempt == retries:
                raise KRDataError(f"{what} failed after {retries + 1} attempts: {message[:300]}") from None
            delay = base_delay * (2 ** attempt)
            logger.info("%s failed (%s); retrying in %.0fs", what, message[:120], delay)
            sleep(delay)
    raise AssertionError("unreachable")
