"""Redis stores: the budget is shared by every process that points here.

Both writes run as Lua scripts, so checking every limit and charging every
counter is one indivisible step on the server. Fifty concurrent reservations
against room for ten admit exactly ten, across any number of workers and
replicas.

Both scripts are idempotent (see ``stores.base``), which matters with
redis-py: from version 6 it retries a command that timed out by default, and
a timeout can arrive after the script already ran.

Keys of one project share a Redis Cluster hash tag, so the scripts also run
on a cluster. The server must allow ``EVAL``/``EVALSHA``; a few managed
offerings disable scripting, and there every reservation is refused (or
allowed, with ``fail_open=True``) rather than checked non-atomically.

You pass the client, so its pool, timeouts, TLS and retry settings stay
yours. Either ``decode_responses`` setting works.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

from pennyguard.stores.base import Counter, ReserveOutcome

if TYPE_CHECKING:
    import redis
    import redis.asyncio

# KEYS[1] is the kill flag, KEYS[2] the reservation marker, KEYS[3..] the
# counters. ARGV[1] is the amount, ARGV[2] the marker's TTL, then one
# (limit, ttl) pair per counter.
# Returns {3, charged} when the marker exists, {2, reason} when killed,
# {0, index, current...} when refused and {1, total...} when charged.
#
# Reading every counter before writing any is what makes a refusal free: the
# script returns before its first INCRBY. EXPIRE goes through pcall because a
# script that aborts keeps the writes it already made, and a failed EXPIRE
# must not leave a half-charged reservation behind. Amounts stay strings so
# Lua never formats a large integer in scientific notation.
_RESERVE = """
local held = redis.call('GET', KEYS[2])
if held then return {3, held} end
local kill = redis.call('GET', KEYS[1])
if kill then return {2, kill} end
local amount = tonumber(ARGV[1])
local n = #KEYS - 2
local current = {}
for i = 1, n do
  current[i] = tonumber(redis.call('GET', KEYS[i + 2]) or '0')
end
for i = 1, n do
  local limit = tonumber(ARGV[2 * i + 1])
  if current[i] >= limit or current[i] + amount > limit then
    local out = {0, i}
    for j = 1, n do out[j + 2] = current[j] end
    return out
  end
end
local out = {1}
for i = 1, n do
  out[i + 1] = redis.call('INCRBY', KEYS[i + 2], ARGV[1])
  local ttl = tonumber(ARGV[2 * i + 2])
  if ttl > 0 then redis.pcall('EXPIRE', KEYS[i + 2], ttl) end
