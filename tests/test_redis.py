"""Against a real Redis. Skipped unless PENNYGUARD_TEST_REDIS_URL is set.

These run the Lua scripts themselves, which no in-process double can, and
the one property a sequential test cannot see: concurrent reservations from
independent clients against one limit.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from decimal import Decimal
from threading import Barrier, Thread

import pytest

from pennyguard import (
    AsyncRedisStore,
    Budget,
    BudgetExceeded,
    Guard,
    GuardKilled,
    RedisStore,
    SyncGuard,
)
from pennyguard.stores.base import Counter

URL = os.environ.get("PENNYGUARD_TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(not URL, reason="PENNYGUARD_TEST_REDIS_URL not set")

redis = pytest.importorskip("redis")
redis_asyncio = pytest.importorskip("redis.asyncio")


@pytest.fixture
def client() -> Iterator[redis.Redis]:
    assert URL is not None
    connection = redis.Redis.from_url(URL, decode_responses=True)
    connection.flushdb()
    yield connection
    connection.flushdb()
    connection.close()


def test_concurrent_async_reservations_never_pass_the_limit(
    client: redis.Redis,
) -> None:
    assert URL is not None

    async def scenario() -> int:
        # Five independent connections stand in for five worker processes.
        connections = [redis_asyncio.Redis.from_url(URL) for _ in range(5)]
        guards = [
            Guard(AsyncRedisStore(c), [Budget("global", 10)], project="race")
            for c in connections
        ]

        async def attempt(i: int) -> bool:
            try:
                await guards[i % 5].reserve(1)
            except BudgetExceeded:
                return False
            return True

        results = await asyncio.gather(*(attempt(i) for i in range(50)))
        for c in connections:
            await c.aclose()
        return sum(results)

    assert asyncio.run(scenario()) == 10
    status = SyncGuard(RedisStore(client), [Budget("global", 10)], project="race")
    assert status.status()[0].spent_usd == Decimal(10)


def test_concurrent_threads_with_sync_clients(client: redis.Redis) -> None:
    assert URL is not None
    budgets = [Budget("global", 100), Budget("tenant", 7, per="tenant")]
    admitted: list[bool] = []
    barrier = Barrier(20)

    def worker() -> None:
        own = redis.Redis.from_url(URL)
        guard = SyncGuard(RedisStore(own), budgets, project="threads")
        barrier.wait()
        try:
            guard.reserve(1, tenant="acme")
            admitted.append(True)
        except BudgetExceeded:
            admitted.append(False)
        finally:
            own.close()

    threads = [Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert admitted.count(True) == 7
    guard = SyncGuard(RedisStore(client), budgets, project="threads")
    spent = {s.budget: s.spent_usd for s in guard.status(tenant="acme")}
    assert spent == {"global": Decimal(7), "tenant": Decimal(7)}


def test_refusal_by_one_budget_charges_none(client: redis.Redis) -> None:
    budgets = [Budget("global", 100), Budget("user", 1, per="user")]
    guard = SyncGuard(RedisStore(client), budgets, project="atomic")
    with pytest.raises(BudgetExceeded) as caught:
        guard.reserve(2, user="u1")
    assert caught.value.budget == "user"
    assert caught.value.spent_usd == Decimal(0)
    assert all(s.spent_usd == 0 for s in guard.status(user="u1"))


def test_settle_release_and_floor_at_zero(client: redis.Redis) -> None:
    store = RedisStore(client)
    guard = SyncGuard(store, [Budget("global", 10)], project="adjust")
    guard.reserve(3).settle(1)
    guard.reserve(2).release()
    assert guard.status()[0].spent_usd == Decimal(1)
    (key,) = client.keys("pg:{adjust}:global:*")
    # A reservation recorded as holding more than the counter now has (the
    # counter expired and was recreated, say) must not push it below zero.
    client.set("pg:{adjust}:op:stale", 5_000_000)
    store.settle("pg:{adjust}:op:stale", [Counter(key, 0, 60)], 0, 300)
    assert client.get(key) == "0"


def test_reserve_and_settle_are_idempotent(client: redis.Redis) -> None:
    # redis-py 6+ retries a command that timed out, and the timeout can come
    # after the script ran. Running each script twice must charge once.
    store = RedisStore(client)
    counter = Counter("pg:{idem}:c", limit=10_000_000, ttl=60)
    first = store.reserve("pg:{idem}:kill", "pg:{idem}:op:1", [counter], 3, 600)
    again = store.reserve("pg:{idem}:kill", "pg:{idem}:op:1", [counter], 3, 600)
    assert (first.status, first.charged, again.status, again.charged) == (
        "ok",
        3,
        "ok",
        3,
    )
    assert client.get("pg:{idem}:c") == "3"
    assert 0 < client.ttl("pg:{idem}:op:1") <= 600
    store.settle("pg:{idem}:op:1", [counter], 1, 300)
    store.settle("pg:{idem}:op:1", [counter], 1, 300)
    assert client.get("pg:{idem}:c") == "1"
    assert 0 < client.ttl("pg:{idem}:op:1") <= 300


def test_compensation_before_a_late_reserve_charges_nothing(
    client: redis.Redis,
) -> None:
    store = RedisStore(client)
    counter = Counter("pg:{late}:c", limit=10, ttl=60)
    store.settle("pg:{late}:op:1", [counter], 0, 300)
    late = store.reserve("pg:{late}:kill", "pg:{late}:op:1", [counter], 4, 600)
    assert (late.status, late.charged) == ("ok", 0)
    assert client.get("pg:{late}:c") is None


def test_large_amounts_stay_integers(client: redis.Redis) -> None:
    # Lua formats big numbers in scientific notation, which INCRBY rejects.
    store = RedisStore(client)
    big = 10**15  # a billion dollars, in micro-dollars
    counter = Counter("pg:{big}:c", limit=10**16, ttl=60)
    assert store.reserve("pg:{big}:k", "pg:{big}:op:1", [counter], big, 600).charged
    store.settle("pg:{big}:op:1", [counter], 3 * big, 300)
    assert client.get("pg:{big}:c") == str(3 * big)


def test_ttls(client: redis.Redis) -> None:
    budgets = [Budget("daily", 10), Budget("forever", 10, "total")]
    SyncGuard(RedisStore(client), budgets, project="ttl").reserve(1)
    (daily,) = client.keys("pg:{ttl}:daily:*")
    (forever,) = client.keys("pg:{ttl}:forever:*")
    assert 0 < client.ttl(daily) <= 2 * 86_400
    assert client.ttl(forever) == -1


def test_a_failing_expire_does_not_abort_the_reservation(client: redis.Redis) -> None:
    # A TTL Redis rejects makes EXPIRE error inside the script. With
    # redis.call instead of pcall the script would abort after INCRBY,
    # leaving the charge written but reported as a failure.
    store = RedisStore(client)
    counter = Counter("pg:{expire}:c", limit=10, ttl=2**62)
    outcome = store.reserve("pg:{expire}:kill", "pg:{expire}:op:1", [counter], 3, 600)
    assert outcome.status == "ok"
    assert client.get("pg:{expire}:c") == "3"
    store.settle("pg:{expire}:op:1", [counter], 2, 300)
    assert client.get("pg:{expire}:c") == "2"


def test_a_marker_redis_rejects_charges_nothing(client: redis.Redis) -> None:
    # A script that aborts keeps the writes it already made. With the marker
    # written after the counters, a TTL Redis rejects (a float, say) would
    # charge the counters and then fail, and nothing would ever refund them.
    store = RedisStore(client)
    counter = Counter("pg:{marker}:c", limit=10, ttl=60)
    with pytest.raises(redis.ResponseError):
        store.reserve("pg:{marker}:k", "pg:{marker}:op:1", [counter], 3, 1.5)  # type: ignore[arg-type]
    assert client.get("pg:{marker}:c") is None
    assert client.get("pg:{marker}:op:1") is None


def test_kill_switch_set_by_hand_reaches_running_guards(client: redis.Redis) -> None:
    guard = SyncGuard(RedisStore(client), [Budget("global", 10)], project="ops")
    client.set("pg:{ops}:kill", "incident 42")
    with pytest.raises(GuardKilled) as caught:
        guard.reserve(1)
    assert caught.value.reason == "incident 42"
    assert guard.killed() == "incident 42"
    guard.resume()
    guard.reserve(1)


def test_bytes_clients_work(client: redis.Redis) -> None:
    assert URL is not None
    raw = redis.Redis.from_url(URL)  # decode_responses=False
    guard = SyncGuard(RedisStore(raw), [Budget("global", 1)], project="bytes")
    guard.reserve(1).settle("0.25")
    assert guard.status()[0].spent_usd == Decimal("0.25")
    guard.kill("bytes reason")
    with pytest.raises(GuardKilled, match="bytes reason"):
        guard.reserve(0)
    raw.close()


def test_scripts_survive_a_script_flush(client: redis.Redis) -> None:
    guard = SyncGuard(RedisStore(client), [Budget("global", 10)], project="flush")
    guard.reserve(1)
    client.script_flush()
    guard.reserve(1).settle(1)
    assert guard.status()[0].spent_usd == Decimal(2)


def test_every_key_of_a_project_shares_one_cluster_slot(client: redis.Redis) -> None:
    budgets = [Budget("a", 10), Budget("b", 10, "hour", per="tenant")]
    guard = SyncGuard(RedisStore(client), budgets, project="slot")
    guard.reserve(1, tenant="t:1")
    guard.kill("x")
    keys = client.keys("pg:*")
    assert len(keys) == 4  # two counters, the reservation marker, the kill flag
    assert {k.split("}")[0] for k in keys} == {"pg:{slot"}
