"""Isolamento da configuracao entre testes.

`get_config` e `lru_cache`, e o `monkeypatch` restaura a env — nao o cache. Sem
este autouse, um teste que liga o kill switch deixa `kill_switch=True` gravado
no processo para todos os que rodarem depois, e a suite so passa porque cada
teste que precisa de config fresca lembra de chamar `cache_clear()` a mao. Isso
e disciplina, nao estrutura.

O caso perigoso e preciso: um teste NOVO de metering que espera `TetoAtingido`
PASSA com o switch preso ligado, porque `consumir` recusa antes de chegar no
codigo sob teste. A asserção fica verde sem ter provado nada.

Limpa antes E depois: antes protege este teste do anterior, depois protege o
proximo deste (inclusive quando este falha no meio).
"""

import asyncio
import os

import pytest

from agent_ops.config import get_config

# Fuso deliberadamente NAO-UTC nos testes de Postgres. A imagem oficial do
# Postgres sobe com TimeZone=UTC, entao um container cru no CI passaria por
# cima da diferenca entre `CURRENT_TIMESTAMP` no SQLite (sempre UTC) e no
# Postgres (resolvido no fuso da sessao) sem nunca exercita-la. Fixar um fuso
# com offset e o que torna o teste capaz de falhar.
FUSO_DE_TESTE = "America/Bahia"   # UTC-3, sem horario de verao


@pytest.fixture(autouse=True)
def config_isolada():
    get_config.cache_clear()
    yield
    get_config.cache_clear()


@pytest.fixture
def postgres_url():
    """DSN de um Postgres descartavel, ou skip.

    Os testes que dependem dele existem porque a suite roda em SQLite, e as
    duas unicas garantias que o pacote faz sobre o banco (o mesmo DDL roda nos
    dois, e o carimbo de tempo e UTC) sao exatamente as que o SQLite nao
    consegue exercitar sozinho.
    """
    url = os.getenv("AGENT_OPS_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("defina AGENT_OPS_TEST_POSTGRES_URL para rodar contra Postgres")
    return url


@pytest.fixture
def engine_postgres(postgres_url):
    """Engine com o fuso da sessao fixado fora do UTC, e schema limpo."""
    from sqlalchemy import create_engine, text

    engine = create_engine(
        postgres_url,
        connect_args={"options": f"-c timezone={FUSO_DE_TESTE}"},
    )
    with engine.begin() as conexao:
        conexao.execute(text("DROP TABLE IF EXISTS decisions"))
        conexao.execute(text("DROP TABLE IF EXISTS job_progress"))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def redis_url():
    """DSN de um Redis descartavel, ou skip.

    Existe pela mesma razao do `postgres_url`: a suite roda em dublê, e o
    dublê nao executa Lua. O script de reserva e o unico pedaco do pacote cuja
    corretude depende do servidor, entao ele precisa de um servidor.
    """
    url = os.getenv("AGENT_OPS_TEST_REDIS_URL")
    if not url:
        pytest.skip("defina AGENT_OPS_TEST_REDIS_URL para rodar contra Redis")
    return url


@pytest.fixture
def em_redis(redis_url, monkeypatch):
    """Roda um cenario inteiro contra um Redis de verdade, num loop so.

    UM loop e nao varios: `asyncio.run` abre e fecha um event loop a cada
    chamada, e o cliente do redis-py amarra no loop que o tocou primeiro. Com
    um dublê isso nao aparece, porque dublê nao tem socket. Com cliente de
    verdade, o segundo `asyncio.run` levanta "Event loop is closed" — que e
    exatamente o problema que `metering.fechar` existe para resolver do lado de
    quem consome. Aqui a saida e nao criar mais de um loop.

    Limpa so as chaves deste projeto de teste, nunca `FLUSHDB`: a variavel pode
    apontar para um Redis compartilhado, e um teste nao tem o direito de apagar
    o que nao e dele.
    """
    import redis.asyncio as aioredis

    from agent_ops.metering import cotas

    monkeypatch.setenv("AGENT_OPS_PROJETO", "testes-redis")
    monkeypatch.setenv("AGENT_OPS_KILL_SWITCH", "false")
    get_config.cache_clear()

    def rodar(corpo):
        async def _cenario():
            cliente = aioredis.from_url(redis_url, decode_responses=True)

            async def _cliente():
                return cliente

            monkeypatch.setattr(cotas, "_redis", _cliente)

            async def _limpar():
                padrao = "ao:budget:testes-redis:*"
                chaves = [c async for c in cliente.scan_iter(padrao)]
                if chaves:
                    await cliente.delete(*chaves)

            try:
                await _limpar()
                return await corpo(cliente)
            finally:
                await _limpar()
                await cliente.aclose()

        return asyncio.run(_cenario())

    return rodar
