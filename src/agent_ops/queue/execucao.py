"""Progresso duravel, retry com backoff e dead-letter.

O `arq` ja reexecuta job que levanta `Retry` e para depois de `max_tries`. O que
ele NAO faz e contar essa historia para quem esta olhando a tela: o resultado
expira em uma hora e um job que falhou ontem some. As funcoes daqui persistem
o suficiente para a UI explicar o que aconteceu.

CHAME `aplicar_schema` NO BOOT. Sem a tabela `job_progress`, `marcar` engole o
erro (contrato "nunca derruba o job") e `ler` devolve `None` — o que e
indistinguivel de "esse job nunca comecou". O sistema de progresso passa a nao
reportar nada, para sempre, sem levantar um unico erro. A pista fica so no log.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta
from importlib import resources
from typing import Any

from arq.worker import Retry
from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.engine import Engine

from agent_ops.tempo import agora_utc

__all__ = [
    "ESTADOS", "MAX_TENTATIVAS", "SQL_SCHEMA", "agora_utc", "aplicar_schema",
    "backoff", "descartar", "esgotou", "ler", "listar_por_estado", "marcar",
    "purgar", "tentar_de_novo", "travados",
]

logger = logging.getLogger(__name__)

# O `ctx` que o arq passa para a funcao do worker. Alias em vez de `dict` cru
# para a assinatura dizer de onde o dicionario vem: quem o preenche e o arq, e
# as chaves usadas aqui sao `job_id` e `job_try`.
Ctx = dict[str, Any]

ESTADOS = frozenset({"pendente", "rodando", "concluido", "falhou", "descartado"})

# Os estados em que ninguem mais vai mexer no job. E o default do `purgar`
# justamente porque o complemento (`pendente`, `rodando`) e onde mora o job
# travado: apagar a linha dele durante a faxina esconde a evidencia sem
# recuperar o trabalho.
ESTADOS_TERMINAIS = frozenset({"concluido", "falhou", "descartado"})

# Teto de tentativas. Este numero e o MESMO que o worker precisa declarar em
# `WorkerSettings.max_tries`, e por isso ele e exportado em vez de ficar como
# literal: no `Worker.run_job` do arq, quando `job_try > max_tries` o job e
# encerrado com JobExecutionFailed SEM chamar a funcao. Com `max_tries = 3` no
# worker e `esgotou` valendo 5, a tentativa que passaria por `descartar` nunca
# executa: `esgotou` nunca fica True, nada vai para a dead-letter, e o job
# simplesmente some da UI de operacao — que e o unico motivo de `descartado`
# existir. Amarrar os dois com `max_tries = queue.MAX_TENTATIVAS` fecha isso
# por construcao, em vez de por disciplina.
MAX_TENTATIVAS = 5

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
        (:job_id, :estado, COALESCE(:percentual, 0), :detalhe,
         COALESCE(:tentativas, 0), :atualizado)
    ON CONFLICT (job_id) DO UPDATE SET
        estado     = excluded.estado,
        -- Os tres COALESCE dizem a mesma coisa: OMITIR preserva, informar
        -- sobrescreve. Sem eles, `descartar` (que so passa `detalhe`) zerava o
        -- percentual e um job morto aos 80% aparecia como 0% na UI de
        -- operacao — jogando fora o unico fato util sobre ele; e um tick de
        -- progresso sem `detalhe` apagava a frase que a tela estava mostrando.
        -- `percentual = 0` continua sobrescrevendo: zero e valor legitimo (um
        -- retry recomeca a barra), so a AUSENCIA e que preserva.
        percentual = COALESCE(:percentual, job_progress.percentual),
        detalhe    = COALESCE(:detalhe, job_progress.detalhe),
        -- COALESCE e nao `+ 1`: `tentativas` conta RETENTATIVAS do job, e
        -- `marcar` e chamado varias vezes dentro de uma mesma tentativa para
        -- mover a barra de progresso. Incrementar aqui faria a coluna contar
        -- atualizacoes de tela e mentir na UI de operacao.
        tentativas = COALESCE(:tentativas, job_progress.tentativas),
        atualizado = :atualizado
    """
).bindparams(bindparam("atualizado", type_=DateTime))


