"""Dead-man's switch: ping healthchecks.io while the inbox loop is healthy. A no-op when no URL is configured."""

from __future__ import annotations

import threading
import time
import urllib.request
from collections.abc import Callable

import structlog

log = structlog.get_logger(__name__)


class Healthcheck:
    def __init__(
        self,
        url: str | None,
        *,
        min_interval_s: float = 60.0,
        timeout_s: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        opener: Callable[..., object] = urllib.request.urlopen,
    ):
        self.url = (url or "").rstrip("/") or None
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self._clock = clock
        self._open = opener
        self._last = float("-inf")
        self._lock = threading.Lock()

    def ping(self, suffix: str = "", *, force: bool = False) -> bool:
        """Ping `<url>[/suffix]` ('fail' or 'start' are healthchecks.io's other endpoints). Returns whether a
        ping went out. Throttled to one per `min_interval_s` unless forced; network errors are logged, not raised."""
        if not self.url:
            return False
        with self._lock:
            now = self._clock()
            if not force and now - self._last < self.min_interval_s:
                return False
            self._last = now
        try:
            with self._open(f"{self.url}/{suffix}" if suffix else self.url, timeout=self.timeout_s):  # type: ignore[attr-defined]
                pass
            return True
        except Exception as e:  # a monitoring hiccup must never take the agent down
            log.warning("health.ping_failed", error=repr(e))
            return False

    def fail(self, note: str = "") -> bool:
        log.error("health.fail", note=note)
        return self.ping("fail", force=True)
