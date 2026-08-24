# tests/test_queue_execucao.py
"""Testes do progresso, do retry e da dead-letter.

O que se prende aqui:
- o progresso sobrevive ao processo. Em memoria, um redeploy no meio da
  ingestao apaga a barra e o cliente nao sabe se o job morreu;
- marcar progresso NUNCA derruba o job. Vale o mesmo contrato da trilha:
  perder a barra e ruim, perder o trabalho ja feito e pior;
- o backoff cresce, senao cinco tentativas contra um provider fora do ar
  acontecem no mesmo segundo e nao sao cinco chances, sao uma;
- esgotar as tentativas leva a `descartado` com motivo legivel, nao ao silencio.
"""

import pytest
from arq.worker import Retry
from sqlalchemy import create_engine

from agent_ops.queue import execucao


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/t.db")
    execucao.aplicar_schema(eng)
    return eng


def test_marcar_e_ler(engine):
    execucao.marcar(engine, "j1", estado="rodando", percentual=40)
    p = execucao.ler(engine, "j1")

    assert p["estado"] == "rodando"
    assert p["percentual"] == 40


def test_marcar_duas_vezes_atualiza_em_vez_de_duplicar(engine):
    execucao.marcar(engine, "j1", estado="rodando", percentual=10)
    execucao.marcar(engine, "j1", estado="rodando", percentual=90)

    assert execucao.ler(engine, "j1")["percentual"] == 90


def test_mover_a_barra_nao_conta_como_nova_tentativa(engine):
    # `tentativas` conta RETENTATIVAS do job. `marcar` e chamado varias vezes
    # dentro de uma mesma tentativa so para mover a barra; se cada chamada
    # incrementasse, a UI de operacao mostraria "12 tentativas" para um job que
    # rodou uma vez so.
    execucao.marcar(engine, "j1", estado="rodando", percentual=10, tentativas=1)
    execucao.marcar(engine, "j1", estado="rodando", percentual=50)
    execucao.marcar(engine, "j1", estado="rodando", percentual=90)

    assert execucao.ler(engine, "j1")["tentativas"] == 1

    execucao.marcar(engine, "j1", estado="rodando", percentual=0, tentativas=2)
    assert execucao.ler(engine, "j1")["tentativas"] == 2


def test_ler_job_desconhecido_devolve_none(engine):
    assert execucao.ler(engine, "nao-existe") is None


def test_estado_invalido_e_recusado(engine):
    with pytest.raises(ValueError):
        execucao.marcar(engine, "j1", estado="quase-la")


def test_marcar_nao_derruba_o_job_quando_o_banco_cai():
    class EngineQuebrado:
        def begin(self):
            raise RuntimeError("banco fora do ar")

    execucao.marcar(EngineQuebrado(), "j1", estado="rodando")  # nao levanta


def test_backoff_cresce_com_a_tentativa():
    assert execucao.backoff(1) < execucao.backoff(2) < execucao.backoff(3)


def test_backoff_tem_teto():
    assert execucao.backoff(50) == execucao.backoff(99)


def test_tentar_de_novo_levanta_retry_do_arq():
    with pytest.raises(Retry):
        execucao.tentar_de_novo({"job_try": 2})


def test_esgotou_so_na_ultima_tentativa():
    assert execucao.esgotou({"job_try": 4}, max_tries=5) is False
    assert execucao.esgotou({"job_try": 5}, max_tries=5) is True


def test_descartar_registra_o_motivo(engine):
    execucao.descartar(engine, "j1", motivo="provider fora do ar apos 5 tentativas")
    p = execucao.ler(engine, "j1")

    assert p["estado"] == "descartado"
    assert "5 tentativas" in p["detalhe"]
