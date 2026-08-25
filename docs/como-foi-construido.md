# Fase 1 — Núcleo `agent-ops` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publicar o pacote instalável `agent-ops` com as três primeiras peças do núcleo — `metering`, `decisions` e `queue` — extraídas do que já funciona em produção no BrainHub e generalizadas para servir aos três projetos do portfólio.

**Architecture:** Repositório próprio, instalável via `pip install git+ssh://...@v0.1.0`. Três subpacotes independentes entre si: `metering` (teto de gasto sobre Redis), `decisions` (trilha append-only sobre Postgres) e `queue` (fila durável sobre arq/Redis). Nenhum deles importa os outros — um projeto pode usar só a fila sem arrastar Postgres junto.

**Tech Stack:** Python 3.12, `redis>=5.0` (asyncio), `arq>=0.26`, `sqlalchemy>=2.0.51`, `pydantic-settings>=2.0.0`, `pytest>=8.0`.

**Spec:** `docs/superpowers/specs/2026-08-24-portfolio-agents-design.md`

## Global Constraints

- **Python `>=3.12`** — mesma base da imagem do BrainHub (`python:3.12-slim`).
- **Sem `pytest-asyncio`.** O BrainHub testa com `pytest` puro, `monkeypatch` e dublês. Funções `async` são testadas com `asyncio.run()`. Não adicionar dependência de teste nova.
- **Nomes públicos preservados.** `consumir`, `devolver`, `panorama` e `TetoAtingido` mantêm a assinatura do `budget.py` atual, para que a migração do BrainHub na fase 2 troque só a linha de import.
- **Docstrings em português explicando o porquê**, seguindo o padrão do repositório de origem: o comentário registra a decisão e o bug que ela evita, não o que a linha faz.
- **Redis ilegível ⇒ recusa.** Invariante do spec: "um teto ilegível não é um teto ausente". Vale para `metering` e para o backpressure da fila.
- **Nenhum efeito colateral em import.** Conexões são criadas na primeira chamada, nunca no topo do módulo.
- **`decisions` grava metadado, nunca conteúdo do usuário.** Vale também para atributos de trace.

---

### Task 1: Esqueleto do pacote

**Files:**
- Create: `pyproject.toml`
- Create: `src/agent_ops/__init__.py`
- Create: `src/agent_ops/config.py`
- Create: `tests/test_pacote.py`
- Create: `.github/workflows/ci.yml`
- Create: `.gitignore`

**Interfaces:**
- Consumes: nada.
- Produces: `agent_ops.__version__: str`; `agent_ops.config.Config` com os campos `redis_url: str`, `projeto: str`, `kill_switch: bool`; `agent_ops.config.get_config() -> Config` (com cache).

- [ ] **Step 1: Criar a estrutura do repositório**

```bash
mkdir -p src/agent_ops tests .github/workflows
git init
```

- [ ] **Step 2: Escrever `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "agent-ops"
version = "0.1.0"
description = "Nucleo compartilhado dos agentes do portfolio: teto de gasto, trilha de decisao e fila duravel."
requires-python = ">=3.12"
dependencies = [
    "redis>=5.0",
    "arq>=0.26",
    "sqlalchemy>=2.0.51",
    "pydantic-settings>=2.0.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[tool.setuptools.packages.find]
where = ["src"]
```

- [ ] **Step 3: Escrever o teste de fumaça**

```python
# tests/test_pacote.py
"""O pacote importa e a configuracao le do ambiente.

Prende aqui: importar `agent_ops` nao pode abrir conexao nenhuma. Se um dia
alguem criar o cliente Redis no topo de um modulo, este teste quebra — e e
para quebrar, porque import com efeito colateral transforma `pytest --collect`
e o `--help` da app em chamada de rede.
"""

import agent_ops
from agent_ops.config import Config, get_config


def test_versao_exposta():
    assert agent_ops.__version__ == "0.1.0"


def test_config_le_do_ambiente(monkeypatch):
    monkeypatch.setenv("AGENT_OPS_REDIS_URL", "redis://exemplo:6379")
    monkeypatch.setenv("AGENT_OPS_PROJETO", "brainhub")
    get_config.cache_clear()

    cfg = get_config()

    assert isinstance(cfg, Config)
    assert cfg.redis_url == "redis://exemplo:6379"
    assert cfg.projeto == "brainhub"
    assert cfg.kill_switch is False


def test_kill_switch_desliga_por_ambiente(monkeypatch):
    monkeypatch.setenv("AGENT_OPS_KILL_SWITCH", "true")
    get_config.cache_clear()

    assert get_config().kill_switch is True
```

- [ ] **Step 4: Rodar o teste e ver falhar**

Run: `python -m pytest tests/test_pacote.py -v`
Expected: FAIL com `ModuleNotFoundError: No module named 'agent_ops'`

- [ ] **Step 5: Escrever `src/agent_ops/__init__.py`**

```python
"""Nucleo compartilhado dos agentes do portfolio.

Os tres subpacotes nao se importam entre si de proposito: um projeto que so
precisa da fila nao deve arrastar SQLAlchemy junto.
"""

__version__ = "0.1.0"
```

- [ ] **Step 6: Escrever `src/agent_ops/config.py`**

```python
"""Configuracao lida do ambiente, com cache.

Prefixo `AGENT_OPS_` para nao colidir com as envs da app hospedeira: o BrainHub
ja tem `REDIS_URL` propria e as duas podem apontar para instancias diferentes.

`extra="ignore"` porque a app hospedeira tem dezenas de envs que nao sao nossas.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_OPS_", extra="ignore")

    redis_url: str = "redis://localhost:6379"

    # Namespaceia toda chave no Redis. Sem isso, dois projetos do portfolio
    # dividindo o mesmo Redis dividiriam tambem o teto diario um do outro.
    projeto: str = "default"

    # Estanca gasto sem rebuild nem redeploy.
    kill_switch: bool = False


@lru_cache(maxsize=1)
def get_config() -> Config:
    return Config()
```

- [ ] **Step 7: Rodar os testes e ver passar**

Run: `pip install -e ".[dev]" && python -m pytest tests/ -v`
Expected: PASS, 3 testes.

- [ ] **Step 8: Escrever o workflow de CI**

```yaml
# .github/workflows/ci.yml
name: CI
on:
  push: { branches: [main] }
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -e ".[dev]"
      - run: python -m pytest tests/ -v
```

- [ ] **Step 9: Escrever `.gitignore`**

```
__pycache__/
*.egg-info/
.pytest_cache/
.venv/
build/
dist/
.DS_Store
```

- [ ] **Step 10: Commit**

```bash
git add pyproject.toml src/ tests/ .github/ .gitignore
git commit -m "feat: esqueleto do pacote agent-ops com configuracao por ambiente"
```

---

### Task 2: `metering` — reserva e devolução de cota

**Files:**
- Create: `src/agent_ops/metering/__init__.py`
- Create: `src/agent_ops/metering/cotas.py`
- Create: `tests/test_metering.py`

**Interfaces:**
- Consumes: `agent_ops.config.get_config()`.
- Produces:
  - `TetoAtingido(Exception)` com atributo `.mensagem: str`
  - `async consumir(tipo: str, limite: int, unidades: int = 1, escopo: str | None = None) -> int` — devolve o restante; levanta `TetoAtingido` sem consumir nada.
  - `async devolver(tipo: str, unidades: int = 1, escopo: str | None = None) -> None`
  - `_chave(tipo: str, escopo: str | None = None, dia: str | None = None) -> str` (interno, testado direto)
  - `_redis() -> redis.asyncio.Redis` (interno, ponto de monkeypatch nos testes)

- [ ] **Step 1: Escrever os testes que falham**

