"""The guards: reserve before a paid call, settle after it.

The cap is enforced when a reservation is made. A call whose real cost turns
out higher than its estimate is still recorded in full, so the counters never
lie about spend, and later reservations in the same window see it.

When the block or callable around a provider call raises before settling,
the reservation is released only if the Anthropic or OpenAI SDK reports that
the provider refused the call with an HTTP error, the one failure known not
to be billed. On anything else (a timeout, a dropped connection, a client
that disconnects mid-stream, an exception from your own code) the estimate
stays charged, because the provider may already have billed.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from types import TracebackType
from typing import Any, TypeVar

from pennyguard._core import (
    SETTLED_TTL,
    BudgetStatus,
    Core,
    Event,
    Priced,
    ReservationState,
    check_retries,
    freeze,
    new_reservation_id,
    provider_rejected,
    retry_delay,
)
from pennyguard.budget import Budget
from pennyguard.errors import GuardKilled, GuardUnavailable
from pennyguard.money import Amount, to_micros
from pennyguard.pricing import Pricing
from pennyguard.stores.base import AsyncStore, Counter, ReserveOutcome, SyncStore
from pennyguard.stores.memory import MemoryStore
from pennyguard.usage import Usage

logger = logging.getLogger("pennyguard")

T = TypeVar("T")

DEFAULT_RESERVATION_TTL = 6 * 3_600


def _check_reason(reason: str) -> None:
    if not isinstance(reason, str) or not reason:
        raise ValueError("reason must be a non-empty string")


def _killed_reason(value: str | None) -> str | None:
    # A flag set by hand with an empty value still refuses every call.
    return None if value is None else (value or "killed")


# Writes that outlive a cancelled caller. asyncio keeps only weak references
# to tasks, so these hold them until they finish.
_BACKGROUND: set[asyncio.Task[None]] = set()


def _keep(write: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    task = asyncio.create_task(write)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return task


async def _uncancellable(write: Coroutine[Any, Any, None]) -> None:
    """Await ``write`` without letting a cancellation of the caller stop it.

    An ASGI server cancels the request task when the client disconnects. If
    that lands while a settle is in flight, the counters would keep the
    estimate instead of the real cost, which undercounts any call that cost
    more. The write runs to the end in the background, and the cancellation
    still reaches the caller.
    """
    await asyncio.shield(_keep(write))


async def _compensate(
    store: AsyncStore, op_key: str, counters: Sequence[Counter], reservation_id: str
) -> None:
    # Settling to zero refunds a reservation whose script ran, and leaves a
    # marker that stops one still in flight from charging when it arrives.
    try:
        await store.settle(op_key, counters, 0, SETTLED_TTL)
    except Exception:
        logger.warning("pennyguard.compensation_failed reservation=%s", reservation_id)


# ---------------------------------------------------------------------------
# async
# ---------------------------------------------------------------------------


class _AsyncMemory:
    """``MemoryStore`` behind the async interface. Its operations never block."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def reserve(
        self,
        kill_key: str,
        op_key: str,
        counters: Sequence[Counter],
        amount: int,
        hold_ttl: int,
    ) -> ReserveOutcome:
        return self._store.reserve(kill_key, op_key, counters, amount, hold_ttl)

    async def settle(
        self, op_key: str, counters: Sequence[Counter], target: int, keep_ttl: int
    ) -> None:
        self._store.settle(op_key, counters, target, keep_ttl)

    async def read(self, keys: Sequence[str]) -> list[int]:
        return self._store.read(keys)

    async def set_kill(self, kill_key: str, reason: str | None) -> None:
        self._store.set_kill(kill_key, reason)

    async def get_kill(self, kill_key: str) -> str | None:
        return self._store.get_kill(kill_key)