# `.columns(atualizado=DateTime)` e o que faz o tipo devolvido NAO depender do
# banco. O driver do Postgres ja entrega `datetime`, o do SQLite entrega a
# string crua que gravou. O unico uso de `atualizado` e aritmetica (achar o job
# travado: `agora - atualizado > limite`), entao a string quebra com TypeError
# em quem desenvolveu contra a suite e nao contra producao.
_SELECT = text(
    "SELECT job_id, estado, percentual, detalhe, tentativas, atualizado "
    "FROM job_progress WHERE job_id = :j"
).columns(atualizado=DateTime)


def aplicar_schema(engine: Engine) -> None:
    """Cria a tabela de progresso. Idempotente."""
    statements = [s.strip() for s in SQL_SCHEMA.split(";") if s.strip()]
    with engine.begin() as conexao:
        for statement in statements:
            conexao.execute(text(statement))


def marcar(
    engine: Engine,
    job_id: str,
    *,
    estado: str,
    percentual: int | None = None,
    detalhe: str | None = None,
    tentativas: int | None = None,
) -> None:
    """Registra onde o job esta. Nunca derruba o job.

    `percentual`, `detalhe` e `tentativas` seguem a mesma regra: OMITIR
    preserva o que ja estava gravado, informar sobrescreve. Antes so
    `tentativas` fazia isso, e as consequencias eram visiveis — `descartar`,
    que so passa `detalhe`, zerava o percentual e um job morto aos 80% aparecia
    como 0%; e um tick de progresso sem `detalhe` apagava a frase que a UI
    estava mostrando. `percentual=0` continua sendo gravado: zero e valor
    legitimo (um retry recomeca a barra), so a AUSENCIA preserva.

    Nao ha como limpar `detalhe` de volta para NULL, e nao precisa: a proxima
    mensagem sobrescreve, e um job sem explicacao nenhuma na tela nao e um
    estado que alguem queira pedir.

    `tentativas` e informado pelo worker via `ctx["job_try"]`; mover a barra de
    progresso nao pode contar como uma nova tentativa.

    Mesmo contrato da trilha de decisao: perder a barra de progresso e ruim,
    perder o trabalho ja feito por causa de um UPDATE e pior.

    `ValueError` para estado invalido e a excecao a regra: e erro de
    programacao, aparece no primeiro teste, e nao acontece em producao.
    """
    if estado not in ESTADOS:
        raise ValueError(
            f"estado invalido: {estado!r}; esperado um de {sorted(ESTADOS)}"
        )

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
                    # Carimbado aqui, nao pelo default da coluna: ver
                    # `agent_ops.tempo`. Achar job travado compara este valor
                    # com o UTC de agora, entao ele nao pode andar com o fuso
                    # da sessao do banco.
                    "atualizado": agora_utc(),
                },
            )
    except Exception as exc:
        # O TIPO vai na mensagem, nao so no traceback anexado. Esquecer
        # `aplicar_schema` produz um sistema de progresso que nao reporta nada,
        # para sempre, sem levantar erro nenhum: `marcar` engole aqui e `ler`
        # devolve None, indistinguivel de "esse job nunca comecou". Este log e
        # a unica pista que sobra, e sem o tipo "no such table: job_progress"
        # (permanente, alguem esqueceu a migracao) e "connection reset"
        # (transitorio, passa sozinho) sao a mesma linha para quem opera —
        # varios formatadores de producao nem imprimem o traceback.
        logger.exception(
            "queue.progresso_falhou job_id=%s erro=%s: %s",
            job_id,
            type(exc).__name__,
            exc,
        )


def ler(engine: Engine, job_id: str) -> dict[str, Any] | None:
    """Estado atual do job, ou `None` se nunca foi marcado.

    `atualizado` vem junto porque sem ele "rodando" ha dez segundos e "rodando"
    desde que o worker levou SIGKILL uma hora atras sao indistinguiveis para
    quem le, e a segunda situacao e exatamente a que alguem precisa enxergar.

    Ele volta como `datetime` ingenuo em UTC nos dois bancos, e nao como a
    string que o SQLite gravou: quem calcula a idade do job nao deve descobrir
    o driver pelo TypeError.
    """
    try:
        with engine.connect() as conexao:
            linha = conexao.execute(
                _SELECT, {"j": job_id}
            ).mappings().one_or_none()
    except Exception as exc:
        # Mesma razao do `marcar`: este `None` e igual ao `None` de "job
        # desconhecido", entao o tipo do erro no log e o que separa "falta a
        # tabela" de "o banco piscou".
        logger.exception(
            "queue.leitura_progresso_falhou job_id=%s erro=%s: %s",
            job_id,
            type(exc).__name__,
            exc,
        )
        return None

    return dict(linha) if linha else None


