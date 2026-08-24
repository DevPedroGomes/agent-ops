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