```python
# tests/test_metering.py
"""Testes do teto de gasto.

O que se prende aqui:
- a cota e consumida ANTES da chamada paga, nunca depois. Contar depois deixa
  uma rajada concorrente passar inteira, porque nenhuma foi contada ainda;
- quem estoura o teto DESFAZ o proprio incremento, senao uma recusa cobraria
  cota do proximo visitante;
- Redis ilegivel RECUSA. Um teto ilegivel nao e um teto ausente;
- o escopo entra na chave, para teto por IP conviver com teto global sem que
  um consuma o do outro;
- o projeto namespaceia tudo, senao dois apps no mesmo Redis dividem o teto.
"""

import asyncio

import pytest

from agent_ops import metering
from agent_ops.config import get_config


class FakeRedis:
    """Dublê minimo: so o que `cotas.py` usa."""

    def __init__(self, explode=False, falha_no_expire=False):
        self.valores: dict[str, int] = {}
        self.expiracoes: dict[str, int] = {}
        self.explode = explode
        # Falha SO no expire, com o incrby passando. Sem esse controle
        # separado nao da para expressar a falha de rede entre as duas idas ao
        # Redis — que e justamente onde a invariante quebrava.
        self.falha_no_expire = falha_no_expire

    async def incrby(self, chave, quanto):
        if self.explode:
            raise ConnectionError("redis fora do ar")
        self.valores[chave] = self.valores.get(chave, 0) + quanto
        return self.valores[chave]

    async def expire(self, chave, segundos):
        if self.explode or self.falha_no_expire:
            raise ConnectionError("redis fora do ar")
        self.expiracoes[chave] = segundos

    async def get(self, chave):
        if self.explode:
            raise ConnectionError("redis fora do ar")
        return self.valores.get(chave)


@pytest.fixture
def redis_falso(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setenv("AGENT_OPS_PROJETO", "testes")
    monkeypatch.setenv("AGENT_OPS_KILL_SWITCH", "false")
    get_config.cache_clear()

    async def _fake():
        return fake

    monkeypatch.setattr(metering.cotas, "_redis", _fake)
    return fake


def test_chave_carrega_projeto_e_dia(redis_falso):
    chave = metering.cotas._chave("chat", dia="2026-08-24")
    assert chave == "ao:budget:testes:2026-08-24:chat"


def test_escopo_entra_na_chave(redis_falso):
    chave = metering.cotas._chave("chat", escopo="ip:abc", dia="2026-08-24")
    assert chave == "ao:budget:testes:2026-08-24:chat:ip:abc"


def test_consumir_devolve_o_restante(redis_falso):
    restante = asyncio.run(metering.consumir("chat", limite=10))
    assert restante == 9


def test_consumir_marca_expiracao_so_na_primeira(redis_falso):
    asyncio.run(metering.consumir("chat", limite=10))
    assert len(redis_falso.expiracoes) == 1
    asyncio.run(metering.consumir("chat", limite=10))
    assert len(redis_falso.expiracoes) == 1


def test_estourar_o_teto_recusa_e_desfaz_o_proprio_incremento(redis_falso):
    asyncio.run(metering.consumir("chat", limite=1))
    chave = metering.cotas._chave("chat")

    with pytest.raises(metering.TetoAtingido):
        asyncio.run(metering.consumir("chat", limite=1))

    # O incremento da chamada recusada foi desfeito: sobrou so o da que passou.
    assert redis_falso.valores[chave] == 1


def test_falha_no_ttl_nao_recusa_a_chamada(monkeypatch):
    # O TTL e faxina, nao corretude. Recusar por causa dele seria ruim; recusar
    # SEM devolver o incremento — que era o comportamento herdado do budget.py
    # original — cobra cota de uma chamada que nunca aconteceu.
    monkeypatch.setenv("AGENT_OPS_PROJETO", "testes")
    monkeypatch.setenv("AGENT_OPS_KILL_SWITCH", "false")
    get_config.cache_clear()
    fake = FakeRedis(falha_no_expire=True)

    async def _fake():
        return fake

    monkeypatch.setattr(metering.cotas, "_redis", _fake)

    restante = asyncio.run(metering.consumir("chat", limite=10))

    assert restante == 9
    assert fake.valores[metering.cotas._chave("chat")] == 1
    assert fake.expiracoes == {}  # o TTL realmente nao foi aplicado


def test_redis_ilegivel_recusa(monkeypatch):
    monkeypatch.setenv("AGENT_OPS_KILL_SWITCH", "false")
    get_config.cache_clear()

    async def _quebrado():
        return FakeRedis(explode=True)

    monkeypatch.setattr(metering.cotas, "_redis", _quebrado)

    with pytest.raises(metering.TetoAtingido):
        asyncio.run(metering.consumir("chat", limite=10))


def test_kill_switch_recusa_antes_de_tocar_no_redis(monkeypatch):
    monkeypatch.setenv("AGENT_OPS_KILL_SWITCH", "true")
    get_config.cache_clear()

    async def _explode():
        raise AssertionError("nao deveria abrir conexao com o kill switch ligado")

    monkeypatch.setattr(metering.cotas, "_redis", _explode)

    with pytest.raises(metering.TetoAtingido):
        asyncio.run(metering.consumir("chat", limite=10))


def test_devolver_reduz_o_contador(redis_falso):
    asyncio.run(metering.consumir("chat", limite=10, unidades=3))
    asyncio.run(metering.devolver("chat", unidades=3))
    assert redis_falso.valores[metering.cotas._chave("chat")] == 0


def test_devolver_engole_falha_do_redis(monkeypatch):
    async def _quebrado():
        return FakeRedis(explode=True)

    monkeypatch.setattr(metering.cotas, "_redis", _quebrado)
    # Nao levanta: uma devolucao perdida custa folga, nao dinheiro.
    asyncio.run(metering.devolver("chat"))
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `python -m pytest tests/test_metering.py -v`
Expected: FAIL com `ModuleNotFoundError: No module named 'agent_ops.metering'`

- [ ] **Step 3: Escrever `src/agent_ops/metering/cotas.py`**

```python
"""Teto diario de chamadas pagas, para demos abertas na internet.

Por que existe separado do rate limit: o rate limit limita o que UM chamador
faz por minuto. Nenhum rate limit limita o que TODOS fazem juntos, nem alguem
criando contas novas — e com cadastro aberto "exigir login" nao e teto de gasto.

Decisoes, todas herdadas do `budget.py` do BrainHub:

- A cota e consumida ANTES da chamada ao provider, nunca depois. Contar depois
  deixa uma rajada concorrente passar toda junta, porque nenhuma delas foi
  contada ainda.
- O dia vira em UTC, para o reset nao andar com o fuso do servidor.
- INCR primeiro e checa depois: duas requisicoes concorrentes enxergam cada uma
  o proprio total pos-incremento, entao nenhuma escapa do teto. Quem perde
  desfaz o proprio incremento.
- Se o Redis nao responde, RECUSA. Um teto ilegivel nao e um teto ausente.
- O kill switch e configuracao de ambiente, para estancar sem redeploy.

O que mudou na extracao: a chave ganhou `projeto` (dois apps no mesmo Redis
dividiriam o teto um do outro) e `escopo` opcional (teto por IP convivendo com
o teto global sem que um consuma o do outro).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis

from agent_ops.config import get_config

logger = logging.getLogger(__name__)

_cliente: aioredis.Redis | None = None

# 48h: sobrevive a virada de dia UTC sem acumular chave velha para sempre.
_TTL_SEGUNDOS = 172_800


class TetoAtingido(Exception):
    """Recusa segura de mostrar ao visitante."""

    def __init__(self, mensagem: str):
        self.mensagem = mensagem
        super().__init__(mensagem)


def _hoje_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _chave(tipo: str, escopo: str | None = None, dia: str | None = None) -> str:
    base = f"ao:budget:{get_config().projeto}:{dia or _hoje_utc()}:{tipo}"
    return f"{base}:{escopo}" if escopo else base


async def _redis() -> aioredis.Redis:
    global _cliente
    if _cliente is None:
        _cliente = aioredis.from_url(get_config().redis_url, decode_responses=True)
    return _cliente


async def consumir(
    tipo: str,
    limite: int,
    unidades: int = 1,
    escopo: str | None = None,
) -> int:
    """Gasta `unidades` da cota de hoje. Devolve o quanto sobrou.

    Levanta `TetoAtingido` sem ter consumido nada — uma chamada recusada nao
    custa cota ao proximo visitante.
    """
    if get_config().kill_switch:
        logger.warning("metering.kill_switch_ativo tipo=%s", tipo)
        raise TetoAtingido("This demo is paused right now. Please try again later.")

    chave = _chave(tipo, escopo)
    try:
        r = await _redis()
        usado = await r.incrby(chave, unidades)
    except Exception as exc:
        # Falhou ANTES de contar: nao ha incremento para desfazer.
        logger.error("metering.estado_ilegivel tipo=%s erro=%s", tipo, exc)
        raise TetoAtingido("This demo is temporarily unavailable.") from exc

    if usado == unidades:
        # O TTL e faxina, nao corretude: o contador ja esta certo sem ele, e a
        # chave de amanha tem outro nome. Falhar aqui NAO pode recusar a
        # chamada — e muito menos recusar sem devolver o incremento que acabou
        # de acontecer. Esse era o bug herdado do `budget.py`: com o `expire`
        # dentro do mesmo `try` do `incrby`, uma falha de rede entre as duas
        # idas ao Redis mandava a execucao para o `except` generico, que
        # levantava TetoAtingido sem nunca alcancar o rollback logo abaixo.
        # Chamada recusada cobrando cota e exatamente a invariante que este
        # modulo existe para garantir.
        try:
            await r.expire(chave, _TTL_SEGUNDOS)
        except Exception:
            logger.warning("metering.ttl_nao_aplicado chave=%s", chave, exc_info=True)

    if usado > limite:
        # Perdeu a corrida: devolve o proprio incremento e recusa.
        try:
            await r.incrby(chave, -unidades)
        except Exception:
            logger.exception("metering.rollback_falhou tipo=%s", tipo)
        logger.warning(
            "metering.esgotado tipo=%s usado=%d limite=%d", tipo, usado, limite
        )
        raise TetoAtingido(
            "This demo reached today's usage cap. It resets at midnight UTC."
        )

    return limite - usado


async def devolver(tipo: str, unidades: int = 1, escopo: str | None = None) -> None:
    """Devolve cota quando a chamada paga nao chegou a acontecer.

    Melhor esforco: uma devolucao perdida custa um pouco de folga, um gasto nao
    registrado custa dinheiro.
    """
    try:
        r = await _redis()
        await r.incrby(_chave(tipo, escopo), -unidades)
    except Exception as exc:
        logger.error("metering.devolucao_falhou tipo=%s erro=%s", tipo, exc)
```

- [ ] **Step 4: Escrever `src/agent_ops/metering/__init__.py`**

```python
"""Teto de gasto para demos publicas."""

from agent_ops.metering import cotas
from agent_ops.metering.cotas import TetoAtingido, consumir, devolver

__all__ = ["cotas", "TetoAtingido", "consumir", "devolver"]
```

- [ ] **Step 5: Rodar os testes e ver passar**

Run: `python -m pytest tests/test_metering.py -v`
Expected: PASS, 10 testes.

- [ ] **Step 6: Commit**

```bash
git add src/agent_ops/metering/ tests/test_metering.py
git commit -m "feat: teto de gasto com reserva antes da chamada e escopo por projeto"
```

---

### Task 3: `metering` — panorama e selo de saúde

**Files:**
- Modify: `src/agent_ops/metering/cotas.py` (acrescentar `panorama`)
- Modify: `src/agent_ops/metering/__init__.py` (exportar `panorama`)
- Modify: `tests/test_metering.py` (acrescentar testes)

**Interfaces:**
- Consumes: `_redis`, `_chave`, `get_config` da Task 2.
- Produces: `async panorama(limites: dict[str, int]) -> dict` com as chaves `date`, `kill_switch`, `used`, `limits`, `remaining`; e, quando o Redis não responde, `date`, `kill_switch`, `degraded: True`.

- [ ] **Step 1: Acrescentar os testes que falham**

```python
# no fim de tests/test_metering.py

def test_panorama_relata_uso_e_restante(redis_falso):
    asyncio.run(metering.consumir("chat", limite=10, unidades=4))

    p = asyncio.run(metering.panorama({"chat": 10, "ingest": 5}))

    assert p["used"] == {"chat": 4, "ingest": 0}
    assert p["limits"] == {"chat": 10, "ingest": 5}
    assert p["remaining"] == {"chat": 6, "ingest": 5}
    assert p["kill_switch"] is False
    assert "degraded" not in p


def test_panorama_nunca_reporta_restante_negativo(redis_falso):
    # O teto e checado depois do INCR, entao o contador pode passar do limite
    # por um instante. O selo publico nao pode mostrar "-1 restante".
    asyncio.run(metering.consumir("chat", limite=100, unidades=7))

    p = asyncio.run(metering.panorama({"chat": 5}))

    assert p["remaining"]["chat"] == 0


def test_panorama_degrada_sem_derrubar_o_health_check(monkeypatch):
    monkeypatch.setenv("AGENT_OPS_KILL_SWITCH", "false")
    get_config.cache_clear()

    async def _quebrado():
        return FakeRedis(explode=True)

    monkeypatch.setattr(metering.cotas, "_redis", _quebrado)

    p = asyncio.run(metering.panorama({"chat": 10}))

    assert p["degraded"] is True
    assert p["kill_switch"] is False
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `python -m pytest tests/test_metering.py -k panorama -v`
Expected: FAIL com `AttributeError: module 'agent_ops.metering' has no attribute 'panorama'`

- [ ] **Step 3: Acrescentar `panorama` ao fim de `cotas.py`**

```python
async def panorama(limites: dict[str, int]) -> dict:
    """Uso de hoje. Serve ao health check e a um selo na UI.

    Recebe os limites em vez de le-los da configuracao: cada projeto tem os
    proprios tipos de cota, e o pacote nao deve conhecer os nomes deles.

    Nunca levanta. Um health check que quebra quando o Redis cai transforma
    uma degradacao parcial em pagina fora do ar.
    """
    dia = _hoje_utc()
    ligado = get_config().kill_switch

    try:
        r = await _redis()
        usados = {
            tipo: int(await r.get(_chave(tipo, dia=dia)) or 0) for tipo in limites
        }
    except Exception:
        logger.warning("metering.panorama_degradado", exc_info=True)
        return {"date": dia, "kill_switch": ligado, "degraded": True}

    return {
        "date": dia,
        "kill_switch": ligado,
        "used": usados,
        "limits": limites,
        # `max(0, ...)`: o teto e checado depois do INCR, entao o contador pode
        # passar do limite por um instante. O selo publico nao mostra negativo.
        "remaining": {t: max(0, limites[t] - usados[t]) for t in limites},
    }
```

- [ ] **Step 4: Atualizar `src/agent_ops/metering/__init__.py`**

```python
"""Teto de gasto para demos publicas."""

from agent_ops.metering import cotas
from agent_ops.metering.cotas import TetoAtingido, consumir, devolver, panorama

__all__ = ["cotas", "TetoAtingido", "consumir", "devolver", "panorama"]
```

- [ ] **Step 5: Rodar todos os testes e ver passar**

Run: `python -m pytest tests/ -v`
Expected: PASS, 16 testes.

- [ ] **Step 6: Commit**

```bash
git add src/agent_ops/metering/ tests/test_metering.py
git commit -m "feat: panorama de uso que degrada sem derrubar o health check"
```

---

### Task 4: `decisions` — schema genérico e migração

**Files:**
- Create: `src/agent_ops/decisions/__init__.py`
- Create: `src/agent_ops/decisions/schema.sql`
- Create: `src/agent_ops/decisions/migracao.py`
- Create: `tests/test_decisions_schema.py`

**Interfaces:**
- Consumes: nada do pacote.
- Produces:
  - `agent_ops.decisions.migracao.SQL_SCHEMA: str` — o DDL lido do `.sql` empacotado.
  - `agent_ops.decisions.migracao.aplicar(engine) -> None` — idempotente.

- [ ] **Step 1: Escrever os testes que falham**

```python
# tests/test_decisions_schema.py
"""Testes do schema da trilha de decisao.

