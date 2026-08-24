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
| `AGENT_OPS_PROJETO` | `default` | Namespaceia chaves e job ids |
| `AGENT_OPS_KILL_SWITCH` | `false` | Estanca gasto sem redeploy |
| `AGENT_OPS_PROFUNDIDADE_MAXIMA` | `500` | Teto da fila antes do 429 |

## `metering` — teto de gasto

```python
from agent_ops import metering

try:
    restante = await metering.consumir("chat", limite=300)
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
try:
    job_id = await queue.enfileirar(
        pool, "processar_ingestao", doc_id,
        digest=decisions.digerir(arquivo_bytes),
    )
except queue.FilaCheia as e:
    return JSONResponse(
        {"error": e.mensagem},
        status_code=429,
        headers={"Retry-After": str(e.retry_after)},
    )
# job_id é None quando o mesmo digest já estava na fila — isso é sucesso.
```

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
