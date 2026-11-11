"""Guard behavior, run against MemoryStore with a controlled clock."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Sequence
from decimal import Decimal
from types import SimpleNamespace as NS
from typing import Any

import pytest

from pennyguard import (
    Budget,
    BudgetExceeded,
    Event,
    Guard,
    GuardKilled,
    GuardUnavailable,
    MemoryStore,
    Pricing,
    SyncGuard,
    Usage,
)
from pennyguard.stores.base import Counter, ReserveOutcome
from tests.conftest import Clock

D = Decimal


def make(
    store: MemoryStore,
    clock: Clock,
    events: list[Event],
    *budgets: Budget,
    **options: Any,
) -> SyncGuard:
    return SyncGuard(
        store,
        budgets or [Budget("global", 10)],
        project="test",
        clock=clock,
        on_event=events.append,
        **options,
    )


def spent(guard: SyncGuard, **scope: str) -> dict[str, Decimal]:
    return {
        (s.budget if s.scope is None else f"{s.budget}:{s.scope}"): s.spent_usd
        for s in guard.status(**scope)
    }


def kinds(events: list[Event]) -> list[str]:
    return [e.kind for e in events]


def anthropic_response(model: str = "model-a", input_tokens: int = 1_000) -> NS:
    return NS(model=model, usage=NS(input_tokens=input_tokens, output_tokens=200))


class TestReserveAndSettle:
    def test_settle_below_the_estimate_refunds_the_difference(
        self, store: MemoryStore, clock: Clock, events: list[Event]
    ) -> None:
        guard = make(store, clock, events)
        reservation = guard.reserve(2)
        assert spent(guard) == {"global": D(2)}
        assert reservation.settle("0.5") == D("0.5")
        assert spent(guard) == {"global": D("0.5")}
        assert reservation.state == "settled"
        assert reservation.charged_usd == D("0.5")
        assert kinds(events) == ["reserved", "settled"]

    def test_settle_above_the_estimate_records_the_overshoot(
        self, store: MemoryStore, clock: Clock, events: list[Event]
    ) -> None:
        guard = make(store, clock, events)
        guard.reserve(9).settle(12)
        assert spent(guard) == {"global": D(12)}
        with pytest.raises(BudgetExceeded):
            guard.reserve(0)

    def test_refusal_charges_nothing_and_explains_itself(
        self, store: MemoryStore, clock: Clock, events: list[Event]
    ) -> None:
        guard = make(store, clock, events)
        guard.reserve(8)
        with pytest.raises(BudgetExceeded) as caught:
            guard.reserve(3)
        error = caught.value
        assert (error.budget, error.scope) == ("global", None)
        assert (error.spent_usd, error.limit_usd, error.requested_usd) == (
            D(8),
            D(10),
            D(3),
        )
        # 15:30 UTC: the day budget resets in eight and a half hours.
        assert error.retry_after == 8 * 3600 + 30 * 60
        assert spent(guard) == {"global": D(8)}
        assert events[-1].kind == "refused"
        assert events[-1].budget == "global"
        guard.reserve(2)  # exactly the room left is allowed

    def test_a_spent_budget_refuses_even_a_zero_estimate(
        self, store: MemoryStore, clock: Clock, events: list[Event]
    ) -> None:
        guard = make(store, clock, events)
        guard.reserve(10)
        with pytest.raises(BudgetExceeded):
            guard.reserve(0)

    def test_estimate_must_not_be_negative(
        self, store: MemoryStore, clock: Clock, events: list[Event]
    ) -> None:
        guard = make(store, clock, events)
        with pytest.raises(ValueError):
            guard.reserve(-1)
        reservation = guard.reserve(1)
        with pytest.raises(ValueError):
            reservation.settle(-1)
        with pytest.raises(ValueError):
            reservation.settle()
        with pytest.raises(ValueError):
            reservation.settle(1, usage=Usage("model-a"))

    def test_release_gives_everything_back(
        self, store: MemoryStore, clock: Clock, events: list[Event]
    ) -> None:
        guard = make(store, clock, events)
        reservation = guard.reserve(4)
        reservation.release()
        assert spent(guard) == {"global": D(0)}
        assert reservation.state == "released"
        assert events[-1].kind == "released"
        assert events[-1].amount_usd == D(4)

    def test_a_reservation_closes_once(
        self, store: MemoryStore, clock: Clock, events: list[Event]
    ) -> None:
        guard = make(store, clock, events)
        settled = guard.reserve(1)
        settled.settle(1)
        with pytest.raises(RuntimeError):
            settled.settle(1)
        with pytest.raises(RuntimeError):
            settled.release()
        released = guard.reserve(1)
        released.release()
        with pytest.raises(RuntimeError):
            released.settle(1)


class TestBudgets:
    def test_per_tenant_budgets_are_separate_accounts(
        self, store: MemoryStore, clock: Clock, events: list[Event]
    ) -> None:
        guard = make(
            store,
            clock,
            events,
            Budget("global", 100),
            Budget("tenant", 5, per="tenant"),
        )
        guard.reserve(5, tenant="acme")
        with pytest.raises(BudgetExceeded) as caught:
            guard.reserve(1, tenant="acme")
        assert (caught.value.budget, caught.value.scope) == ("tenant", "acme")
        guard.reserve(5, tenant="globex")
        assert spent(guard, tenant="acme") == {"global": D(10), "tenant:acme": D(5)}

    def test_refusal_by_one_budget_charges_none_of_them(
        self, store: MemoryStore, clock: Clock, events: list[Event]
    ) -> None:
        guard = make(
            store,
            clock,
            events,
            Budget("global", 100),
            Budget("tenant", 5, per="tenant"),
            Budget("user", 1, "hour", per="user"),
        )
        with pytest.raises(BudgetExceeded) as caught:
            guard.reserve(2, tenant="acme", user="u1")
        assert caught.value.budget == "user"
        assert caught.value.retry_after == 30 * 60
        assert spent(guard, tenant="acme", user="u1") == {
            "global": D(0),
            "tenant:acme": D(0),
            "user:u1": D(0),
        }

    def test_windows_reset_at_utc_boundaries(
        self, store: MemoryStore, clock: Clock, events: list[Event]
    ) -> None:
        guard = make(
            store, clock, events, Budget("daily", 1), Budget("monthly", 3, "month")
        )
        guard.reserve(1)
        with pytest.raises(BudgetExceeded):
            guard.reserve(1)
        clock.advance(hours=9)  # past midnight UTC
        guard.reserve(1)
        clock.advance(days=1)
        guard.reserve(1)
        clock.advance(days=1)
        with pytest.raises(BudgetExceeded) as caught:
            guard.reserve(1)
        assert caught.value.budget == "monthly"

    def test_a_late_settle_lands_on_the_window_it_reserved(
        self, store: MemoryStore, clock: Clock, events: list[Event]
    ) -> None:
        guard = make(store, clock, events, Budget("daily", 10))
        clock.advance(hours=8, minutes=29, seconds=59)  # 23:59:59
        reservation = guard.reserve(6)
        clock.advance(seconds=2)  # the next day
        guard.reserve(1)
        reservation.settle(1)
        # Today's counter only has today's call; yesterday's got its refund.
        assert spent(guard) == {"daily": D(1)}
        clock.advance(days=-1)
        assert spent(guard) == {"daily": D(1)}

    def test_counters_expire_in_the_memory_store(
        self, store: MemoryStore, clock: Clock, events: list[Event]
    ) -> None:
        guard = make(store, clock, events, Budget("hourly", 1, "hour"))
        guard.reserve(1)
        key = guard._core.plan({}).counters[0].key
        clock.advance(hours=1)
        assert store.read([key]) == [1_000_000]
        clock.advance(hours=1)
        assert store.read([key]) == [0]
        assert key not in store._values


class TestScopeValidation:
    def test_a_missing_dimension_is_an_error(
        self, store: MemoryStore, clock: Clock, events: list[Event]
    ) -> None:
        guard = make(store, clock, events, Budget("tenant", 5, per="tenant"))
        with pytest.raises(ValueError, match="pass tenant="):
            guard.reserve(1)

    def test_an_unknown_dimension_is_an_error(
        self, store: MemoryStore, clock: Clock, events: list[Event]
    ) -> None:
        guard = make(store, clock, events, Budget("tenant", 5, per="tenant"))
        with pytest.raises(ValueError, match="tennant"):
            guard.reserve(1, tennant="acme")

    def test_scope_values_must_be_non_empty_strings(
        self, store: MemoryStore, clock: Clock, events: list[Event]
    ) -> None:
        guard = make(store, clock, events, Budget("tenant", 5, per="tenant"))
        with pytest.raises(ValueError):
            guard.reserve(1, tenant="")
        with pytest.raises(ValueError):
            guard.reserve(1, tenant=42)  # type: ignore[arg-type]

    def test_guard_configuration(self, store: MemoryStore, clock: Clock) -> None:
        with pytest.raises(ValueError):
            SyncGuard(store, [])
        with pytest.raises(ValueError):
            SyncGuard(store, [Budget("a", 1), Budget("a", 2)])
        with pytest.raises(ValueError):
            SyncGuard(store, [Budget("a", 1)], project="")

    def test_naive_clock_is_rejected(self, store: MemoryStore) -> None:
        from datetime import datetime

        guard = SyncGuard(store, [Budget("a", 1)], clock=datetime.now)
        with pytest.raises(ValueError, match="timezone-aware"):
            guard.reserve(1)


class ProviderError(Exception):
    """Shaped like the Anthropic and OpenAI SDKs' status errors."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class HttpxStyleError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.response = NS(status_code=status_code)