def backoff(job_try: int) -> int:
    """Segundos ate a proxima tentativa: 5, 10, 20, 40... com teto de 300."""
    return int(min(_BASE_SEGUNDOS * (2 ** max(0, job_try - 1)), _TETO_SEGUNDOS))


def tentar_de_novo(ctx: Ctx) -> None:
    """Devolve o job para a fila com atraso crescente.

    Levanta `arq.Retry`, que e como o arq espera receber essa intencao.
    """
    raise Retry(defer=backoff(ctx["job_try"]))


def esgotou(ctx: Ctx, max_tries: int = MAX_TENTATIVAS) -> bool:
    """Esta e a ultima tentativa? Se sim, o chamador deve descartar.

    O default e `MAX_TENTATIVAS` para o worker poder declarar
    `max_tries = queue.MAX_TENTATIVAS` e os dois numeros nunca divergirem. So
    passe outro valor se `WorkerSettings.max_tries` tambem for esse.
    """
    return bool(ctx["job_try"] >= max_tries)


def descartar(engine: Engine, job_id: str, *, motivo: str) -> None:
    """Dead-letter: para de tentar e guarda o motivo legivel para a UI.

    Nao passa `percentual` de proposito: `marcar` preserva o que ja estava
    gravado, entao um job morto aos 80% continua mostrando 80% — que e o dado
    mais util que a UI de operacao tem sobre ele.
    """
    logger.error("queue.descartado job_id=%s motivo=%s", job_id, motivo)
    marcar(engine, job_id, estado="descartado", detalhe=motivo)


# Mesmas colunas do `ler`, para a listagem e a leitura de um job so nao
# divergirem em forma conforme quem pergunta.
_COLUNAS = "job_id, estado, percentual, detalhe, tentativas, atualizado"

_POR_ESTADO = text(
    f"SELECT {_COLUNAS} FROM job_progress "
    "WHERE estado = :estado "
    "ORDER BY atualizado DESC "
    "LIMIT :limite"
).columns(atualizado=DateTime)

_TRAVADOS = text(
    f"SELECT {_COLUNAS} FROM job_progress "
    "WHERE estado = :estado AND atualizado < :corte "
    "ORDER BY atualizado ASC "
    "LIMIT :limite"
).columns(atualizado=DateTime).bindparams(bindparam("corte", type_=DateTime))

# Teto para a tela de operacao nao puxar a tabela inteira para a memoria.
LIMITE_PADRAO = 50


def _validar(estado: str, limite: int) -> None:
    if estado not in ESTADOS:
        raise ValueError(
            f"estado invalido: {estado!r}; esperado um de {sorted(ESTADOS)}"
        )
    if limite <= 0:
        raise ValueError(f"limite deve ser positivo; recebeu {limite!r}")


def listar_por_estado(
    engine: Engine, estado: str, *, limite: int = LIMITE_PADRAO
) -> list[dict[str, Any]]:
    """Jobs num estado, do mais recente para o mais antigo.

    Existe principalmente para `descartado`: varrer a dead-letter. O indice
    `idx_job_progress_estado` nasceu junto com a tabela para esta consulta e
    ficou sem consumidor nenhum, entao `descartado` era um estado que o pacote
    sabia escrever e nao sabia mostrar.

    LEVANTA quando o banco falha, ao contrario de `ler`. Sao contratos
    diferentes de proposito: `ler` responde a um cliente que da poll no proprio
    job, e ali engolir a falha degrada a barra de progresso e nada mais. Esta
    aqui responde a quem opera, e uma dead-letter vazia por falha de banco diz
    "nao ha nada morto", que e a resposta errada mais cara desta tela.
    """
    _validar(estado, limite)
    with engine.connect() as conexao:
        linhas = conexao.execute(
            _POR_ESTADO, {"estado": estado, "limite": limite}
        ).mappings().all()
    return [dict(linha) for linha in linhas]


