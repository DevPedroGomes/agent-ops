# pennyguard

[![CI](https://github.com/DevPedroGomes/agent-ops/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/DevPedroGomes/agent-ops/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/DevPedroGomes/agent-ops/blob/main/LICENSE)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)

**Hard dollar budgets for LLM calls, enforced atomically across every worker
and replica of your app.**

An agent stuck in a loop, a public demo that goes viral, or one heavy tenant
can burn a month of API budget in an afternoon. Rate limits cap requests, not
dollars. A counter in process memory gives each of your eight workers its own
full budget. pennyguard reserves the worst-case cost of a call before it runs,
refuses it if any budget (global, per tenant, per user, per hour, per month)
lacks room, and records the real cost from the provider's usage afterwards.
The check and the charge are one Lua script in the Redis you already run.

```python
from pennyguard import AsyncRedisStore, Budget, Guard, Price, Pricing

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
`python examples/quickstart.py`.

## Contents

* [Install](#install)
* [Why not something else](#why-not-something-else)
* [How it works](#how-it-works)
* [Budgets](#budgets)
* [Reserving and settling](#reserving-and-settling)
* [Pricing](#pricing)
* [Reading usage from responses](#reading-usage-from-responses)
* [Errors and HTTP status codes](#errors-and-http-status-codes)
* [Kill switch](#kill-switch)
* [Status and observability](#status-and-observability)
* [Running it in production](#running-it-in-production)
* [Limitations](#limitations)
* [Development](#development)

## Install

Not on PyPI yet. Install from this repository:

```bash
pip install "pennyguard[redis] @ git+https://github.com/DevPedroGomes/agent-ops@main"
```

In production, pin a commit: replace `main` with its SHA.

Python 3.11 or newer.

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
   exactly ten, whatever the number of processes (a test runs this against a
   real Redis).
2. **Call the model.**
3. **Settle** with the real cost, computed from the response's usage. The
   counters move so that the reservation holds exactly the real cost.

If the call raises instead, what happens depends on whether it can have
been billed. When the Anthropic or OpenAI SDK raises its error for an HTTP
status of 400 or more (`APIStatusError` and its subclasses, LiteLLM's
included), the provider refused before generating anything, so the
reservation is **released**. Anything else keeps the estimate charged: a
timeout after the request was sent, a stream cut halfway, a client that
disconnects mid-response, an exception from your own code after the reply,
even one with an HTTP status such as FastAPI's `HTTPException`. In all of
those the provider may already have billed, and a user who closes the
connection just before the end must not get the call for free. An SDK error
that follows one of the SDK's own retries keeps the estimate too; see
[Running it in production](#running-it-in-production).

The limit is enforced at reservation time. A call that costs more than its
estimate is still recorded in full, in the window it was reserved in, so the
counters never under-report and later reservations in that window see it.
`guard.estimate` prices one call at its worst case (the full `max_tokens`,
every input token at the dearest rate), which bounds that overshoot for a
single attempt at the rate you pass.

## Budgets

```python
Budget("global", limit_usd=200, window="day")                 # one shared counter
Budget("tenant", limit_usd=5, window="day", per="tenant")     # one counter per tenant
Budget("user-hourly", limit_usd=0.50, window="hour", per="user")
Budget("lifetime-trial", limit_usd=10, window="total", per="user")
```

* `window` is `"hour"`, `"day"`, `"month"` or `"total"`. Windows are fixed and
  aligned to UTC: a day budget resets at midnight UTC, a month budget on the
  first of the month. `"total"` never resets.
* `per` names a dimension. Pass its value as a keyword when reserving
  (`tenant="acme"`). A reservation is checked against every global budget and
  every budget whose dimension it names, all or nothing.
* Leaving out a dimension a budget needs, or passing one no budget uses,
  raises `ValueError`. A typo such as `tennant="acme"` fails in the first test
  instead of silently skipping the per-tenant cap.
* Amounts are exact decimals stored as integer micro-dollars. A small-model
  call costs a fraction of a cent, so whole cents would round most calls to
  zero.

## Reserving and settling

`guard.call` covers the common case: reserve, run the callable, settle from
the response it returns. If the callable raises, the rule above applies.

For anything else, hold the reservation yourself. As a context manager it
applies the same rule to an exception before `settle`, and keeps the
estimate if the block exits without settling. Both cases log and emit an
`unsettled` event carrying the exception. With a client other than the
Anthropic and OpenAI SDKs, call `release()` yourself inside the block when
you know a failed call was not billed.

**Streaming:**

```python
async with await guard.reserve(estimate, tenant=org_id) as reservation:
    async with client.messages.stream(model=MODEL, max_tokens=4096, messages=msgs) as stream:
        async for text in stream.text_stream:
            yield text
        final = await stream.get_final_message()
    await reservation.settle_response(final)
```

If the stream fails partway, the estimate stays charged: tokens already
generated are billed.

**An agent loop with a cap per task:** reserve each step on its own, and add
a budget per task next to the per-tenant one. A runaway loop is stopped at
the step that would pass the task's cap, and a failed step affects only its
own reservation.

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
    )
    ...
```

One reservation held across many calls is possible, but a failure in the
middle has no right answer: releasing refunds the calls that succeeded, and
settling leaves out the one that failed. Per-step reservations avoid the
question.

**A cost you computed yourself:** `await reservation.settle(cost_usd="0.0123")`.

`settle` and `release` never raise because of Redis or pricing once the call
is paid for. Turning a paid answer into an error page helps nobody. Instead
the estimate stays charged, the failure is logged, and a `store_error` or
`pricing_failed` event is emitted. They do raise on programmer errors, such as
settling twice or passing a negative cost.

## Pricing

pennyguard ships no prices. A bundled table goes stale the week a provider
changes a price, and a stale price in a spending cap undercharges silently.

```python
from pennyguard import Price, Pricing

# Your own table, in dollars per million tokens.
pricing = Pricing({
    "claude-opus-5-5": Price(4, 20, cache_read_per_mtok="0.20"),
    "gpt-4o": Price(2.5, 10, cache_read_per_mtok=1.25),
})

# Or LiteLLM's community-maintained table, with your own entries on top.
pricing = Pricing.from_litellm("model_prices_and_context_window.json").with_prices({
    "my-fine-tune": Price(3, 12),
})
```

* **Versioned names** resolve to their base entry when the suffix is a date,
  a version or `latest`: `gpt-4o-2024-08-06` finds `gpt-4o`. A different model
  never matches by prefix: `gpt-4o-mini` does not fall back to `gpt-4o`.
* **Unknown models** raise `UnknownModel` from `Pricing.price`. At settle time
  the guard keeps the estimate instead and emits `pricing_failed`. Pass
  `default=Price(...)` to price unknown models conservatively instead.
* **Missing cache prices default upward**: a cache read costs the input
  price, a cache write 1.25 times it, and a one-hour cache write (Anthropic's
  `ttl: "1h"`, reported under `usage.cache_creation`) twice it.
* **Long-context tiers** (`above={200_000: Price(...)}`) price the whole call at
  the tier once the prompt passes the threshold, cached tokens included.
  `from_litellm` reads them from the `*_above_<N>k_tokens` fields.
* **Dearer rates** such as Anthropic's fast mode or OpenAI's priority tier are
  read from the response into `Usage.tier` and priced from
  `variants={"fast": Price(...)}`. A variant can carry its own `above` tiers.
  A dearer rate with no price, including a long prompt at a variant without
  a tier for it, is not billed at the standard price: settle keeps the
  estimate and emits `pricing_failed`. `from_litellm` reads OpenAI's
  priority and flex rates with their tiers.
* **Audio tokens** (OpenAI's audio models, Gemini's audio input) are read
  into `Usage.input_audio_tokens` and `output_audio_tokens` and priced from
  `input_audio_per_mtok` and `output_audio_per_mtok`, which `from_litellm`
  reads. Audio with no audio price is treated like a dearer rate with no
  price. Pass `audio=True` to `guard.estimate` for calls that carry audio.
* **Estimates** (`guard.estimate(model, input_tokens=..., max_output_tokens=...)`)
  price every input token at the dearest of the input and cache-write
  prices, and assume the full `max_output_tokens`, thinking included. Pass
  `tier="fast"` (or another variant) when the call runs at that rate. With
  refusal fallbacks a declined call is billed twice, so add the fallback
  model's estimate if overshoot matters (the FastAPI example does).

## Reading usage from responses

`settle_response` and `Usage.all_from_response` read Anthropic Messages, OpenAI
Chat Completions and OpenAI Responses objects, or the same shapes as dicts.

* Anthropic reports uncached input, cache reads and cache writes separately.
  OpenAI includes cached tokens in `prompt_tokens` and repeats them under
  `prompt_tokens_details`. Both map to one `Usage` shape so nothing is counted
  twice and cache writes are not missed.
* **Anthropic refusal fallbacks.** With server-side `fallbacks`, a declined
  attempt and the fallback model's answer are billed separately, each at its
  own model's rate, and the top-level `usage` covers only the answer. Every
  attempt is listed in `usage.iterations`, and pennyguard prices each one
  under its own model. A declined attempt that produced no output is billed
  only for some refusal categories; pennyguard counts it regardless, which
  errs toward overcounting. Server-side compaction iterations are counted the
  same way.

## Errors and HTTP status codes

| Exception | Meaning | Answer with |
|---|---|---|
| `BudgetExceeded` | A budget lacks room. `.budget`, `.scope`, `.spent_usd`, `.limit_usd`, `.requested_usd`, `.resets_at`, `.retry_after` | 429, `Retry-After: retry_after` (none for `"total"` budgets) |
| `GuardKilled` | The kill switch is on. `.reason` | 503 |
| `GuardUnavailable` | Redis could not be read, so the call is refused | 503 |
| `UnknownModel` | No price for a model (from `Pricing`; never raised by settle) | |

All four derive from `PennyGuardError`. `fail_open=True` makes an unreadable
Redis allow the call instead of refusing it, with an `unchecked` event; the
cost is still recorded at settle time if Redis is back by then. The default
refuses, because an unreadable budget is not an absent budget.

## Kill switch

```python
await guard.kill("runaway agent, investigating")   # every process, immediately
await guard.resume()
await guard.killed()                               # the reason, or None
```

The flag lives in Redis and is checked inside the reservation script, so it
costs no extra round trip and takes effect on the next reservation of every
process, without a restart or a deploy. From a shell during an incident:

```bash
redis-cli SET 'pg:{default}:kill' 'cost spike'   # pg:{<project>}:kill
```

## Status and observability

`await guard.status(tenant="acme")` returns, for every global budget and every
budget whose dimension you name: spent, limit, remaining and when it resets.
It suits a health endpoint or a "you have $1.20 left today" badge.

Every reservation, refusal, settlement and failure goes to `on_event`:

```python
guard = Guard(store, budgets, on_event=lambda event: log.info("%s", event))
```

An `Event` carries the kind, project, reservation id, scope, your `tags`, the
amount, and for settlements the usage and cost of every billed attempt. The
hook's own errors are logged and swallowed.

For OpenTelemetry metrics (`pip install "pennyguard[otel]"`):

```python
from pennyguard.otel import metrics_hook

guard = Guard(store, budgets, on_event=metrics_hook(scope_keys=["tenant"], tag_keys=["route"]))
```

This records `pennyguard.cost` (USD, by model), `pennyguard.tokens` (by model
and token type), `pennyguard.refusals` (by budget, `killed` or
`unavailable`) and `pennyguard.unchecked` (calls let through by
`fail_open`). Scope and tag attributes are opt-in because a per-user
attribute on a metric multiplies its time series by the number of users.

## Running it in production

* **Pass your own Redis client.** Its pool, timeouts, TLS and retry settings
  stay yours, and either `decode_responses` setting works.
* **Timeouts and retries are safe.** Every reservation has an id and a small
  marker key, `pg:{project}:op:<id>`, recording what it has charged. Running
  a reservation twice charges once, and settling moves the charge to a
  target rather than by a delta, so redis-py's retry of a command that timed
  out (the default from version 6) cannot double-charge. If a reservation's
  reply is lost, the guard settles it to zero before raising
  `GuardUnavailable`; with `fail_open`, the later settle lands on the real
  cost whether or not the lost reservation went through.
* **SDK retries.** The Anthropic and OpenAI SDKs retry timeouts, dropped
  connections, 429 and 5xx on their own, twice by default. An earlier
  attempt that timed out may have been billed, so a status error that
  follows a retry keeps the estimate instead of releasing it. Under a burst
  of 429s that charges estimates for calls that never ran. To avoid that,
  give the client the guard wraps `max_retries=0` and retry around
  `guard.call`: every attempt gets its own reservation, released on a 429.
* **Client disconnects.** An ASGI server cancels the request when the
  client goes away. A settle or release already in flight finishes in the
  background, and a reservation cancelled before its reply is refunded.
* **`reservation_ttl`** (six hours by default, in whole seconds) is how long
  an unsettled reservation stays settleable. A settle after that records
  the real cost on top of the estimate instead of replacing it: an
  overcount, never an undercount. Raise it for work that holds one
  reservation longer. Settled reservations keep their marker for five
  minutes.
* **Redis Cluster** works: every key of a project carries the same hash tag,
  `pg:{project}:...`, which a multi-key script requires.
* **The server must allow `EVAL`.** A few managed offerings disable scripting;
  there every reservation is refused (or allowed, with `fail_open`) rather
  than checked non-atomically.
* **Keys expire on their own**, after two windows of inactivity (62 days for
  monthly budgets). `"total"` budgets never expire.
* **`project`** namespaces every key, so several apps can share one Redis.
* **`MemoryStore` is per process.** Behind several workers each one gets the
  full budget; use it for tests, scripts and single-process apps.

## Limitations

* **Your estimate bounds the overshoot.** A call that costs more than its
  reservation is recorded in full but was already allowed. Concurrent calls
  can each overshoot by their own difference. `guard.estimate` with the
  call's `max_tokens` and rate bounds one attempt; a refusal fallback adds a
  second attempt.
* **A failed call keeps its whole estimate**, even a stream that stopped
  after a few tokens. That overcounts; settle with `cost_usd` yourself if
  the difference matters to you.
* **Fixed windows, not rolling ones.** A day budget can be spent at 23:59 and
  again at 00:01. Pair a day budget with an hour budget if bursts matter. A
  call settled after its window ended is recorded in the window it was
  reserved in.
* **Prices are your responsibility.** pennyguard computes from the table you
  give it. Batch discounts are not modeled (a batch call is charged at the
  standard rate, which overcounts). Fees outside tokens, such as web search
  per query or code execution time, are not priced; settle with `cost_usd`
  if you use them.
* **Refusals before output are counted** even in the refusal categories
  Anthropic does not bill.
* **Cached audio is priced as cached text**, because responses do not report
  it apart.
* **No rolling history.** Counters hold the current window only. For
  per-call history, send events to your logs or metrics.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
PENNYGUARD_TEST_REDIS_URL=redis://localhost:6379/15 .venv/bin/python -m pytest   # with Redis
.venv/bin/ruff check src tests examples && .venv/bin/mypy
```

The suite runs in under a second without Redis. The Redis tests run the Lua
scripts themselves: fifty concurrent reservations from separate clients
against room for ten, a script run twice that must charge once, and
regressions that fail if `redis.pcall` around `EXPIRE` becomes `redis.call`,
if the marker is written after the charge, if the floor at zero goes, or if
settling reverts to a delta. CI runs them
against a real Redis on Python 3.11, 3.12 and 3.13, and fails if any of them
is skipped.

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
