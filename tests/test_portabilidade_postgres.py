"""O que o SQLite nao consegue provar sobre o Postgres.

A suite inteira roda em SQLite, de proposito: ela e rapida e nao precisa de
infraestrutura. O preco e que as duas garantias que o pacote faz sobre o banco
ficam sem cobertura justamente onde elas podem quebrar.

1. "O mesmo DDL roda nos dois." Aplicar o arquivo no SQLite nao prova nada
   sobre o Postgres.
2. "O carimbo de tempo e UTC." O `CURRENT_TIMESTAMP` do SQLite e SEMPRE UTC; o
   do Postgres e `timestamptz` e, gravado numa coluna sem fuso, e convertido
   para o FUSO DA SESSAO. Com a sessao fora do UTC, o mesmo DDL guarda hora
   local em producao e UTC no teste, sem erro nenhum.

Por isso o `engine_postgres` fixa `America/Bahia` (UTC-3) na sessao. A imagem
oficial do Postgres sobe em UTC, entao um container cru no CI passaria por cima
da diferenca sem exercita-la, e o teste seria verde por acidente.

Rodar: `AGENT_OPS_TEST_POSTGRES_URL=postgresql+psycopg://... pytest`.
Sem a variavel, tudo aqui e skip.
"""

import datetime

from sqlalchemy import text

from agent_ops.decisions import digerir, migracao, registrar
from agent_ops.queue import execucao

# Quanto o carimbo pode se afastar do relogio antes de ser outra coisa. Folga
# generosa para round trip: o defeito que este teste persegue vale HORAS.
TOLERANCIA_SEGUNDOS = 120


def _agora_utc_ingenuo() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


def test_os_dois_schemas_aplicam_no_postgres(engine_postgres):
    migracao.aplicar(engine_postgres)
    execucao.aplicar_schema(engine_postgres)

    with engine_postgres.connect() as conexao:
        tabelas = set(
            conexao.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            ).scalars()
        )
    assert {"decisions", "job_progress"} <= tabelas


def test_os_dois_schemas_sao_idempotentes_no_postgres(engine_postgres):
    migracao.aplicar(engine_postgres)
    migracao.aplicar(engine_postgres)
    execucao.aplicar_schema(engine_postgres)
    execucao.aplicar_schema(engine_postgres)


def test_o_upsert_de_progresso_funciona_no_postgres(engine_postgres):
    """`ON CONFLICT ... DO UPDATE` com `excluded` roda nos dois, e os COALESCE
    preservam o que foi omitido tambem aqui."""
    execucao.aplicar_schema(engine_postgres)

    execucao.marcar(engine_postgres, "j1", estado="rodando", percentual=80,
                    detalhe="etapa 3")
    execucao.descartar(engine_postgres, "j1", motivo="provider fora do ar")

    linha = execucao.ler(engine_postgres, "j1")
    assert linha["estado"] == "descartado"
    assert linha["detalhe"] == "provider fora do ar"
    assert linha["percentual"] == 80, "o percentual alcancado foi perdido"


def test_created_at_da_trilha_e_utc_com_a_sessao_em_outro_fuso(engine_postgres):
    """O carimbo nao pode andar com o fuso do banco.

    Com `CURRENT_TIMESTAMP` como default numa coluna sem fuso, o Postgres grava
    a hora LOCAL da sessao. O SQLite grava UTC. Ou seja: o mesmo DDL produz
    carimbos diferentes conforme o banco, e a suite em SQLite nunca ve isso.

    Quem paga a conta e a correlacao: o dia da cota vira a meia-noite UTC, e
    comparar a trilha com ele fica errado pelo offset inteiro.
    """
    migracao.aplicar(engine_postgres)

    antes = _agora_utc_ingenuo()
    novo_id = registrar(
        engine_postgres,
        tenant_id="t1",
        correlation_id="run-1",
        input_digest=digerir("conteudo"),
        rule_code="REGRA.X",
    )
    assert novo_id is not None, "a gravacao falhou; ver o log"

    with engine_postgres.connect() as conexao:
        gravado = conexao.execute(
            text("SELECT created_at FROM decisions WHERE id = :i"), {"i": novo_id}
        ).scalar_one()

    desvio = abs((gravado - antes).total_seconds())
    assert desvio < TOLERANCIA_SEGUNDOS, (
        f"created_at={gravado} esta a {desvio / 3600:.1f}h do UTC ({antes}): "
        "o carimbo seguiu o fuso da sessao do banco"
    )


def test_atualizado_do_progresso_e_utc_com_a_sessao_em_outro_fuso(engine_postgres):
    """Mesma armadilha do lado da fila, e aqui ela tem consequencia direta.

    Achar job travado e comparar `atualizado` com o UTC de agora. Com o carimbo
    em hora local, um job parado ha tres horas parece recem-atualizado (ou o
    contrario, conforme o sinal do offset) e a UI de operacao nunca o mostra.
    """
    execucao.aplicar_schema(engine_postgres)

    antes = _agora_utc_ingenuo()
    execucao.marcar(engine_postgres, "j2", estado="rodando", percentual=10)

    gravado = execucao.ler(engine_postgres, "j2")["atualizado"]

    desvio = abs((gravado - antes).total_seconds())
    assert desvio < TOLERANCIA_SEGUNDOS, (
        f"atualizado={gravado} esta a {desvio / 3600:.1f}h do UTC ({antes}): "
        "deteccao de job travado fica errada pelo offset inteiro"
    )


