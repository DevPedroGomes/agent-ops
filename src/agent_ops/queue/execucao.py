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