class TestContextManager:
    @pytest.mark.parametrize(
        "error", [ProviderError(529), ProviderError(400), HttpxStyleError(500)]
    )
    def test_an_http_error_from_the_provider_releases(
        self, store: MemoryStore, clock: Clock, events: list[Event], error: Exception
    ) -> None:
        guard = make(store, clock, events)
        with pytest.raises(type(error)), guard.reserve(3):
            raise error
        assert spent(guard) == {"global": D(0)}
        assert events[-1].kind == "released"

    @pytest.mark.parametrize(
        "error", [TimeoutError(), ConnectionResetError(), KeyError("bug")]
    )
    def test_any_other_exception_keeps_the_estimate(
        self, store: MemoryStore, clock: Clock, events: list[Event], error: Exception
    ) -> None:
        # The provider may already have billed: a timeout after the request
        # was sent, a stream cut halfway, a bug after the reply.
        guard = make(store, clock, events)
        with pytest.raises(type(error)), guard.reserve(3):
            raise error
        assert spent(guard) == {"global": D(3)}
        assert events[-1].kind == "unsettled"
        assert events[-1].error is error

    def test_a_client_that_disconnects_mid_stream_still_pays(
        self, store: MemoryStore, clock: Clock, events: list[Event]
    ) -> None:
        guard = make(store, clock, events)

        def stream() -> Iterator[str]:
            with guard.reserve(3) as reservation:
                yield from ("a", "b", "c")
                reservation.settle(1)

        for _ in range(3):
            tokens = stream()
            next(tokens)
            tokens.close()  # GeneratorExit inside the block
        assert spent(guard) == {"global": D(9)}

    def test_cancellation_keeps_the_estimate_without_io(
        self, store: MemoryStore, clock: Clock, events: list[Event]
    ) -> None:
        guard = Guard(store, [Budget("global", 10)], clock=clock)

        async def scenario() -> None:
            async def handler() -> None:
                async with await guard.reserve(3):
                    await asyncio.sleep(10)

            task = asyncio.create_task(handler())
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert (await guard.status())[0].spent_usd == D(3)

        asyncio.run(scenario())

    def test_an_exception_after_settle_keeps_the_charge(
        self, store: MemoryStore, clock: Clock, events: list[Event]
    ) -> None:
        guard = make(store, clock, events)

        def handler() -> None:
            with guard.reserve(3) as reservation:
                reservation.settle(1)
                raise KeyError("bug in post-processing")

        with pytest.raises(KeyError):
            handler()
        assert spent(guard) == {"global": D(1)}

    def test_leaving_without_settle_keeps_the_estimate(
        self, store: MemoryStore, clock: Clock, events: list[Event]
    ) -> None:
        guard = make(store, clock, events)
        with guard.reserve(3):
            pass
        assert spent(guard) == {"global": D(3)}
        assert events[-1].kind == "unsettled"
        assert events[-1].amount_usd == D(3)