class Reservation(ReservationState):
    """Money set aside for one call (or one task) until it is settled."""

    def __init__(self, store: AsyncStore, *args: Any) -> None:
        super().__init__(*args)
        self._store = store

    async def settle(
        self,
        cost_usd: Amount | None = None,
        *,
        usage: Usage | Sequence[Usage] | None = None,
    ) -> Decimal:
        """Replace the estimate with the real cost and return it.

        Pass the cost in dollars, or the call's ``Usage`` (one, or one per
        billed attempt) to price it with the guard's pricing.

        Never raises on a backend or pricing failure once the call is paid
        for. The estimate stays charged and an event is emitted instead.
        """
        self._require_open("settle")
        priced = self._price(cost_usd, usage)
        if priced.cost is None:
            return self._keep_estimate(
                priced.error or ValueError("unpriced"), priced.usage
            )
        self._state = "settled"
        await _uncancellable(self._settle_to(to_micros(priced.cost), priced))
        return priced.cost

    async def _settle_to(self, actual: int, priced: Priced) -> None:
        try:
            await self._store.settle(self._op_key, self._counters, actual, SETTLED_TTL)
        except Exception as exc:
            self._store_failed("settle", exc)
            return
        self._settled(actual, priced)

    async def settle_response(self, response: Any) -> Decimal:
        """Settle from an Anthropic or OpenAI response object (or dict).

        Every billed attempt is counted, including the declined attempt of a
        response served by a refusal fallback.
        """
        self._require_open("settle")
        self._core.require_pricing("settle_response")
        try:
            usage = Usage.all_from_response(response)
        except ValueError as exc:
            return self._keep_estimate(exc)
        return await self.settle(usage=usage)

    async def release(self) -> None:
        """Give the whole reservation back: the paid call did not happen."""
        self._require_open("release")
        self._state = "released"
        await _uncancellable(self._release())

    async def _release(self) -> None:
        refunded = self._charged
        try:
            await self._store.settle(self._op_key, self._counters, 0, SETTLED_TTL)
        except Exception as exc:
            self._store_failed("release", exc)
            return
        self._released(refunded)

    async def __aenter__(self) -> Reservation:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._state != "open":
            return
        if exc is not None and provider_rejected(exc):
            await self.release()
        else:
            self._unsettled(exc)


