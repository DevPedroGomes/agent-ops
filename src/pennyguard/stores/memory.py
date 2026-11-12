"""An in-process store for development, tests and single-process apps.

Each process has its own counters. Behind several workers or replicas every
one of them gets the full budget, so production with more than one process
needs ``RedisStore`` or ``AsyncRedisStore``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence

from pennyguard.stores.base import Counter, ReserveOutcome


class MemoryStore:
    """Thread-safe counters in a dict. Works with ``Guard`` and ``SyncGuard``.

    Same semantics as the Redis stores, markers and expiry included.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._values: dict[str, int] = {}
        self._expires: dict[str, float] = {}
        self._kill: dict[str, str] = {}

    def _get(self, key: str, now: float) -> int | None:
        expires = self._expires.get(key)
        if expires is not None and expires <= now:
            self._values.pop(key, None)
            self._expires.pop(key, None)
        return self._values.get(key)

    def _set(self, key: str, value: int, ttl: int, now: float) -> None:
        self._values[key] = value
        if ttl > 0:
            self._expires[key] = now + ttl

    def reserve(
        self,
        kill_key: str,
        op_key: str,
        counters: Sequence[Counter],
        amount: int,
        hold_ttl: int,
    ) -> ReserveOutcome:
        with self._lock:
            now = self._clock()
            held = self._get(op_key, now)
            if held is not None:
                return ReserveOutcome("ok", charged=held)
            reason = self._kill.get(kill_key)
            if reason is not None:
                return ReserveOutcome("killed", kill_reason=reason)
            current = [self._get(c.key, now) or 0 for c in counters]
            for index, (counter, value) in enumerate(
                zip(counters, current, strict=True)
            ):
                if value >= counter.limit or value + amount > counter.limit:
                    return ReserveOutcome(
                        "refused", totals=tuple(current), refused_index=index
                    )
            for counter, value in zip(counters, current, strict=True):
                self._set(counter.key, value + amount, counter.ttl, now)
            self._set(op_key, amount, hold_ttl, now)
            return ReserveOutcome(
                "ok", totals=tuple(v + amount for v in current), charged=amount
            )

    def settle(
        self, op_key: str, counters: Sequence[Counter], target: int, keep_ttl: int
    ) -> None:
        with self._lock:
            now = self._clock()
            delta = target - (self._get(op_key, now) or 0)
            if delta:
                for counter in counters:
                    value = max(0, (self._get(counter.key, now) or 0) + delta)
                    self._set(counter.key, value, counter.ttl, now)
            self._set(op_key, target, keep_ttl, now)

    def read(self, keys: Sequence[str]) -> list[int]:
        with self._lock:
            now = self._clock()
            return [self._get(key, now) or 0 for key in keys]

    def set_kill(self, kill_key: str, reason: str | None) -> None:
        with self._lock:
            if reason is None:
                self._kill.pop(kill_key, None)
            else:
                self._kill[kill_key] = reason

    def get_kill(self, kill_key: str) -> str | None:
        with self._lock:
            return self._kill.get(kill_key)