class TestPricing:
    def test_settle_with_usage(
        self, store: MemoryStore, clock: Clock, events: list[Event], pricing: Pricing
    ) -> None:
        guard = make(store, clock, events, pricing=pricing)
        usage = Usage("model-a", input_tokens=100_000, output_tokens=20_000)
        cost = guard.reserve(1).settle(usage=usage)
        assert cost == D("0.2")
        assert events[-1].usage == (usage,)
        assert events[-1].usage_costs == (D("0.2"),)
        assert guard.cost(usage) == cost

    def test_a_fallback_response_bills_every_attempt_at_its_own_price(
        self, store: MemoryStore, clock: Clock, events: list[Event], pricing: Pricing
    ) -> None:
        # Shape from Anthropic's refusal-fallback docs: the top-level usage is
        # only the attempt that answered; iterations hold every attempt.
        response = {
            "model": "model-a",
            "usage": {
                "input_tokens": 1_000,
                "output_tokens": 1_000,
                "iterations": [
                    {
                        "type": "message",
                        "model": "model-b",
                        "input_tokens": 1_000,
                        "output_tokens": 100,
                    },
                    {
                        "type": "fallback_message",
                        "model": "model-a",
                        "input_tokens": 1_000,
                        "output_tokens": 1_000,
                    },
                ],
            },
        }
        guard = make(store, clock, events, pricing=pricing)
        cost = guard.reserve(1).settle_response(response)
        # model-b: 1000 * $10 + 100 * $50; model-a: 1000 * $1 + 1000 * $5.
        assert cost == D("0.021")
        assert spent(guard) == {"global": D("0.021")}
        assert [u.model for u in events[-1].usage] == ["model-b", "model-a"]
        assert events[-1].usage_costs == (D("0.015"), D("0.006"))

    def test_an_unknown_model_keeps_the_estimate_instead_of_raising(
        self, store: MemoryStore, clock: Clock, events: list[Event], pricing: Pricing
    ) -> None:
        guard = make(store, clock, events, pricing=pricing)
        kept = guard.reserve(2).settle(usage=Usage("brand-new-model", 10, 10))
        assert kept == D(2)
        assert spent(guard) == {"global": D(2)}
        assert events[-1].kind == "pricing_failed"

    def test_an_unpriced_fast_mode_call_keeps_the_estimate(
        self, store: MemoryStore, clock: Clock, events: list[Event], pricing: Pricing
    ) -> None:
        guard = make(store, clock, events, pricing=pricing)
        response = {
            "model": "model-a",
            "usage": {"input_tokens": 10, "output_tokens": 10, "speed": "fast"},
        }
        assert guard.reserve(2).settle_response(response) == D(2)
        assert events[-1].kind == "pricing_failed"

    def test_an_unreadable_response_keeps_the_estimate(
        self, store: MemoryStore, clock: Clock, events: list[Event], pricing: Pricing
    ) -> None:
        guard = make(store, clock, events, pricing=pricing)
        assert guard.reserve(2).settle_response({"text": "no usage"}) == D(2)
        assert events[-1].kind == "pricing_failed"

    def test_usage_without_pricing_is_a_configuration_error(
        self, store: MemoryStore, clock: Clock, events: list[Event]
    ) -> None:
        guard = make(store, clock, events)
        with pytest.raises(ValueError, match="pricing"):
            guard.reserve(1).settle(usage=Usage("model-a"))
        with pytest.raises(ValueError, match="pricing"):
            guard.estimate("model-a", input_tokens=1, max_output_tokens=1)

    def test_estimate(
        self, store: MemoryStore, clock: Clock, events: list[Event], pricing: Pricing
    ) -> None:
        guard = make(store, clock, events, pricing=pricing)
        estimate = guard.estimate(
            "model-a", input_tokens=10_000, max_output_tokens=1_000
        )
        # 10,000 input at the one-hour cache-write default (2 x $1),
        # 1,000 output at $5.
        assert estimate == D("0.025")