class Guard:
    """Dollar budgets for async code.

    ``store`` is an ``AsyncRedisStore`` in production, or a ``MemoryStore``
    for development and tests. ``reservation_ttl`` is how long an unsettled
    reservation stays settleable; past it, a late settle still records the
    real cost but the estimate is not refunded.
    """

    def __init__(
        self,
        store: AsyncStore | MemoryStore,
        budgets: Sequence[Budget],
        *,
        project: str = "default",
        pricing: Pricing | None = None,
        fail_open: bool = False,
        on_event: Callable[[Event], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        reservation_ttl: int = DEFAULT_RESERVATION_TTL,
    ) -> None:
        if isinstance(store, MemoryStore):
            self._store: AsyncStore = _AsyncMemory(store)
        elif inspect.iscoroutinefunction(getattr(store, "reserve", None)):
            self._store = store
        else:
            raise TypeError(
                "Guard needs an async store such as AsyncRedisStore; "
                "use SyncGuard with RedisStore"
            )
        self._core = Core(
            budgets,
            project=project,
            pricing=pricing,
            fail_open=fail_open,
            on_event=on_event,
            clock=clock,
            reservation_ttl=reservation_ttl,
        )

    @property
    def project(self) -> str:
        return self._core.project

    @property
    def budgets(self) -> tuple[Budget, ...]:
        return self._core.budgets

    @property
    def pricing(self) -> Pricing | None:
        return self._core.pricing

    def cost(self, usage: Usage) -> Decimal:
        """Price ``usage`` with this guard's pricing."""
        return self._core.cost(usage)

    def estimate(
        self,
        model: str,
        *,
        input_tokens: int,
        max_output_tokens: int,
        tier: str | None = None,
        audio: bool = False,
    ) -> Decimal:
        """Worst-case cost of one call, to pass as ``estimate_usd``.

        Pass ``tier="fast"`` (or another variant) when the call runs at that
        rate, and ``audio=True`` when it sends or returns audio.
        """
        return self._core.estimate(
            model,
            input_tokens=input_tokens,
            max_output_tokens=max_output_tokens,
            tier=tier,
            audio=audio,
        )

    async def reserve(
        self,
        estimate_usd: Amount,
        *,
        tags: Mapping[str, str] | None = None,
        **scope: str,
    ) -> Reservation:
        """Set ``estimate_usd`` aside against every applicable budget.

        Raises ``BudgetExceeded`` if any budget lacks room, ``GuardKilled``
        if the kill switch is on, and ``GuardUnavailable`` if the backend
        cannot be read (unless ``fail_open``). A refusal charges nothing.

        ``scope`` names the value of each budget dimension, such as
        ``tenant="acme"``. ``tags`` ride along to events only.
        """
        core = self._core
        estimate, amount = core.estimate_micros(estimate_usd)
        plan = core.plan(scope)
        frozen_scope, frozen_tags = freeze(scope, "scope"), freeze(tags, "tags")
        rid = new_reservation_id()
        op_key = core.op_key(rid)

        def reservation(charged: int) -> Reservation:
            return Reservation(
                self._store,
                core,
                rid,
                plan.counters,
                charged,
                estimate,
                frozen_scope,
                frozen_tags,
            )

        try:
            outcome = await self._store.reserve(
                core.kill_key, op_key, plan.counters, amount, core.reservation_ttl
            )
        except asyncio.CancelledError:
            # The caller is gone, so no call follows, but the script may
            # already have charged.
            _keep(_compensate(self._store, op_key, plan.counters, rid))
            raise
        except Exception as exc:
            if core.fail_open:
                logger.warning("pennyguard.fail_open error=%s", exc)
                core.emit(
                    "unchecked",
                    scope=frozen_scope,
                    tags=frozen_tags,
                    amount_usd=estimate,
                    error=exc,
                )
                return reservation(0)
            core.emit("unavailable", scope=frozen_scope, tags=frozen_tags, error=exc)
            # The script may have run with only the reply lost.
            await _uncancellable(_compensate(self._store, op_key, plan.counters, rid))
            raise GuardUnavailable("budget backend unavailable") from exc
        if outcome.status == "killed":
            reason = _killed_reason(outcome.kill_reason) or "killed"
            core.emit("killed", scope=frozen_scope, tags=frozen_tags)
            raise GuardKilled(reason)
        if outcome.status == "refused":
            error = core.refusal(plan, outcome, estimate)
            core.emit(
                "refused",
                scope=frozen_scope,
                tags=frozen_tags,
                amount_usd=estimate,
                budget=error.budget,
            )
            raise error
        held = reservation(outcome.charged)
        held._event("reserved", amount_usd=estimate)
        return held

    async def call(
        self,
        fn: Callable[[], Awaitable[T]],
        *,
        estimate_usd: Amount,
        tags: Mapping[str, str] | None = None,
        retries: int = 0,
        **scope: str,
    ) -> T:
        """Reserve, run ``fn``, settle from its response, return the response.

        If ``fn`` raises the Anthropic or OpenAI SDK's error for an HTTP
        error status, on the SDK's first attempt, the reservation is
        released. If it raises anything else, the estimate stays charged.
        The response must carry ``usage`` the way the Anthropic and OpenAI
        SDKs return it; for streams, use ``reserve``.

        ``retries`` retries what the SDKs would (408, 409, 429, 5xx,
        connection errors and timeouts), each attempt under a reservation
        of its own: a rejected attempt is released and a timed-out one keeps
        its estimate. Give the client ``max_retries=0`` so the SDK does not
        retry underneath. A retry that the budget no longer has room for
        raises ``BudgetExceeded``.
        """
        self._core.require_pricing("call()")
        check_retries(retries)
        attempt = 0
        while True:
            reservation = await self.reserve(estimate_usd, tags=tags, **scope)
            try:
                response = await fn()
            except BaseException as exc:
                if provider_rejected(exc):
                    await reservation.release()
                else:
                    reservation._unsettled(exc)
                delay = retry_delay(exc, attempt) if attempt < retries else None
                if delay is None:
                    raise
                attempt += 1
                logger.info("pennyguard.retry attempt=%d delay=%.2f", attempt, delay)
                await asyncio.sleep(delay)
                continue
            await reservation.settle_response(response)
            return response

    async def status(self, **scope: str) -> list[BudgetStatus]:
        """Spend and room left for the global budgets, plus every per-dimension
        budget whose value is given in ``scope``."""
        plan = self._core.plan(scope, partial=True)
        try:
            values = await self._store.read([c.key for c in plan.counters])
        except Exception as exc:
            raise GuardUnavailable("budget backend unavailable") from exc
        return self._core.statuses(plan, values)

    async def kill(self, reason: str) -> None:
        """Refuse every reservation in this project, on every process, now."""
        _check_reason(reason)
        try:
            await self._store.set_kill(self._core.kill_key, reason)
        except Exception as exc:
            raise GuardUnavailable("budget backend unavailable") from exc

    async def resume(self) -> None:
        """Clear the kill switch."""
        try:
            await self._store.set_kill(self._core.kill_key, None)
        except Exception as exc:
            raise GuardUnavailable("budget backend unavailable") from exc

    async def killed(self) -> str | None:
        """The kill switch reason, or ``None`` when spending is allowed."""
        try:
            return _killed_reason(await self._store.get_kill(self._core.kill_key))
        except Exception as exc:
            raise GuardUnavailable("budget backend unavailable") from exc


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


class SyncReservation(ReservationState):
    """The synchronous twin of ``Reservation``."""

    def __init__(self, store: SyncStore, *args: Any) -> None:
        super().__init__(*args)
        self._store = store

    def settle(
        self,
        cost_usd: Amount | None = None,
        *,
        usage: Usage | Sequence[Usage] | None = None,
    ) -> Decimal:
        """See ``Reservation.settle``."""
        self._require_open("settle")
        priced = self._price(cost_usd, usage)
        if priced.cost is None:
            return self._keep_estimate(
                priced.error or ValueError("unpriced"), priced.usage
            )
        actual = to_micros(priced.cost)
        self._state = "settled"
        try:
            self._store.settle(self._op_key, self._counters, actual, SETTLED_TTL)
        except Exception as exc:
            self._store_failed("settle", exc)
            return priced.cost
        self._settled(actual, priced)
        return priced.cost

    def settle_response(self, response: Any) -> Decimal:
        """See ``Reservation.settle_response``."""
        self._require_open("settle")
        self._core.require_pricing("settle_response")
        try:
            usage = Usage.all_from_response(response)
        except ValueError as exc:
            return self._keep_estimate(exc)
        return self.settle(usage=usage)

    def release(self) -> None:
        """See ``Reservation.release``."""
        self._require_open("release")
        self._state = "released"
        refunded = self._charged
        try:
            self._store.settle(self._op_key, self._counters, 0, SETTLED_TTL)
        except Exception as exc:
            self._store_failed("release", exc)
            return
        self._released(refunded)

    def __enter__(self) -> SyncReservation:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._state != "open":
            return
        if exc is not None and provider_rejected(exc):
            self.release()
        else:
            self._unsettled(exc)


class SyncGuard:
    """Dollar budgets for synchronous code. Same behavior as ``Guard``.

    ``store`` is a ``RedisStore`` in production, or a ``MemoryStore``.
    """

    def __init__(
        self,
        store: SyncStore,
        budgets: Sequence[Budget],
        *,
        project: str = "default",
        pricing: Pricing | None = None,
        fail_open: bool = False,
        on_event: Callable[[Event], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        reservation_ttl: int = DEFAULT_RESERVATION_TTL,
    ) -> None:
        if inspect.iscoroutinefunction(getattr(store, "reserve", None)):
            raise TypeError(
                "SyncGuard needs a sync store such as RedisStore; "
                "use Guard with AsyncRedisStore"
            )
        self._store = store
        self._core = Core(
            budgets,
            project=project,
            pricing=pricing,
            fail_open=fail_open,
            on_event=on_event,
            clock=clock,
            reservation_ttl=reservation_ttl,
        )

    @property
    def project(self) -> str:
        return self._core.project

    @property
    def budgets(self) -> tuple[Budget, ...]:
        return self._core.budgets

    @property
    def pricing(self) -> Pricing | None:
        return self._core.pricing

    def cost(self, usage: Usage) -> Decimal:
        """See ``Guard.cost``."""
        return self._core.cost(usage)

    def estimate(
        self,
        model: str,
        *,
        input_tokens: int,
        max_output_tokens: int,
        tier: str | None = None,
        audio: bool = False,
    ) -> Decimal:
        """See ``Guard.estimate``."""
        return self._core.estimate(
            model,
            input_tokens=input_tokens,
            max_output_tokens=max_output_tokens,
            tier=tier,
            audio=audio,
        )

    def reserve(
        self,
        estimate_usd: Amount,
        *,
        tags: Mapping[str, str] | None = None,
        **scope: str,
    ) -> SyncReservation:
        """See ``Guard.reserve``."""
        core = self._core
        estimate, amount = core.estimate_micros(estimate_usd)
        plan = core.plan(scope)
        frozen_scope, frozen_tags = freeze(scope, "scope"), freeze(tags, "tags")
        rid = new_reservation_id()
        op_key = core.op_key(rid)

        def reservation(charged: int) -> SyncReservation:
            return SyncReservation(
                self._store,
                core,
                rid,
                plan.counters,
                charged,
                estimate,
                frozen_scope,
                frozen_tags,
            )

        try:
            outcome = self._store.reserve(
                core.kill_key, op_key, plan.counters, amount, core.reservation_ttl
            )
        except Exception as exc:
            if core.fail_open:
                logger.warning("pennyguard.fail_open error=%s", exc)
                core.emit(
                    "unchecked",
                    scope=frozen_scope,
                    tags=frozen_tags,
                    amount_usd=estimate,
                    error=exc,
                )
                return reservation(0)
            core.emit("unavailable", scope=frozen_scope, tags=frozen_tags, error=exc)
            try:
                self._store.settle(op_key, plan.counters, 0, SETTLED_TTL)
            except Exception:
                logger.warning("pennyguard.compensation_failed reservation=%s", rid)
            raise GuardUnavailable("budget backend unavailable") from exc
        if outcome.status == "killed":
            reason = _killed_reason(outcome.kill_reason) or "killed"
            core.emit("killed", scope=frozen_scope, tags=frozen_tags)
            raise GuardKilled(reason)
        if outcome.status == "refused":
            error = core.refusal(plan, outcome, estimate)
            core.emit(
                "refused",
                scope=frozen_scope,
                tags=frozen_tags,
                amount_usd=estimate,
                budget=error.budget,
            )
            raise error
        held = reservation(outcome.charged)
        held._event("reserved", amount_usd=estimate)
        return held

    def call(
        self,
        fn: Callable[[], T],
        *,
        estimate_usd: Amount,
        tags: Mapping[str, str] | None = None,
        retries: int = 0,
        **scope: str,
    ) -> T:
        """See ``Guard.call``."""
        self._core.require_pricing("call()")
        check_retries(retries)
        attempt = 0
        while True:
            reservation = self.reserve(estimate_usd, tags=tags, **scope)
            try:
                response = fn()
            except BaseException as exc:
                if provider_rejected(exc):
                    reservation.release()
                else:
                    reservation._unsettled(exc)
                delay = retry_delay(exc, attempt) if attempt < retries else None
                if delay is None:
                    raise
                attempt += 1
                logger.info("pennyguard.retry attempt=%d delay=%.2f", attempt, delay)
                time.sleep(delay)
                continue
            reservation.settle_response(response)
            return response

    def status(self, **scope: str) -> list[BudgetStatus]:
        """See ``Guard.status``."""
        plan = self._core.plan(scope, partial=True)
        try:
            values = self._store.read([c.key for c in plan.counters])
        except Exception as exc:
            raise GuardUnavailable("budget backend unavailable") from exc
        return self._core.statuses(plan, values)

    def kill(self, reason: str) -> None:
        """See ``Guard.kill``."""
        _check_reason(reason)
        try:
            self._store.set_kill(self._core.kill_key, reason)
        except Exception as exc:
            raise GuardUnavailable("budget backend unavailable") from exc

    def resume(self) -> None:
        """See ``Guard.resume``."""
        try:
            self._store.set_kill(self._core.kill_key, None)
        except Exception as exc:
            raise GuardUnavailable("budget backend unavailable") from exc

    def killed(self) -> str | None:
        """See ``Guard.killed``."""
        try:
            return _killed_reason(self._store.get_kill(self._core.kill_key))
        except Exception as exc:
            raise GuardUnavailable("budget backend unavailable") from exc
