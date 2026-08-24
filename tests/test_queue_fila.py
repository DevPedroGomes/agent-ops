"""Testes do enfileiramento.

O que se prende aqui:
- o mesmo `(tenant, digest)` nao entra duas vezes. Reentregar um job nao pode
  duplicar efeito, e no BrainHub isso significa nao reprocessar (nem recobrar)
  o mesmo PDF subido duas vezes;
- o job_id deriva do projeto + tenant + digest. Sem o projeto, dois apps
  dividindo o Redis deduplicariam o trabalho um do outro; sem o tenant, dois
  tenants subindo o MESMO arquivo compartilham um job so — o segundo e
  silenciosamente cancelado e passa a ler o progresso do primeiro;
- o id continua nomeavel por fora (`job_id_de`) quando a deduplicacao devolve
  `None`, senao o cliente do trabalho duplicado nao teria SSE para abrir;
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


def test_job_id_isola_tenants_com_o_mesmo_digest():
    # `digest` e hash de CONTEUDO: dois tenants subindo o mesmo arquivo (um
    # formulario padrao, um relatorio anual publico, um modelo de CV) produzem
    # digests identicos. Sem o tenant no id, os dois jobs sao o mesmo job.
    assert queue.fila._job_id("abc123", tenant="t1") == "brainhub:t1:abc123"
    assert queue.fila._job_id("abc123", tenant="t2") == "brainhub:t2:abc123"


def test_sem_tenant_o_id_mantem_o_formato_antigo():
    # Compatibilidade: quem ja chama sem tenant continua com o mesmo id.
    assert queue.fila._job_id("abc123") == "brainhub:abc123"


def test_tenants_diferentes_com_o_mesmo_digest_ambos_enfileiram():
    pool = FakePool()
    a = asyncio.run(queue.enfileirar(pool, "processar", digest="abc", tenant="t1"))
    b = asyncio.run(queue.enfileirar(pool, "processar", digest="abc", tenant="t2"))

    assert a == "brainhub:t1:abc"
    assert b == "brainhub:t2:abc"
    assert len(pool.enfileirados) == 2


def test_mesmo_tenant_e_mesmo_digest_deduplica():
    pool = FakePool()
    um = asyncio.run(queue.enfileirar(pool, "processar", digest="abc", tenant="t1"))
    dois = asyncio.run(queue.enfileirar(pool, "processar", digest="abc", tenant="t1"))

    assert um == "brainhub:t1:abc"
    assert dois is None
    assert len(pool.enfileirados) == 1


def test_tenant_nao_vaza_para_os_kwargs_do_job():
    # `tenant` nomeia o job, nao e argumento da funcao processada. Vazando para
    # os kwargs, toda funcao de worker teria de aceitar um parametro que nao
    # pediu — e o TypeError so apareceria no worker, em producao.
    pool = FakePool()
    asyncio.run(queue.enfileirar(pool, "processar", digest="abc", tenant="t1"))

    assert pool.enfileirados[0][3] == {}


def test_job_id_de_e_publico_e_bate_com_o_id_enfileirado():
    pool = FakePool()
    enfileirado = asyncio.run(
        queue.enfileirar(pool, "processar", digest="abc", tenant="t1")
    )

    assert queue.job_id_de("abc", tenant="t1") == enfileirado


def test_job_id_de_nomeia_o_job_mesmo_quando_enfileirar_deduplica():
    # Na deduplicacao `enfileirar` devolve None, e sem um id publico o chamador
    # responderia {"job_id": null} — o cliente nunca conseguiria abrir o SSE do
    # trabalho que esta de fato acontecendo.
    pool = FakePool()
    asyncio.run(queue.enfileirar(pool, "processar", digest="abc", tenant="t1"))
    duplicado = asyncio.run(
        queue.enfileirar(pool, "processar", digest="abc", tenant="t1")
    )

    assert duplicado is None
    assert queue.job_id_de("abc", tenant="t1") == "brainhub:t1:abc"


class PoolComFilaPropria:
    """Pool criado com `create_pool(..., default_queue_name=...)`.

    O `arq` deixa nomear a fila, e o `enqueue_job` respeita esse nome. Medir a
    profundidade na fila padrao nesse caso le um sorted set sempre vazio.
    """

    default_queue_name = "arq:queue:ingestao"

    def __init__(self, profundidade=0):
        self.lidas: list[str] = []
        self.enfileirados: list[tuple] = []
        self._profundidade = profundidade

    async def zcard(self, nome):
        # So a fila nomeada tem conteudo. Ler qualquer outra chave devolve 0,
        # que e exatamente o que o Redis responde para um sorted set que nao
        # existe — e por isso a leitura errada passava despercebida.
        self.lidas.append(nome)
        return self._profundidade if nome == self.default_queue_name else 0

    async def enqueue_job(self, funcao, *args, _job_id=None, **kwargs):
        self.enfileirados.append((funcao, args, _job_id, kwargs))

        class _Job:
            job_id = _job_id

        return _Job()


def test_profundidade_le_a_fila_do_pool_e_nao_a_padrao():
    pool = PoolComFilaPropria(profundidade=3)

    assert asyncio.run(queue.profundidade(pool)) == 3
    assert pool.lidas == ["arq:queue:ingestao"]


def test_backpressure_vale_para_fila_nomeada():
    # Lendo a fila padrao, esta profundidade seria 0 e o enfileirar aceitaria:
    # a invariante 3 do spec (fila cheia recusa) pararia de valer em silencio
    # para todo consumidor que nomeia a propria fila.
    pool = PoolComFilaPropria(profundidade=500)

    with pytest.raises(queue.FilaCheia):
        asyncio.run(queue.enfileirar(pool, "processar", digest="abc", tenant="t1"))

    assert pool.enfileirados == []


def test_redis_ilegivel_levanta_o_subtipo_de_indisponibilidade():
    # "A fila esta cheia" e "nao da para ler a fila" sao 429 e 503. Com um tipo
    # so, o chamador respondia 429 para uma queda de infra.
    pool = FakePool(explode=True)

    with pytest.raises(queue.FilaIndisponivel):
        asyncio.run(queue.enfileirar(pool, "processar", digest="abc"))


def test_fila_indisponivel_continua_sendo_pega_por_fila_cheia():
    # Compatibilidade: quem ja escreve `except FilaCheia` nao pode passar a
    # deixar a excecao subir depois desta mudanca.
    pool = FakePool(explode=True)

    with pytest.raises(queue.FilaCheia):
        asyncio.run(queue.enfileirar(pool, "processar", digest="abc"))


def test_fila_cheia_de_verdade_nao_e_indisponibilidade():
    pool = FakePool(profundidade=500)

    with pytest.raises(queue.FilaCheia) as exc:
        asyncio.run(queue.enfileirar(pool, "processar", digest="abc"))

    assert not isinstance(exc.value, queue.FilaIndisponivel)


def test_tenant_e_digest_com_dois_pontos_sao_recusados():
    # projeto="brainhub" + digest="b:c" produziria o mesmo id que
    # projeto="brainhub" + tenant="b" + digest="c": o job de um tenant viraria
    # o job de outro, que e exatamente o vazamento que o tenant fechou.
    with pytest.raises(ValueError):
        queue.fila._job_id("abc", tenant="acme:prod")

    with pytest.raises(ValueError):
        queue.fila._job_id("b:c")


def test_enfileirar_recusa_tenant_ambiguo():
    pool = FakePool()

    with pytest.raises(ValueError):
        asyncio.run(
            queue.enfileirar(pool, "processar", digest="abc", tenant="acme:prod")
        )

    assert pool.enfileirados == []