class TestCall:
    def test_settles_from_the_response(
        self, store: MemoryStore, clock: Clock, events: list[Event], pricing: Pricing
    ) -> None:
        guard = make(store, clock, events, pricing=pricing)
        response = guard.call(
            lambda: anthropic_response(), estimate_usd=1, tags={"feature": "chat"}
        )
        assert response.model == "model-a"
        assert spent(guard) == {"global": D("0.002")}
        assert events[-1].kind == "settled"
        assert events[-1].tags == {"feature": "chat"}

    def test_releases_when_the_provider_answers_with_an_error(
        self, store: MemoryStore, clock: Clock, events: list[Event], pricing: Pricing
    ) -> None:
        guard = make(store, clock, events, pricing=pricing)

        def overloaded() -> None:
            raise ProviderError(529)

        with pytest.raises(ProviderError):
            guard.call(overloaded, estimate_usd=1)
        assert spent(guard) == {"global": D(0)}

    def test_keeps_the_estimate_when_the_call_may_have_been_billed(
        self, store: MemoryStore, clock: Clock, events: list[Event], pricing: Pricing
    ) -> None:
        guard = make(store, clock, events, pricing=pricing)

        def timed_out() -> None:
            raise TimeoutError

        with pytest.raises(TimeoutError):
            guard.call(timed_out, estimate_usd=1)
        assert spent(guard) == {"global": D(1)}
        assert events[-1].kind == "unsettled"

    def test_needs_pricing_before_the_call_runs(
        self, store: MemoryStore, clock: Clock, events: list[Event]
    ) -> None:
        guard = make(store, clock, events)
        ran = []
        with pytest.raises(ValueError, match="pricing"):
            guard.call(lambda: ran.append(1), estimate_usd=1)
        assert ran == []
        assert spent(guard) == {"global": D(0)}

    def test_refused_calls_never_run(
        self, store: MemoryStore, clock: Clock, events: list[Event], pricing: Pricing
    ) -> None:
        guard = make(store, clock, events, pricing=pricing)
        ran = []
        with pytest.raises(BudgetExceeded):
            guard.call(lambda: ran.append(1), estimate_usd=11)
        assert ran == []


