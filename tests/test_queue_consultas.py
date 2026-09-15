"""Testes da UI de operacao: varrer a dead-letter e achar job travado.

O que se prende aqui:
- `descartado` vira lista. O indice `idx_job_progress_estado` nasceu junto com
  a tabela exatamente para isso e ficou sem nenhum consumidor: `ler` responde
  por um job de cada vez, e quem opera precisa da pergunta inversa, "quais
  morreram?";
- job travado tem definicao operacional: `rodando` com `atualizado` velho. E o
  worker que levou SIGKILL no meio. Sem essa consulta ele fica `rodando` para
  sempre e ninguem ve;
- a janela e calculada em UTC contra o carimbo gravado em UTC. Era aqui que o
  carimbo no fuso da sessao do Postgres fazia o estrago: o corte errava pelo
  offset inteiro e a lista vinha vazia (ou vinha inteira);
- estas consultas LEVANTAM quando o banco quebra, ao contrario de `ler`. Uma
  dead-letter vazia por falha de banco diz "nao ha nada morto", que e a
  resposta errada mais cara que essa tela pode dar.
"""

import datetime

import pytest
from sqlalchemy import DateTime, bindparam, create_engine, text
from sqlalchemy.exc import OperationalError

from agent_ops.queue import execucao


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/t.db")
    execucao.aplicar_schema(eng)
    return eng


def _envelhecer(engine, job_id, segundos):
    """Empurra `atualizado` para tras. Nao da para esperar de verdade no teste."""
    velho = execucao.agora_utc() - datetime.timedelta(seconds=segundos)
    with engine.begin() as conexao:
        conexao.execute(
            text(
                "UPDATE job_progress SET atualizado = :a WHERE job_id = :j"
            ).bindparams(bindparam("a", type_=DateTime)),
            {"a": velho, "j": job_id},
        )


def test_listar_por_estado_traz_a_dead_letter(engine):
    execucao.marcar(engine, "vivo", estado="rodando")
    execucao.descartar(engine, "morto", motivo="provider fora do ar")

    linhas = execucao.listar_por_estado(engine, "descartado")

    assert [linha["job_id"] for linha in linhas] == ["morto"]
    assert linhas[0]["detalhe"] == "provider fora do ar"


def test_listar_por_estado_vem_do_mais_recente(engine):
    execucao.descartar(engine, "antigo", motivo="x")
    execucao.descartar(engine, "recente", motivo="y")
    _envelhecer(engine, "antigo", 600)

    linhas = execucao.listar_por_estado(engine, "descartado")

    assert [linha["job_id"] for linha in linhas] == ["recente", "antigo"]


def test_listar_por_estado_respeita_o_limite(engine):
    for i in range(5):
        execucao.descartar(engine, f"j{i}", motivo="x")

    assert len(execucao.listar_por_estado(engine, "descartado", limite=2)) == 2


def test_listar_por_estado_recusa_estado_invalido(engine):
    with pytest.raises(ValueError):
        execucao.listar_por_estado(engine, "morreu")


def test_listar_por_estado_recusa_limite_nao_positivo(engine):
    with pytest.raises(ValueError):
        execucao.listar_por_estado(engine, "descartado", limite=0)


def test_travados_acha_o_job_cujo_worker_morreu(engine):
    execucao.marcar(engine, "recente", estado="rodando", percentual=10)
    execucao.marcar(engine, "abandonado", estado="rodando", percentual=40)
    _envelhecer(engine, "abandonado", 3600)

    linhas = execucao.travados(engine, mais_velho_que_segundos=1800)

    assert [linha["job_id"] for linha in linhas] == ["abandonado"]
    assert linhas[0]["percentual"] == 40


def test_travados_ignora_quem_ja_terminou(engine):
    execucao.marcar(engine, "pronto", estado="concluido", percentual=100)
    _envelhecer(engine, "pronto", 86_400)

    assert execucao.travados(engine, mais_velho_que_segundos=60) == []


def test_travados_enxerga_o_estado_que_se_pedir(engine):
    """Um job preso em `pendente` tambem e um job que ninguem vai buscar."""
    execucao.marcar(engine, "esquecido", estado="pendente")
    _envelhecer(engine, "esquecido", 7200)

    linhas = execucao.travados(
        engine, mais_velho_que_segundos=3600, estado="pendente"
    )

    assert [linha["job_id"] for linha in linhas] == ["esquecido"]


