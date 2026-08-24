"""Testes da gravacao da trilha.

O que se prende aqui:
- gravar a trilha NUNCA derruba o chamador. A trilha e observabilidade; perder
  uma linha e ruim, perder a resposta que o visitante ja estava recebendo e
  pior. Este e o mesmo contrato do BrainHub atual;
- o digest e estavel: a mesma entrada da sempre o mesmo hash, senao a
  deduplicacao da fila (Task 6) nao funciona;
- `evidence` e `outcome` viajam como JSON e voltam como dict;
- `parent_id` amarra worker ao orquestrador.
"""

import json

import pytest
from sqlalchemy import create_engine, text

from agent_ops.config import get_config
from agent_ops.decisions import migracao, registro


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OPS_PROJETO", "triagem")
    get_config.cache_clear()
    eng = create_engine(f"sqlite:///{tmp_path}/t.db")
    migracao.aplicar(eng)
    return eng


def test_digest_e_estavel():
    assert registro.digerir("abc") == registro.digerir("abc")
    assert registro.digerir("abc") != registro.digerir("abd")
    assert len(registro.digerir("abc")) == 64


def test_digest_aceita_bytes_e_str_igualmente():
    assert registro.digerir("abc") == registro.digerir(b"abc")


def test_registrar_grava_e_devolve_o_id(engine):
    id_ = registro.registrar(
        engine,
        tenant_id="t1",
        correlation_id="exec-1",
        input_digest=registro.digerir("curriculo-1"),
        rule_code="RUBRICA.PYTHON.SENIOR",
        evidence={"anos_python": 7, "origem": "linha 12"},
        outcome={"pontos": 30},
        model="claude-haiku-4-5",
        tokens_in=800,
        tokens_out=120,
        cost_cents=2,
    )

    assert id_ is not None
    with engine.connect() as c:
        linha = c.execute(
            text("SELECT project, tenant_id, rule_code, evidence, outcome, "
                 "tokens_in, cost_cents FROM decisions WHERE id = :i"),
            {"i": id_},
        ).one()

    assert linha.project == "triagem"
    assert linha.tenant_id == "t1"
    assert linha.rule_code == "RUBRICA.PYTHON.SENIOR"
    assert json.loads(linha.evidence)["anos_python"] == 7
    assert json.loads(linha.outcome)["pontos"] == 30
    assert linha.tokens_in == 800
    assert linha.cost_cents == 2


def test_registrar_liga_worker_ao_orquestrador(engine):
    pai = registro.registrar(
        engine, tenant_id="t1", correlation_id="exec-1",
        input_digest="d0", rule_code="PLANO.CRIADO",
    )
    filho = registro.registrar(
        engine, tenant_id="t1", correlation_id="exec-1",
        input_digest="d1", rule_code="RUBRICA.PYTHON.SENIOR", parent_id=pai,
    )

    with engine.connect() as c:
        achados = c.execute(
            text("SELECT id FROM decisions WHERE parent_id = :p"), {"p": pai}
        ).scalars().all()

    assert achados == [filho]


def test_banco_quebrado_nao_derruba_o_chamador():
    class EngineQuebrado:
        def begin(self):
            raise RuntimeError("banco fora do ar")

    resultado = registro.registrar(
        EngineQuebrado(), tenant_id="t1", correlation_id="e",
        input_digest="d", rule_code="R",
    )

    assert resultado is None  # engoliu a falha e seguiu


def test_evidence_e_outcome_vazios_viram_json_vazio(engine):
    id_ = registro.registrar(
        engine, tenant_id="t1", correlation_id="e",
        input_digest="d", rule_code="R",
    )

    with engine.connect() as c:
        linha = c.execute(
            text("SELECT evidence, outcome FROM decisions WHERE id = :i"), {"i": id_}
        ).one()

    assert json.loads(linha.evidence) == {}
    assert json.loads(linha.outcome) == {}