class TestKillSwitch:
    def test_kill_refuses_everything_until_resume(
        self, store: MemoryStore, clock: Clock, events: list[Event]
    ) -> None:
        guard = make(store, clock, events)
        assert guard.killed() is None
        guard.kill("runaway agent")
        assert guard.killed() == "runaway agent"
        with pytest.raises(GuardKilled) as caught:
            guard.reserve(0)
        assert caught.value.reason == "runaway agent"
        assert events[-1].kind == "killed"
        guard.resume()
        guard.reserve(1)

    def test_kill_reaches_other_guards_on_the_same_store(
        self, store: MemoryStore, clock: Clock, events: list[Event]
    ) -> None:
        make(store, clock, events).kill("stop")
        with pytest.raises(GuardKilled):
            make(store, clock, events).reserve(1)
        with pytest.raises(ValueError):
            make(store, clock, events).kill("")

    def test_a_flag_set_by_hand_without_a_reason_still_reads_as_killed(
        self, store: MemoryStore, clock: Clock, events: list[Event]
    ) -> None:
        guard = make(store, clock, events)
        store.set_kill("pg:{test}:kill", "")
        assert guard.killed() == "killed"
        with pytest.raises(GuardKilled, match="killed"):
            guard.reserve(1)


class BrokenStore:
    """A store whose backend is down, or whose replies get lost.

    ``lose_reserve_reply`` runs the reservation and then raises, the way a
    client sees a timeout that arrives after the server already ran the
    script.
    """

    def __init__(
        self,
        fail_settle: bool = True,
        fail_reserve: bool = True,
        lose_reserve_reply: bool = False,
    ) -> None:
        self.inner = MemoryStore()
        self.fail_settle = fail_settle
        self.fail_reserve = fail_reserve
        self.lose_reserve_reply = lose_reserve_reply

    def reserve(
        self,
        kill_key: str,
        op_key: str,
        counters: Sequence[Counter],
        amount: int,
        hold_ttl: int,
    ) -> ReserveOutcome:
        if self.lose_reserve_reply:
            self.inner.reserve(kill_key, op_key, counters, amount, hold_ttl)
            raise TimeoutError("reply lost")
        if self.fail_reserve:
            raise ConnectionError("redis down")
        return self.inner.reserve(kill_key, op_key, counters, amount, hold_ttl)

    def settle(
        self, op_key: str, counters: Sequence[Counter], target: int, keep_ttl: int
    ) -> None:
        if self.fail_settle:
            raise ConnectionError("redis down")
        self.inner.settle(op_key, counters, target, keep_ttl)

    def read(self, keys: Sequence[str]) -> list[int]:
        raise ConnectionError("redis down")

    def set_kill(self, kill_key: str, reason: str | None) -> None:
        raise ConnectionError("redis down")

    def get_kill(self, kill_key: str) -> str | None:
        raise ConnectionError("redis down")


