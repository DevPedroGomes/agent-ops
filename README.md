# agent-ops

Operational primitives for LLM agents that run in production: a spend cap that
refuses when it cannot read itself, an append-only decision trail that answers
"why did it decide that" with a rule code, and a durable job queue that does not
lose work across a redeploy.

Not an agent framework. It does not compete with LangChain, Agno or the OpenAI
SDK, and it does not care which one you use. It is the layer underneath — the
part you write after the first outage.

Extracted from a document Q&A platform serving live traffic, then generalized.
The extraction found a bug that was in production at the time; see
[Design decisions](#design-decisions).

- Python 3.12+
- 947 lines of source, 95 tests
- Redis for metering and the queue, PostgreSQL for the trail and job progress
- [How it was built](docs/como-foi-construido.md), including the defects found on the way

## Install

```bash
pip install "git+https://github.com/DevPedroGomes/agent-ops.git@v0.2.0"
```

Pinned by tag on purpose. A dependency that tracks `main` breaks your production
without a commit in your repository.

## A note on naming

The public API is in Portuguese (`consumir`, `enfileirar`, `marcar`). It was
extracted from a Portuguese-language codebase and the names were preserved
deliberately, so the origin application could migrate by changing an import line
rather than every call site. Documentation, comments in this file, and all
reasoning are in English.

## Configuration

Every setting reads from the environment with the `AGENT_OPS_` prefix, so it
cannot collide with the host application's own variables.

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_OPS_REDIS_URL` | `redis://localhost:6379` | Metering and queue backend |
| `AGENT_OPS_PROJETO` | `default` | Namespaces every Redis key and job id |
| `AGENT_OPS_PROFUNDIDADE_MAXIMA` | `500` | Queue depth above which enqueueing is refused |
| `AGENT_OPS_KILL_SWITCH` | `false` | Stops all paid calls, read live on every call |

Two of these are load-bearing in ways that are easy to miss.

`AGENT_OPS_REDIS_URL` is **separate** from your application's own `REDIS_URL`.
They may point at different instances. Because metering fails closed, forgetting
this variable does not degrade the service — it refuses every quota-consuming
request, while `localhost` keeps working in development. That failure mode has
happened; it is why the variable is documented this prominently.

`AGENT_OPS_KILL_SWITCH` is read with `os.getenv` on every call rather than
through the cached settings object. A cached emergency brake engages only after
a process restart, which is not what an emergency brake is for.

---

## metering

### The problem

A public demo spends real money per click. A per-caller rate limit does not
help: it bounds what **one** caller does per minute, not what **everyone** does
together, and it does nothing about someone creating new accounts. With open
signup, "require login" is not a spending cap.

### The guarantees

- Quota is consumed **before** the paid call, never after. Counting afterwards
  lets a concurrent burst through, because none of them have been counted yet.
- `INCR` first, check second. Two concurrent requests each see their own
  post-increment total, so neither escapes the cap. Whoever loses the race
  undoes its own increment.
- A refused call costs no quota.
- **An unreadable backend refuses.** An unreadable cap is not an absent cap.
- The day rolls over in UTC, so the reset does not drift with the server's
  timezone.

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
clauses keep working. **Order matters**: Python matches top to bottom, so the
narrow handler must come first or every 503 silently becomes a 429.

`devolver` is best-effort and never raises. A lost refund costs a little slack;
an unrecorded spend costs money.

### Health endpoint

```python
await metering.panorama({"chat": 300, "ingest": 100})
# {"date": "2026-08-24", "kill_switch": False, "degraded": False,
#  "used": {...}, "limits": {...}, "remaining": {...}}
```

`panorama` never raises, and both branches return the same shape. A health check
that dies when Redis is down turns a partial degradation into a full outage
page — and a handler reading `p["remaining"]` would raise `KeyError` exactly
when the backend is unreachable. When `degraded` is true, the counters read zero
because the real values are unknown, not because quota is exhausted.

---

## decisions

### The problem

When an agent writes to a customer's system, somebody will eventually ask why.
"The model decided" is not an answer that survives an audit.

### The guarantees

- **`registrar` never raises.** The trail is observability. Losing a row is bad;
  killing the response a visitor was already receiving because of an `INSERT` is
  worse.
- **Metadata, never content.** `input_digest` is a hash. There is no column for
  the input text, and a test enforces the exact column set — any new column, under
  any name, fails it. Without that rule the trail becomes a second copy of the
  corpus, without the isolation protections of the first.
- `rule_code` is `NOT NULL`. It is the field that answers "why" with a code
  instead of generated prose.
- `tenant_id` on every row.
- The DDL is portable: the same schema runs on PostgreSQL in production and
  SQLite in tests, so there is no second schema to keep in sync.

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
    parent_id=orchestrator_decision_id,   # worker -> orchestrator
)
```

The split between `evidence` and `outcome` is the point. The model **extracts**
evidence, with a pointer to where it came from. A deterministic rule **decides**.
Changing the rule re-scores the entire history without spending a token, and the
same input always produces the same output.

Arguments are keyword-only: twelve fields, and a wrong positional order would
write `outcome` into `evidence` with no type error to catch it.

---

## queue

### The problem

Work that must not vanish. Under FastAPI's `BackgroundTasks`, a job runs inside
the web process after the response: a redeploy mid-job loses it silently, and a
blocking call stalls everything including the health endpoint.

### The guarantees

- **Deduplication rides on arq's own uniqueness.** `enqueue_job(_job_id=X)`
  returns `None` when a job with that id is queued or running, so dedup needs no
  table and no lock. This package adds the `(project, tenant)` namespace.
- **Refusal before acceptance.** Above the depth cap, or when the depth cannot
  be read, enqueueing raises rather than accepting work it cannot do. A queue
  that only grows is indistinguishable from a service that is down, except it
  lies to the client.
- Durable progress in PostgreSQL, so it survives the process.
- Exponential backoff with a ceiling: 5s, 10s, 20s, 40s, capped at 300s. Without
  growth, five retries against a downed provider all land in the same second —
  that is one chance, not five.
- A dead-letter state carrying a human-readable reason.

### Setup

```python
from agent_ops import queue

