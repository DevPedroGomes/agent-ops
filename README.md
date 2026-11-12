# pennyguard

[![CI](https://github.com/DevPedroGomes/agent-ops/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/DevPedroGomes/agent-ops/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/DevPedroGomes/agent-ops/blob/main/LICENSE)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)

**Hard dollar budgets for LLM calls, enforced atomically across every worker
and replica of your app.**

*Formerly `agent-ops`: see [History](#history).*

An agent stuck in a loop, a public demo that goes viral, or one heavy tenant
can burn a month of API budget in an afternoon. Rate limits cap requests, not
dollars. A counter in process memory gives each of your eight workers its own
full budget. pennyguard reserves the worst-case cost of a call before it runs,
refuses it if any budget (global, per tenant, per user, per hour, per month)
lacks room, and records the real cost from the provider's usage afterwards.
The check and the charge are one Lua script in the Redis you already run.

```python
from anthropic import AsyncAnthropic
from pennyguard import AsyncRedisStore, Budget, Guard, Price, Pricing

client = AsyncAnthropic(max_retries=0)          # guard.call does the retrying
guard = Guard(
    AsyncRedisStore(redis),                     # your redis.asyncio client
    [
        Budget("global", limit_usd=200, window="day"),
        Budget("tenant", limit_usd=5, window="day", per="tenant"),
        Budget("tenant-monthly", limit_usd=60, window="month", per="tenant"),
    ],
    pricing=Pricing({"claude-opus-5-5": Price(input_per_mtok=4, output_per_mtok=20)}),
)

response = await guard.call(
    lambda: client.messages.create(model="claude-opus-5-5", max_tokens=2048, messages=msgs),
    estimate_usd=guard.estimate("claude-opus-5-5", input_tokens=3_000, max_output_tokens=2048),
    tenant=org_id,
    retries=2,
)
# Over budget: BudgetExceeded (429, with retry_after). Kill switch on or Redis
# down: GuardKilled / GuardUnavailable (503). The call never reaches the model.
```

* No dependencies in the core. `pennyguard[redis]` for production,
  `pennyguard[otel]` for metrics.
* Reads usage from Anthropic and OpenAI responses (and anything
  OpenAI-compatible) without importing either SDK.
* Async (`Guard`) and sync (`SyncGuard`) with the same API.
* `MemoryStore` for tests and single-process apps, no Redis needed.

Try it offline from a clone: `pip install -e .`, then
`python examples/quickstart.py`. For a full app, see
[`examples/fastapi_anthropic.py`](https://github.com/DevPedroGomes/agent-ops/blob/main/examples/fastapi_anthropic.py).

## Install

Not on PyPI yet. Install from this repository:

```bash
pip install "pennyguard[redis] @ git+https://github.com/DevPedroGomes/agent-ops@main"
```

In production, pin a commit: replace `main` with its SHA. Python 3.11 or
newer.

## Why not something else

| What you might use | What it does well | What it does not do here |
|---|---|---|
| Spending limits in the provider console | A monthly cap per organization, workspace or project | Not per tenant or user of your app, short of one workspace and API key per customer |
| An LLM gateway such as LiteLLM's proxy | Budgets per key, user or team | Another service in the request path, to deploy and keep up |
| Observability platforms (Langfuse, Phoenix and others) | Show what each call cost | Record the cost after the call; nothing stops the next one |
| A counter in process memory | Simple | Each worker and replica has its own copy of the budget |

pennyguard is the fourth row done properly: a library in your process, with
the counters in Redis so that every process shares them, and the check and
charge done atomically on the server.

## How it works

```
reserve(estimate) ──► Lua script on Redis ──► refused: nothing charged, raise
                         │ check every budget
                         │ charge every budget
                         ▼
                      provider call
                         │
     raised ◄────────────┴────────────► returned
        │                                   │
  provider refused (SDK error         settle(usage): the charge becomes
  status): release the estimate       the real cost
  anything else: estimate stays
```

1. **Reserve.** One script reads every applicable counter, refuses if any of
   them would pass its limit, and otherwise charges all of them. A refusal
   writes nothing. Fifty concurrent reservations against room for ten admit
   exactly ten, whatever the number of processes.
2. **Call the model.**
3. **Settle** with the real cost, computed from the response's usage and your
   prices. The counters move so the reservation holds exactly that cost.

If the call raises, the reservation is released only when the provider
provably refused it: the Anthropic or OpenAI SDK raised its `APIStatusError`
(LiteLLM's errors included) with status 400 or more, without retrying. Every
other failure keeps the estimate, because the provider may already have
billed: a timeout, a stream cut halfway, a client that disconnects, an
exception from your own code after the reply (FastAPI's `HTTPException`
included).

The limit is enforced at reservation time. A call that costs more than its
estimate is still recorded in full, in the window it was reserved in.
`guard.estimate` prices a call at its worst case (the full `max_tokens`,
every input token at the dearest rate), which bounds that overshoot.

## Budgets

```python
Budget("global", limit_usd=200, window="day")                 # one shared counter
Budget("tenant", limit_usd=5, window="day", per="tenant")     # one counter per tenant
Budget("user-hourly", limit_usd=0.50, window="hour", per="user")
Budget("lifetime-trial", limit_usd=10, window="total", per="user")
```

* `window` is `"hour"`, `"day"`, `"month"` or `"total"`, fixed and aligned to
  UTC. `"total"` never resets.
* `per` names a dimension, whose value you pass when reserving
  (`tenant="acme"`). A reservation is checked against every global budget and
  every budget whose dimension it names, all or nothing.
* Leaving out a dimension a budget needs, or passing one no budget uses,
  raises `ValueError`, so a typo such as `tennant="acme"` fails in the first
  test instead of silently skipping the cap.
* Amounts are exact decimals stored as integer micro-dollars: a small-model
  call costs a fraction of a cent.

## Calling the model

**`guard.call`** reserves, runs the callable and settles from the response.
With `retries`, it retries what the SDKs would retry (408, 409, 429, 5xx,
timeouts), honoring `retry-after`, and gives each attempt a reservation of
its own: a 429 is refunded, a timeout keeps its estimate. Turn the SDK's own
retries off (`max_retries=0`). Behind them, a 429 that follows a retried
timeout looks the same as one that follows a retried 429, so pennyguard has
to keep the estimate for both.

**Streaming**, with the reservation as a context manager. It applies the same
failure rule, and keeps the estimate if the block exits without settling:

```python
async with await guard.reserve(estimate, tenant=org_id) as reservation:
    async with client.messages.stream(model=MODEL, max_tokens=4096, messages=msgs) as stream:
        async for text in stream.text_stream:
            yield text
        final = await stream.get_final_message()
    await reservation.settle_response(final)
```

**An agent loop with a cap per task:** reserve each step on its own, with a
budget per task next to the per-tenant one. A runaway loop stops at the step
that would pass the task's cap.

```python
budgets = [
    Budget("tenant", limit_usd=5, window="day", per="tenant"),
    Budget("task", limit_usd=2, window="day", per="task"),
]
...
while not done:
    response = await guard.call(
        lambda: client.messages.create(model=MODEL, max_tokens=4096, messages=msgs),
        estimate_usd=step_estimate,
        tenant=org_id,
        task=task_id,
        retries=2,
    )
```

**A cost you computed yourself:** `await reservation.settle(cost_usd="0.0123")`.

Once a call is paid for, `settle` and `release` never raise because of Redis
or pricing: the estimate stays charged and a `store_error` or
`pricing_failed` event is emitted instead.

## Pricing

pennyguard ships no prices: a bundled table goes stale the week a provider
changes one, and a stale price in a spending cap undercharges silently. Pass
your own, or load LiteLLM's community-maintained table:

```python
pricing = Pricing({"claude-opus-5-5": Price(4, 20, cache_read_per_mtok="0.20")})
pricing = Pricing.from_litellm("model_prices_and_context_window.json")
```

Cache reads and writes, one-hour cache writes, long-context tiers, fast,
priority and flex rates, and audio tokens are all priced. Anything the table
cannot price keeps the estimate rather than being billed as standard text.
Details in [Pricing and usage](https://github.com/DevPedroGomes/agent-ops/blob/main/docs/pricing.md).

## Errors and HTTP status codes

| Exception | Meaning | Answer with |
|---|---|---|
| `BudgetExceeded` | A budget lacks room. `.budget`, `.scope`, `.spent_usd`, `.limit_usd`, `.requested_usd`, `.resets_at`, `.retry_after` | 429, `Retry-After: retry_after` (none for `"total"` budgets) |
| `GuardKilled` | The kill switch is on. `.reason` | 503 |
| `GuardUnavailable` | Redis could not be read, so the call is refused | 503 |
| `UnknownModel` | No price for a model (from `Pricing`; never raised by settle) | |

All four derive from `PennyGuardError`. `fail_open=True` lets calls through
when Redis is unreadable, with an `unchecked` event. The default refuses,
because an unreadable budget is not an absent budget.

## Kill switch

```python
await guard.kill("runaway agent, investigating")   # every process, immediately
await guard.resume()
await guard.killed()                               # the reason, or None
```

The flag lives in Redis and is checked inside the reservation script, so it
takes effect on the next reservation of every process, without a restart or
a deploy. From a shell: `redis-cli SET 'pg:{default}:kill' 'cost spike'`
(the key is `pg:{<project>}:kill`).

## Status and observability

`await guard.status(tenant="acme")` returns spent, limit, remaining and reset
time for every global budget and every budget whose dimension you name: a
health endpoint, or a "you have $1.20 left today" badge.

Every reservation, refusal, settlement and failure goes to `on_event`, with
the scope, your `tags` and, for settlements, the usage and cost of every
billed attempt. `pennyguard.otel.metrics_hook()` turns them into
OpenTelemetry metrics (`pip install "pennyguard[otel]"`): cost and tokens by
model, refusals by budget, and calls let through by `fail_open`.

## More documentation

* [Pricing and usage](https://github.com/DevPedroGomes/agent-ops/blob/main/docs/pricing.md):
  price tables, versioned names, cache and long-context prices, rate
  variants, audio, estimates, and how usage is read from each provider.
* [Running it in production](https://github.com/DevPedroGomes/agent-ops/blob/main/docs/production.md):
  Redis clients and retries, client disconnects, reservation lifetime,
  Redis Cluster, key expiry, and the limitations.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
PENNYGUARD_TEST_REDIS_URL=redis://localhost:6379/15 .venv/bin/python -m pytest   # with Redis
.venv/bin/ruff check src tests examples && .venv/bin/mypy
```

The suite runs in under a second without Redis. The Redis tests run the Lua
scripts themselves, including fifty concurrent reservations from separate
clients against room for ten, and regressions that fail if the scripts lose
their idempotence, their floor at zero, their `pcall` around `EXPIRE`, or the
order that writes the marker before the charge. CI runs them on Python 3.11,
3.12 and 3.13 against a real Redis, and fails if any of them is skipped.

### History

This repository started as `agent-ops`, three primitives extracted from a
production document Q&A service: a daily call counter, an append-only
decision trail and a durable job queue. Version 0.4.0 keeps the idea that
mattered (a spending cap that refuses rather than guesses) and rebuilds it as
a dollar-denominated, multi-budget library. The earlier code remains
installable from the `v0.3.0` tag, and
[CHANGELOG.md](https://github.com/DevPedroGomes/agent-ops/blob/main/CHANGELOG.md)
has the details.

## License

MIT. See [LICENSE](https://github.com/DevPedroGomes/agent-ops/blob/main/LICENSE).