class TestBackendFailure:
    def test_an_unreadable_budget_refuses(
        self, clock: Clock, events: list[Event]
    ) -> None:
        guard = SyncGuard(
            BrokenStore(), [Budget("g", 1)], clock=clock, on_event=events.append
        )
        with pytest.raises(GuardUnavailable):
            guard.reserve(1)
        assert kinds(events) == ["unavailable"]
        for read in (guard.status, guard.killed, guard.resume):
            with pytest.raises(GuardUnavailable):
                read()
        with pytest.raises(GuardUnavailable):
            guard.kill("x")

    def test_fail_open_allows_and_records_the_full_cost_later(
        self, clock: Clock, events: list[Event]
    ) -> None:
        store = BrokenStore(fail_settle=False)
        guard = SyncGuard(store, [Budget("g", 1)], clock=clock, fail_open=True)
        reservation = guard.reserve(1)
        assert reservation.charged_usd == D(0)
        reservation.settle("0.4")
        store.fail_reserve = False
        with pytest.raises(BudgetExceeded):
            guard.reserve("0.7")

    def test_a_lost_reply_is_refunded_when_the_call_is_refused(
        self, clock: Clock, events: list[Event]
    ) -> None:
        store = BrokenStore(fail_settle=False, lose_reserve_reply=True)
        guard = SyncGuard(store, [Budget("g", 5)], clock=clock)
        with pytest.raises(GuardUnavailable):
            guard.reserve(1)
        assert store.inner.read([guard._core.plan({}).counters[0].key]) == [0]

    def test_a_lost_reply_with_fail_open_settles_to_the_real_cost(
        self, clock: Clock, events: list[Event]
    ) -> None:
        # The script charged $1 but the client never learned it. Settling
        # must land on the real cost, not on the cost plus the lost $1.
        store = BrokenStore(fail_settle=False, lose_reserve_reply=True)
        guard = SyncGuard(store, [Budget("g", 5)], clock=clock, fail_open=True)
        guard.reserve(1).settle("0.2")
        store.lose_reserve_reply = False
        store.fail_reserve = False
        key = guard._core.plan({}).counters[0].key
        assert store.inner.read([key]) == [200_000]

    def test_settle_and_release_never_raise_on_backend_errors(
        self, clock: Clock, events: list[Event]
    ) -> None:
        store = BrokenStore(fail_reserve=False)
        guard = SyncGuard(store, [Budget("g", 5)], clock=clock, on_event=events.append)
        guard.reserve(1).settle(2)
        guard.reserve(1).release()
        assert kinds(events) == ["reserved", "store_error", "reserved", "store_error"]

    def test_a_failing_event_hook_is_contained(
        self, store: MemoryStore, clock: Clock
    ) -> None:
        def hook(event: Event) -> None:
            raise RuntimeError("telemetry broke")

        guard = SyncGuard(store, [Budget("g", 5)], clock=clock, on_event=hook)
        guard.reserve(1).settle(1)


