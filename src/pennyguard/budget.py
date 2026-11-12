"""Budget definitions, time windows and counter keys."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, get_args
from urllib.parse import quote

from pennyguard.money import Amount, to_decimal, to_micros

Window = Literal["hour", "day", "month", "total"]
WINDOWS: tuple[Window, ...] = get_args(Window)

_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_DIMENSION = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# Keyword arguments of reserve() and call(). A dimension with one of these
# names could not be passed as a scope keyword.
RESERVED_DIMENSIONS = frozenset({"estimate_usd", "fn", "retries", "tags"})

# A counter outlives its window by at least one full window after its last
# write, so a settle that arrives late still finds the key it reserved on.
_TTL_SECONDS: dict[Window, int] = {
    "hour": 2 * 3_600,
    "day": 2 * 86_400,
    "month": 62 * 86_400,
    "total": 0,
}


@dataclass(frozen=True)
class Budget:
    """A spending limit in dollars over a fixed UTC window.

    ``per`` names the dimension the limit applies to separately, such as
    ``"tenant"`` or ``"user"``. Without it the budget is global: one counter
    shared by every call in the project.

    Windows are fixed and aligned to UTC: an ``"hour"`` budget resets on the
    hour, ``"day"`` at midnight UTC, ``"month"`` on the first of the month.
    ``"total"`` never resets.
    """

    name: str
    limit_usd: Decimal
    window: Window = "day"
    per: str | None = None
    _limit_micros: int = field(init=False, repr=False, compare=False)

    def __init__(
        self,
        name: str,
        limit_usd: Amount,
        window: Window = "day",
        per: str | None = None,
    ) -> None:
        if not _NAME.match(name):
            raise ValueError(f"budget name must match {_NAME.pattern}, got {name!r}")
        if window not in WINDOWS:
            raise ValueError(f"window must be one of {WINDOWS}, got {window!r}")
        if per is not None:
            if not _DIMENSION.match(per):
                raise ValueError(f"per must match {_DIMENSION.pattern}, got {per!r}")
            if per in RESERVED_DIMENSIONS:
                raise ValueError(
                    f"per={per!r} clashes with a keyword argument of reserve(); "
                    f"pick another name"
                )
        limit = to_decimal(limit_usd, "limit_usd")
        if limit <= 0:
            raise ValueError(f"limit_usd must be positive, got {limit_usd!r}")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "limit_usd", limit)
        object.__setattr__(self, "window", window)
        object.__setattr__(self, "per", per)
        object.__setattr__(self, "_limit_micros", to_micros(limit, round_up=False))

    @property
    def limit_micros(self) -> int:
        return self._limit_micros

    @property
    def ttl_seconds(self) -> int:
        return _TTL_SECONDS[self.window]


def window_bounds(window: Window, now: datetime) -> tuple[str, datetime | None]:
    """The period id of ``now`` in ``window`` and the moment it ends."""
    now = now.astimezone(UTC)
    if window == "hour":
        start = now.replace(minute=0, second=0, microsecond=0)
        return start.strftime("%Y%m%dT%H"), start + timedelta(hours=1)
    if window == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.strftime("%Y%m%d"), start + timedelta(days=1)
    if window == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
        return start.strftime("%Y%m"), end
    return "all", None


def seconds_until(moment: datetime | None, now: datetime) -> int | None:
    """Whole seconds from ``now`` to ``moment``, never below 1.

    The floor matters in the last fraction of a second before a reset:
    truncating 0.4s to ``Retry-After: 0`` invites an immediate retry loop.
    """
    if moment is None:
        return None
    remaining = (moment - now).total_seconds()
    return max(1, int(remaining) + (0 if remaining.is_integer() else 1))


def _part(value: str) -> str:
    # Percent-encoding leaves no ":" "{" or "}" inside a field, so two
    # different (field, value) combinations can never build the same key.
    return quote(value, safe="")


def project_prefix(project: str) -> str:
    # The braces are a Redis Cluster hash tag: every key of one project hashes
    # to the same slot, which a multi-key Lua script requires.
    return f"pg:{{{_part(project)}}}"


def counter_key(
    project: str, budget: Budget, period: str, scope_value: str | None
) -> str:
    base = f"{project_prefix(project)}:{budget.name}:{period}"
    if scope_value is None:
        return base
    # The dimension is part of the key, so two guards with a budget of the
    # same name over different dimensions never share a counter.
    return f"{base}:{budget.per}:{_part(scope_value)}"


def kill_key(project: str) -> str:
    return f"{project_prefix(project)}:kill"


def op_key(project: str, reservation_id: str) -> str:
    return f"{project_prefix(project)}:op:{_part(reservation_id)}"
