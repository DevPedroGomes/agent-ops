"""Testes da leitura da trilha.

O que se prende aqui:
- `tenant_id` e OBRIGATORIO e keyword-only em toda consulta. A regra do spec e
  "a leitura filtra por tenant, nunca so por correlation_id", e enquanto ela
  morava so na prosa cada app reimplementava o SELECT e podia esquecer. Aqui
  ela e da assinatura: nao da para chamar sem passar o tenant;
- um `correlation_id` de outro tenant nao devolve nada. Id de correlacao nao e
  credencial: quem conhece o id de uma execucao alheia nao pode ler a trilha
  dela;
- `evidence` e `outcome` voltam como dicionario. Eles entram como dicionario e
  ficam em TEXT por portabilidade, entao devolver a string crua empurraria um
  `json.loads` para todo chamador;
- leitura QUEBRA quando o banco quebra, ao contrario da escrita. A escrita
  nunca derruba a resposta do visitante porque perder uma linha de trilha e
  melhor que perder o trabalho. Na leitura o custo inverte: devolver lista
  vazia por falha de banco mostra uma auditoria limpa que nao existe.
"""

import datetime

import pytest
from sqlalchemy import DateTime, bindparam, create_engine, text
from sqlalchemy.exc import OperationalError

from agent_ops.decisions import consultas, digerir, migracao, registrar


