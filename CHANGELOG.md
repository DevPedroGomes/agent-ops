# Changelog

Tag-pinned installs are the only supported way to consume this package, so
every entry here is something that changes what a pinned upgrade does to you.

## 0.3.0

### Fixed

* **Timestamps were UTC in tests and local time in production.** `created_at`
  and `atualizado` defaulted to the database's `CURRENT_TIMESTAMP` on a column
  without a time zone. SQLite always resolves that to UTC; PostgreSQL resolves
  it in the session time zone. A database running in `America/Sao_Paulo` stored
  local time while the test suite stored UTC, with no error anywhere. Stuck-job
  detection and any correlation with the UTC quota day were off by the full
  offset. Both writes now stamp in UTC from Python (`agent_ops.tempo`). The DDL
  keeps its default as a fallback for hand-written SQL.
* **The metering key could outlive its day forever.** The 48 hour TTL was
  applied only when the post-increment counter equalled the amount reserved, a
  proxy for "first write" that fails whenever the counter does not start at
  zero. One orphaned refund followed by traffic charging a different amount
  skipped the value permanently, and the key was never given an expiry.
  `INCRBY` and `EXPIRE` now travel together in one transaction, so the TTL no
  longer depends on guessing which call is first, and `devolver` applies it too.
* **`enfileirar` leaked raw driver exceptions.** Only the depth read was
  translated into `FilaIndisponivel`. A backend failure during the enqueue
  itself propagated untranslated, so a caller following the documented
  `except FilaIndisponivel` returned 500 instead of 503. Both round trips are
  now covered. A colon in `tenant` or `digest` still raises `ValueError`, since
  it is a programming error and not a backend failure.
* **A refused call could charge quota.** `consumir` incremented, then checked,
  then compensated over a second round trip. That made the module's central
  promise, that a refused call costs the next visitor nothing, depend on the
  compensating call succeeding. When it did not, because the network blinked or
  the process died in between, the counter stayed inflated until the day rolled
  over and later visitors were refused for quota nobody spent. Reserve, check
  and compensate are now a single Lua script, so there is no window and no
  second call that can fail. Inside it, `EXPIRE` uses `pcall` and `INCRBY` uses
  `call`, because a Redis script does not undo its own writes when it aborts,
  which would have hidden the original bug inside the Lua instead of fixing it.
  Measured against a real server: with a non-atomic check-then-reserve, 50
  concurrent calls all passed a limit of 10.
* **A refund could raise the day's cap.** `devolver` computes its key from
  today's date, so a call reserved at 23:59:59 whose refund runs at 00:00:01
  landed on tomorrow's key and created it at a negative value. Every user of
  that quota then got the refunded amount for free, and it did not self correct
  until the next rollover. Measured before the fix: 15 units served against a
  limit of 10. The counter is now floored at zero, so refunding what was never
  consumed is a no-op instead of a credit.
* **`ler` returned a different type per backend.** `atualizado` came back as
  `datetime` from PostgreSQL and as a raw string from SQLite, so the arithmetic
  its docstring recommends worked on one and raised `TypeError` on the other.
  It is now a naive UTC `datetime` on both.

### Added

* **Read API for the decision trail**, `agent_ops.decisions.consultas`:
  `listar`, `por_execucao`, `filhos`, `contagem_por_regra`. The package already
  shipped four indexes shaped for exactly these questions and no functions that
  used them. `tenant_id` is a required keyword-only argument on every query
  that returns rows, which turns "reads filter by tenant, never by
  correlation_id alone" from prose into something you cannot forget. A test
  fails if anyone gives it a default.
* **Operations queries for the queue**: `queue.listar_por_estado` for sweeping
  the dead letter, and `queue.travados` for finding jobs whose worker died
  mid-flight. Both raise on a database failure, unlike `ler`, because an empty
  dead letter caused by an outage is the most expensive wrong answer that
  screen can give.
* **Redis in CI.** The hand-written double cannot execute Lua, so what runs in
  the fast suite is a model of the script's semantics. `tests/test_portabilidade_redis.py`
  runs the script itself, and runs the one test a sequential double can never
  perform: 50 concurrent reservations against a cap of 10.
* **PostgreSQL in CI.** The suite still runs on SQLite in under a second, and
  `tests/test_portabilidade_postgres.py` runs the same code against a real
  PostgreSQL with a deliberately non-UTC session time zone. The official image
  boots in UTC, where the timestamp defect above does not reproduce, so the
  fixture pins an offset on purpose. CI fails if those tests skip.

* **`panorama` can report a scope.** `consumir` has always accepted `escopo`
  so a per-IP cap can run alongside the global one, but the overview only read
  the unscoped key. A per-IP cap therefore existed, refused calls, and appeared
  nowhere: not in the health check and not in a badge telling a visitor what
  they have left. `panorama(limites, escopo=...)` reads the same counter, and
  the scope comes back in the result so a global report is not mistaken for
  somebody's.
* **Async variants**, `decisions.aio` and `queue.aio`. The database functions
  are synchronous SQLAlchemy and the natural place to call them is inside an
  `async def`. arq runs up to `max_jobs` jobs concurrently on one event loop,
  so a progress tick blocked its siblings and the worker health check for the
  duration of the round trip. These wrap each call in `asyncio.to_thread`,
  which fixes the actual problem without a second SQL implementation over
  `AsyncEngine` or forcing an async driver on the consumer. A test asserts
  every async signature still matches its synchronous twin, and that no
  database function was left without one.
* **Retention.** `decisions.purgar` and `queue.purgar` delete rows older than
  a cutoff and return how many went, so the caller loops until zero. They
  delete in batches on purpose: an unbounded `DELETE` on a table that only
  grows is the incident retention exists to prevent, not a way to prevent it.
  `queue.purgar` never touches `pendente` or `rodando` by default, because a
  stalled job's row is the evidence `travados` exists to surface. Passing
  `tenant_id` to `decisions.purgar` covers erasure on request, which is a
  different question from retention.
* **`metering.fechar`** closes the Redis client and releases the module
  singleton, for a clean shutdown and for processes that run more than one
  event loop. It never raises, and the singleton is released even when the
  close fails, since the backend already being down is one of the reasons you
  are shutting down.
* **`py.typed`**, so the annotations reach the consumer's type checker instead
  of stopping at the package boundary. CI verifies it travels inside the wheel,
  the same way it already verified the `.sql` files.
* **ruff and mypy in strict mode**, both clean, both enforced by a CI job.
  Annotations were filled in on every public function that took an untyped
  `engine` or `pool`. The pool is a `Protocol` rather than `ArqRedis`, because
  only two methods are ever used and the docstring already said so.

### Changed

* `evidence` and `outcome` come back from the read API already deserialized. A
  row whose JSON is unparseable yields `{}` plus the raw value under
  `evidence_bruto` rather than breaking the whole listing.
* `decisions.registrar` writes an explicit `created_at`. An application that
  inserts into `decisions` with its own SQL is unaffected.

## 0.2.0

* `panorama` takes its limits as an argument instead of reading them from
  configuration, because each project names its own quota types. This is the
  one place the "names are preserved from the origin codebase" guarantee does
  not hold, and it is why the minor version moved.
* `TetoIndisponivel` and `FilaIndisponivel` added, so a caller can tell an
  exhausted quota (429, resets at a known time) from an unreadable backend
  (503) without comparing exception messages.

## 0.1.x

Initial extraction of `metering`, `decisions` and `queue` from the origin
application. `0.1.1` shipped with metadata reporting `0.1.0`, because the
version number lived in two places; it now has a single source in
`pyproject.toml`.