O que se prende aqui:
- o DDL e idempotente: aplicar duas vezes nao pode quebrar o boot da app;
- a tabela guarda METADADO, nao conteudo. `input_digest` e hash, e nao existe
  coluna para o texto da entrada. Quem prende isso e a comparacao EXATA do
  conjunto de colunas: qualquer coluna nova, com qualquer nome, derruba o
  teste e passa por revisao. Uma lista de nomes proibidos nao serviria — so
  pegaria os nomes que alguem ja imaginou, e `user_message TEXT` passaria;
- `rule_code` existe e e obrigatorio: e ele que responde "por que decidiu
  isso?" com um codigo, nao com um paragrafo do modelo;
- `parent_id` existe, senao nao da para ligar worker ao orquestrador.
"""

from sqlalchemy import create_engine, inspect, text

from agent_ops.decisions import migracao


def test_ddl_nao_carrega_coluna_de_conteudo():
    ddl = migracao.SQL_SCHEMA.lower()
    for proibida in (" input text", " prompt text", " content text", " raw_input"):
        assert proibida not in ddl, f"coluna de conteudo no schema: {proibida}"


def test_ddl_e_idempotente():
    assert "create table if not exists" in migracao.SQL_SCHEMA.lower()


def test_aplicar_cria_a_tabela_com_as_colunas_esperadas(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    migracao.aplicar(engine)

    colunas = {c["name"] for c in inspect(engine).get_columns("decisions")}
    esperadas = {
        "id", "project", "tenant_id", "correlation_id", "input_digest",
        "rule_code", "evidence", "outcome", "model", "tokens_in",
        "tokens_out", "cost_cents", "parent_id", "created_at",
    }
    # Igualdade exata, nao subconjunto. Este assert e a unica rede de seguranca
    # da regra mais importante da tabela: metadado, nunca conteudo. Com `<=`,
    # acrescentar `user_message TEXT` ao schema passaria calado — o conjunto
    # esperado continuaria contido no real. Com `==`, qualquer coluna nova
    # derruba o teste e obriga uma decisao consciente.
    assert esperadas == colunas


def test_aplicar_duas_vezes_nao_quebra(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    migracao.aplicar(engine)
    migracao.aplicar(engine)  # nao levanta

    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM decisions")).scalar() == 0
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `python -m pytest tests/test_decisions_schema.py -v`
Expected: FAIL com `ModuleNotFoundError: No module named 'agent_ops.decisions'`

- [ ] **Step 3: Escrever `src/agent_ops/decisions/schema.sql`**

```sql
-- A decisao do agente vira registro consultavel, nao so texto na tela.
--
-- MOTIVACAO: a cada execucao o pipeline decide bastante coisa e nada disso
-- sobrevivia ao fim do stream. O painel mostra o caminho enquanto acontece e
-- some quando a pagina recarrega. Com a decisao persistida da para responder
-- depois "por que ele decidiu isso?", comparar execucoes ao longo do tempo, e
-- achar o caso que sempre cai na rede de seguranca — que e o sinal de que
-- falta insumo, nao de que o modelo esta ruim.
--
-- METADADO, NUNCA CONTEUDO: `input_digest` e hash da entrada. O texto da
-- entrada nao entra aqui. Sem essa regra a trilha vira uma segunda copia do
-- acervo, e uma copia sem as protecoes de isolamento da tabela original.
--
-- ISOLAMENTO: `tenant_id` em toda linha. A leitura da trilha filtra por tenant,
-- nunca so por `correlation_id`.
--
-- Tipos propositalmente portateis (TEXT/INTEGER, sem UUID nem JSONB nativos):
-- o mesmo DDL roda em Postgres na producao e em SQLite nos testes, sem um
-- segundo schema para manter em sincronia.

CREATE TABLE IF NOT EXISTS decisions (
    id              TEXT PRIMARY KEY,

    -- Qual app gerou a linha. Os tres projetos podem dividir um banco.
    project         TEXT NOT NULL,
    tenant_id       TEXT NOT NULL,

    -- Amarra todas as decisoes de UMA execucao, do orquestrador aos workers.
    correlation_id  TEXT NOT NULL,

    -- Hash da entrada, nunca a entrada. Serve para deduplicar e para provar
    -- que duas execucoes viram exatamente o mesmo insumo.
    input_digest    TEXT NOT NULL,

    -- O campo que sustenta a tese: a resposta a "por que decidiu isso?" e um
    -- codigo de regra, nao um paragrafo gerado.
    rule_code       TEXT NOT NULL,

    -- O que o modelo EXTRAIU (com ponteiro para a origem) e o que a regra
    -- DECIDIU. Separados de proposito: trocar a regra re-decide o historico
    -- inteiro sem gastar um token.
    evidence        TEXT NOT NULL DEFAULT '{}',
    outcome         TEXT NOT NULL DEFAULT '{}',

    model           TEXT,
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    cost_cents      INTEGER NOT NULL DEFAULT 0,

    -- Trilha orquestrador -> worker. Nulo na raiz.
    parent_id       TEXT,

    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Listagem da trilha do tenant, do mais recente para o mais antigo.
CREATE INDEX IF NOT EXISTS idx_decisions_tenant_created
    ON decisions (tenant_id, created_at DESC);

-- Reconstruir uma execucao inteira em ordem.
CREATE INDEX IF NOT EXISTS idx_decisions_correlation
    ON decisions (correlation_id, created_at);

-- Descer do orquestrador para os workers dele.
CREATE INDEX IF NOT EXISTS idx_decisions_parent
    ON decisions (parent_id);

-- Semear o golden set do eval: agrupar por regra e achar a que sempre cai na
-- rede de seguranca.
CREATE INDEX IF NOT EXISTS idx_decisions_project_rule
    ON decisions (project, rule_code);
```

- [ ] **Step 4: Escrever `src/agent_ops/decisions/migracao.py`**

```python
"""Aplicacao do schema da trilha.

