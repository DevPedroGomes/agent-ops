# agent-ops

Operational primitives for LLM agents that run in production: a spend cap that
refuses when it cannot read itself, an append-only decision trail that answers
"why did it decide that" with a rule code, and a durable job queue that does not
lose work across a redeploy.

Not an agent framework. It does not compete with LangChain, Agno or the OpenAI
SDK, and it does not care which one you use. It is the layer underneath, the
part you write after the first outage.

Extracted from a document Q&A platform serving live traffic, then generalized.
The extraction found a bug that was in production at the time; see
[Design decisions](#design-decisions).

* Python 3.12 or newer
* 1900 lines of source, 192 tests, no infrastructure needed to run them
* Redis for metering and the queue, PostgreSQL for the trail and job progress
* [How it was built](docs/como-foi-construido.md), including the defects found
  along the way

## Contents

* [Install](#install)
* [A note on naming](#a-note-on-naming)
* [Configuration](#configuration)
* [metering](#metering)
* [decisions](#decisions)
* [queue](#queue)
* [API reference](#api-reference)
* [Design decisions](#design-decisions)
* [Operating notes](#operating-notes)
* [Testing](#testing)
* [Known limitations](#known-limitations)
* [Versioning](#versioning)

## Install

```bash
pip install "git+https://github.com/DevPedroGomes/agent-ops.git@v0.3.0"
```

Pinned by tag on purpose. A dependency that tracks `main` breaks your production
without a commit in your repository.

Installing pulls `redis`, `arq`, `sqlalchemy` and `pydantic-settings`. Import
cost is per subpackage, not per install: `agent_ops.metering` loads Redis only,
and neither SQLAlchemy nor arq. A test pins that.

## A note on naming

The public API is in Portuguese (`consumir`, `enfileirar`, `marcar`). It was
extracted from a Portuguese-language codebase and the names were preserved
deliberately, so the origin application could migrate by changing an import line
rather than every call site. Documentation, comments in this file, and all
reasoning are in English.

A short glossary, since the names appear throughout:

| Portuguese | English |
|---|---|
| `consumir` / `devolver` | consume quota / refund quota |
| `fechar` | close the client, at shutdown |
| `aio` | the async twin of a database function |
| `panorama` | overview, for the health endpoint |
| `registrar` / `digerir` | record a decision / hash an input |
| `listar` / `por_execucao` / `filhos` | read the trail: recent / one run / children |
| `contagem_por_regra` | count decisions per rule |
| `purgar` | delete rows past a retention cutoff |
| `enfileirar` / `profundidade` | enqueue / queue depth |
| `marcar` / `ler` / `descartar` | mark progress / read progress / dead-letter |
| `listar_por_estado` / `travados` | list by state / find stalled jobs |
| `tentar_de_novo` / `esgotou` | retry / are retries exhausted |
| `TetoAtingido` / `TetoIndisponivel` | cap reached / cap unreadable |
| `FilaCheia` / `FilaIndisponivel` | queue full / queue unreadable |

## Configuration

Every setting reads from the environment with the `AGENT_OPS_` prefix, so it
cannot collide with the host application's own variables. Unknown variables are
ignored, because the host has dozens that are none of this package's business.

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_OPS_REDIS_URL` | `redis://localhost:6379` | Metering and queue backend |
| `AGENT_OPS_PROJETO` | `default` | Namespaces every Redis key and job id |
| `AGENT_OPS_PROFUNDIDADE_MAXIMA` | `500` | Queue depth above which enqueueing is refused |
| `AGENT_OPS_KILL_SWITCH` | `false` | Stops all paid calls, read live on every call |

Three of these are load-bearing in ways that are easy to miss.

`AGENT_OPS_REDIS_URL` is separate from your application's own `REDIS_URL`. They
may point at different instances. Because metering fails closed, forgetting this
variable does not degrade the service: it refuses every quota-consuming request,
while `localhost` keeps working in development. That failure mode has happened,
which is why the variable is documented this prominently.

`AGENT_OPS_PROJETO` must not contain a colon. It is validated at load time,
because the colon separates the fields of every Redis key and job id this
package builds, and a colon inside a field makes two different keys collapse
into the same one. That collision does not raise; it silently merges the spend
cap of two projects, or hands one tenant another tenant's job.

`AGENT_OPS_KILL_SWITCH` is read with `os.getenv` on every call rather than
through the cached settings object. A cached emergency brake engages only after
a process restart, which is not what an emergency brake is for.

Nothing connects at import time. Clients are created on the first call, so
importing the package in a test, a migration script or a CLI opens no sockets.

---

## metering

### The problem

A public demo spends real money per click. A per-caller rate limit does not
help: it bounds what one caller does per minute, not what everyone does
together, and it does nothing about someone creating new accounts. With open
signup, "require login" is not a spending cap.

### The guarantees

* Quota is consumed before the paid call, never after. Counting afterwards lets
  a concurrent burst through, because none of them have been counted yet.
* `INCR` first, check second. Two concurrent requests each see their own
  post-increment total, so neither escapes the cap. Whoever loses the race
  undoes its own increment.
* **A refused call costs no quota, and the server guarantees it.** Reserving,
  checking and compensating happen in one Lua script, so there is no window in
  which the counter is inflated and no second round trip that can fail. While
  compensation was a separate call, a blink of the network left a refused call
  charged until the day rolled over, and later visitors were turned away for
  quota nobody spent.
* An unreadable backend refuses. An unreadable cap is not an absent cap.
* The day rolls over in UTC, so the reset does not drift with the server's
  timezone.
* Counters are namespaced by project, day, quota type and optional scope, so a
  per-IP cap and a global cap can run side by side without consuming each other.

### Usage

```python
from agent_ops import metering

try:
    remaining = await metering.consumir("chat", limite=300)
except metering.TetoIndisponivel as exc:
    # Backend unreadable. This is unavailability, not a limit. No Retry-After:
    # nobody knows when Redis comes back.
    raise HTTPException(503, exc.mensagem) from exc
except metering.TetoAtingido as exc:
    # Daily cap reached. This is a usage limit, and it resets at a known time.
    raise HTTPException(
        429, exc.mensagem,
        headers={"Retry-After": str(metering.segundos_ate_meia_noite_utc())},
    ) from exc

try:
    result = await call_provider()
except Exception:
    await metering.devolver("chat")   # the paid call never happened
    raise
```

`TetoIndisponivel` subclasses `TetoAtingido`, so existing `except TetoAtingido`
clauses keep working. Order matters: Python matches top to bottom, so the narrow
handler must come first or every 503 silently becomes a 429.

`devolver` is best-effort and never raises on a backend failure. A lost refund
costs a little slack; an unrecorded spend costs money. It does raise
`ValueError` for a non-positive amount, because `devolver(-1)` is an increment
in disguise, which is charging quota through the refund path.

It also never drives the counter below zero. The key is derived from today's
date, so a call reserved at 23:59:59 whose refund runs at 00:00:01 lands on
tomorrow's key. Refunding what was never consumed has to be harmless rather
than turn into credit for everyone using that quota.

`unidades` is where computed values enter: pages in a PDF, an estimated token
count. Zero and negative are rejected for the same reason, from the other side.
A negative increment would push the counter below zero and hand free budget to
everyone.

Scoped caps use the same call:

```python
await metering.consumir("chat", limite=20, escopo=f"ip:{client_ip}")
```

The scope is the last field of the key, so it may contain colons. The quota type
may not, and is validated.

Pass the same scope to `panorama` to read that counter back. Scoped and global
counts are separate accounts on purpose, so neither shows up in the other.
There is no way to enumerate every scope: they are usually per IP, and scanning
Redis for them would put an unbounded operation in the health check path.

### Shutdown

```python
await metering.fechar()   # in the app lifespan teardown
```

The Redis client is created on the first call and held for the process. That is
correct for a service that stays up, and it gets in the way twice: an
interpreter exiting under load complains about an unclosed connection, and a
process that runs more than one event loop finds the client bound to the first
one. `fechar` closes it and releases the singleton, so the next call opens a
fresh client. It never raises, and it releases the singleton even when the
close fails.

### Health endpoint

```python
await metering.panorama({"chat": 300, "ingest": 100})
# {"date": "2026-08-24", "escopo": None, "kill_switch": False,
#  "degraded": False, "used": {...}, "limits": {...}, "remaining": {...}}

await metering.panorama({"chat": 20}, escopo=f"ip:{client_ip}")   # one visitor
```

Limits are passed in rather than read from configuration, because each project
names its own quota types and the package should not know them.

`panorama` never raises, and both branches return the same shape. A health check
that dies when Redis is down turns a partial degradation into a full outage
page, and a handler reading `p["remaining"]` would raise `KeyError` exactly when
the backend is unreachable. When `degraded` is true, the counters read zero
because the real values are unknown, not because quota is exhausted.

The kill switch is read live here too, so the health endpoint cannot report
"running" while `consumir` is already refusing.

---

## decisions

### The problem

When an agent writes to a customer's system, somebody will eventually ask why.
"The model decided" is not an answer that survives an audit. Decisions that live
only in a response stream are gone when the page reloads.

### The guarantees

* `registrar` never raises. The trail is observability. Losing a row is bad;
  killing the response a visitor was already receiving because of an `INSERT` is
  worse. It returns the new row id, or `None` when the write failed.
* Metadata, never content. `input_digest` is a hash. There is no column for the
  input text, and a test enforces the exact column set, so any new column under
  any name fails it. Without that rule the trail becomes a second copy of the
  corpus, without the isolation protections of the first.
* `rule_code` is `NOT NULL`. It is the field that answers "why" with a code
  instead of generated prose.
* `tenant_id` on every row, and reads are expected to filter by it.
* The DDL is idempotent and safe to run on every boot.
* The DDL is portable: the same file runs on PostgreSQL and on SQLite, so there
  is no second schema to keep in sync.

### Usage

```python
from agent_ops import decisions

decisions.migracao.aplicar(engine)   # idempotent, safe on every boot

decisions.registrar(
    engine,
    tenant_id=user_id,
    correlation_id=run_id,           # ties every decision of one run together
    input_digest=decisions.digerir(resume_bytes),
    rule_code="RUBRIC.PYTHON.SENIOR",
    evidence={"years_python": 7, "source": "line 12"},
    outcome={"points": 30},
    model="claude-haiku-4-5",
    tokens_in=800, tokens_out=120, cost_cents=2,
    parent_id=orchestrator_decision_id,   # worker to orchestrator
)
```

The split between `evidence` and `outcome` is the point. The model extracts
evidence, with a pointer to where it came from. A deterministic rule decides.
Changing the rule re-scores the entire history without spending a token, and the
same input always produces the same output.

Arguments are keyword-only: twelve fields, and a wrong positional order would
write `outcome` into `evidence` with no type error to catch it.

`digerir` accepts `str` or `bytes` and normalizes `str` to UTF-8 first, so the
same content read from an upload and from a form field produces the same digest.
Without that, the queue's deduplication would let the repeated work through.

### Reading the trail

Four indexes exist for the four reads the trail was designed for, and one
function covers each:

```python
from agent_ops import decisions

decisions.listar(engine, tenant_id=user_id, limite=50)
decisions.por_execucao(engine, tenant_id=user_id, correlation_id=run_id)
decisions.filhos(engine, tenant_id=user_id, parent_id=orchestrator_id)
decisions.contagem_por_regra(engine, desde=last_week)
```

| Function | The question it answers | Index |
|---|---|---|
| `listar` | What did this tenant's agent decide recently | `(tenant_id, created_at DESC)` |
| `por_execucao` | Replay one run in order | `(correlation_id, created_at)` |
| `filhos` | Which workers did this orchestrator spawn | `(parent_id)` |
| `contagem_por_regra` | Which rule always falls through to the safety net | `(project, rule_code)` |

`tenant_id` is keyword-only and has no default on every query that returns
rows. A correlation id is not a permission: it appears in logs, in URLs and in
error payloads, and a query filtered by it alone hands over another customer's
entire trail. Making the tenant part of the signature is what turns that rule
from a convention into something you cannot forget, and a test fails if anyone
gives it a default.

`contagem_por_regra` is the one query without a tenant, deliberately. It asks
about the whole project rather than about any customer, which is why it returns
counts and no rows: no digest, no id, nothing that ties a number to a person.

`evidence` and `outcome` come back as dictionaries rather than as the stored
JSON text. A row whose JSON does not parse yields `{}` plus the raw value under
`evidence_bruto`, because a trail that refuses to open because of one bad row
is worse than one incomplete row.

These reads raise when the database fails, unlike every write in this package.
The inversion is deliberate. A write swallows its failure because losing one
row of observability beats killing the response a visitor was already
receiving. A read cannot do the same: an empty list caused by an outage is
indistinguishable from "this tenant made no decisions", and that is the most
dangerous answer an audit can give.

`listar` paginates by timestamp, not by offset. Pass the `created_at` of the
last row you saw as `antes_de`. The table only grows, and a large offset
rescans everything before it on every page.

---

## queue

### The problem

Work that must not vanish. Under FastAPI's `BackgroundTasks`, a job runs inside
the web process after the response: a redeploy mid-job loses it silently, and a
blocking call stalls everything including the health endpoint.

### The guarantees

* Deduplication rides on arq's own uniqueness. `enqueue_job(_job_id=X)` returns
  `None` when a job with that id is queued or running, so dedup needs no table
  and no lock. This package adds the project and tenant namespace.
* Refusal before acceptance. Above the depth cap, or when the depth cannot be
  read, enqueueing raises rather than accepting work it cannot do. A queue that
  only grows is indistinguishable from a service that is down, except it lies to
  the client.
* Durable progress in PostgreSQL, so it survives the process and outlives arq's
  result key.
* Exponential backoff with a ceiling: 5s, 10s, 20s, 40s, capped at 300s. Without
  growth, five retries against a downed provider all land in the same second,
  which is one chance, not five.
* A dead-letter state carrying a human-readable reason.

### Setup

```python
from agent_ops import queue

queue.aplicar_schema(engine)   # idempotent; call it on both web and worker boot
```

Forgetting this is silent: `marcar` swallows "no such table" and `ler` returns
`None`, which is indistinguishable from "the job never started". The only trace
is a log line, which is why that log line carries the exception type.

### Enqueueing

```python
from agent_ops import decisions, queue

pool = await queue.criar_pool()       # once per process, in the app lifespan

digest = decisions.digerir(file_bytes)
job_id = queue.job_id_de(digest, tenant=user_id)

try:
    await queue.enfileirar(
        pool, "ingest", doc_id,
        digest=digest, tenant=user_id,
    )
except queue.FilaIndisponivel as exc:
    raise HTTPException(503, exc.mensagem) from exc
except queue.FilaCheia as exc:
    raise HTTPException(429, exc.mensagem,
                        headers={"Retry-After": str(exc.retry_after)}) from exc

return {"job_id": job_id}
```

Two things worth reading twice.

`enfileirar` returning `None` means the work was already queued, and that is
success, not failure. The caller only needs to know the work will happen. That
is why the response uses `job_id_de`, which is deterministic and available on
both paths; returning the enqueue result would leave the client unable to watch
its own upload.

`tenant` is not optional in practice. Without it, two users who submit
byte-identical content collide onto the same job id: the second is told the work
is happening, no job runs for them, and they poll a job belonging to someone
else. Neither `tenant` nor `digest` may contain a colon, for the same reason
`AGENT_OPS_PROJETO` may not.

Depth is read from the pool's own queue name, not from arq's default, so an
application that names its queue still gets backpressure.

### The worker

```python
import asyncio
from arq.connections import RedisSettings
from agent_ops import queue
from agent_ops.config import get_config

async def ingest(ctx, doc_id):
    job_id = ctx["job_id"]
    queue.marcar(engine, job_id, estado="rodando", percentual=0,
                 tentativas=ctx["job_try"])
    try:
        await do_the_work(doc_id)
    except asyncio.CancelledError:
        # job_timeout and worker shutdown cancel the task. CancelledError
        # derives from BaseException, so the clause below does not catch it,
        # and without this clause the row stays at "rodando" forever.
        # Record it as pending, not as failed: with retry_jobs left at its
        # default, arq puts a cancelled job back on the queue.
        queue.marcar(engine, job_id, estado="pendente",
                     detalhe="interrupted, will run again")
        raise
    except Exception as exc:
        if queue.esgotou(ctx):
            queue.descartar(engine, job_id, motivo=f"{type(exc).__name__}: {exc}")
            return
        queue.marcar(engine, job_id, estado="pendente",
                     detalhe=f"retrying after {type(exc).__name__}")
        queue.tentar_de_novo(ctx)          # raises arq.Retry with backoff
    else:
        queue.marcar(engine, job_id, estado="concluido", percentual=100)

class WorkerSettings:
    functions = [ingest]
    redis_settings = RedisSettings.from_dsn(get_config().redis_url)
    max_tries = queue.MAX_TENTATIVAS      # tie these together, see below
    max_jobs = 4                          # size this to the host, not to hope
    job_timeout = 1_800
    health_check_interval = 30
```

Run it: `arq module.WorkerSettings`

`max_tries` and `esgotou` must use the same constant. In arq, when
`job_try > max_tries` the job is finished without calling the function. A
mismatch means `descartar` never runs and the row stays `rodando` forever, so
the job disappears from the operations view with no error anywhere. Declaring
`max_tries = queue.MAX_TENTATIVAS` ties the two by construction.

The work function must let failures escape. If it catches everything and returns
normally, the envelope never sees a failure: retry, backoff and dead-letter
become dead code, and the job records success for work that failed.

`marcar`, `ler`, `registrar` and `descartar` are synchronous SQLAlchemy calls.
arq runs up to `max_jobs` jobs concurrently on a single event loop, so a
progress tick blocks its siblings and the worker health check for the duration
of the round trip. With a local PostgreSQL that is microseconds and does not
matter. Otherwise use the async twins, which run the same call off the loop:

```python
from agent_ops.queue import aio as queue_aio

await queue_aio.marcar(engine, job_id, estado="rodando", percentual=0)
```

Every database function has one, in `decisions.aio` and `queue.aio`, with the
same name and the same signature. They are `asyncio.to_thread` wrappers rather
than a second SQL implementation over `AsyncEngine`, which would mean
maintaining two versions of every statement and forcing an async driver on the
consumer. A test asserts that no signature drifts and that no database function
is left without a twin.

### Reading progress

```python
p = queue.ler(engine, job_id)
# None            -> never marked (or the read failed; check the log)
# p["estado"]     -> pendente | rodando | concluido | falhou | descartado
# p["percentual"] -> preserved on dead-letter: a job that died at 80% shows 80%
# p["detalhe"]    -> the sentence the UI displays
# p["tentativas"] -> retries of the job, not progress updates
# p["atualizado"] -> when it last changed
```

Omitting `percentual` or `detalhe` in `marcar` preserves the stored value;
passing them overwrites. Only omission preserves, so a progress tick does not
erase the last message, and a dead-letter does not erase how far the job got.
Passing `percentual=0` does write zero, because a retry legitimately restarts
the bar.

`tentativas` follows the same rule and is never auto-incremented. It counts
retries of the job, reported by the worker from `ctx["job_try"]`. Incrementing
it inside `marcar` would make the column count screen refreshes.

`atualizado` is returned because without it, "running for ten seconds" and
"running since the worker took a SIGKILL an hour ago" look identical, and the
second is the one somebody needs to see.

### The operations view

`ler` answers for one job, which is what a client polling its own upload needs.
Operating the queue needs the opposite questions:

```python
from agent_ops import queue

queue.listar_por_estado(engine, "descartado", limite=50)
queue.travados(engine, mais_velho_que_segundos=1800)
```

`listar_por_estado` sweeps the dead letter, newest first. `travados` finds jobs
stalled in a state, oldest first, which in practice means `rodando` with a
stale `atualizado`: the worker took a SIGKILL mid-flight. Nothing in arq
reports that, because from its point of view the job was delivered, so without
this query the row stays `rodando` forever.

Both raise on a database failure, unlike `ler`. An empty dead letter caused by
an outage says "nothing died", which is the most expensive wrong answer that
screen can give.

Neither takes a tenant, because `job_progress` has no tenant column. The job id
carries the tenant, so filter on it yourself if an operations view is exposed
to customers rather than to operators.

---

## API reference

Everything below is re-exported from its subpackage, so
`from agent_ops import metering, decisions, queue` reaches all of it.

### `agent_ops.metering`

| Name | Signature | Raises |
|---|---|---|
| `consumir` | `async (tipo, limite, unidades=1, escopo=None) -> int` | `TetoAtingido`, `TetoIndisponivel`, `ValueError` |
| `devolver` | `async (tipo, unidades=1, escopo=None) -> None` | `ValueError` only |
| `panorama` | `async (limites: dict[str, int], escopo=None) -> dict` | never |
| `fechar` | `async () -> None`, closes the client | never |
| `segundos_ate_meia_noite_utc` | `() -> int` | never |
| `TetoAtingido` | `Exception` with `.mensagem` | |
| `TetoIndisponivel` | subclass of `TetoAtingido` | |

### `agent_ops.decisions`

| Name | Signature | Raises |
|---|---|---|
| `digerir` | `(payload: str \| bytes) -> str` | |
| `registrar` | `(engine, *, tenant_id, correlation_id, input_digest, rule_code, evidence=None, outcome=None, model=None, tokens_in=0, tokens_out=0, cost_cents=0, parent_id=None) -> str \| None` | never |
| `listar` | `(engine, *, tenant_id, limite=50, antes_de=None) -> list[dict]` | on a broken engine, `ValueError` |
| `por_execucao` | `(engine, *, tenant_id, correlation_id, limite=500) -> list[dict]` | on a broken engine, `ValueError` |
| `filhos` | `(engine, *, tenant_id, parent_id, limite=500) -> list[dict]` | on a broken engine, `ValueError` |
| `contagem_por_regra` | `(engine, *, desde=None) -> dict[str, int]` | on a broken engine |
| `purgar` | `(engine, *, antes_de, tenant_id=None, limite=1000) -> int` | on a broken engine, `ValueError` |
| `migracao.aplicar` | `(engine) -> None` | on a broken engine |
| `migracao.SQL_SCHEMA` | `str`, the packaged DDL | |

### `agent_ops.queue`

| Name | Signature | Raises |
|---|---|---|
| `criar_pool` | `async () -> ArqRedis` | on a broken Redis |
| `enfileirar` | `async (pool, funcao, *args, digest, tenant=None, **kwargs) -> str \| None` | `FilaCheia`, `FilaIndisponivel`, `ValueError` |
| `job_id_de` | `(digest, tenant=None) -> str` | `ValueError` |
| `profundidade` | `async (pool) -> int` | on a broken Redis |
| `aplicar_schema` | `(engine) -> None` | on a broken engine |
| `marcar` | `(engine, job_id, *, estado, percentual=None, detalhe=None, tentativas=None) -> None` | `ValueError` on a bad state |
| `ler` | `(engine, job_id) -> dict \| None` | never |
| `listar_por_estado` | `(engine, estado, *, limite=50) -> list[dict]` | on a broken engine, `ValueError` |
| `travados` | `(engine, *, mais_velho_que_segundos, estado="rodando", limite=50) -> list[dict]` | on a broken engine, `ValueError` |
| `purgar` | `(engine, *, antes_de, estados=ESTADOS_TERMINAIS, limite=1000) -> int` | on a broken engine, `ValueError` |
| `ESTADOS_TERMINAIS` | `frozenset`: concluido, falhou, descartado | |
| `descartar` | `(engine, job_id, *, motivo) -> None` | never |
| `backoff` | `(job_try: int) -> int` | never |
| `tentar_de_novo` | `(ctx) -> NoReturn` | always, `arq.Retry` |
| `esgotou` | `(ctx, max_tries=MAX_TENTATIVAS) -> bool` | never |
| `ESTADOS` | `frozenset` of the five state names | |
| `MAX_TENTATIVAS` | `int`, 5 | |
| `FilaCheia` | `Exception` with `.mensagem`, `.retry_after` | |
| `FilaIndisponivel` | subclass of `FilaCheia` | |

---

## Design decisions

The choices a reviewer is most likely to question, and why they are what they
are.

Fail closed for anything that gates money or work; fail open for anything that
only reports. `consumir` and `enfileirar` refuse when their backend is
unreadable. `devolver`, `panorama`, `registrar`, `marcar` and `ler` never raise
on a backend failure. The rule is consistent across all three subpackages: if it
authorizes, it refuses under uncertainty; if it observes, it degrades.

The one exception is programmer error. An invalid state name, a non-positive
unit count, a colon inside a key field: these raise `ValueError`, because they
surface in the first test run and never during a traffic spike.

Two exception types where one would be simpler. `TetoIndisponivel` and
`FilaIndisponivel` subclass their respective limit exceptions. Without them a
caller cannot distinguish "you hit the limit" (429, resets at a known time) from
"the backend is unreadable" (503, nobody knows when). Collapsing both into one
type means every dashboard reports saturation during an outage.

A production bug found during extraction. The original spend cap had `INCR` and
`EXPIRE` inside the same `try`. If `INCR` succeeded and `EXPIRE` then failed, a
transient blip between two round trips, execution fell into the generic handler
and raised without undoing the increment. A refused call charged the next
visitor's quota. The fix separates the two error scopes: the TTL is
housekeeping, not correctness, so a failure there logs and continues. There is a
regression test that reproduces the original bug.

Subpackages do not import each other. `metering` pulls no SQLAlchemy. A test
pins the actual import cost of each subpackage, including the one case where the
cost is real: `queue` does load SQLAlchemy, because its durable half is a
PostgreSQL table, and the test asserts that rather than wishing otherwise.

The schema is portable by construction, not by translation. Plain `TEXT` and
`INTEGER` rather than native `UUID` and `JSONB`, so the same DDL file runs on
PostgreSQL and SQLite. One schema, no drift.

The DDL ships as a `.sql` file rather than a Python string. It stays reviewable
as SQL, it colorizes in an editor, and an operator can run it by hand against
production without booting the application.

No migration framework. Both schemas are `CREATE TABLE IF NOT EXISTS` plus
`CREATE INDEX IF NOT EXISTS`, applied by the host application at boot with its
own engine. The package never owns a connection. Adding a column is meant to be
deliberate: it breaks the column-set test, which is the point.

---

## Operating notes

Things that are correct in code and still need a decision from whoever runs it.

Timestamps are UTC regardless of how your database is configured, and you do
not have to arrange that. `created_at` and `atualizado` are stamped in UTC from
Python rather than taken from the database default, because
`CURRENT_TIMESTAMP` into a column without a time zone resolves to UTC on SQLite
and to the session time zone on PostgreSQL. The DDL keeps its default as a
fallback for hand-written SQL, so an `INSERT` you run yourself against a
database in a local zone still stores local time. Both columns read back as
naive UTC `datetime` on both backends.

Schedule the retention job. Nothing deletes on its own. `decisions` grows per
decision rather than per request, so an orchestrator that spawns five workers
writes six rows, and `job_progress` keeps one row per job forever. Both
`purgar` functions delete in batches and return how many rows went, so the
caller loops:

```python
from datetime import timedelta
from agent_ops import decisions, queue
from agent_ops.tempo import agora_utc

corte = agora_utc() - timedelta(days=365)
while decisions.purgar(engine, antes_de=corte):
    pass
while queue.purgar(engine, antes_de=agora_utc() - timedelta(days=30)):
    pass
```

Batching is not a detail. An unbounded `DELETE` on a table that only grows
holds the transaction and the write-ahead log for the whole scan, which turns
the cure into the outage that retention existed to prevent.

`queue.purgar` leaves `pendente` and `rodando` alone unless you name them
explicitly. A job stuck in `rodando` with a stale timestamp is exactly what
`travados` exists to surface, and sweeping the row away resolves the symptom by
destroying the evidence while the work stays lost.

`decisions.purgar` also takes an optional `tenant_id`, which answers a
different question: erasure on request rather than retention by age.
`decisions` grows per decision, not per request, so an orchestrator that spawns
five workers writes six rows. Schedule a delete, or plan for the disk.

The Redis client is a process-level singleton created on first use and never
closed. On a normal service that is correct and cheap. It matters in two places:
a process that runs more than one event loop over its lifetime, such as a script
calling `asyncio.run` twice, will find the client bound to the first loop; and
there is no explicit shutdown, so an interpreter exiting under load may log an
unclosed-connection warning.

Queue depth counts every job in arq's sorted set, which includes deferred jobs
and jobs waiting out a retry backoff. During a provider outage, retrying jobs
raise the measured depth and the API starts shedding new work sooner. That is
usually the behavior you want, but it is not the same number as "jobs waiting
right now".

The depth check and the enqueue are two round trips, so the cap is soft.
Concurrent requests can each see room and each enqueue, overshooting the cap by
roughly the concurrency level. For backpressure that is fine; do not read
`profundidade_maxima` as a hard bound.

The Redis server must allow `EVAL`. Quota reservation is a Lua script, which
is standard Redis and has been since 2.6, but a few managed offerings and
proxies disable scripting. If yours does, `consumir` fails closed and refuses
every call rather than quietly falling back to something weaker.

A digest is not anonymization. `digerir` hashes whatever you give it. Hashing a
document is fine, since guessing it is infeasible. Hashing a low-entropy
identifier, an email address or a national id number, produces a digest that can
be reversed by enumeration, and under most privacy regimes a value that still
singles out a person remains personal data. Digest the content, not the
identity.

`ler` trusts the job id it is given. The id embeds the tenant, which is what
separates two tenants who uploaded byte-identical files, so never pass a
client-supplied job id through without checking that its tenant field matches
the caller. The progress table has no tenant column to catch it for you.

---

## Testing

95 tests, no Redis, no PostgreSQL, no network, no provider key. Async functions
are tested with `asyncio.run()` and hand-written doubles rather than a plugin, to
avoid adding a test dependency the consuming applications do not already have.

The suite runs in under a second, which matters more than it sounds: a test
suite that needs infrastructure is a test suite that gets skipped.

```bash
pip install -e ".[dev]"
python -m pytest tests/ -q
ruff check src/ tests/
mypy
```

`ruff` and `mypy` in strict mode both run clean and both gate CI. Strict is
worth the cost in a package other code imports: without it the annotations
serve the reader and not the caller, and `py.typed` would export promises
nobody checks.

CI does two things. It runs the suite, and it builds a wheel and installs it
into a clean virtualenv, asserting that the packaged `.sql` files are present.
An editable install resolves those from the source tree regardless, so without
that second job the package could stop being installable with CI fully green.

A dedicated test pins the public surface of all three subpackages. The consuming
application migrates by changing an import line, so a silent rename from
`consumir` to `consume` has to break here rather than at someone's deploy.

Two more files run the same code against real PostgreSQL and real Redis, and
skip when `AGENT_OPS_TEST_POSTGRES_URL` and `AGENT_OPS_TEST_REDIS_URL` are
unset, so the fast path stays fast. CI sets both and fails if those tests skip,
because a typo in a variable name would turn that whole coverage into a silent
pass.

The Redis file exists because the double cannot execute Lua and cannot be
concurrent. What runs in the fast suite is a model of the reservation script;
the real file runs the script. It also runs the test a sequential double can
never perform: 50 simultaneous reservations against a cap of 10. With a
non-atomic check-then-reserve, all 50 get through.

That fixture pins the session time zone to `America/Bahia` on purpose. The
official PostgreSQL image boots in UTC, and in UTC the defect these tests exist
for does not reproduce: `CURRENT_TIMESTAMP` coincides with UTC and everything
goes green by accident. An offset is what gives the test a chance to fail.

```bash
AGENT_OPS_TEST_POSTGRES_URL=postgresql+psycopg://user:pass@localhost/db \
AGENT_OPS_TEST_REDIS_URL=redis://localhost:6379/15 \
  python -m pytest tests/ -q
```

---

## Known limitations

Stated plainly, because a library that hides its limits wastes your time.

No cross-request deduplication by content. The digest identifies whatever you
hash. If you hash per-request identifiers, re-submitting the same file runs
again. Content-addressed dedup needs a `(tenant, content_digest)` row consulted
before the quota is charged, which is the application's decision, not this
package's.

Deduplication is time-bounded. arq drops a duplicate only while the job or its
result key lives, and `keep_result` is one hour by default. After that the same
digest enqueues again and the work is repeated, and recharged.

Timeouts do not reach the dead-letter. `descartar` runs from inside your work
function, so it only sees exceptions your function raised. A job cancelled by
`job_timeout` is retried by arq, and once `job_try` passes `max_tries` arq
finishes it without calling the function at all. Such a job never gets a
`descartado` row; find it by its stale `atualizado`.

Refunds are keyed to the current UTC day, not to the day the quota was
reserved. A refund for a call reserved just before UTC midnight lands on the
new day's key, where it is floored at zero rather than credited. The refund is
lost, which is the documented best-effort behavior for `devolver`, but it can
no longer raise anyone's cap.

The trail has no tenant-scoped delete. `contagem_por_regra` is the only query
that crosses tenants, and it returns counts rather than rows, but a data
subject erasure request still means writing the `DELETE` yourself.

No fair-share scheduling. One tenant can occupy the whole worker pool. arq has
no native support for it, and doing it properly means per-tenant queues and a
scheduler, which is a larger piece of work than the rest of this package.

No metrics. Everything is stdlib `logging` with dotted event names
(`metering.esgotado`, `queue.cheia`, `decisions.gravacao_falhou`) and key=value
pairs. There is no Prometheus exporter and no OpenTelemetry integration.

It is not an agent framework, a prompt library, or a model router.

---

## Versioning

Semantic versioning, consumed by git tag. The version has a single source, the
`version` field in `pyproject.toml`, read back at runtime through
`importlib.metadata`. An earlier release shipped with the number written in two
places and the two disagreeing, which is the failure mode that arrangement
produces.

Every release that changes what a pinned upgrade does to you is written down in
[CHANGELOG.md](CHANGELOG.md).

`0.3.0` fixed timestamps that were UTC in tests and local time in production,
added the read API for the trail, and put PostgreSQL in CI. `0.2.0` changed
`panorama` to take its limits as an argument instead of reading them from
configuration, which is the one place the "names are preserved" guarantee does
not hold.

Pin the tag. Read the diff before moving it.
