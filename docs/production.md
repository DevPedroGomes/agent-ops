# Running it in production

What pennyguard does when things go wrong, and what to set up around it.

## Redis

* **Pass your own Redis client.** Its pool, timeouts, TLS and retry settings
  stay yours, and either `decode_responses` setting works. `RedisStore` takes
  a `redis.Redis` and works with `SyncGuard`; `AsyncRedisStore` takes a
  `redis.asyncio` client and works with `Guard`. Each refuses the other kind.
* **Redis timeouts and retries are safe.** Every reservation has an id and a
  small marker key, `pg:{project}:op:<id>`, recording what it has charged,
  written before any counter moves. Running a reservation twice charges once,
  and settling moves the charge to a target rather than by a delta, so
  redis-py's retry of a command that timed out (the default from version 6)
  cannot double-charge. If a reservation's reply is lost, the guard settles
  it to zero before raising `GuardUnavailable`; with `fail_open`, the later
  settle lands on the real cost whether or not the lost reservation went
  through.
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

## Provider failures and retries

* **Which failures refund.** A reservation is released only when the
  Anthropic or OpenAI SDK raises its `APIStatusError` (or a subclass, such as
  LiteLLM's) with status 400 or more, on the SDK's first attempt. Every other
  exception keeps the estimate and emits an `unsettled` event carrying it. A
  mid-stream error event arrives with the stream's 200 status and keeps the
  estimate too: tokens already generated are billed.
* **Retry in `guard.call`, not in the SDK.** The SDKs retry timeouts, dropped
  connections, 429 and 5xx on their own, twice by default. An attempt that
  timed out may have been billed, so a status error that follows one of
  those retries keeps the estimate, and under a burst of 429s that charges
  estimates for calls that never ran. Give the client `max_retries=0` and
  pass `retries=` to `guard.call`: every attempt gets its own reservation,
  released on a 429 and kept on a timeout.
* **Other clients.** With anything but those two SDKs, every failure keeps
  the estimate. Hold the reservation yourself and call `release()` when you
  know a failed call was not billed.
* **Client disconnects.** An ASGI server cancels the request when the client
  goes away. In `Guard`, a settle or release already in flight finishes in
  the background, and a reservation cancelled before its reply is refunded.
* **Settle never fails a paid call.** Once a call is paid for, `settle` and
  `release` do not raise because of Redis or pricing: the estimate stays
  charged, the failure is logged, and a `store_error` or `pricing_failed`
  event is emitted. They do raise on programmer errors, such as settling
  twice or passing a negative cost.

## Reservations

* **`reservation_ttl`** (six hours by default, in whole seconds) is how long
  an unsettled reservation stays settleable. A settle after that records the
  real cost on top of the estimate instead of replacing it: an overcount,
  never an undercount. Raise it for work that holds one reservation longer.
  Settled reservations keep their marker for five minutes.
* **One reservation per call.** A reservation held across several calls has
  no right answer when one of them fails: releasing refunds the calls that
  succeeded, and settling leaves out the one that failed. Reserve each call,
  and cap the total with a budget `per` task.
* **`fail_open=True`** lets calls through when Redis is unreadable, with an
  `unchecked` event and the `pennyguard.unchecked` metric. Their cost is
  still recorded at settle time if Redis is back by then.

## Limitations

* **Your estimate bounds the overshoot.** A call that costs more than its
  reservation is recorded in full but was already allowed. Concurrent calls
  can each overshoot by their own difference. `guard.estimate` with the
  call's `max_tokens` and rate bounds one attempt; a refusal fallback adds a
  second attempt.
* **A failed call keeps its whole estimate**, even a stream that stopped
  after a few tokens. That overcounts; settle with `cost_usd` yourself if the
  difference matters to you.
* **Fixed windows, not rolling ones.** A day budget can be spent at 23:59 and
  again at 00:01. Pair a day budget with an hour budget if bursts matter. A
  call settled after its window ended is recorded in the window it was
  reserved in.
* **Prices are your responsibility.** pennyguard computes from the table you
  give it; see [Pricing and usage](pricing.md) for what is and is not priced.
* **Refusals before output are counted** even in the refusal categories
  Anthropic does not bill.
* **No rolling history.** Counters hold the current window only. For
  per-call history, send events to your logs or metrics.
