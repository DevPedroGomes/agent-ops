"""The storage contract a guard needs.

A store holds integer counters in micro-dollars, one kill flag per project,
and one small marker per open reservation recording how much that
reservation has charged. Both writes are atomic, and both are idempotent
through the marker:

* ``reserve`` with an operation key that already exists charges nothing and
  reports what was charged the first time. A client library that retries a
  command after a timeout therefore cannot charge twice.
* ``settle`` moves the counters so that the reservation's charge equals a
  target, not by a delta. Retrying it is harmless, releasing is settling to
  zero, and settling a reservation whose ``reserve`` outcome was never
  learned (a timeout, ``fail_open``) still lands on the right total.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class Counter:
    key: str
    limit: int
    """Limit in micro-dollars."""
    ttl: int
    """Seconds the key lives after each write. Zero means no expiry."""


@dataclass(frozen=True)
class ReserveOutcome:
    status: Literal["ok", "refused", "killed"]
    totals: tuple[int, ...] = ()
    """Each counter after the charge when ok, untouched when refused. Empty
    when the reservation already existed."""
    refused_index: int | None = None
    kill_reason: str | None = None
    charged: int = 0
    """What the reservation holds when ok."""


class SyncStore(Protocol):
    def reserve(
        self,
        kill_key: str,
        op_key: str,
        counters: Sequence[Counter],
        amount: int,
        hold_ttl: int,
    ) -> ReserveOutcome:
        """Atomically: if ``op_key`` exists, report it and charge nothing;
        refuse if killed; refuse if any counter would pass its limit (or is
        already at it); otherwise add ``amount`` to every counter and record
        it under ``op_key`` for ``hold_ttl`` seconds. A refusal writes
        nothing."""
        ...

    def settle(
        self, op_key: str, counters: Sequence[Counter], target: int, keep_ttl: int
    ) -> None:
        """Atomically move every counter by ``target`` minus the amount
        recorded under ``op_key`` (zero if absent), flooring each counter at
        zero, then record ``target`` under ``op_key`` for ``keep_ttl``."""
        ...

    def read(self, keys: Sequence[str]) -> list[int]: ...

    def set_kill(self, kill_key: str, reason: str | None) -> None:
        """Engage the kill switch with ``reason``, or clear it with ``None``."""
        ...

    def get_kill(self, kill_key: str) -> str | None: ...


class AsyncStore(Protocol):
    async def reserve(
        self,
        kill_key: str,
        op_key: str,
        counters: Sequence[Counter],
        amount: int,
        hold_ttl: int,
    ) -> ReserveOutcome: ...

    async def settle(
        self, op_key: str, counters: Sequence[Counter], target: int, keep_ttl: int
    ) -> None: ...

    async def read(self, keys: Sequence[str]) -> list[int]: ...

    async def set_kill(self, kill_key: str, reason: str | None) -> None: ...

    async def get_kill(self, kill_key: str) -> str | None: ...