def test_as_consultas_da_trilha_rodam_no_postgres(engine_postgres, monkeypatch):
    """SQL escrito contra SQLite nao e SQL que roda no Postgres.

    `:desde IS NULL OR ...` e o caso classico: o Postgres precisa saber o tipo
    do parametro para planejar, e um NULL sem tipo declarado derruba a consulta
    com "could not determine data type of parameter". No SQLite passa.
    """
    from agent_ops.config import get_config
    from agent_ops.decisions import consultas

    monkeypatch.setenv("AGENT_OPS_PROJETO", "triagem")
    get_config.cache_clear()
    migracao.aplicar(engine_postgres)

    raiz = registrar(
        engine_postgres, tenant_id="t1", correlation_id="run-1",
        input_digest=digerir("a"), rule_code="R.A",
        evidence={"anos": 7}, outcome={"pontos": 30},
    )
    registrar(
        engine_postgres, tenant_id="t1", correlation_id="run-1",
        input_digest=digerir("b"), rule_code="R.B", parent_id=raiz,
    )
    registrar(
        engine_postgres, tenant_id="t2", correlation_id="run-1",
        input_digest=digerir("c"), rule_code="R.A",
    )

    listadas = consultas.listar(engine_postgres, tenant_id="t1")
    assert {linha["tenant_id"] for linha in listadas} == {"t1"}
    assert listadas[-1]["evidence"] == {"anos": 7}, "JSON nao desserializou"

    execucao_t1 = consultas.por_execucao(
        engine_postgres, tenant_id="t1", correlation_id="run-1"
    )
    assert len(execucao_t1) == 2, "o filtro de tenant nao pegou no Postgres"

    assert len(consultas.filhos(engine_postgres, tenant_id="t1", parent_id=raiz)) == 1

    # Os dois caminhos do parametro opcional, que e onde o Postgres reclama.
    assert consultas.contagem_por_regra(engine_postgres) == {"R.A": 2, "R.B": 1}
    ontem = _agora_utc_ingenuo() - datetime.timedelta(days=1)
    assert consultas.contagem_por_regra(engine_postgres, desde=ontem) == {
        "R.A": 2, "R.B": 1
    }

    # Paginacao por carimbo: o `antes_de` tambem viaja como parametro tipado.
    corte = listadas[0]["created_at"]
    assert len(consultas.listar(engine_postgres, tenant_id="t1", antes_de=corte)) == 1


def test_as_consultas_de_operacao_rodam_no_postgres(engine_postgres):
    """E a janela de `travados` tem que casar com o carimbo gravado.

    Este e o teste que junta as duas metades do defeito: se o carimbo seguisse
    o fuso da sessao e o corte fosse calculado em UTC, o job envelhecido em uma
    hora nao apareceria numa janela de trinta minutos.
    """
    from sqlalchemy import DateTime, bindparam

    execucao.aplicar_schema(engine_postgres)

    execucao.marcar(engine_postgres, "vivo", estado="rodando", percentual=5)
    execucao.marcar(engine_postgres, "abandonado", estado="rodando", percentual=40)
    execucao.descartar(engine_postgres, "morto", motivo="provider fora do ar")

    velho = _agora_utc_ingenuo() - datetime.timedelta(hours=1)
    with engine_postgres.begin() as conexao:
        conexao.execute(
            text(
                "UPDATE job_progress SET atualizado = :a WHERE job_id = 'abandonado'"
            ).bindparams(bindparam("a", type_=DateTime)),
            {"a": velho},
        )

    mortos = execucao.listar_por_estado(engine_postgres, "descartado")
    assert [linha["job_id"] for linha in mortos] == ["morto"]

    presos = execucao.travados(engine_postgres, mais_velho_que_segundos=1800)
    assert [linha["job_id"] for linha in presos] == ["abandonado"]
    assert presos[0]["percentual"] == 40


def test_a_retencao_roda_e_conta_certo_no_postgres(engine_postgres, monkeypatch):
    """`rowcount` depois de DELETE e `IN (SELECT ... LIMIT)` nos dois bancos.

    O laco de faxina para quando `purgar` devolve zero, entao uma contagem
    errada aqui e um laco infinito ou uma faxina que desiste com a tabela
    ainda cheia.
    """
    from sqlalchemy import DateTime, bindparam

    from agent_ops.config import get_config
    from agent_ops.decisions import consultas

    monkeypatch.setenv("AGENT_OPS_PROJETO", "triagem")
    get_config.cache_clear()
    migracao.aplicar(engine_postgres)
    execucao.aplicar_schema(engine_postgres)

    for i in range(5):
        registrar(
            engine_postgres, tenant_id="t1", correlation_id=f"run-{i}",
            input_digest=digerir(str(i)), rule_code="R.A",
        )
    velho = _agora_utc_ingenuo() - datetime.timedelta(days=400)
    with engine_postgres.begin() as conexao:
        conexao.execute(
            text("UPDATE decisions SET created_at = :c").bindparams(
                bindparam("c", type_=DateTime)
            ),
            {"c": velho},
        )

    corte = _agora_utc_ingenuo() - datetime.timedelta(days=365)
    assert consultas.purgar(engine_postgres, antes_de=corte, limite=2) == 2
    assert consultas.purgar(engine_postgres, antes_de=corte, limite=2) == 2
    assert consultas.purgar(engine_postgres, antes_de=corte, limite=2) == 1
    assert consultas.purgar(engine_postgres, antes_de=corte, limite=2) == 0

    # Lado da fila, incluindo a garantia de nao levar job vivo junto.
    execucao.marcar(engine_postgres, "terminal", estado="concluido")
    execucao.marcar(engine_postgres, "travado", estado="rodando")
    with engine_postgres.begin() as conexao:
        conexao.execute(
            text("UPDATE job_progress SET atualizado = :a").bindparams(
                bindparam("a", type_=DateTime)
            ),
            {"a": velho},
        )

    assert execucao.purgar(engine_postgres, antes_de=corte) == 1
    assert execucao.ler(engine_postgres, "travado") is not None
