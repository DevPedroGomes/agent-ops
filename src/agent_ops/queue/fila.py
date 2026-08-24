"""Enfileiramento com deduplicacao e backpressure.

DEDUPLICACAO: o `arq` ja resolve unicidade — `enqueue_job(..., _job_id=X)`
devolve `None` quando um job com esse id ja esta na fila ou rodando. Entao a
idempotencia nao precisa de tabela nem de lock: basta derivar o `_job_id` da
entrada. O que o pacote acrescenta e o namespace do projeto, senao dois apps no
mesmo Redis deduplicariam o trabalho um do outro, e o `tenant`, porque a chave
de deduplicacao do spec e `(tenant, input_digest)` e nao o digest sozinho.

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


def _job_id(digest: str, tenant: str | None = None) -> str:
    projeto = get_config().projeto
    return f"{projeto}:{tenant}:{digest}" if tenant else f"{projeto}:{digest}"


def job_id_de(digest: str, tenant: str | None = None) -> str:
    """O id que `enfileirar` daria a este trabalho, calculado sem enfileirar.

    Existe porque `enfileirar` devolve `None` no caminho da deduplicacao, e sem
    um id o chamador responderia `{"job_id": null}` — o cliente nunca abriria o
    SSE do trabalho que esta de fato acontecendo. Como o id e deterministico,
    quem deduplicou pode nomear o job alheio e acompanhar o mesmo progresso.

    Passe os MESMOS `digest` e `tenant` usados no `enfileirar`, senao o id
    aponta para outro job (ou para nenhum).
    """
    return _job_id(digest, tenant)


async def criar_pool() -> ArqRedis:
    """Pool para o lado que ENFILEIRA (a app web). O worker cria o proprio."""
    return await create_pool(RedisSettings.from_dsn(get_config().redis_url))


async def profundidade(pool) -> int:
    """Quantos jobs estao esperando. Levanta se o Redis nao responde.

    Le a fila DO POOL, nao a padrao: `create_pool(default_queue_name=...)` deixa
    o consumidor nomear a propria fila e o `enqueue_job` respeita esse nome.
    Medindo sempre `arq:queue`, um app com fila nomeada leria um sorted set
    vazio, `enfileirar` nunca recusaria e a invariante 3 do spec (fila acima do
    teto responde 429) deixaria de valer sem nenhum sinal.

    `getattr` com fallback porque o parametro aceita qualquer objeto com
    `zcard` — os dubles dos testes incluidos.
    """
    return await pool.zcard(getattr(pool, "default_queue_name", default_queue_name))


async def enfileirar(
    pool,
    funcao: str,
    *args,
    digest: str,
    tenant: str | None = None,
    **kwargs,
) -> str | None:
    """Enfileira `funcao` se ainda nao houver job com o mesmo `(tenant, digest)`.

    Devolve o `job_id`, ou `None` quando o trabalho ja estava na fila — que e
    resposta de sucesso, nao erro: o chamador so precisa saber que o trabalho
    vai acontecer, nao se foi ele quem o criou. Para acompanhar o progresso
    nesse caso, use `job_id_de(digest, tenant)`.

    PASSE SEMPRE O `tenant` NUM APP MULTI-TENANT. `digest` e hash de CONTEUDO:
    dois tenants subindo o mesmo arquivo — um formulario padrao, um relatorio
    anual publico, um modelo de CV — produzem o mesmo digest. Sem o tenant no
    id, o segundo enqueue cai na deduplicacao do primeiro e o segundo tenant
    recebe "aceito" sem que job nenhum rode para ele; pior, ao consultar o
    progresso por esse id ele le o job do PRIMEIRO tenant, com o `detalhe` em
    texto livre que o outro escreveu. `job_progress` nao tem coluna de tenant
    para barrar isso — quem separa os dois e o id.

    `tenant` fica opcional (e por ultimo) para nao quebrar quem ja chama sem
    ele; omitido, o id mantem o formato antigo `projeto:digest`.

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

    job = await pool.enqueue_job(
        funcao, *args, _job_id=_job_id(digest, tenant), **kwargs
    )
    if job is None:
        logger.info("queue.duplicado tenant=%s digest=%s", tenant, digest)
        return None

    return job.job_id
