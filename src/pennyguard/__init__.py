"""Hard dollar budgets for LLM calls, shared by every process of your app.

Reserve an estimate before the call, settle the real cost after it. The
reservation is checked and charged atomically against every budget that
applies (global, per tenant, per user...), so concurrent calls across workers
and replicas cannot spend past a limit.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

from pennyguard._core import BudgetStatus, Event
from pennyguard.budget import Budget, Window
from pennyguard.errors import (
    BudgetExceeded,
    GuardKilled,
    GuardUnavailable,
    PennyGuardError,
    UnknownModel,
)
from pennyguard.guard import Guard, Reservation, SyncGuard, SyncReservation
from pennyguard.pricing import Price, Pricing
from pennyguard.stores import AsyncRedisStore, MemoryStore, RedisStore
from pennyguard.usage import Usage

try:
    __version__ = _version("pennyguard")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0+unknown"

__all__ = [
    "AsyncRedisStore",
    "Budget",
    "BudgetExceeded",
    "BudgetStatus",
    "Event",
    "Guard",
    "GuardKilled",
    "GuardUnavailable",
    "MemoryStore",
    "PennyGuardError",
    "Price",
    "Pricing",
    "RedisStore",
    "Reservation",
    "SyncGuard",
    "SyncReservation",
    "UnknownModel",
    "Usage",
    "Window",
    "__version__",
]
