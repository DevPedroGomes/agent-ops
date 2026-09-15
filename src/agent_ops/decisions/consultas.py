"""Leitura da trilha.

Por que o pacote passou a trazer as consultas junto: a trilha existe para
responder "por que ele decidiu isso?", e ate aqui o pacote entregava a tabela,
os quatro indices desenhados para essas perguntas exatas, e nenhuma funcao que
os usasse. Cada app escrevia o proprio SELECT. Duas coisas viravam convencao
sem dono nesse arranjo, e as duas sao as que este modulo prende:

TENANT OBRIGATORIO, KEYWORD-ONLY. A regra e "a leitura filtra por tenant, nunca
so por `correlation_id`". Como assinatura, ela deixa de depender de alguem
lembrar: nao existe chamada valida sem tenant. Um `correlation_id` nao e
credencial — ele aparece em log, em URL, em payload de erro — e uma consulta
so por ele entrega a trilha inteira de outro cliente. A unica funcao sem tenant
e `contagem_por_regra`, que devolve contagem e nenhuma linha.

LEITURA LEVANTA. Ao contrario de `registrar`, `marcar` e `ler`, que engolem a
falha. La o raciocinio e que perder uma linha de observabilidade e melhor que
derrubar a resposta que o visitante ja estava recebendo. Aqui ele se inverte:
uma lista vazia por causa de banco fora do ar e indistinguivel de "este tenant
nao tem decisao nenhuma", e essa e a resposta mais perigosa que uma auditoria
pode dar. Quem chama decide o que mostrar.

`evidence` e `outcome` voltam desserializados. Eles entram como dicionario e
ficam em TEXT porque o DDL e portatil, entao devolver a string crua empurraria
o mesmo `json.loads` para dentro de todo chamador.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.engine import Connection, Engine

_COLUNAS = (
    "id, project, tenant_id, correlation_id, input_digest, rule_code, "
    "evidence, outcome, model, tokens_in, tokens_out, cost_cents, parent_id, "
    "created_at"
)

_LISTAR = text(
    f"SELECT {_COLUNAS} FROM decisions "
    "WHERE tenant_id = :tenant_id "
    "ORDER BY created_at DESC, id DESC "
    "LIMIT :limite"
).columns(created_at=DateTime)

_POR_EXECUCAO = text(
    f"SELECT {_COLUNAS} FROM decisions "
    "WHERE tenant_id = :tenant_id AND correlation_id = :correlation_id "
    "ORDER BY created_at ASC, id ASC "
    "LIMIT :limite"
).columns(created_at=DateTime)

_FILHOS = text(
    f"SELECT {_COLUNAS} FROM decisions "
    "WHERE tenant_id = :tenant_id AND parent_id = :parent_id "
    "ORDER BY created_at ASC, id ASC "
    "LIMIT :limite"
).columns(created_at=DateTime)

_CONTAGEM = text(
    "SELECT rule_code, COUNT(*) AS total FROM decisions "
    "WHERE project = :project AND (:desde IS NULL OR created_at >= :desde) "
    "GROUP BY rule_code"
).bindparams(bindparam("desde", type_=DateTime))

# Teto de seguranca para as consultas que devolvem linhas. Sem ele, uma
# listagem de tenant grande puxa a tabela inteira para a memoria do processo
# web. Quem precisa de mais pagina com `antes_de`.
LIMITE_PADRAO = 50


def _limite_valido(limite: int) -> int:
    """`ValueError` e nao clamp: limite zero ou negativo e erro de programacao.

    Clampar silenciosamente para 1 devolveria uma pagina de tamanho errado e o
    bug apareceria como "a listagem some", longe da causa.
    """
    if limite <= 0:
        raise ValueError(f"limite deve ser positivo; recebeu {limite!r}")
    return limite


def _linhas(
    conexao: Connection, statement: Any, parametros: dict[str, Any]
) -> list[dict[str, Any]]:
    """Executa e desserializa `evidence` e `outcome` de volta para dicionario.

    Um JSON invalido na coluna vira `{}` em vez de derrubar a listagem inteira:
    a linha foi gravada por uma versao anterior ou por outra app, e uma trilha
    que nao abre por causa de uma linha ruim e pior que uma linha incompleta.
    O campo cru fica em `evidence_bruto` para quem precisa investigar.
    """
    resultado = []
    for linha in conexao.execute(statement, parametros).mappings():
        dados = dict(linha)
        for campo in ("evidence", "outcome"):
            bruto = dados.get(campo)
            try:
                dados[campo] = json.loads(bruto) if bruto else {}
            except (TypeError, ValueError):
                dados[campo] = {}
                dados[f"{campo}_bruto"] = bruto
        resultado.append(dados)
    return resultado


def listar(
    engine: Engine,
    *,
    tenant_id: str,
    limite: int = LIMITE_PADRAO,
    antes_de: datetime | None = None,
) -> list[dict[str, Any]]:
    """Decisoes do tenant, da mais recente para a mais antiga.

    Usa `idx_decisions_tenant_created`. `antes_de` pagina: passe o `created_at`
    da ultima linha da pagina anterior. A paginacao e por carimbo e nao por
    OFFSET porque a tabela so cresce e um OFFSET grande varre tudo que veio
    antes a cada pagina.
    """
    _limite_valido(limite)
    if antes_de is None:
        statement, parametros = _LISTAR, {"tenant_id": tenant_id, "limite": limite}
    else:
        statement = text(
            f"SELECT {_COLUNAS} FROM decisions "
            "WHERE tenant_id = :tenant_id AND created_at < :antes_de "
            "ORDER BY created_at DESC, id DESC "
            "LIMIT :limite"
        ).columns(created_at=DateTime).bindparams(
            bindparam("antes_de", type_=DateTime)
        )
        parametros = {
            "tenant_id": tenant_id,
            "antes_de": antes_de,
            "limite": limite,
        }

    with engine.connect() as conexao:
        return _linhas(conexao, statement, parametros)


def por_execucao(
    engine: Engine,
    *,
    tenant_id: str,
    correlation_id: str,
    limite: int = 500,
) -> list[dict[str, Any]]:
    """Uma execucao inteira em ordem cronologica, do orquestrador aos workers.

    Usa `idx_decisions_correlation`. O `tenant_id` nao e redundante com o
    `correlation_id`: ele e o que impede que conhecer um id de execucao alheio
    seja o bastante para ler a trilha dela. O limite e mais alto aqui porque a
    unidade e uma execucao, e truncar uma execucao no meio esconde justamente o
    passo final que explica o resultado.
    """
    _limite_valido(limite)
    with engine.connect() as conexao:
        return _linhas(
            conexao,
            _POR_EXECUCAO,
            {
                "tenant_id": tenant_id,
                "correlation_id": correlation_id,
                "limite": limite,
            },
        )


def filhos(
    engine: Engine,
    *,
    tenant_id: str,
    parent_id: str,
    limite: int = 500,
) -> list[dict[str, Any]]:
    """As decisoes que descendem diretamente de `parent_id`.

    Usa `idx_decisions_parent`. Um nivel so: a arvore e rasa por construcao
    (orquestrador para workers) e uma recursiva portatil entre Postgres e
    SQLite custa mais do que o caso de uso pede.
    """
    _limite_valido(limite)
    with engine.connect() as conexao:
        return _linhas(
            conexao,
            _FILHOS,
            {"tenant_id": tenant_id, "parent_id": parent_id, "limite": limite},
        )


def contagem_por_regra(
    engine: Engine, *, desde: datetime | None = None
) -> dict[str, int]:
    """Quantas vezes cada `rule_code` decidiu, no projeto configurado.

    Usa `idx_decisions_project_rule`. E a consulta que semeia o golden set do
    eval: a regra que sempre cai na rede de seguranca aponta falta de insumo,
    nao modelo ruim.

    Unica consulta sem `tenant_id`, de proposito: a pergunta e sobre o projeto
    inteiro e nao sobre um cliente. Por isso ela devolve contagem e nenhuma
    linha — nenhum digest, nenhum id, nada que ligue um numero a alguem.

    `desde` e um `datetime` ingenuo em UTC, igual ao que foi gravado. Ver
    `agent_ops.tempo`.
    """
    from agent_ops.config import get_config

    with engine.connect() as conexao:
        linhas = conexao.execute(
            _CONTAGEM, {"project": get_config().projeto, "desde": desde}
        ).all()
    # Comprehension com coercao em vez de `dict(linhas)`: o `Row` do
    # SQLAlchemy chega como `Any` e o `dict()` cru deixaria o tipo do retorno
    # sem garantia nenhuma.
    return {str(rule_code): int(total) for rule_code, total in linhas}


# Lote padrao do `purgar`. Grande o bastante para a faxina terminar, pequeno o
# bastante para a transacao nao segurar a tabela.
LOTE_PADRAO = 1_000


def purgar(
    engine: Engine,
    *,
    antes_de: datetime,
    tenant_id: str | None = None,
    limite: int = LOTE_PADRAO,
) -> int:
    """Apaga linhas anteriores a `antes_de`. Devolve quantas saíram.

    A trilha cresce por DECISAO e nao por request: um orquestrador que dispara
    cinco workers grava seis linhas. Sem nada que apague, a tabela so cresce, e
    esse e o incidente mais provavel de um servico pequeno rodando por um ano.

    APAGA EM LOTES, e por isso devolve a contagem: chame em laco ate receber
    zero. Um `DELETE` sem teto numa tabela grande segura a transacao e o disco
    de WAL o tempo todo da varredura, ou seja, o proprio remedio vira a queda
    que a retencao existia para evitar. O `IN (SELECT ... LIMIT)` roda igual no
    Postgres e no SQLite.

    `tenant_id` atende a outra pergunta, que nao e retencao: exclusao a pedido
    do titular. Nesse caso passe uma janela que cubra tudo. Ela fica opcional e
    por nome porque o caso comum e a faxina por idade, que atravessa tenants de
    proposito.

    `antes_de` e um `datetime` ingenuo em UTC, igual ao que foi gravado. Ver
    `agent_ops.tempo`.

    LEVANTA se o banco falhar, como toda leitura deste modulo. Uma faxina que
    engole o erro reporta zero linha apagada e parece um laco que terminou.
    """
    _limite_valido(limite)

    filtro = "created_at < :antes_de"
    parametros: dict[str, Any] = {"antes_de": antes_de, "limite": limite}
    if tenant_id is not None:
        filtro += " AND tenant_id = :tenant_id"
        parametros["tenant_id"] = tenant_id

    statement = text(
        "DELETE FROM decisions WHERE id IN ("
        f"SELECT id FROM decisions WHERE {filtro} LIMIT :limite)"
    ).bindparams(bindparam("antes_de", type_=DateTime))

    with engine.begin() as conexao:
        return int(conexao.execute(statement, parametros).rowcount)