queue.aplicar_schema(engine)   # idempotent; call it on both web and worker boot
```

Forgetting this is silent: `marcar` swallows "no such table" and `ler` returns
`None`, which is indistinguishable from "the job never started".

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

`enfileirar` returning `None` means the work was **already queued** — that is
success, not failure. The caller only needs to know the work will happen. That
is why the response uses `job_id_de`, which is deterministic and available on
both paths; returning the enqueue result would leave the client unable to watch
its own upload.

`tenant` is not optional in practice. Without it, two users who submit
byte-identical content collide onto the same job id: the second is told the work
is happening, no job runs for them, and they poll a job belonging to someone
else.

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
        # CancelledError derives from BaseException, so the clause below does
        # not catch it. Without this, a job that hits job_timeout leaves the row
        # at "rodando" forever and the UI polls it indefinitely.
        queue.marcar(engine, job_id, estado="falhou", detalhe="cancelled")
        raise
    except Exception as exc:
        if queue.esgotou(ctx):
            queue.descartar(engine, job_id, motivo=f"{type(exc).__name__}: {exc}")
            return
        queue.tentar_de_novo(ctx)          # raises arq.Retry with backoff
    else:
        queue.marcar(engine, job_id, estado="concluido", percentual=100)

class WorkerSettings:
    functions = [ingest]
    redis_settings = RedisSettings.from_dsn(get_config().redis_url)
    max_tries = queue.MAX_TENTATIVAS      # tie these together, see below
    job_timeout = 1_800
    health_check_interval = 30
```

Run it: `arq module.WorkerSettings`

