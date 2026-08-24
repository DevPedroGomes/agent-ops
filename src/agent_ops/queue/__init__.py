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
