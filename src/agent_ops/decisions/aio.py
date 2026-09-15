"""As mesmas funcoes de banco, sem travar o event loop.

Ver o docstring de `agent_ops.queue.aio`: mesmo motivo, mesma tecnica, mesmo
teste de paridade de assinatura vigiando.

`registrar` e a que mais importa aqui. Ela e chamada no meio do caminho de uma
resposta que o visitante ja esta recebendo, e o contrato dela e "nunca derruba
o chamador". Bloquear o event loop nao derruba nada, mas atrasa todo mundo, que
e um jeito mais silencioso de errar.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from sqlalchemy.engine import Engine

from agent_ops.decisions import consultas, migracao, registro
from agent_ops.decisions.consultas import LIMITE_PADRAO, LOTE_PADRAO

__all__ = [
    "aplicar", "registrar",
    "listar", "por_execucao", "filhos", "contagem_por_regra", "purgar",
]


async def aplicar(engine: Engine) -> None:
    return await asyncio.to_thread(migracao.aplicar, engine)


async def registrar(
    engine: Engine,
    *,
    tenant_id: str,
    correlation_id: str,
    input_digest: str,
    rule_code: str,
    evidence: dict[str, Any] | None = None,
    outcome: dict[str, Any] | None = None,
    model: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_cents: int = 0,
    parent_id: str | None = None,
) -> str | None:
    return await asyncio.to_thread(
        registro.registrar,
        engine,
        tenant_id=tenant_id,
        correlation_id=correlation_id,
        input_digest=input_digest,
        rule_code=rule_code,
        evidence=evidence,
        outcome=outcome,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_cents=cost_cents,
        parent_id=parent_id,
    )


async def listar(
    engine: Engine,
    *,
    tenant_id: str,
    limite: int = LIMITE_PADRAO,
    antes_de: datetime | None = None,
) -> list[dict[str, Any]]:
    return await asyncio.to_thread(
        consultas.listar,
        engine,
        tenant_id=tenant_id,
        limite=limite,
        antes_de=antes_de,
    )


async def por_execucao(
    engine: Engine,
    *,
    tenant_id: str,
    correlation_id: str,
    limite: int = 500,
) -> list[dict[str, Any]]:
    return await asyncio.to_thread(
        consultas.por_execucao,
        engine,
        tenant_id=tenant_id,
        correlation_id=correlation_id,
        limite=limite,
    )


async def filhos(
    engine: Engine,
    *,
    tenant_id: str,
    parent_id: str,
    limite: int = 500,
) -> list[dict[str, Any]]:
    return await asyncio.to_thread(
        consultas.filhos,
        engine,
        tenant_id=tenant_id,
        parent_id=parent_id,
        limite=limite,
    )


async def contagem_por_regra(
    engine: Engine, *, desde: datetime | None = None
) -> dict[str, int]:
    return await asyncio.to_thread(consultas.contagem_por_regra, engine, desde=desde)


async def purgar(
    engine: Engine,
    *,
    antes_de: datetime,
    tenant_id: str | None = None,
    limite: int = LOTE_PADRAO,
) -> int:
    return await asyncio.to_thread(
        consultas.purgar,
        engine,
        antes_de=antes_de,
        tenant_id=tenant_id,
        limite=limite,
    )