Um `.sql` empacotado em vez de DDL em string Python: o arquivo e revisavel
como SQL, colorizado no editor, e pode ser rodado a mao contra o banco quando
alguem precisa investigar producao sem subir a app.
"""

from __future__ import annotations

from importlib import resources

from sqlalchemy import text
from sqlalchemy.engine import Engine

SQL_SCHEMA: str = (
    resources.files("agent_ops.decisions").joinpath("schema.sql").read_text("utf-8")
)


def aplicar(engine: Engine) -> None:
    """Cria tabela e indices. Idempotente: pode rodar em todo boot.

    Cada statement vai numa execucao propria porque o driver do SQLite recusa
    varios comandos num `execute()` so — e os testes rodam em SQLite.
    """
    statements = [s.strip() for s in SQL_SCHEMA.split(";") if s.strip()]
    with engine.begin() as conexao:
        for statement in statements:
            conexao.execute(text(statement))
```

- [ ] **Step 5: Escrever `src/agent_ops/decisions/__init__.py`**

```python
"""Trilha append-only de decisoes do agente."""

from agent_ops.decisions import migracao

__all__ = ["migracao"]
```

- [ ] **Step 6: Declarar o `.sql` como dado do pacote**

Acrescentar ao `pyproject.toml`, depois do bloco `[tool.setuptools.packages.find]`:

```toml
[tool.setuptools.package-data]
"agent_ops.decisions" = ["*.sql"]
```

- [ ] **Step 7: Rodar os testes e ver passar**

Run: `pip install -e ".[dev]" && python -m pytest tests/test_decisions_schema.py -v`
Expected: PASS, 4 testes.

- [ ] **Step 8: Commit**

```bash
git add src/agent_ops/decisions/ tests/test_decisions_schema.py pyproject.toml
git commit -m "feat: schema portatil da trilha de decisao com codigo de regra obrigatorio"
```

---

### Task 5: `decisions` — registrar sem nunca derrubar o chamador

**Files:**
- Create: `src/agent_ops/decisions/registro.py`
- Modify: `src/agent_ops/decisions/__init__.py`
- Create: `tests/test_decisions_registro.py`

**Interfaces:**
- Consumes: `migracao.aplicar` (nos testes), `agent_ops.config.get_config()` para o campo `project`.
- Produces:
  - `digerir(payload: str | bytes) -> str` — SHA-256 hex, 64 chars.
  - `registrar(engine, *, tenant_id: str, correlation_id: str, input_digest: str, rule_code: str, evidence: dict | None = None, outcome: dict | None = None, model: str | None = None, tokens_in: int = 0, tokens_out: int = 0, cost_cents: int = 0, parent_id: str | None = None) -> str | None` — devolve o `id` gerado, ou `None` se a gravação falhou. **Nunca levanta.**

- [ ] **Step 1: Escrever os testes que falham**

```python
# tests/test_decisions_registro.py
"""Testes da gravacao da trilha.

O que se prende aqui:
- gravar a trilha NUNCA derruba o chamador. A trilha e observabilidade; perder
  uma linha e ruim, perder a resposta que o visitante ja estava recebendo e
  pior. Este e o mesmo contrato do BrainHub atual;
- o digest e estavel: a mesma entrada da sempre o mesmo hash, senao a
  deduplicacao da fila (Task 6) nao funciona;
- `evidence` e `outcome` viajam como JSON e voltam como dict;
- `parent_id` amarra worker ao orquestrador.
"""

import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine, text

from agent_ops.config import get_config
from agent_ops.decisions import migracao, registro


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OPS_PROJETO", "triagem")
    get_config.cache_clear()
    eng = create_engine(f"sqlite:///{tmp_path}/t.db")
    migracao.aplicar(eng)
    return eng


def test_digest_e_estavel():
    assert registro.digerir("abc") == registro.digerir("abc")
    assert registro.digerir("abc") != registro.digerir("abd")
    assert len(registro.digerir("abc")) == 64


def test_digest_aceita_bytes_e_str_igualmente():
    assert registro.digerir("abc") == registro.digerir(b"abc")


def test_registrar_grava_e_devolve_o_id(engine):
    id_ = registro.registrar(
        engine,
        tenant_id="t1",
        correlation_id="exec-1",
        input_digest=registro.digerir("curriculo-1"),
        rule_code="RUBRICA.PYTHON.SENIOR",
        evidence={"anos_python": 7, "origem": "linha 12"},
        outcome={"pontos": 30},
        model="claude-haiku-4-5",
        tokens_in=800,
        tokens_out=120,
        cost_cents=2,
    )

    assert id_ is not None
    with engine.connect() as c:
        linha = c.execute(
            text("SELECT project, tenant_id, rule_code, evidence, outcome, "
                 "tokens_in, cost_cents FROM decisions WHERE id = :i"),
            {"i": id_},
        ).one()

    assert linha.project == "triagem"
    assert linha.tenant_id == "t1"
    assert linha.rule_code == "RUBRICA.PYTHON.SENIOR"
    assert json.loads(linha.evidence)["anos_python"] == 7
    assert json.loads(linha.outcome)["pontos"] == 30
    assert linha.tokens_in == 800
    assert linha.cost_cents == 2


def test_registrar_liga_worker_ao_orquestrador(engine):
    pai = registro.registrar(
        engine, tenant_id="t1", correlation_id="exec-1",
        input_digest="d0", rule_code="PLANO.CRIADO",
    )
    filho = registro.registrar(
        engine, tenant_id="t1", correlation_id="exec-1",
        input_digest="d1", rule_code="RUBRICA.PYTHON.SENIOR", parent_id=pai,
    )

    with engine.connect() as c:
        achados = c.execute(
            text("SELECT id FROM decisions WHERE parent_id = :p"), {"p": pai}
        ).scalars().all()

    assert achados == [filho]


def test_banco_quebrado_nao_derruba_o_chamador():
    class EngineQuebrado:
        def begin(self):
            raise RuntimeError("banco fora do ar")

    resultado = registro.registrar(
        EngineQuebrado(), tenant_id="t1", correlation_id="e",
        input_digest="d", rule_code="R",
    )

    assert resultado is None  # engoliu a falha e seguiu


def test_evidence_e_outcome_vazios_viram_json_vazio(engine):
    id_ = registro.registrar(
        engine, tenant_id="t1", correlation_id="e",
        input_digest="d", rule_code="R",
    )

    with engine.connect() as c:
        linha = c.execute(
            text("SELECT evidence, outcome FROM decisions WHERE id = :i"), {"i": id_}
        ).one()

    assert json.loads(linha.evidence) == {}
    assert json.loads(linha.outcome) == {}


def test_evidencia_nao_serializavel_nao_derruba_o_chamador(engine):
    # O gatilho mais realista do contrato "nunca levanta" nao e o banco cair:
    # e alguem montar a evidencia com um `datetime`, um `set` ou um objeto de
    # modelo, e `json.dumps` levantar TypeError. Hoje a serializacao esta
    # dentro do `try`, entao passa. Mas se um dia alguem mover a montagem do
    # dict para fora — por exemplo para encurtar a transacao — o contrato
    # quebra em silencio, e este teste e a unica coisa que avisa.
    resultado = registro.registrar(
        engine,
        tenant_id="t1",
        correlation_id="e",
        input_digest="d",
        rule_code="R",
        evidence={"quando": datetime(2026, 8, 24, 12, 0, 0)},
    )

    assert resultado is None
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `python -m pytest tests/test_decisions_registro.py -v`
Expected: FAIL com `ImportError: cannot import name 'registro'`

- [ ] **Step 3: Escrever `src/agent_ops/decisions/registro.py`**

```python
"""Gravacao da trilha.

