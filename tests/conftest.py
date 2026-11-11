from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pennyguard import Event, MemoryStore, Price, Pricing


class Clock:
    """A settable UTC clock for guards (wall time) and MemoryStore (seconds)."""

    def __init__(self, now: datetime) -> None:
        self.now = now
        self.seconds = 0.0

    def __call__(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.seconds

    def advance(self, **delta: float) -> None:
        step = timedelta(**delta)
        self.now += step
        self.seconds += step.total_seconds()


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 11, 11, 15, 30, tzinfo=UTC))


@pytest.fixture
def store(clock: Clock) -> MemoryStore:
    return MemoryStore(clock=clock.monotonic)


@pytest.fixture
def events() -> list[Event]:
    return []


@pytest.fixture
def pricing() -> Pricing:
    # Made-up prices: tests check arithmetic, not any provider's rate card.
    return Pricing(
        {
            "model-a": Price(
                1, 5, cache_read_per_mtok="0.1", cache_write_per_mtok="1.25"
            ),
            "model-b": Price(10, 50),
        }
    )
