# How it was built

agent-ops was extracted from a document Q&A platform running in production and
then generalized so other applications could install it. This page lists the
constraints set before writing code, the main design decisions, and the defects
found along the way. Each defect comes from the [changelog](../CHANGELOG.md) or
from a commit message in this repository, and the commit is named where there
is one.

The first version followed a plan of eight tasks: the package skeleton, two
tasks each for `metering`, `decisions` and `queue`, and the README. Each of the
first seven began with tests that had to fail before any implementation was
written. Versions 0.1.0 to 0.2.0 were tagged on 2026-08-24, and 0.3.0 on
2026-09-15.

## Constraints

* Python 3.12, the same base as the origin application's image.
* No `pytest-asyncio`. Async code is tested with `asyncio.run()` and
  hand-written doubles, so a consuming application gets no new test dependency.
* Public names kept from the origin code (`consumir`, `devolver`, `panorama`,
  `TetoAtingido`), so the origin could migrate by changing an import line. This
  is why the API is in Portuguese.
* A Redis that cannot be read refuses the call.
* Importing the package opens no connection.
* The decision trail stores metadata and never user content.

## Design decisions

* The three subpackages do not import each other. `metering` loads Redis only.
  `queue` also loads SQLAlchemy, because durable progress is a database table.
  An early docstring claimed otherwise, and a test now pins the real import
  cost of each subpackage (53f6814).
* What authorizes spend or work fails closed. What only reports never raises
  on a backend failure.
* `TetoIndisponivel` and `FilaIndisponivel` subclass the limit exceptions, so a
  caller can answer 503 for an outage and 429 for an exhausted limit. Before
  them, every outage became a 429 with `Retry-After: 30` (e95b232).
* One DDL file, written with `TEXT` and `INTEGER`, runs on PostgreSQL and
  SQLite.
* Fair-share scheduling per tenant was left out. arq has no native support for
  it, and building it needs per-tenant queues and a scheduler.

## Defects found along the way

### Inherited from the origin code

* **A refused call charged the next visitor.** The origin spend cap ran `INCR`
  and `EXPIRE` inside the same `try`. When `INCR` succeeded and `EXPIRE` failed
  between the two round trips, the generic handler raised before the code that
  undid the increment, so the call was refused and its quota stayed charged.
  Commit 01ef190 split the two error scopes. In 0.3.0 the reservation moved
  into a Lua script, where `EXPIRE` runs through `redis.pcall` so its error
  cannot abort the script after the increment is written.

### metering

* **Negative or zero units.** `unidades=-5` against `limite=10` returned 15
  remaining and left the counter at -5. `unidades=0` passed the cap without
  reserving anything (9340fe5).
* **Colliding keys.** Fields were joined with `:` and nothing was escaped, so
  `_chave("a", escopo="b")` and `_chave("a:b")` were the same key (fe143b3).
* **`panorama` broke when Redis was down.** The degraded result omitted
  `remaining`, so a health check reading it raised `KeyError` (1a465df).
* **The kill switch was frozen at boot**, read once per process through a
  cached settings object. It is now read with `os.getenv` on every call
  (80444da). A later review found the README still promised more: a running
  process's environment cannot be changed from outside, so engaging the switch
  on a deployed service takes a restart. The README now says so.
* **Keys that never expired.** The TTL was applied only when the counter after
  the increment equalled the units reserved, which holds only when the counter
  starts at zero. After an orphaned refund left it negative, later increments
  could skip that value and the key never got an expiry (0.3.0).
* **A refused call could still charge quota.** Reserve, check and compensate
  were two round trips, and a failed compensation left the counter inflated
  until the day rolled over. All three now run in one Lua script. Measured
  against a real Redis, a non-atomic check-then-reserve let 50 concurrent calls
  through a limit of 10 (0.3.0).
* **A refund could raise the cap.** A refund that ran after UTC midnight landed
  on the next day's key and created it with a negative value. Measured before
  the fix, 15 units were served against a limit of 10. The counter is now
  floored at zero (0.3.0).
* **An unguarded floor on `Retry-After`.** The one-second floor keeps the last
  fraction of a second before midnight from becoming `Retry-After: 0`. Its
  docstring placed it at the wrong moment, and its test passed without it
  (dac87c9).

### decisions and queue

* **Timestamps in two time zones.** `CURRENT_TIMESTAMP` on a column without a
  time zone is UTC on SQLite and the session time zone on PostgreSQL, so tests
  stored UTC and production stored local time. Timestamps are now written in
  UTC from Python, and the PostgreSQL tests pin `America/Bahia` because the
  official image boots in UTC and hides the defect (0.3.0).
* **Two tenants, one job.** The job id was built from project and digest only.
  A second tenant uploading the same file got success with no job and read the
  first tenant's progress (ed98bb6).
* **No backpressure on named queues.** Depth was read from arq's default queue,
  so an application with its own queue name was never refused (c4fd3d1).
* **Progress lost on dead-letter.** `marcar` overwrote omitted fields with
  defaults, so a job that died at 80% showed 0% (af6a05d).
* **Jobs that never reached the dead-letter.** With `max_tries=3` in the worker
  and `esgotou` counting to 5, arq stopped the job before the attempt that
  would call `descartar`. Both now use `MAX_TENTATIVAS` (9ea6c7a).
* **Untranslated errors.** A failure during the enqueue itself escaped
  `enfileirar` as a raw driver error, and `ler` returned `atualizado` as a
  `datetime` on PostgreSQL and a string on SQLite (0.3.0).

### Packaging and tests

* **SQL files missing from the wheel would go unnoticed**, because an editable
  install reads them from the source tree. CI now installs the built wheel in a
  clean virtualenv. Removing `package-data` locally produced a wheel without
  `schema.sql` and a failing import (7c7d97e).
* **Version numbers.** 0.1.1 shipped with metadata saying 0.1.0, and the
  version now lives only in `pyproject.toml` (dac87c9). A later test compared
  against the literal "0.2.0" and passed locally on stale editable metadata
  before failing in CI (ed7e1c5).
* **Configuration leaking between tests.** A test that turned the kill switch
  on left it on, so a later test expecting a refusal could pass without running
  the code under test (8c125a7).
* **The `pcall` around `EXPIRE` had no test.** The fast suite runs a Python
  model of the Lua script, and the model swallows the `EXPIRE` error on its
  own. Replacing `redis.pcall` with `redis.call` left all 192 tests green,
  including those against a real Redis. Two tests now make `EXPIRE` fail inside
  the real script, and both fail under that change (unreleased).
