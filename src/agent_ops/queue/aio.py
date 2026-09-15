"""As mesmas funcoes de banco, sem travar o event loop.

POR QUE ISTO EXISTE: `marcar`, `ler` e as consultas usam SQLAlchemy sincrono, e
o lugar natural de chamar todas elas e dentro de `async def` — um handler do
FastAPI ou uma funcao de job do arq. O arq roda ate `max_jobs` jobs
concorrentes NUM UNICO event loop, entao um tick de progresso bloqueia os
irmaos e o health check do worker durante a ida ao banco. Com o banco local
isso e microssegundos e nao importa; com banco remoto, ou `max_jobs` alto, um
job lento passa a atrasar todos os outros sem nada no log explicando por que.

A alternativa seria um segundo caminho de SQL sobre `AsyncEngine`, com driver
async proprio. Seria manter duas implementacoes do mesmo INSERT e obrigar quem
consome a adotar asyncpg. `asyncio.to_thread` resolve o problema real (tirar o
bloqueio do loop) sem nenhuma das duas coisas: a Engine sincrona do SQLAlchemy
ja e segura entre threads, e o pool dela cuida do resto.

As assinaturas sao copias fieis das sincronas, e `test_aio_paridade.py` falha
se alguma divergir ou faltar. Boilerplate com um teste que o vigia e melhor que
uma indirecao esperta que nao aparece no autocomplete nem no mypy.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy.engine import Engine

from agent_ops.queue import execucao
from agent_ops.queue.execucao import ESTADOS_TERMINAIS, LIMITE_PADRAO, LOTE_PADRAO

__all__ = [
    "aplicar_schema", "marcar", "ler", "descartar",
    "listar_por_estado", "travados", "purgar",
]


async def aplicar_schema(engine: Engine) -> None:
    return await asyncio.to_thread(execucao.aplicar_schema, engine)


async def marcar(
    engine: Engine,
    job_id: str,
    *,
    estado: str,
    percentual: int | None = None,
    detalhe: str | None = None,
    tentativas: int | None = None,
) -> None:
    return await asyncio.to_thread(
        execucao.marcar,
        engine,
        job_id,
        estado=estado,
        percentual=percentual,
        detalhe=detalhe,
        tentativas=tentativas,
    )


async def ler(engine: Engine, job_id: str) -> dict[str, Any] | None:
    return await asyncio.to_thread(execucao.ler, engine, job_id)


async def descartar(engine: Engine, job_id: str, *, motivo: str) -> None:
    return await asyncio.to_thread(execucao.descartar, engine, job_id, motivo=motivo)


async def listar_por_estado(
    engine: Engine, estado: str, *, limite: int = LIMITE_PADRAO
) -> list[dict[str, Any]]:
    return await asyncio.to_thread(
        execucao.listar_por_estado, engine, estado, limite=limite
    )


async def travados(
    engine: Engine,
    *,
    mais_velho_que_segundos: int,
    estado: str = "rodando",
    limite: int = LIMITE_PADRAO,
) -> list[dict[str, Any]]:
    return await asyncio.to_thread(
        execucao.travados,
        engine,
        mais_velho_que_segundos=mais_velho_que_segundos,
        estado=estado,
        limite=limite,
    )


async def purgar(
    engine: Engine,
    *,
    antes_de: datetime,
    estados: Iterable[str] = ESTADOS_TERMINAIS,
    limite: int = LOTE_PADRAO,
) -> int:
    return await asyncio.to_thread(
        execucao.purgar, engine, antes_de=antes_de, estados=estados, limite=limite
    )
