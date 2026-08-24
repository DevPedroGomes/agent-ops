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
