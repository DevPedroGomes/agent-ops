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
