"""Logic shared by the async and sync guards. Nothing here does I/O."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Literal

from pennyguard.budget import (
    Budget,
    Window,
    counter_key,
    kill_key,
    op_key,
    seconds_until,
    window_bounds,
)
from pennyguard.errors import BudgetExceeded, UnknownModel
from pennyguard.money import Amount, from_micros, to_decimal, to_micros
from pennyguard.pricing import Pricing
from pennyguard.stores.base import Counter, ReserveOutcome
from pennyguard.usage import Usage

logger = logging.getLogger("pennyguard")

EventKind = Literal[
    "reserved",
    "refused",
    "killed",
    "unavailable",
    "settled",
    "released",
    "unsettled",
    "pricing_failed",
    "store_error",
]


@dataclass(frozen=True)
class Event:
    """Something a guard did, passed to ``on_event``.

    ``amount_usd`` depends on the kind: the estimate for ``reserved``, the
    amount asked for on ``refused``, the real cost for ``settled``, the amount
    given back for ``released``, and the estimate that stays charged for
    ``unsettled`` and ``pricing_failed``.

    ``usage`` lists the billed attempts of a settled call, and
    ``usage_costs`` the cost of each, in the same order. A call answered by
    a fallback model has two entries under two models.
    """

    kind: EventKind
    project: str
    reservation_id: str | None = None
    scope: Mapping[str, str] = field(default_factory=dict)
    tags: Mapping[str, str] = field(default_factory=dict)
    amount_usd: Decimal | None = None
    budget: str | None = None
    usage: tuple[Usage, ...] = ()
    usage_costs: tuple[Decimal, ...] = ()
    error: BaseException | None = None


@dataclass(frozen=True)
class BudgetStatus:
    """Where one budget stands right now."""

    budget: str
    window: Window
    scope: str | None
    spent_usd: Decimal
    limit_usd: Decimal
    remaining_usd: Decimal
    resets_at: datetime | None


@dataclass(frozen=True)
class Priced:
    cost: Decimal | None
    usage: tuple[Usage, ...]
    costs: tuple[Decimal, ...]
    error: BaseException | None


@dataclass(frozen=True)
class Plan:
    budgets: tuple[Budget, ...]
    counters: tuple[Counter, ...]
    scope_values: tuple[str | None, ...]
    resets: tuple[datetime | None, ...]
    now: datetime


# How long a settled or released reservation keeps its marker: long enough
# for a client library to retry a settle that timed out.
SETTLED_TTL = 300


def _utcnow() -> datetime:
    return datetime.now(UTC)


def freeze(mapping: Mapping[str, str] | None, what: str) -> Mapping[str, str]:
    result = dict(mapping or {})
    for key, value in result.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError(f"{what} keys and values must be str")
    return MappingProxyType(result)


def provider_rejected(error: BaseException) -> bool:
    """Did the provider answer this call with an HTTP error?

    That is the one failure known not to be billed: the provider refused
    before generating anything. Anything else (a timeout, a dropped
    connection, a client that disconnects mid-stream, a bug after the reply)
    may come after the provider started billing. Duck-typed on
    ``status_code``, which the Anthropic and OpenAI SDKs' status errors carry,
    or on ``response.status_code`` as httpx's do.
    """
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    return isinstance(status, int) and not isinstance(status, bool) and status >= 400


class Core:
    def __init__(
        self,
        budgets: Sequence[Budget],
        *,
        project: str,
        pricing: Pricing | None,
        fail_open: bool,
        on_event: Callable[[Event], None] | None,
        clock: Callable[[], datetime] | None,
        reservation_ttl: int,
    ) -> None:
        if not budgets:
            raise ValueError("a guard needs at least one budget")
        names = [b.name for b in budgets]
        if len(set(names)) != len(names):
            raise ValueError(f"budget names must be unique, got {names}")
        if not isinstance(project, str) or not project:
            raise ValueError("project must be a non-empty string")
        if reservation_ttl < SETTLED_TTL:
            raise ValueError(f"reservation_ttl must be at least {SETTLED_TTL}s")
        self.budgets = tuple(budgets)
        self.project = project
        self.pricing = pricing
        self.fail_open = fail_open
        self.on_event = on_event
        self.clock = clock or _utcnow
        self.reservation_ttl = reservation_ttl
        self.kill_key = kill_key(project)
        self.dimensions = frozenset(b.per for b in budgets if b.per is not None)

    def op_key(self, reservation_id: str) -> str:
        return op_key(self.project, reservation_id)

    def now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("clock must return timezone-aware datetimes")
        return now

    def check_scope(self, scope: Mapping[str, str]) -> None:
        unknown = set(scope) - self.dimensions
        if unknown:
            known = sorted(self.dimensions) or "none"
            raise ValueError(
                f"no budget is per {sorted(unknown)}; dimensions in use: {known}"
            )
        for key, value in scope.items():
            if not isinstance(value, str) or not value:
                raise ValueError(f"{key} must be a non-empty string, got {value!r}")

    def plan(self, scope: Mapping[str, str], *, partial: bool = False) -> Plan:
        """Counters for ``scope``. With ``partial``, budgets whose dimension is
        absent are skipped instead of refused (for status reads)."""
        self.check_scope(scope)
        now = self.now()
        budgets, counters, values, resets = [], [], [], []
        for budget in self.budgets:
            value: str | None = None
            if budget.per is not None:
                value = scope.get(budget.per)
                if value is None:
                    if partial:
                        continue
                    raise ValueError(
                        f"budget {budget.name!r} is per {budget.per!r}: "
                        f"pass {budget.per}=..."
                    )
            period, reset = window_bounds(budget.window, now)
            budgets.append(budget)
            counters.append(
                Counter(
                    counter_key(self.project, budget, period, value),
                    budget.limit_micros,
                    budget.ttl_seconds,
                )
            )
            values.append(value)
            resets.append(reset)
        return Plan(tuple(budgets), tuple(counters), tuple(values), tuple(resets), now)

    def estimate_micros(self, estimate_usd: Amount) -> tuple[Decimal, int]:
        estimate = to_decimal(estimate_usd, "estimate_usd")
        if estimate < 0:
            raise ValueError(f"estimate_usd must not be negative, got {estimate_usd!r}")
        return estimate, to_micros(estimate)

    def refusal(
        self, plan: Plan, outcome: ReserveOutcome, requested: Decimal
    ) -> BudgetExceeded:
        index = outcome.refused_index or 0
        budget = plan.budgets[index]
        reset = plan.resets[index]
        return BudgetExceeded(
            budget=budget.name,
            scope=plan.scope_values[index],
            spent_usd=from_micros(outcome.totals[index]),
            limit_usd=budget.limit_usd,
            requested_usd=requested,
            resets_at=reset,
            retry_after=seconds_until(reset, plan.now),
        )

    def statuses(self, plan: Plan, values: Sequence[int]) -> list[BudgetStatus]:
        result = []
        for budget, value, scope, reset in zip(
            plan.budgets, values, plan.scope_values, plan.resets, strict=True
        ):
            spent = from_micros(value)
            result.append(
                BudgetStatus(
                    budget=budget.name,
                    window=budget.window,
                    scope=scope,
                    spent_usd=spent,
                    limit_usd=budget.limit_usd,
                    remaining_usd=max(Decimal(0), budget.limit_usd - spent),
                    resets_at=reset,
                )
            )
        return result

    def cost(self, usage: Usage) -> Decimal:
        if self.pricing is None:
            raise ValueError("this guard has no pricing; pass pricing=Pricing(...)")
        return self.pricing.cost(usage)

    def estimate(
        self,
        model: str,
        *,
        input_tokens: int,
        max_output_tokens: int,
        tier: str | None = None,
    ) -> Decimal:
        if self.pricing is None:
            raise ValueError("this guard has no pricing; pass pricing=Pricing(...)")
        return self.pricing.estimate(
            model,
            input_tokens=input_tokens,
            max_output_tokens=max_output_tokens,
            tier=tier,
        )

    def require_pricing(self, action: str) -> None:
        if self.pricing is None:
            raise ValueError(
                f"{action} prices the response, so the guard needs pricing=Pricing(...)"
            )

    def emit(self, kind: EventKind, **fields: object) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(Event(kind=kind, project=self.project, **fields))  # type: ignore[arg-type]
        except Exception:
            logger.exception("pennyguard.on_event_failed kind=%s", kind)


class ReservationState:
    """Bookkeeping shared by ``Reservation`` and ``SyncReservation``."""

    def __init__(
        self,
        core: Core,
        reservation_id: str,
        counters: tuple[Counter, ...],
        charged_micros: int,
        estimate: Decimal,
        scope: Mapping[str, str],
        tags: Mapping[str, str],
    ) -> None:
        self._core = core
        self._counters = counters
        self._op_key = core.op_key(reservation_id)
        self._charged = charged_micros
        self._state: Literal["open", "settled", "released"] = "open"
        self.id = reservation_id
        self.estimate_usd = estimate
        self.scope = scope
        self.tags = tags

    @property
    def state(self) -> Literal["open", "settled", "released"]:
        return self._state

    @property
    def charged_usd(self) -> Decimal:
        """What the counters hold for this reservation right now."""
        return from_micros(self._charged)

    def _event(self, kind: EventKind, **fields: object) -> None:
        self._core.emit(
            kind, reservation_id=self.id, scope=self.scope, tags=self.tags, **fields
        )

    def _require_open(self, action: str) -> None:
        if self._state != "open":
            raise RuntimeError(f"cannot {action}: reservation is already {self._state}")

    def _price(
        self, cost_usd: Amount | None, usage: Usage | Sequence[Usage] | None
    ) -> Priced:
        """The real cost, or a ``Priced`` with ``error`` set when it cannot be
        priced."""
        if (cost_usd is None) == (usage is None):
            raise ValueError("pass exactly one of cost_usd or usage")
        if usage is not None:
            usages = (usage,) if isinstance(usage, Usage) else tuple(usage)
            if not usages:
                raise ValueError("usage must not be empty")
            if self._core.pricing is None:
                raise ValueError(
                    "settle(usage=...) needs a guard with pricing; "
                    "pass pricing=Pricing(...) or settle(cost_usd=...)"
                )
            try:
                costs = tuple(self._core.pricing.cost(u) for u in usages)
            except UnknownModel as exc:
                return Priced(None, usages, (), exc)
            return Priced(sum(costs, Decimal(0)), usages, costs, None)
        cost = to_decimal(cost_usd, "cost_usd")  # type: ignore[arg-type]
        if cost < 0:
            raise ValueError(f"cost_usd must not be negative, got {cost_usd!r}")
        return Priced(cost, (), (), None)

    def _keep_estimate(
        self, error: BaseException, usage: tuple[Usage, ...] = ()
    ) -> Decimal:
        # The money is spent and the response is in hand. Raising here would
        # turn a paid answer into an error page, so the estimate stays charged
        # and the failure goes to the log and the event hook instead.
        self._state = "settled"
        kept = from_micros(self._charged)
        logger.error(
            "pennyguard.pricing_failed reservation=%s kept=%s error=%s",
            self.id,
            kept,
            error,
        )
        self._event("pricing_failed", amount_usd=kept, usage=usage, error=error)
        return kept

    def _store_failed(self, action: str, error: BaseException) -> None:
        logger.error(
            "pennyguard.store_error action=%s reservation=%s error=%s",
            action,
            self.id,
            error,
        )
        self._event("store_error", error=error)

    def _unsettled(self, error: BaseException | None = None) -> None:
        # No I/O on this path: it also runs while a task is being cancelled,
        # where an await would be cancelled in turn.
        self._state = "settled"
        kept = from_micros(self._charged)
        logger.warning(
            "pennyguard.unsettled reservation=%s kept=%s error=%r",
            self.id,
            kept,
            error,
        )
        self._event("unsettled", amount_usd=kept, error=error)


def new_reservation_id() -> str:
    return uuid.uuid4().hex