end
redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[2])
return out
"""

# KEYS[1] is the reservation marker, KEYS[2..] the counters. ARGV[1] is the
# target charge, ARGV[2] the marker's TTL afterwards, then one ttl per
# counter. Moving to a target rather than by a delta is what makes a retried
# settle, or a settle after a reserve whose reply was lost, land correctly.
# The floor at zero runs in the same script, so a concurrent charge can never
# slip between reading a negative value and correcting it.
_SETTLE = """
local delta = tonumber(ARGV[1]) - tonumber(redis.call('GET', KEYS[1]) or '0')
if delta ~= 0 then
  local step = string.format('%d', delta)
  for i = 2, #KEYS do
    local total = redis.call('INCRBY', KEYS[i], step)
    if total < 0 then
      redis.call('INCRBY', KEYS[i], string.format('%d', -total))
    end
    local ttl = tonumber(ARGV[i + 1])
    if ttl > 0 then redis.pcall('EXPIRE', KEYS[i], ttl) end
  end
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
"""


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _reserve_args(
    kill_key: str,
    op_key: str,
    counters: Sequence[Counter],
    amount: int,
    hold_ttl: int,
) -> tuple[list[str], list[int]]:
    keys = [kill_key, op_key, *(c.key for c in counters)]
    args = [amount, hold_ttl]
    for counter in counters:
        args += [counter.limit, counter.ttl]
    return keys, args


def _settle_args(
    op_key: str, counters: Sequence[Counter], target: int, keep_ttl: int
) -> tuple[list[str], list[int]]:
    return (
        [op_key, *(c.key for c in counters)],
        [target, keep_ttl, *(c.ttl for c in counters)],
    )


def _outcome(raw: Sequence[Any]) -> ReserveOutcome:
    status = int(raw[0])
    if status == 3:
        return ReserveOutcome("ok", charged=int(raw[1]))
    if status == 2:
        return ReserveOutcome("killed", kill_reason=_text(raw[1]))
    if status == 0:
        return ReserveOutcome(
            "refused",
            totals=tuple(int(v) for v in raw[2:]),
            refused_index=int(raw[1]) - 1,
        )
    totals = tuple(int(v) for v in raw[1:])
    return ReserveOutcome("ok", totals=totals)


def _ints(values: Sequence[Any]) -> list[int]:
    return [int(v) if v is not None else 0 for v in values]


class RedisStore:
    """For ``SyncGuard``, over a ``redis.Redis`` or ``redis.RedisCluster``."""

    def __init__(self, client: redis.Redis | Any) -> None:
        self._client = client
        self._reserve = client.register_script(_RESERVE)
        self._settle = client.register_script(_SETTLE)

    def reserve(
        self,
        kill_key: str,
        op_key: str,
        counters: Sequence[Counter],
        amount: int,
        hold_ttl: int,
    ) -> ReserveOutcome:
        keys, args = _reserve_args(kill_key, op_key, counters, amount, hold_ttl)
        outcome = _outcome(self._reserve(keys=keys, args=args))
        return _with_charge(outcome, amount)

    def settle(
        self, op_key: str, counters: Sequence[Counter], target: int, keep_ttl: int
    ) -> None:
        keys, args = _settle_args(op_key, counters, target, keep_ttl)
        self._settle(keys=keys, args=args)

    def read(self, keys: Sequence[str]) -> list[int]:
        if not keys:
            return []
        # redis-py 5 types mget as `Awaitable | Any` on the sync client, and
        # later versions as a list. A cast holds under both.
        return _ints(cast("list[Any]", self._client.mget(list(keys))))

    def set_kill(self, kill_key: str, reason: str | None) -> None:
        if reason is None:
            self._client.delete(kill_key)
        else:
            self._client.set(kill_key, reason)

    def get_kill(self, kill_key: str) -> str | None:
        value = self._client.get(kill_key)
        return None if value is None else _text(value)


class AsyncRedisStore:
    """For ``Guard``, over a ``redis.asyncio.Redis`` or ``RedisCluster``."""

    def __init__(self, client: redis.asyncio.Redis | Any) -> None:
        self._client = client
        self._reserve = client.register_script(_RESERVE)
        self._settle = client.register_script(_SETTLE)

    async def reserve(
        self,
        kill_key: str,
        op_key: str,
        counters: Sequence[Counter],
        amount: int,
        hold_ttl: int,
    ) -> ReserveOutcome:
        keys, args = _reserve_args(kill_key, op_key, counters, amount, hold_ttl)
        outcome = _outcome(await self._reserve(keys=keys, args=args))
        return _with_charge(outcome, amount)

    async def settle(
        self, op_key: str, counters: Sequence[Counter], target: int, keep_ttl: int
    ) -> None:
        keys, args = _settle_args(op_key, counters, target, keep_ttl)
        await self._settle(keys=keys, args=args)

    async def read(self, keys: Sequence[str]) -> list[int]:
        if not keys:
            return []
        # redis-py types mget on the asyncio client as `Awaitable | Any`.
        values: list[Any] = await self._client.mget(list(keys))
        return _ints(values)

    async def set_kill(self, kill_key: str, reason: str | None) -> None:
        if reason is None:
            await self._client.delete(kill_key)
        else:
            await self._client.set(kill_key, reason)

    async def get_kill(self, kill_key: str) -> str | None:
        value = await self._client.get(kill_key)
        return None if value is None else _text(value)


def _with_charge(outcome: ReserveOutcome, amount: int) -> ReserveOutcome:
    # A fresh charge reports the amount; a replay already carries its own.
    if outcome.status == "ok" and outcome.totals:
        return ReserveOutcome("ok", totals=outcome.totals, charged=amount)
    return outcome
