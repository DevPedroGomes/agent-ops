# agent-ops

Núcleo compartilhado dos agentes do portfólio: teto de gasto, trilha de decisão
e fila durável. Extraído do BrainHub, generalizado para servir aos três projetos.

Spec: `awesome-llm-apps/docs/superpowers/specs/2026-08-24-portfolio-agents-design.md`

## Instalação

```bash
pip install "git+ssh://git@github.com/DevPedroGomes/agent-ops.git@v0.1.0"
```

## Configuração

Todas as envs usam o prefixo `AGENT_OPS_`, para não colidir com as da app
hospedeira.

| Env | Padrão | Para quê |
|---|---|---|
| `AGENT_OPS_REDIS_URL` | `redis://localhost:6379` | Metering e fila |
| `AGENT_OPS_PROJETO` | `default` | Namespaceia chaves e job ids (sem `:`) |
| `AGENT_OPS_KILL_SWITCH` | `true`/`false` | Estanca gasto (ver abaixo) |
| `AGENT_OPS_PROFUNDIDADE_MAXIMA` | `500` | Teto da fila antes do 429 |

`:` é o separador das chaves do Redis e dos job ids, e as juntas não escapam
nada. Por isso `AGENT_OPS_PROJETO`, o `tipo` da cota e o `tenant`/`digest` do
job **não podem conter `:`** — `projeto="a:b"` + `digest="c"` produziria o mesmo
id que `projeto="a"` + `tenant="b"` + `digest="c"`, e colisão de chave aqui não
levanta erro: mistura o teto de dois projetos ou entrega a um tenant o job do
outro. O `escopo` do metering é a exceção proposital — é o último pedaço da
chave, então `ip:1.2.3.4` continua valendo.

## `metering` — teto de gasto

```python
from agent_ops import metering

try:
    restante = await metering.consumir("chat", limite=300)
except metering.TetoIndisponivel as e:      # subclasse: vem ANTES
    return JSONResponse({"error": e.mensagem}, status_code=503)
except metering.TetoAtingido as e:
    return JSONResponse({"error": e.mensagem}, status_code=429)

try:
    resposta = await chamar_provider()
except Exception:
    await metering.devolver("chat")   # a chamada paga não aconteceu
    raise
```

A cota é consumida **antes** da chamada paga. Redis ilegível recusa: um teto
ilegível não é um teto ausente.

`TetoIndisponivel` é subclasse de `TetoAtingido`, então quem só escreve
`except TetoAtingido` continua funcionando. Ela separa "acabou a cota" (429, e
`Retry-After` faz sentido) de "o backend não pôde ser lido" (503, e nenhum prazo
prometido ao cliente conserta). O kill switch é parada **deliberada**, não falha
de backend: continua `TetoAtingido` puro.

### Kill switch

`AGENT_OPS_KILL_SWITCH=true` faz `consumir` recusar **antes** de tocar no Redis.
Ele é lido a cada chamada, não no boot: dá para ligar no container em pé
(`docker compose exec` não serve — a env precisa entrar no processo; na prática
é `AGENT_OPS_KILL_SWITCH=true docker compose up -d web worker`, que recria os
contêineres em segundos **sem rebuild de imagem e sem redeploy**). O restante
da configuração (`REDIS_URL`, `PROJETO`) continua sendo lido uma vez por
processo, porque não muda com o serviço no ar.

### `panorama` — health check e selo de uso

```python
p = await metering.panorama({"chat": 300, "ingest": 50})
# {"date": "2026-08-24", "kill_switch": False, "degraded": False,
#  "used": {...}, "limits": {...}, "remaining": {...}}
```

`panorama` **nunca levanta** — um health check que quebra quando o Redis cai
transforma degradação parcial em página fora do ar. Quando o Redis não responde
ele devolve o mesmo formato com `degraded: True` e os contadores zerados, então
`p["remaining"]["chat"]` é seguro nos dois casos. Mostre o selo apenas quando
`degraded` for `False`: com o Redis fora, os zeros não significam "cota livre",
significam "não sei".

## `decisions` — trilha auditável

```python
from agent_ops import decisions

decisions.migracao.aplicar(engine)   # idempotente, pode rodar no boot

decisions.registrar(
    engine,
    tenant_id=user_id,
    correlation_id=execucao_id,
    input_digest=decisions.digerir(curriculo_bytes),
    rule_code="RUBRICA.PYTHON.SENIOR",
    evidence={"anos_python": 7, "origem": "linha 12"},
    outcome={"pontos": 30},
    model="claude-haiku-4-5",
    tokens_in=800, tokens_out=120, cost_cents=2,
)
```

`registrar` **nunca levanta**. Guarda metadado, nunca conteúdo do usuário.

## `queue` — fila durável

Lado que enfileira:

```python
from agent_ops import queue

pool = await queue.criar_pool()
digest = decisions.digerir(arquivo_bytes)
try:
    await queue.enfileirar(
        pool, "processar_ingestao", doc_id,
        digest=digest,
        tenant=user_id,          # SEMPRE, num app multi-tenant — ver abaixo
    )
except queue.FilaIndisponivel as e:      # subclasse: vem ANTES
    return JSONResponse({"error": e.mensagem}, status_code=503)
except queue.FilaCheia as e:
    return JSONResponse(
        {"error": e.mensagem},
        status_code=429,
        headers={"Retry-After": str(e.retry_after)},
    )

# `enfileirar` devolve None quando o mesmo (tenant, digest) já estava na fila —
# isso é sucesso. Para responder ao cliente e servir o SSE, nomeie o job pelo id
# determinístico em vez do retorno: ele existe nos dois casos.
return {"job_id": queue.job_id_de(digest, tenant=user_id)}
```

**Passe o `tenant`.** `digest` é hash de *conteúdo*: dois tenants subindo o mesmo
arquivo — um formulário padrão, um relatório anual público, um modelo de CV —
produzem o mesmo digest. Sem o tenant no id, o segundo enqueue cai na
deduplicação do primeiro: o segundo tenant recebe "aceito" sem que job nenhum
rode para ele e, ao consultar o progresso por esse id, lê o job do **primeiro**
tenant. `job_progress` não tem coluna de tenant — quem separa os dois é o id.
`tenant` é opcional só por compatibilidade; omitido, o id mantém o formato
antigo `projeto:digest`.

Lado do worker:

```python
from arq.connections import RedisSettings
from agent_ops import queue

async def processar_ingestao(ctx, doc_id):
    job_id = ctx["job_id"]
    queue.marcar(engine, job_id, estado="rodando", percentual=0)
    try:
        ...
        queue.marcar(engine, job_id, estado="concluido", percentual=100)
    except ProviderIndisponivel as e:
        if queue.esgotou(ctx):
            queue.descartar(engine, job_id, motivo=f"provider fora do ar: {e}")
            return
        queue.tentar_de_novo(ctx)      # levanta arq.Retry com backoff

class WorkerSettings:
    functions = [processar_ingestao]
    redis_settings = RedisSettings.from_dsn(os.environ["AGENT_OPS_REDIS_URL"])
    max_jobs = 4        # dimensionar para a VPS de 2 núcleos
    max_tries = 5
```

Rodar: `arq meu_modulo.WorkerSettings`

## Testes

```bash
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Sem `pytest-asyncio`: funções `async` são testadas com `asyncio.run()` e dublês
por `monkeypatch`, seguindo o padrão do BrainHub.