def travados(
    engine: Engine,
    *,
    mais_velho_que_segundos: int,
    estado: str = "rodando",
    limite: int = LIMITE_PADRAO,
) -> list[dict[str, Any]]:
    """Jobs parados num estado ha mais tempo que a janela, do mais velho primeiro.

    A definicao operacional de job travado: `rodando` com `atualizado` velho e
    o worker que levou SIGKILL no meio do trabalho. Ninguem vai retomar esse
    job e nada no arq o denuncia, porque do ponto de vista dele o job foi
    entregue. Sem esta consulta ele fica `rodando` para sempre.

    O corte e calculado em UTC contra um carimbo gravado em UTC (ver
    `agent_ops.tempo`). Enquanto o carimbo vinha do default da coluna, o
    Postgres o resolvia no fuso da SESSAO e esta comparacao errava pelo offset
    inteiro: com UTC-3 e janela de uma hora, um job parado ha duas horas nao
    aparecia.

    A janela tem que ser positiva. Zero devolveria todo job no estado,
    inclusive o que comecou neste segundo, e uma tela de "travados" que mostra
    os saudaveis junto nao e usada duas vezes.
    """
    if mais_velho_que_segundos <= 0:
        raise ValueError(
            "mais_velho_que_segundos deve ser positivo; recebeu "
            f"{mais_velho_que_segundos!r}"
        )
    _validar(estado, limite)

    corte = agora_utc() - timedelta(seconds=mais_velho_que_segundos)
    with engine.connect() as conexao:
        linhas = conexao.execute(
            _TRAVADOS, {"estado": estado, "corte": corte, "limite": limite}
        ).mappings().all()
    return [dict(linha) for linha in linhas]


LOTE_PADRAO = 1_000


def purgar(
    engine: Engine,
    *,
    antes_de: datetime,
    estados: Iterable[str] = ESTADOS_TERMINAIS,
    limite: int = LOTE_PADRAO,
) -> int:
    """Apaga progresso terminal anterior a `antes_de`. Devolve quantos saíram.

    A tabela guarda uma linha por job para sempre. Depois que o job terminou e
    que ninguem mais vai abrir a tela dele, a linha e so disco.

    O DEFAULT NAO INCLUI `pendente` NEM `rodando`. Um job `rodando` com
    `atualizado` velho e exatamente o job travado que `travados` existe para
    mostrar: apagar a linha dele durante a faxina resolve o sintoma apagando a
    evidencia, e o trabalho continua perdido, agora sem rastro. Quem quiser
    incluir esses estados passa `estados` explicitamente, e ai e uma decisao e
    nao um efeito colateral.

    Apaga em LOTES e devolve a contagem: chame em laco ate receber zero. Ver o
    mesmo raciocinio em `agent_ops.decisions.consultas.purgar`.

    `antes_de` e um `datetime` ingenuo em UTC. Ver `agent_ops.tempo`.
    """
    estados = list(estados)
    if not estados:
        raise ValueError("estados nao pode ser vazio")
    for estado in estados:
        if estado not in ESTADOS:
            raise ValueError(
                f"estado invalido: {estado!r}; esperado um de {sorted(ESTADOS)}"
            )
    if limite <= 0:
        raise ValueError(f"limite deve ser positivo; recebeu {limite!r}")

    # Lista expandida em placeholders nomeados em vez de `expanding`, para o
    # SQL sair identico nos dois bancos e continuar legivel num EXPLAIN.
    nomes = [f"e{i}" for i in range(len(estados))]
    marcadores = ", ".join(f":{nome}" for nome in nomes)
    statement = text(
        "DELETE FROM job_progress WHERE job_id IN ("
        "SELECT job_id FROM job_progress "
        f"WHERE atualizado < :antes_de AND estado IN ({marcadores}) "
        "LIMIT :limite)"
    ).bindparams(bindparam("antes_de", type_=DateTime))

    parametros: dict[str, Any] = {"antes_de": antes_de, "limite": limite}
    parametros.update(dict(zip(nomes, estados, strict=True)))

    with engine.begin() as conexao:
        return int(conexao.execute(statement, parametros).rowcount)