CONTRATO CENTRAL: `registrar` nunca levanta. A trilha e observabilidade — uma
linha perdida e ruim, mas derrubar a resposta que o visitante ja estava
recebendo por causa de um INSERT e pior. E o mesmo contrato que o BrainHub ja
prende em teste hoje.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid

from sqlalchemy import text

from agent_ops.config import get_config

logger = logging.getLogger(__name__)

_INSERT = text(
    """
    INSERT INTO decisions (
        id, project, tenant_id, correlation_id, input_digest, rule_code,
        evidence, outcome, model, tokens_in, tokens_out, cost_cents, parent_id
    ) VALUES (
        :id, :project, :tenant_id, :correlation_id, :input_digest, :rule_code,
        :evidence, :outcome, :model, :tokens_in, :tokens_out, :cost_cents,
        :parent_id
    )
    """
)


def digerir(payload: str | bytes) -> str:
    """SHA-256 hex da entrada.

    Normaliza `str` para UTF-8 antes de digerir, senao o mesmo conteudo lido de
    um upload (bytes) e de um formulario (str) geraria digests diferentes — e a
    deduplicacao da fila deixaria passar o trabalho repetido.
    """
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def registrar(
    engine,
    *,
    tenant_id: str,
    correlation_id: str,
    input_digest: str,
    rule_code: str,
    evidence: dict | None = None,
    outcome: dict | None = None,
    model: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_cents: int = 0,
    parent_id: str | None = None,
) -> str | None:
    """Grava uma linha da trilha. Devolve o `id`, ou `None` se falhou.

    Argumentos so por nome de proposito: a chamada tem doze campos e uma
    ordem posicional errada gravaria `outcome` no lugar de `evidence` sem
    nenhum erro de tipo para denunciar.
    """
    novo_id = str(uuid.uuid4())
    try:
        with engine.begin() as conexao:
            conexao.execute(
                _INSERT,
                {
                    "id": novo_id,
                    "project": get_config().projeto,
                    "tenant_id": tenant_id,
                    "correlation_id": correlation_id,
                    "input_digest": input_digest,
                    "rule_code": rule_code,
                    "evidence": json.dumps(evidence or {}, ensure_ascii=False),
                    "outcome": json.dumps(outcome or {}, ensure_ascii=False),
                    "model": model,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "cost_cents": cost_cents,
                    "parent_id": parent_id,
                },
            )
    except Exception:
        logger.exception(
            "decisions.gravacao_falhou correlation_id=%s rule_code=%s",
            correlation_id,
            rule_code,
        )
        return None

    return novo_id
```

- [ ] **Step 4: Atualizar `src/agent_ops/decisions/__init__.py`**

```python
"""Trilha append-only de decisoes do agente."""

from agent_ops.decisions import migracao, registro
from agent_ops.decisions.registro import digerir, registrar

__all__ = ["migracao", "registro", "digerir", "registrar"]
```

- [ ] **Step 5: Rodar todos os testes e ver passar**

Run: `python -m pytest tests/ -v`
Expected: PASS, 27 testes.

- [ ] **Step 6: Commit**

```bash
git add src/agent_ops/decisions/ tests/test_decisions_registro.py
git commit -m "feat: gravacao da trilha que engole falha em vez de derrubar a resposta"
```

---

### Task 6: `queue` — enfileirar com idempotência e backpressure

**Files:**
- Create: `src/agent_ops/queue/__init__.py`
- Create: `src/agent_ops/queue/fila.py`
- Create: `tests/test_queue_fila.py`
- Modify: `src/agent_ops/config.py` (acrescentar `profundidade_maxima`)

**Interfaces:**
- Consumes: `agent_ops.config.get_config()`.
- Produces:
  - `FilaCheia(Exception)` com `.mensagem: str` e `.retry_after: int`
  - `async criar_pool() -> ArqRedis`
  - `async profundidade(pool) -> int`
  - `async enfileirar(pool, funcao: str, *args, digest: str, **kwargs) -> str | None` — devolve o `job_id`, ou `None` se já existia um job com o mesmo digest. Levanta `FilaCheia` quando a profundidade excede o teto.

- [ ] **Step 1: Acrescentar `profundidade_maxima` ao `Config`**

Em `src/agent_ops/config.py`, dentro da classe `Config`, depois de `projeto`:

```python
    # Teto de profundidade da fila. Acima disso a API recusa com 429 em vez de
    # aceitar trabalho que nao vai conseguir fazer — uma fila que so cresce e
    # indistinguivel de um servico fora do ar, mas mente para o cliente.
    profundidade_maxima: int = 500
```

- [ ] **Step 2: Escrever os testes que falham**

```python
# tests/test_queue_fila.py
"""Testes do enfileiramento.

O que se prende aqui:
- o mesmo digest nao entra duas vezes. Reentregar um job nao pode duplicar
  efeito, e no BrainHub isso significa nao reprocessar (nem recobrar) o mesmo
  PDF subido duas vezes;
- o job_id deriva do projeto + digest, senao dois apps dividindo o Redis
  deduplicariam o trabalho um do outro;
- fila cheia RECUSA com Retry-After, nao aceita;
- Redis ilegivel RECUSA, mesma invariante do metering.
"""

import asyncio

import pytest

from agent_ops import queue
from agent_ops.config import get_config


class FakePool:
    def __init__(self, profundidade=0, explode=False):
        self.enfileirados: list[tuple] = []
        self.ids_existentes: set[str] = set()
        self._profundidade = profundidade
        self.explode = explode

    async def zcard(self, _nome):
        if self.explode:
            raise ConnectionError("redis fora do ar")
        return self._profundidade

    async def enqueue_job(self, funcao, *args, _job_id=None, **kwargs):
        if _job_id in self.ids_existentes:
            return None  # arq devolve None quando o id ja existe
        self.ids_existentes.add(_job_id)
        self.enfileirados.append((funcao, args, _job_id, kwargs))

        class _Job:
            job_id = _job_id

        return _Job()


@pytest.fixture(autouse=True)
def projeto(monkeypatch):
    monkeypatch.setenv("AGENT_OPS_PROJETO", "brainhub")
    monkeypatch.setenv("AGENT_OPS_PROFUNDIDADE_MAXIMA", "500")
    get_config.cache_clear()


def test_job_id_deriva_de_projeto_e_digest():
    assert queue.fila._job_id("abc123") == "brainhub:abc123"


def test_enfileira_e_devolve_o_job_id():
    pool = FakePool()
    job_id = asyncio.run(queue.enfileirar(pool, "processar", "doc-1", digest="abc"))

    assert job_id == "brainhub:abc"
    assert pool.enfileirados[0][0] == "processar"
    assert pool.enfileirados[0][1] == ("doc-1",)


