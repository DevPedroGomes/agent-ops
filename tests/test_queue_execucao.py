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

import logging

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


def test_descartar_preserva_o_percentual_ja_alcancado(engine):
    # Um job morto aos 80% mostrando 0% joga fora o unico fato util sobre ele
    # na UI de operacao: quanto trabalho ja tinha sido feito.
    execucao.marcar(engine, "j1", estado="rodando", percentual=80)
    execucao.descartar(engine, "j1", motivo="provider fora do ar")

    p = execucao.ler(engine, "j1")
    assert p["estado"] == "descartado"
    assert p["percentual"] == 80


def test_mover_a_barra_nao_apaga_o_detalhe(engine):
    # `detalhe` e a frase que a UI mostra. Um tick de progresso que a apaga
    # deixa a tela sem explicacao entre uma mensagem e a proxima.
    execucao.marcar(
        engine, "j1", estado="rodando", percentual=10, detalhe="extraindo texto"
    )
    execucao.marcar(engine, "j1", estado="rodando", percentual=40)

    assert execucao.ler(engine, "j1")["detalhe"] == "extraindo texto"


def test_percentual_zero_continua_sendo_gravado(engine):
    # So a OMISSAO preserva. Zero e um valor legitimo — um retry recomeca a
    # barra — e precisa continuar sobrescrevendo.
    execucao.marcar(engine, "j1", estado="rodando", percentual=90)
    execucao.marcar(engine, "j1", estado="rodando", percentual=0)

    assert execucao.ler(engine, "j1")["percentual"] == 0


def test_primeira_marcacao_sem_percentual_grava_zero(engine):
    execucao.marcar(engine, "j1", estado="pendente")

    assert execucao.ler(engine, "j1")["percentual"] == 0


def test_ler_devolve_quando_o_job_foi_atualizado(engine):
    # Sem `atualizado`, "rodando" ha dez segundos e "rodando" desde que o
    # worker levou SIGKILL uma hora atras sao indistinguiveis para quem le.
    execucao.marcar(engine, "j1", estado="rodando", percentual=10)

    p = execucao.ler(engine, "j1")
    assert p["atualizado"] is not None


def test_max_tentativas_e_o_default_do_esgotou():
    # O default de `esgotou` e o `max_tries` do worker PRECISAM ser o mesmo
    # numero. No `Worker.run_job` do arq, quando `job_try > max_tries` o job e
    # encerrado com JobExecutionFailed SEM chamar a funcao: com
    # `WorkerSettings.max_tries = 3` e `esgotou` valendo 5, `esgotou` nunca fica
    # True, `descartar` nunca roda e o job sai da UI de operacao sem virar
    # dead-letter — que e a unica razao de `descartado` existir.
    assert execucao.esgotou({"job_try": execucao.MAX_TENTATIVAS}) is True
    assert execucao.esgotou({"job_try": execucao.MAX_TENTATIVAS - 1}) is False


def test_max_tentativas_e_publico_no_subpacote():
    from agent_ops import queue

    assert queue.MAX_TENTATIVAS == execucao.MAX_TENTATIVAS


def test_falhou_registra_o_motivo_sem_perder_o_progresso(engine):
    # `falhou` existia em ESTADOS e ninguem escrevia: toda excecao fora da
    # prevista deixava a linha em `rodando` para sempre, indistinguivel de um
    # job lento.
    execucao.marcar(engine, "j1", estado="rodando", percentual=30)
    execucao.marcar(
        engine, "j1", estado="falhou", detalhe="ValueError: PDF corrompido"
    )

    p = execucao.ler(engine, "j1")
    assert p["estado"] == "falhou"
    assert p["percentual"] == 30
    assert "PDF corrompido" in p["detalhe"]


def test_marcar_sem_schema_deixa_o_tipo_do_erro_no_log(caplog, tmp_path):
    # Esquecer `aplicar_schema` produz um sistema de progresso que nao reporta
    # nada, para sempre, sem levantar erro: `marcar` engole (contrato "nunca
    # derruba o job") e `ler` devolve None, indistinguivel de "nunca comecou".
    # O log e a UNICA pista, e sem o tipo do erro nele "tabela nao existe"
    # (permanente) e "conexao caiu" (transitorio) sao a mesma linha.
    eng = create_engine(f"sqlite:///{tmp_path}/sem-schema.db")

    with caplog.at_level(logging.ERROR):
        execucao.marcar(eng, "j1", estado="rodando")

    assert execucao.ler(eng, "j1") is None
    # Na MENSAGEM, nao so no traceback: quem opera le a linha formatada, e
    # varios formatadores de producao nao imprimem o traceback anexado.
    mensagens = [registro.getMessage() for registro in caplog.records]
    assert any("OperationalError" in m for m in mensagens), mensagens