**`max_tries` and `esgotou` must use the same constant.** In arq, when
`job_try > max_tries` the job is finished **without calling the function**. A
mismatch means `descartar` never runs and the row stays `rodando` forever — the
job disappears from the operations view with no error anywhere.

**The work function must let failures escape.** If it catches everything and
returns normally, the envelope never sees a failure: retry, backoff and
dead-letter become dead code, and the job records success for work that failed.

### Reading progress

```python
p = queue.ler(engine, job_id)
# None            -> never marked (or the read failed; check the log)
# p["estado"]     -> pendente | rodando | concluido | falhou | descartado
# p["percentual"] -> preserved on dead-letter: a job that died at 80% shows 80%
# p["detalhe"]    -> the sentence the UI displays
# p["atualizado"] -> when it last changed
```

Omitting `percentual` or `detalhe` in `marcar` **preserves** the stored value;
passing them overwrites. Only omission preserves, so a progress tick does not
erase the last message, and a dead-letter does not erase how far the job got.

---

## Design decisions

The choices a reviewer is most likely to question, and why they are what they
are.

**Fail closed for anything that gates money or work; fail open for anything that
only reports.** `consumir` and `enfileirar` refuse when their backend is
unreadable. `devolver`, `panorama`, `registrar`, `marcar` and `ler` never raise.
The rule is consistent across all three subpackages: if it authorizes, it
refuses under uncertainty; if it observes, it degrades.

**Two exception types where one would be simpler.** `TetoIndisponivel` and
`FilaIndisponivel` subclass their respective limit exceptions. Without them a
caller cannot distinguish "you hit the limit" (429, resets at a known time) from
"the backend is unreadable" (503, nobody knows when). Collapsing both into one
type means every dashboard reports saturation during an outage.

**A production bug found during extraction.** The original spend cap had `INCR`
and `EXPIRE` inside the same `try`. If `INCR` succeeded and `EXPIRE` then failed
— a transient blip between two round trips — execution fell into the generic
handler and raised, **without undoing the increment**. A refused call charged the
next visitor's quota. The fix separates the two error scopes: the TTL is
housekeeping, not correctness, so a failure there logs and continues. There is a
regression test that reproduces the original bug.

**Subpackages do not import each other.** `metering` pulls no SQLAlchemy; a
worker that only enqueues does not load half an ORM. A test pins the actual
import cost of each subpackage, including the one case where the cost is real
and the docstring says so.

**The schema is portable by construction, not by translation.** Plain
`TEXT`/`INTEGER` rather than native `UUID`/`JSONB`, so the same DDL file runs on
PostgreSQL and SQLite. One schema, no drift.

## Testing

95 tests, no Redis, no PostgreSQL, no network, no provider key. Async functions
are tested with `asyncio.run()` and hand-written doubles rather than a plugin.

The suite runs in under a second, which matters more than it sounds: a test
suite that needs infrastructure is a test suite that gets skipped.

CI does two things. It runs the suite, and it **builds a wheel and installs it
into a clean virtualenv**, asserting that the packaged `.sql` files are present.
An editable install resolves those from the source tree regardless, so without
that job the package could stop being installable with CI fully green.

```bash
pip install -e ".[dev]"
python -m pytest tests/ -q
```

## What this does not do

Stated plainly, because a library that hides its limits wastes your time.

- **No cross-request deduplication by content.** The digest identifies whatever
  you hash. If you hash per-request identifiers, re-submitting the same file
  runs again. Content-addressed dedup needs a `(tenant, content_digest)` row
  consulted before the quota is charged, which is the application's decision,
  not this package's.
- **Deduplication is time-bounded.** arq drops a duplicate only while the job or
  its result key lives (`keep_result`, one hour by default). After that the same
  digest enqueues again.
- **No fair-share scheduling.** One tenant can occupy the whole worker pool.
- **No retention policy.** The trail and the progress table grow without bound.
- **It is not an agent framework**, a prompt library, or a model router.