@pytest.fixture
def engine(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OPS_PROJETO", "triagem")
    eng = create_engine(f"sqlite:///{tmp_path}/t.db")
    migracao.aplicar(eng)
    return eng


def _grava(engine, **kwargs):
    base = {
        "tenant_id": "t1",
        "correlation_id": "run-1",
        "input_digest": digerir("curriculo"),
        "rule_code": "RUBRICA.PYTHON.SENIOR",
    }
    base.update(kwargs)
    return registrar(engine, **base)


def test_listar_devolve_so_o_tenant_pedido(engine):
    _grava(engine, tenant_id="t1")
    _grava(engine, tenant_id="t2")

    linhas = consultas.listar(engine, tenant_id="t1")

    assert [linha["tenant_id"] for linha in linhas] == ["t1"]


def test_listar_vem_do_mais_novo_para_o_mais_velho(engine):
    primeiro = _grava(engine, rule_code="R.PRIMEIRA")
    segundo = _grava(engine, rule_code="R.SEGUNDA")

    linhas = consultas.listar(engine, tenant_id="t1")

    assert [linha["id"] for linha in linhas] == [segundo, primeiro]


def test_listar_respeita_o_limite(engine):
    for _ in range(5):
        _grava(engine)

    assert len(consultas.listar(engine, tenant_id="t1", limite=2)) == 2


def test_listar_recusa_limite_nao_positivo(engine):
    with pytest.raises(ValueError):
        consultas.listar(engine, tenant_id="t1", limite=0)


def test_evidence_e_outcome_voltam_como_dicionario(engine):
    _grava(engine, evidence={"anos_python": 7}, outcome={"pontos": 30})

    linha = consultas.listar(engine, tenant_id="t1")[0]

    assert linha["evidence"] == {"anos_python": 7}
    assert linha["outcome"] == {"pontos": 30}


def test_por_execucao_reconstroi_a_execucao_em_ordem(engine):
    primeiro = _grava(engine, correlation_id="run-9", rule_code="R.A")
    segundo = _grava(engine, correlation_id="run-9", rule_code="R.B")
    _grava(engine, correlation_id="run-outra")

    linhas = consultas.por_execucao(engine, tenant_id="t1", correlation_id="run-9")

    assert [linha["id"] for linha in linhas] == [primeiro, segundo]


def test_correlation_id_de_outro_tenant_nao_vaza(engine):
    """O teste que justifica o `tenant_id` obrigatorio na assinatura.

    Dois tenants podem ter execucoes com o mesmo id se o app derivar esse id de
    algo previsivel, e mesmo sem isso um id vazado num log ou numa URL nao pode
    virar chave de leitura. Filtrar so por `correlation_id` entrega a trilha
    inteira de outro cliente: entrada digerida, regra aplicada, custo.
    """
    _grava(engine, tenant_id="vitima", correlation_id="run-compartilhado")

    linhas = consultas.por_execucao(
        engine, tenant_id="atacante", correlation_id="run-compartilhado"
    )

    assert linhas == []


def test_filhos_desce_do_orquestrador_para_os_workers(engine):
    raiz = _grava(engine, rule_code="R.ORQUESTRADOR")
    filho = _grava(engine, rule_code="R.WORKER", parent_id=raiz)
    _grava(engine, rule_code="R.SOLTO")

    linhas = consultas.filhos(engine, tenant_id="t1", parent_id=raiz)

    assert [linha["id"] for linha in linhas] == [filho]


def test_filhos_tambem_filtra_por_tenant(engine):
    raiz = _grava(engine, tenant_id="vitima", rule_code="R.ORQUESTRADOR")
    _grava(engine, tenant_id="vitima", rule_code="R.WORKER", parent_id=raiz)

    assert consultas.filhos(engine, tenant_id="atacante", parent_id=raiz) == []


def test_contagem_por_regra_agrupa_dentro_do_projeto(engine):
    _grava(engine, rule_code="R.A")
    _grava(engine, rule_code="R.A")
    _grava(engine, rule_code="R.B")

    assert consultas.contagem_por_regra(engine) == {"R.A": 2, "R.B": 1}


def test_contagem_por_regra_atravessa_tenants_de_proposito(engine):
    """Semear o golden set do eval olha o projeto inteiro, nao um cliente.

    E a unica consulta sem `tenant_id`, porque a pergunta e "qual regra sempre
    cai na rede de seguranca" e ela nao e sobre ninguem em particular. Nao
    devolve linha nenhuma, so contagem.
    """
    _grava(engine, tenant_id="t1", rule_code="R.A")
    _grava(engine, tenant_id="t2", rule_code="R.A")

    assert consultas.contagem_por_regra(engine) == {"R.A": 2}


def test_contagem_por_regra_respeita_a_janela(engine):
    _grava(engine, rule_code="R.VELHA")
    amanha = datetime.datetime.now(datetime.UTC).replace(
        tzinfo=None
    ) + datetime.timedelta(days=1)

    assert consultas.contagem_por_regra(engine, desde=amanha) == {}


def test_leitura_quebrada_levanta_em_vez_de_mentir(tmp_path):
    """Escrita engole, leitura nao. Ver o docstring do modulo."""
    # `OperationalError` e nao `Exception`: com `Exception` este teste ficaria
    # verde ate contra um modulo que nem tem a funcao, porque AttributeError
    # tambem e Exception.
    engine = create_engine(f"sqlite:///{tmp_path}/sem-schema.db")

    with pytest.raises(OperationalError):
        consultas.listar(engine, tenant_id="t1")


def test_tenant_id_e_keyword_only(engine):
    with pytest.raises(TypeError):
        consultas.listar(engine, "t1")


def test_linha_com_json_corrompido_nao_derruba_a_listagem(engine):
    """Uma linha ruim nao pode esconder a trilha inteira.

    `evidence` e `outcome` sao TEXT por portabilidade, entao nada no banco
    impede que uma versao antiga, outra app ou um INSERT a mao gravem algo que
    nao e JSON. Fazer a listagem explodir por causa de uma linha entrega uma
    auditoria que nao abre justamente quando alguem foi olhar.
    """
    _grava(engine, evidence={"ok": 1})
    with engine.begin() as conexao:
        conexao.execute(
            text("UPDATE decisions SET evidence = 'nao e json'")
        )

    linha = consultas.listar(engine, tenant_id="t1")[0]

    assert linha["evidence"] == {}
    assert linha["evidence_bruto"] == "nao e json", (
        "o valor cru tem que sobreviver, senao nao da para investigar a linha"
    )


# ---------------------------------------------------------------- retencao


def _envelhecer_trilha(engine, dias):
    velho = consultas_tempo() - datetime.timedelta(days=dias)
    with engine.begin() as conexao:
        conexao.execute(
            text("UPDATE decisions SET created_at = :c").bindparams(
                bindparam("c", type_=DateTime)
            ),
            {"c": velho},
        )


def consultas_tempo():
    from agent_ops.tempo import agora_utc

    return agora_utc()


def test_purgar_apaga_o_que_passou_da_janela(engine):
    _grava(engine)
    _envelhecer_trilha(engine, 400)
    _grava(engine)  # este e de hoje

    corte = consultas_tempo() - datetime.timedelta(days=365)
    apagadas = consultas.purgar(engine, antes_de=corte)

    assert apagadas == 1
    assert len(consultas.listar(engine, tenant_id="t1")) == 1


def test_purgar_nao_toca_no_que_esta_dentro_da_janela(engine):
    _grava(engine)

    corte = consultas_tempo() - datetime.timedelta(days=365)

    assert consultas.purgar(engine, antes_de=corte) == 0
    assert len(consultas.listar(engine, tenant_id="t1")) == 1


def test_purgar_pode_ser_escopado_a_um_tenant(engine):
    """O caminho de exclusao a pedido do titular, que nao e o de retencao."""
    _grava(engine, tenant_id="quer_sumir")
    _grava(engine, tenant_id="fica")
    _envelhecer_trilha(engine, 400)

    corte = consultas_tempo() - datetime.timedelta(days=365)
    apagadas = consultas.purgar(engine, antes_de=corte, tenant_id="quer_sumir")

    assert apagadas == 1
    assert len(consultas.listar(engine, tenant_id="fica")) == 1


def test_purgar_apaga_em_lotes(engine):
    """Um DELETE sem teto numa tabela que so cresce e o incidente que a
    retencao existe para evitar, nao um jeito de evita-lo."""
    for _ in range(5):
        _grava(engine)
    _envelhecer_trilha(engine, 400)

    corte = consultas_tempo() - datetime.timedelta(days=365)

    assert consultas.purgar(engine, antes_de=corte, limite=2) == 2
    assert consultas.purgar(engine, antes_de=corte, limite=2) == 2
    assert consultas.purgar(engine, antes_de=corte, limite=2) == 1
    assert consultas.purgar(engine, antes_de=corte, limite=2) == 0


def test_purgar_recusa_limite_nao_positivo(engine):
    with pytest.raises(ValueError, match="limite"):
        consultas.purgar(engine, antes_de=consultas_tempo(), limite=0)


def test_purgar_exige_antes_de_por_nome(engine):
    with pytest.raises(TypeError):
        consultas.purgar(engine, consultas_tempo())
