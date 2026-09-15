"""As variantes async nao podem se afastar das sincronas.

Wrapper escrito a mao envelhece: alguem acrescenta um parametro em `marcar` e
`aio.marcar` continua com a assinatura velha. O erro nao aparece no import nem
no mypy do pacote, so na chamada de quem consome, e a mensagem fala de um
argumento inesperado sem dizer que existem duas versoes da funcao.

Dois testes, e o segundo e o que importa mais: o primeiro prende as que ja
existem, o segundo descobre as que alguem esqueceu de espelhar. A regra e
mecanica de proposito: toda funcao publica que recebe `engine` como primeiro
parametro precisa de uma gemea async.
"""

import inspect

import pytest

from agent_ops.decisions import aio as decisions_aio
from agent_ops.decisions import consultas, migracao, registro
from agent_ops.queue import aio as queue_aio
from agent_ops.queue import execucao

PARES = [
    (decisions_aio.aplicar, migracao.aplicar),
    (decisions_aio.registrar, registro.registrar),
    (decisions_aio.listar, consultas.listar),
    (decisions_aio.por_execucao, consultas.por_execucao),
    (decisions_aio.filhos, consultas.filhos),
    (decisions_aio.contagem_por_regra, consultas.contagem_por_regra),
    (decisions_aio.purgar, consultas.purgar),
    (queue_aio.aplicar_schema, execucao.aplicar_schema),
    (queue_aio.marcar, execucao.marcar),
    (queue_aio.ler, execucao.ler),
    (queue_aio.descartar, execucao.descartar),
    (queue_aio.listar_por_estado, execucao.listar_por_estado),
    (queue_aio.travados, execucao.travados),
    (queue_aio.purgar, execucao.purgar),
]


@pytest.mark.parametrize(
    ("assincrona", "sincrona"), PARES, ids=[s.__name__ for _, s in PARES]
)
def test_a_assinatura_async_e_igual_a_sincrona(assincrona, sincrona):
    assert inspect.iscoroutinefunction(assincrona)
    assert inspect.signature(assincrona) == inspect.signature(sincrona), (
        f"{sincrona.__module__}.{sincrona.__name__} e a versao async dela "
        "divergiram"
    )


@pytest.mark.parametrize(
    ("modulo", "aio_modulo"),
    [
        (consultas, decisions_aio),
        (registro, decisions_aio),
        (migracao, decisions_aio),
        (execucao, queue_aio),
    ],
    ids=["consultas", "registro", "migracao", "execucao"],
)
def test_nenhuma_funcao_de_banco_ficou_sem_gemea(modulo, aio_modulo):
    """A regra que descobre o esquecimento, e nao so a divergencia."""
    for nome, funcao in vars(modulo).items():
        if nome.startswith("_") or not inspect.isfunction(funcao):
            continue
        if funcao.__module__ != modulo.__name__:
            continue  # reexport, nao e daqui
        parametros = list(inspect.signature(funcao).parameters)
        if not parametros or parametros[0] != "engine":
            continue
        assert hasattr(aio_modulo, nome), (
            f"{modulo.__name__}.{nome} recebe `engine` e nao tem gemea em "
            f"{aio_modulo.__name__}: quem chamar de dentro de um `async def` "
            "vai bloquear o event loop sem perceber"
        )


# --------------------------------------------------- comportamento, nao so forma


def test_a_versao_async_grava_de_verdade(tmp_path, monkeypatch):
    """Assinatura igual nao prova que o wrapper chama alguem."""
    import asyncio

    from sqlalchemy import create_engine

    monkeypatch.setenv("AGENT_OPS_PROJETO", "triagem")
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")

    async def cenario():
        await decisions_aio.aplicar(engine)
        novo_id = await decisions_aio.registrar(
            engine,
            tenant_id="t1",
            correlation_id="run-1",
            input_digest="abc",
            rule_code="R.A",
            evidence={"anos": 7},
        )
        return novo_id, await decisions_aio.listar(engine, tenant_id="t1")

    novo_id, linhas = asyncio.run(cenario())

    assert novo_id is not None
    assert [linha["id"] for linha in linhas] == [novo_id]
    assert linhas[0]["evidence"] == {"anos": 7}


def test_a_versao_async_da_fila_grava_e_le(tmp_path):
    import asyncio

    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path}/t.db")

    async def cenario():
        await queue_aio.aplicar_schema(engine)
        await queue_aio.marcar(engine, "j1", estado="rodando", percentual=40)
        await queue_aio.descartar(engine, "j1", motivo="provider fora do ar")
        return await queue_aio.ler(engine, "j1"), await queue_aio.listar_por_estado(
            engine, "descartado"
        )

    linha, mortos = asyncio.run(cenario())

    assert linha["estado"] == "descartado"
    assert linha["percentual"] == 40, "o wrapper perdeu a semantica de preservar"
    assert [m["job_id"] for m in mortos] == ["j1"]


def test_a_versao_async_nao_bloqueia_o_event_loop(tmp_path):
    """O ponto todo do modulo: a ida ao banco sai do loop.

    Um relogio roda no loop enquanto a consulta acontece numa thread. Se a
    chamada bloqueasse, o relogio nao teria avancado nenhuma vez.
    """
    import asyncio

    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    queue_aio_engine_lento(engine)

    async def cenario():
        await queue_aio.aplicar_schema(engine)

        tiques = 0

        async def relogio():
            nonlocal tiques
            while True:
                await asyncio.sleep(0.001)
                tiques += 1

        batida = asyncio.create_task(relogio())
        await queue_aio.marcar(engine, "j1", estado="rodando")
        batida.cancel()
        return tiques

    assert asyncio.run(cenario()) > 0, (
        "o loop ficou parado durante a ida ao banco: o wrapper nao saiu da thread"
    )


def queue_aio_engine_lento(engine):
    """Deixa cada INSERT lento o bastante para o relogio conseguir bater."""
    import time

    from sqlalchemy import event

    @event.listens_for(engine, "before_cursor_execute")
    def _atrasar(conn, cursor, statement, parameters, context, executemany):
        if "job_progress" in statement and "INSERT" in statement.upper():
            time.sleep(0.05)