def test_travados_recusa_janela_nao_positiva(engine):
    """Janela zero devolveria todo job rodando, inclusive o que comecou agora."""
    with pytest.raises(ValueError, match="mais_velho_que_segundos"):
        execucao.travados(engine, mais_velho_que_segundos=0)


def test_consultas_de_operacao_levantam_em_vez_de_mentir(tmp_path):
    # `OperationalError` e nao `Exception`: com `Exception` este teste passaria
    # verde contra um modulo que nem tem as funcoes ainda, porque AttributeError
    # tambem e Exception.
    engine = create_engine(f"sqlite:///{tmp_path}/sem-schema.db")

    with pytest.raises(OperationalError):
        execucao.listar_por_estado(engine, "descartado")

    with pytest.raises(OperationalError):
        execucao.travados(engine, mais_velho_que_segundos=60)


def test_atualizado_das_listagens_tambem_e_datetime(engine):
    execucao.descartar(engine, "morto", motivo="x")

    linha = execucao.listar_por_estado(engine, "descartado")[0]

    assert isinstance(linha["atualizado"], datetime.datetime)


# ---------------------------------------------------------------- retencao


def test_purgar_apaga_job_terminal_fora_da_janela(engine):
    execucao.marcar(engine, "velho", estado="concluido", percentual=100)
    _envelhecer(engine, "velho", 86_400 * 40)
    execucao.marcar(engine, "novo", estado="concluido", percentual=100)

    corte = execucao.agora_utc() - datetime.timedelta(days=30)
    apagados = execucao.purgar(engine, antes_de=corte)

    assert apagados == 1
    assert execucao.ler(engine, "velho") is None
    assert execucao.ler(engine, "novo") is not None


def test_purgar_nunca_apaga_job_que_ainda_esta_rodando(engine):
    """O teste que define o default.

    Um job `rodando` com `atualizado` velho e exatamente o job travado que a
    UI de operacao precisa enxergar. Apagar a linha dele durante a faxina
    resolve o sintoma apagando a evidencia, e o trabalho continua perdido, so
    que agora sem rastro nenhum.
    """
    execucao.marcar(engine, "travado", estado="rodando", percentual=40)
    _envelhecer(engine, "travado", 86_400 * 90)

    corte = execucao.agora_utc() - datetime.timedelta(days=30)

    assert execucao.purgar(engine, antes_de=corte) == 0
    assert execucao.ler(engine, "travado") is not None


def test_purgar_tambem_leva_a_dead_letter(engine):
    execucao.descartar(engine, "morto", motivo="provider fora do ar")
    _envelhecer(engine, "morto", 86_400 * 90)

    corte = execucao.agora_utc() - datetime.timedelta(days=30)

    assert execucao.purgar(engine, antes_de=corte) == 1


def test_purgar_aceita_estados_explicitos(engine):
    """Quem quiser incluir `rodando` tem que pedir, e ai e decisao de quem pede."""
    execucao.marcar(engine, "travado", estado="rodando")
    _envelhecer(engine, "travado", 86_400 * 90)

    corte = execucao.agora_utc() - datetime.timedelta(days=30)
    apagados = execucao.purgar(engine, antes_de=corte, estados=["rodando"])

    assert apagados == 1


def test_purgar_recusa_estado_invalido(engine):
    with pytest.raises(ValueError, match="estado invalido"):
        execucao.purgar(
            engine, antes_de=execucao.agora_utc(), estados=["morreu"]
        )


def test_purgar_apaga_em_lotes(engine):
    for i in range(5):
        execucao.marcar(engine, f"j{i}", estado="concluido")
        _envelhecer(engine, f"j{i}", 86_400 * 90)

    corte = execucao.agora_utc() - datetime.timedelta(days=30)

    assert execucao.purgar(engine, antes_de=corte, limite=2) == 2
    assert execucao.purgar(engine, antes_de=corte, limite=2) == 2
    assert execucao.purgar(engine, antes_de=corte, limite=2) == 1
    assert execucao.purgar(engine, antes_de=corte, limite=2) == 0


def test_purgar_recusa_limite_nao_positivo(engine):
    with pytest.raises(ValueError, match="limite"):
        execucao.purgar(engine, antes_de=execucao.agora_utc(), limite=0)