def test_mesmo_digest_nao_entra_duas_vezes():
    pool = FakePool()
    primeiro = asyncio.run(queue.enfileirar(pool, "processar", digest="abc"))
    segundo = asyncio.run(queue.enfileirar(pool, "processar", digest="abc"))

    assert primeiro == "brainhub:abc"
    assert segundo is None
    assert len(pool.enfileirados) == 1


def test_fila_cheia_recusa_com_retry_after():
    pool = FakePool(profundidade=500)

    with pytest.raises(queue.FilaCheia) as exc:
        asyncio.run(queue.enfileirar(pool, "processar", digest="abc"))

    assert exc.value.retry_after > 0
    assert pool.enfileirados == []


def test_fila_no_limite_menos_um_ainda_aceita():
    pool = FakePool(profundidade=499)
    assert asyncio.run(queue.enfileirar(pool, "processar", digest="abc")) is not None


def test_redis_ilegivel_recusa():
    pool = FakePool(explode=True)

    with pytest.raises(queue.FilaCheia):
        asyncio.run(queue.enfileirar(pool, "processar", digest="abc"))

    assert pool.enfileirados == []


def test_kwargs_chegam_no_job():
    pool = FakePool()
    asyncio.run(
        queue.enfileirar(pool, "processar", digest="abc", user_id="u1")
    )
    assert pool.enfileirados[0][3] == {"user_id": "u1"}
```

- [ ] **Step 3: Rodar e ver falhar**

Run: `python -m pytest tests/test_queue_fila.py -v`
Expected: FAIL com `ModuleNotFoundError: No module named 'agent_ops.queue'`

- [ ] **Step 4: Escrever `src/agent_ops/queue/fila.py`**

```python
"""Enfileiramento com deduplicacao e backpressure.

DEDUPLICACAO: o `arq` ja resolve unicidade — `enqueue_job(..., _job_id=X)`
devolve `None` quando um job com esse id ja esta na fila ou rodando. Entao a
idempotencia nao precisa de tabela nem de lock: basta derivar o `_job_id` do
digest da entrada. O que o pacote acrescenta e o namespace do projeto, senao
dois apps no mesmo Redis deduplicariam o trabalho um do outro.

BACKPRESSURE: o `arq` guarda a fila num sorted set, entao `zcard(queue_name)` da
a profundidade sem varrer nada. Acima do teto a API recusa com 429 e
`Retry-After`. Uma fila que so cresce e indistinguivel de um servico fora do
ar, com o agravante de mentir para o cliente que o trabalho foi aceito.

REDIS ILEGIVEL RECUSA, mesma invariante do metering: nao da para afirmar que ha
espaco na fila sem conseguir ler a fila.
"""

from __future__ import annotations

import logging

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from arq.constants import default_queue_name

from agent_ops.config import get_config

logger = logging.getLogger(__name__)

# Quanto pedir ao cliente para esperar quando a fila esta cheia.
_RETRY_AFTER_SEGUNDOS = 30


class FilaCheia(Exception):
    """Recusa segura de mostrar ao visitante, com dica de quando voltar."""

    def __init__(self, mensagem: str, retry_after: int = _RETRY_AFTER_SEGUNDOS):
        self.mensagem = mensagem
        self.retry_after = retry_after
        super().__init__(mensagem)


def _job_id(digest: str) -> str:
    return f"{get_config().projeto}:{digest}"


async def criar_pool() -> ArqRedis:
    """Pool para o lado que ENFILEIRA (a app web). O worker cria o proprio."""
    return await create_pool(RedisSettings.from_dsn(get_config().redis_url))


async def profundidade(pool) -> int:
    """Quantos jobs estao esperando. Levanta se o Redis nao responde."""
    return await pool.zcard(default_queue_name)


async def enfileirar(pool, funcao: str, *args, digest: str, **kwargs) -> str | None:
    """Enfileira `funcao` se ainda nao houver job com o mesmo digest.

    Devolve o `job_id`, ou `None` quando o trabalho ja estava na fila — que e
    resposta de sucesso, nao erro: o chamador so precisa saber que o trabalho
    vai acontecer, nao se foi ele quem o criou.

    Levanta `FilaCheia` quando a fila passou do teto ou nao pode ser lida.
    """
    teto = get_config().profundidade_maxima
    try:
        atual = await profundidade(pool)
    except Exception as exc:
        logger.error("queue.profundidade_ilegivel erro=%s", exc)
        raise FilaCheia("The queue is temporarily unavailable.") from exc

    if atual >= teto:
        logger.warning("queue.cheia atual=%d teto=%d", atual, teto)
        raise FilaCheia("The queue is full right now. Please retry shortly.")

    job = await pool.enqueue_job(funcao, *args, _job_id=_job_id(digest), **kwargs)
    if job is None:
        logger.info("queue.duplicado digest=%s", digest)
        return None

    return job.job_id
```

- [ ] **Step 5: Escrever `src/agent_ops/queue/__init__.py`**

```python
"""Fila duravel sobre arq."""

from agent_ops.queue import fila
from agent_ops.queue.fila import FilaCheia, criar_pool, enfileirar, profundidade

__all__ = ["fila", "FilaCheia", "criar_pool", "enfileirar", "profundidade"]
```

- [ ] **Step 6: Rodar os testes e ver passar**

Run: `python -m pytest tests/test_queue_fila.py -v`
Expected: PASS, 7 testes.

- [ ] **Step 7: Commit**

```bash
git add src/agent_ops/queue/ tests/test_queue_fila.py src/agent_ops/config.py
git commit -m "feat: enfileiramento com deduplicacao por digest e recusa sob backpressure"
```

---

### Task 7: `queue` — progresso durável, retry e dead-letter

**Files:**
- Create: `src/agent_ops/queue/execucao.py`
- Create: `src/agent_ops/queue/progresso.sql`
- Modify: `src/agent_ops/queue/__init__.py`
- Create: `tests/test_queue_execucao.py`
- Modify: `pyproject.toml` (dados do pacote)

**Interfaces:**
- Consumes: `agent_ops.decisions.migracao.aplicar` (padrão de DDL), `arq.Retry`.
- Produces:
  - `aplicar_schema(engine) -> None` — cria a tabela `job_progress`.
  - `marcar(engine, job_id: str, *, estado: str, percentual: int = 0, detalhe: str | None = None, tentativas: int | None = None) -> None` — upsert, nunca levanta. `tentativas` só é gravado quando informado.
  - `ler(engine, job_id: str) -> dict | None`
  - `backoff(job_try: int) -> int` — segundos até a próxima tentativa.
  - `tentar_de_novo(ctx) -> None` — levanta `arq.Retry` com o backoff da tentativa atual.
  - `esgotou(ctx, max_tries: int = 5) -> bool`
  - `descartar(engine, job_id: str, *, motivo: str) -> None` — dead-letter: marca `descartado` e guarda o motivo legível para a UI.
  - Estados válidos em `ESTADOS: frozenset` = `{"pendente", "rodando", "concluido", "falhou", "descartado"}`.

- [ ] **Step 1: Escrever `src/agent_ops/queue/progresso.sql`**

```sql
-- Progresso do job fora da memoria do processo.
--
-- MOTIVACAO: com o progresso em memoria, um redeploy no meio de uma ingestao
-- longa apaga a barra de progresso e o cliente reconecta no SSE sem saber se o
-- trabalho morreu ou continua. Persistido, a reconexao retoma a leitura.
--
-- Por que nao guardar no Redis junto com a fila: o resultado do arq expira
-- (`keep_result`, 1h por padrao) e o progresso precisa sobreviver a isso para
-- a UI conseguir explicar um job que falhou ontem.
--
-- `descartado` e o dead-letter: esgotou as tentativas e ninguem vai tentar de
-- novo sozinho. `detalhe` carrega o motivo legivel que a UI mostra.