class TestAsyncGuard:
    def test_the_same_flow_async(
        self, store: MemoryStore, clock: Clock, events: list[Event], pricing: Pricing
    ) -> None:
        guard = Guard(
            store,
            [Budget("global", 10), Budget("tenant", 3, per="tenant")],
            clock=clock,
            pricing=pricing,
            on_event=events.append,
        )

        async def scenario() -> None:
            reservation = await guard.reserve(2, tenant="acme")
            assert await reservation.settle(1) == D(1)
            with pytest.raises(BudgetExceeded):
                await guard.reserve(3, tenant="acme")
            with pytest.raises(ProviderError):
                async with await guard.reserve(1, tenant="acme"):
                    raise ProviderError(503)
            async with await guard.reserve(1, tenant="acme") as held:
                await held.settle_response(anthropic_response())

            async def model_call() -> NS:
                return anthropic_response()

            await guard.call(model_call, estimate_usd=1, tenant="acme")

            async def failing_call() -> NS:
                raise ProviderError(500)

            with pytest.raises(ProviderError):
                await guard.call(failing_call, estimate_usd=1, tenant="acme")
            statuses = {s.budget: s for s in await guard.status(tenant="acme")}
            assert statuses["tenant"].spent_usd == D("1.004")
            assert statuses["tenant"].remaining_usd == D("1.996")
            await guard.kill("stop")
            assert await guard.killed() == "stop"
            with pytest.raises(GuardKilled):
                await guard.reserve(0, tenant="acme")
            await guard.resume()
            unsettled = await guard.reserve(1, tenant="acme")
            async with unsettled:
                pass
            assert unsettled.state == "settled"
            # 2.004 spent: the unsettled reservation kept its estimate.
            await (await guard.reserve("0.5", tenant="acme")).release()

        asyncio.run(scenario())
        assert "released" in kinds(events)
        assert "unsettled" in kinds(events)

    def test_store_types_are_checked(self, store: MemoryStore) -> None:
        from pennyguard import AsyncRedisStore, RedisStore

        fake = NS(register_script=lambda script: None)
        with pytest.raises(TypeError, match="SyncGuard"):
            Guard(RedisStore(fake), [Budget("g", 1)])
        with pytest.raises(TypeError, match="Guard with AsyncRedisStore"):
            SyncGuard(AsyncRedisStore(fake), [Budget("g", 1)])
        Guard(store, [Budget("g", 1)])
        SyncGuard(store, [Budget("g", 1)])


class TestIdempotence:
    def test_invalid_tags_fail_before_anything_is_charged(
        self, store: MemoryStore, clock: Clock, events: list[Event]
    ) -> None:
        guard = make(store, clock, events)
        with pytest.raises(TypeError):
            guard.reserve("0.4", tags={"attempt": 1})  # type: ignore[dict-item]
        assert spent(guard) == {"global": D(0)}

    def test_a_retried_reserve_charges_once(self, store: MemoryStore) -> None:
        counter = Counter("pg:{t}:c", limit=10, ttl=60)
        first = store.reserve("pg:{t}:kill", "pg:{t}:op:1", [counter], 3, 600)
        again = store.reserve("pg:{t}:kill", "pg:{t}:op:1", [counter], 3, 600)
        assert (first.status, first.charged) == ("ok", 3)
        assert (again.status, again.charged) == ("ok", 3)
        assert store.read([counter.key]) == [3]

    def test_settle_moves_to_a_target_so_retries_are_harmless(
        self, store: MemoryStore
    ) -> None:
        counter = Counter("pg:{t}:c", limit=10, ttl=60)
        store.reserve("pg:{t}:kill", "pg:{t}:op:1", [counter], 5, 600)
        store.settle("pg:{t}:op:1", [counter], 2, 300)
        store.settle("pg:{t}:op:1", [counter], 2, 300)
        assert store.read([counter.key]) == [2]
        store.settle("pg:{t}:op:1", [counter], 0, 300)
        store.settle("pg:{t}:op:1", [counter], 0, 300)
        assert store.read([counter.key]) == [0]

    def test_a_reserve_that_arrives_after_its_compensation_charges_nothing(
        self, store: MemoryStore
    ) -> None:
        counter = Counter("pg:{t}:c", limit=10, ttl=60)
        store.settle("pg:{t}:op:1", [counter], 0, 300)
        late = store.reserve("pg:{t}:kill", "pg:{t}:op:1", [counter], 4, 600)
        assert (late.status, late.charged) == ("ok", 0)
        assert store.read([counter.key]) == [0]

    def test_markers_expire(self, store: MemoryStore, clock: Clock) -> None:
        counter = Counter("pg:{t}:c", limit=100, ttl=0)
        store.reserve("pg:{t}:kill", "pg:{t}:op:1", [counter], 5, 600)
        clock.advance(seconds=601)
        # Past the hold time a settle no longer knows the estimate, so it
        # records the cost on top of it: an overcount, never an undercount.
        store.settle("pg:{t}:op:1", [counter], 2, 300)
        assert store.read([counter.key]) == [7]
