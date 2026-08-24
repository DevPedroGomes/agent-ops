"""Testes do schema da trilha de decisao.

O que se prende aqui:
- o DDL e idempotente: aplicar duas vezes nao pode quebrar o boot da app;
- a tabela guarda METADADO, nao conteudo. `input_digest` e hash, e nao existe
  coluna para o texto da entrada — se alguem acrescentar uma, este teste cai;
- `rule_code` existe e e obrigatorio: e ele que responde "por que decidiu
  isso?" com um codigo, nao com um paragrafo do modelo;
- `parent_id` existe, senao nao da para ligar worker ao orquestrador.
"""

from sqlalchemy import create_engine, inspect, text

from agent_ops.decisions import migracao


def test_ddl_nao_carrega_coluna_de_conteudo():
    ddl = migracao.SQL_SCHEMA.lower()
    for proibida in (" input text", " prompt text", " content text", " raw_input"):
        assert proibida not in ddl, f"coluna de conteudo no schema: {proibida}"


def test_ddl_e_idempotente():
    assert "create table if not exists" in migracao.SQL_SCHEMA.lower()


def test_aplicar_cria_a_tabela_com_as_colunas_esperadas(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    migracao.aplicar(engine)

    colunas = {c["name"] for c in inspect(engine).get_columns("decisions")}
    esperadas = {
        "id", "project", "tenant_id", "correlation_id", "input_digest",
        "rule_code", "evidence", "outcome", "model", "tokens_in",
        "tokens_out", "cost_cents", "parent_id", "created_at",
    }
    assert esperadas <= colunas


def test_aplicar_duas_vezes_nao_quebra(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    migracao.aplicar(engine)
    migracao.aplicar(engine)  # nao levanta

    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM decisions")).scalar() == 0