CREATE TABLE IF NOT EXISTS job_progress (
    job_id      TEXT PRIMARY KEY,
    estado      TEXT NOT NULL,
    percentual  INTEGER NOT NULL DEFAULT 0,
    detalhe     TEXT,
    tentativas  INTEGER NOT NULL DEFAULT 0,
    atualizado  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Varrer a dead-letter na UI de operacao.
CREATE INDEX IF NOT EXISTS idx_job_progress_estado
    ON job_progress (estado, atualizado DESC);
```

- [ ] **Step 2: Escrever os testes que falham**

```python
# tests/test_queue_execucao.py
"""Testes do progresso, do retry e da dead-letter.

O que se prende aqui:
- o progresso sobrevive ao processo. Em memoria, um redeploy no meio da
  ingestao apaga a barra e o cliente nao sabe se o job morreu;
- marcar progresso NUNCA derruba o job. Vale o mesmo contrato da trilha:
  perder a barra e ruim, perder o trabalho ja feito e pior;
- o backoff cresce, senao cinco tentativas contra um provider fora do ar
  acontecem no mesmo segundo e nao sao cinco chances, sao uma;
- esgotar as tentativas leva a `descartado` com motivo legivel, nao ao silencio.
"""

import pytest
from arq.worker import Retry
from sqlalchemy import create_engine

from agent_ops.queue import execucao


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/t.db")
    execucao.aplicar_schema(eng)
    return eng


def test_marcar_e_ler(engine):
    execucao.marcar(engine, "j1", estado="rodando", percentual=40)
    p = execucao.ler(engine, "j1")

    assert p["estado"] == "rodando"
    assert p["percentual"] == 40


def test_marcar_duas_vezes_atualiza_em_vez_de_duplicar(engine):
    execucao.marcar(engine, "j1", estado="rodando", percentual=10)
    execucao.marcar(engine, "j1", estado="rodando", percentual=90)

    assert execucao.ler(engine, "j1")["percentual"] == 90


def test_mover_a_barra_nao_conta_como_nova_tentativa(engine):
    # `tentativas` conta RETENTATIVAS do job. `marcar` e chamado varias vezes
    # dentro de uma mesma tentativa so para mover a barra; se cada chamada
    # incrementasse, a UI de operacao mostraria "12 tentativas" para um job que
    # rodou uma vez so.
    execucao.marcar(engine, "j1", estado="rodando", percentual=10, tentativas=1)
    execucao.marcar(engine, "j1", estado="rodando", percentual=50)
    execucao.marcar(engine, "j1", estado="rodando", percentual=90)

    assert execucao.ler(engine, "j1")["tentativas"] == 1

    execucao.marcar(engine, "j1", estado="rodando", percentual=0, tentativas=2)
    assert execucao.ler(engine, "j1")["tentativas"] == 2


def test_ler_job_desconhecido_devolve_none(engine):
    assert execucao.ler(engine, "nao-existe") is None


def test_estado_invalido_e_recusado(engine):
    with pytest.raises(ValueError):
        execucao.marcar(engine, "j1", estado="quase-la")


def test_marcar_nao_derruba_o_job_quando_o_banco_cai():
    class EngineQuebrado:
        def begin(self):
            raise RuntimeError("banco fora do ar")

    execucao.marcar(EngineQuebrado(), "j1", estado="rodando")  # nao levanta


def test_backoff_cresce_com_a_tentativa():
    assert execucao.backoff(1) < execucao.backoff(2) < execucao.backoff(3)


def test_backoff_tem_teto():
    assert execucao.backoff(50) == execucao.backoff(99)


def test_tentar_de_novo_levanta_retry_do_arq():
    with pytest.raises(Retry):
        execucao.tentar_de_novo({"job_try": 2})


def test_esgotou_so_na_ultima_tentativa():
    assert execucao.esgotou({"job_try": 4}, max_tries=5) is False
    assert execucao.esgotou({"job_try": 5}, max_tries=5) is True


def test_descartar_registra_o_motivo(engine):
    execucao.descartar(engine, "j1", motivo="provider fora do ar apos 5 tentativas")
    p = execucao.ler(engine, "j1")

    assert p["estado"] == "descartado"
    assert "5 tentativas" in p["detalhe"]
```

- [ ] **Step 3: Rodar e ver falhar**

Run: `python -m pytest tests/test_queue_execucao.py -v`
Expected: FAIL com `ImportError: cannot import name 'execucao'`

- [ ] **Step 4: Escrever `src/agent_ops/queue/execucao.py`**

```python
"""Progresso duravel, retry com backoff e dead-letter.

O `arq` ja reexecuta job que levanta `Retry` e para depois de `max_tries`. O que
ele NAO faz e contar essa historia para quem esta olhando a tela: o resultado
expira em uma hora e um job que falhou ontem some. As funcoes daqui persistem
o suficiente para a UI explicar o que aconteceu.
"""

from __future__ import annotations

import logging
from importlib import resources

from arq.worker import Retry
from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

ESTADOS = frozenset({"pendente", "rodando", "concluido", "falhou", "descartado"})

# Backoff exponencial com teto. Sem crescimento, cinco tentativas contra um
# provider fora do ar acontecem quase no mesmo segundo — sao uma chance, nao
# cinco. O teto evita que a quinta tentativa caia daqui a horas.
_BASE_SEGUNDOS = 5
_TETO_SEGUNDOS = 300

SQL_SCHEMA: str = (
    resources.files("agent_ops.queue").joinpath("progresso.sql").read_text("utf-8")
)

_UPSERT = text(
    """
    INSERT INTO job_progress
        (job_id, estado, percentual, detalhe, tentativas, atualizado)
    VALUES
        (:job_id, :estado, :percentual, :detalhe,
         COALESCE(:tentativas, 0), CURRENT_TIMESTAMP)
    ON CONFLICT (job_id) DO UPDATE SET
        estado     = excluded.estado,
        percentual = excluded.percentual,
        detalhe    = excluded.detalhe,
        -- COALESCE e nao `+ 1`: `tentativas` conta RETENTATIVAS do job, e
        -- `marcar` e chamado varias vezes dentro de uma mesma tentativa para
        -- mover a barra de progresso. Incrementar aqui faria a coluna contar
        -- atualizacoes de tela e mentir na UI de operacao.
        tentativas = COALESCE(:tentativas, job_progress.tentativas),
        atualizado = CURRENT_TIMESTAMP
    """
)


def aplicar_schema(engine: Engine) -> None:
    """Cria a tabela de progresso. Idempotente."""
    statements = [s.strip() for s in SQL_SCHEMA.split(";") if s.strip()]
    with engine.begin() as conexao:
        for statement in statements:
            conexao.execute(text(statement))


def marcar(
    engine,
    job_id: str,
    *,
    estado: str,
    percentual: int = 0,
    detalhe: str | None = None,
    tentativas: int | None = None,
) -> None:
    """Registra onde o job esta. Nunca derruba o job.

    `tentativas` so e escrito quando informado (o worker passa `ctx["job_try"]`).
    Omitido, o valor ja gravado e preservado — mover a barra de progresso nao
    pode contar como uma nova tentativa.

    Mesmo contrato da trilha de decisao: perder a barra de progresso e ruim,
    perder o trabalho ja feito por causa de um UPDATE e pior.

    `ValueError` para estado invalido e a excecao a regra: e erro de
    programacao, aparece no primeiro teste, e nao acontece em producao.
    """
    if estado not in ESTADOS:
        raise ValueError(f"estado invalido: {estado!r}; esperado um de {sorted(ESTADOS)}")

    try:
        with engine.begin() as conexao:
            conexao.execute(
                _UPSERT,
                {
                    "job_id": job_id,
                    "estado": estado,
                    "percentual": percentual,
                    "detalhe": detalhe,
                    "tentativas": tentativas,
                },
            )
    except Exception:
        logger.exception("queue.progresso_falhou job_id=%s", job_id)


def ler(engine, job_id: str) -> dict | None:
    """Estado atual do job, ou `None` se nunca foi marcado."""
    try:
        with engine.connect() as conexao:
            linha = conexao.execute(
                text(
                    "SELECT job_id, estado, percentual, detalhe, tentativas "
                    "FROM job_progress WHERE job_id = :j"
                ),
                {"j": job_id},
            ).mappings().one_or_none()
    except Exception:
        logger.exception("queue.leitura_progresso_falhou job_id=%s", job_id)
        return None

    return dict(linha) if linha else None


def backoff(job_try: int) -> int:
    """Segundos ate a proxima tentativa: 5, 10, 20, 40... com teto de 300."""
    return min(_BASE_SEGUNDOS * (2 ** max(0, job_try - 1)), _TETO_SEGUNDOS)


def tentar_de_novo(ctx) -> None:
    """Devolve o job para a fila com atraso crescente.

    Levanta `arq.Retry`, que e como o arq espera receber essa intencao.
    """
    raise Retry(defer=backoff(ctx["job_try"]))


def esgotou(ctx, max_tries: int = 5) -> bool:
    """Esta e a ultima tentativa? Se sim, o chamador deve descartar."""
    return ctx["job_try"] >= max_tries


def descartar(engine, job_id: str, *, motivo: str) -> None:
    """Dead-letter: para de tentar e guarda o motivo legivel para a UI."""
    logger.error("queue.descartado job_id=%s motivo=%s", job_id, motivo)
    marcar(engine, job_id, estado="descartado", detalhe=motivo)
```

- [ ] **Step 5: Atualizar `src/agent_ops/queue/__init__.py`**

```python
"""Fila duravel sobre arq."""

from agent_ops.queue import execucao, fila
from agent_ops.queue.execucao import (
    ESTADOS,
    aplicar_schema,
    backoff,
    descartar,
    esgotou,
    ler,
    marcar,
    tentar_de_novo,
)
from agent_ops.queue.fila import FilaCheia, criar_pool, enfileirar, profundidade

__all__ = [
    "fila", "execucao",
    "FilaCheia", "criar_pool", "enfileirar", "profundidade",
    "ESTADOS", "aplicar_schema", "backoff", "descartar", "esgotou",
    "ler", "marcar", "tentar_de_novo",
]
```

- [ ] **Step 6: Declarar o `.sql` da fila como dado do pacote**

Em `pyproject.toml`, substituir o bloco `[tool.setuptools.package-data]` por:

```toml
[tool.setuptools.package-data]
"agent_ops.decisions" = ["*.sql"]
"agent_ops.queue" = ["*.sql"]
```

- [ ] **Step 7: Rodar todos os testes e ver passar**

Run: `pip install -e ".[dev]" && python -m pytest tests/ -v`
Expected: PASS, 45 testes.

- [ ] **Step 8: Commit**

```bash
git add src/agent_ops/queue/ tests/test_queue_execucao.py pyproject.toml
git commit -m "feat: progresso duravel, backoff exponencial e dead-letter com motivo legivel"
```

---

### Task 8: README e publicação da v0.1.0

**Files:**
- Create: `README.md`
- Create: `tests/test_contrato_publico.py`

**Interfaces:**
- Consumes: tudo das Tasks 1–7.
- Produces: tag `v0.1.0` instalável via `pip install git+ssh://git@github.com/DevPedroGomes/agent-ops.git@v0.1.0`.

- [ ] **Step 1: Escrever o teste do contrato público**

```python
# tests/test_contrato_publico.py
"""Prende a superficie publica do pacote.

Por que existe: a fase 2 troca a linha de import do BrainHub para apontar aqui.
Se um rename silencioso mudar `consumir` para `consume`, o BrainHub quebra no
deploy, nao no CI. Este teste faz a quebra acontecer aqui.

Prende tambem o isolamento entre subpacotes: importar a fila nao pode arrastar
SQLAlchemy nem Postgres junto, senao um worker que so enfileira carrega meio
ORM sem precisar.
"""

import importlib
import sys


def test_metering_expoe_os_nomes_que_o_brainhub_usa():
    import agent_ops.metering as m

    for nome in ("consumir", "devolver", "panorama", "TetoAtingido"):
        assert hasattr(m, nome), f"faltou {nome} na superficie publica"


def test_decisions_expoe_registrar_e_digerir():
    import agent_ops.decisions as d

    assert hasattr(d, "registrar")
    assert hasattr(d, "digerir")


def test_queue_expoe_enfileirar_e_progresso():
    import agent_ops.queue as q

    for nome in ("enfileirar", "FilaCheia", "marcar", "ler", "descartar"):
        assert hasattr(q, nome), f"faltou {nome} na superficie publica"


def test_metering_nao_arrasta_sqlalchemy():
    originais = {
        modulo: sys.modules[modulo]
        for modulo in list(sys.modules)
        if modulo.startswith(("agent_ops", "sqlalchemy"))
    }
    for modulo in originais:
        del sys.modules[modulo]

    try:
        importlib.import_module("agent_ops.metering")

        assert "sqlalchemy" not in sys.modules, (
            "metering importou SQLAlchemy; os subpacotes devem ser independentes"
        )
    finally:
        # Sem restaurar o cache original, o SQLAlchemy fica com classes
        # duplicadas em memoria (a QueuePool recem-importada nao e a mesma
        # que o event registry conhece) e todo teste de decisions/queue que
        # roda depois deste na mesma sessao do pytest quebra com
        # "No such event 'connect' for target". Restaurar aqui devolve o
        # processo ao estado anterior ao teste, entao a ordem dos arquivos
        # de teste deixa de importar.
        for modulo in list(sys.modules):
            if modulo.startswith(("agent_ops", "sqlalchemy")) and modulo not in originais:
                del sys.modules[modulo]
        sys.modules.update(originais)
```

> **Nota:** o `try/finally` não é decoração. Mexer em `sys.modules` sem
> restaurar é um efeito colateral de processo, não de teste: ele sobrevive ao
> fim da função e contamina todo teste posterior da mesma sessão. A asserção em
> si continua idêntica — se houver import cruzado de verdade, este teste falha
> do mesmo jeito.

- [ ] **Step 2: Rodar e ver o resultado**

Run: `python -m pytest tests/test_contrato_publico.py -v`
Expected: PASS, 4 testes. Se `test_metering_nao_arrasta_sqlalchemy` falhar, há um import cruzado entre subpacotes — corrigir antes de seguir.

- [ ] **Step 3: Escrever o `README.md`**

````markdown
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
````

- [ ] **Step 4: Rodar a suíte inteira**

Run: `python -m pytest tests/ -v`
Expected: PASS, 49 testes.

- [ ] **Step 5: Commit e tag**

```bash
git add README.md tests/test_contrato_publico.py
git commit -m "docs: README com o contrato de uso dos tres subpacotes"
git tag -a v0.1.0 -m "v0.1.0: metering, decisions e queue"
```

- [ ] **Step 6: Verificar o empacotamento LOCALMENTE, sem publicar**

A pergunta que importa é se os `.sql` viajam dentro do artefato. Um install
editável resolve o `.sql` da árvore de fontes de qualquer jeito, então até aqui
nada provou o `package-data`. Um artefato construído e instalado num venv limpo
prova — e não depende de rede nem de repositório remoto.

```bash
./.venv/bin/pip install build
./.venv/bin/python -m build --wheel --outdir dist/
python3 -m venv /tmp/verifica-agent-ops
/tmp/verifica-agent-ops/bin/pip install dist/agent_ops-0.1.0-py3-none-any.whl
/tmp/verifica-agent-ops/bin/python -c "
import agent_ops
from agent_ops.decisions import migracao
from agent_ops.queue import execucao
import agent_ops.metering
assert 'CREATE TABLE IF NOT EXISTS decisions' in migracao.SQL_SCHEMA
assert 'CREATE TABLE IF NOT EXISTS job_progress' in execucao.SQL_SCHEMA
print('ok', agent_ops.__version__)
"
```

Expected: imprime `ok 0.1.0`. Se algum `.sql` não viajou, o import levanta
`FileNotFoundError` e o `package-data` do `pyproject.toml` está errado — que é
exatamente o que este passo existe para descobrir antes de publicar.

Limpar depois: `rm -rf /tmp/verifica-agent-ops dist/`.

- [ ] **Step 7: Publicar — REQUER APROVAÇÃO HUMANA**

**Não execute este passo autonomamente.** Criar repositório e dar push são
efeitos fora da máquina; quem decide é o dono do código.

```bash
gh repo create DevPedroGomes/agent-ops --private --source=. --remote=origin
git push -u origin main
git push origin v0.1.0
gh run watch -R DevPedroGomes/agent-ops
```

---

## Self-Review

**Cobertura do spec (seção 3 — o núcleo).** As cinco peças previstas: `metering`
(Tasks 2–3), `decisions` (Tasks 4–5) e `queue` (Tasks 6–7) entram nesta fase.
`eval` e `tracing` são fase 3 por decisão explícita do spec — dependem de dados
reais de `decisions` que só a fase 2 produz. Os cinco requisitos da seção 3.3
estão cobertos: idempotência (Task 6), retry com backoff (Task 7), progresso
durável (Task 7), backpressure (Task 6) e dead-letter (Task 7).

**Fair share ficou de fora.** A seção 3.3 do spec também pede fair share por
tenant. O `arq` não oferece isso nativamente e implementar exigiria filas
nomeadas por tenant com um escalonador próprio — escopo grande o bastante para
distorcer esta fase. Fica registrado como o primeiro item da fase 2, quando
houver carga real para dimensionar a solução. Com um só tenant ativo (o
BrainHub hoje), a ausência não muda comportamento.

**Invariantes do spec (seção 8).** Cobertas 1, 2 e 3 (reserva antes da chamada,
Redis ilegível recusa, fila cheia devolve 429 com `Retry-After`) e 4
(idempotência por digest). A invariante 5 (portão humano) é do projeto 1,
fase 5.

**Consistência de tipos.** `digerir` (Task 5) devolve o `str` que `enfileirar`
(Task 6) recebe em `digest=`. `marcar`/`ler`/`descartar` usam `job_id: str`,
que é o que `enfileirar` devolve e o que `ctx["job_id"]` carrega no worker.
`ESTADOS` é a única fonte dos nomes de estado e `descartar` usa `"descartado"`,
que está no conjunto.

**Nomes preservados.** `consumir`, `devolver`, `panorama` e `TetoAtingido`
mantêm a assinatura do `budget.py`, com um parâmetro `escopo` opcional
acrescentado ao fim — as chamadas atuais do BrainHub continuam válidas sem
edição. `panorama` é a exceção: passou a receber `limites` como argumento em
vez de lê-los das settings, porque o pacote não deve conhecer os nomes de cota
de cada projeto. A migração do BrainHub na fase 2 precisa passar
`{"chat": settings.daily_chat_limit, "ingest": settings.daily_ingest_limit}`.
