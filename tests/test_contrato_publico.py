"""Prende a superficie publica do pacote.

Por que existe: a fase 2 troca a linha de import do BrainHub para apontar aqui.
Se um rename silencioso mudar `consumir` para `consume`, o BrainHub quebra no
deploy, nao no CI. Este teste faz a quebra acontecer aqui.

Prende tambem o isolamento entre subpacotes: importar a fila nao pode arrastar
SQLAlchemy nem Postgres junto, senao um worker que so enfileira carrega meio
ORM sem precisar.
"""

import importlib
import sys


def test_metering_expoe_os_nomes_que_o_brainhub_usa():
    import agent_ops.metering as m

    for nome in (
        "consumir", "devolver", "panorama", "TetoAtingido", "TetoIndisponivel",
    ):
        assert hasattr(m, nome), f"faltou {nome} na superficie publica"


def test_decisions_expoe_registrar_e_digerir():
    import agent_ops.decisions as d

    assert hasattr(d, "registrar")
    assert hasattr(d, "digerir")


def test_queue_expoe_enfileirar_e_progresso():
    import agent_ops.queue as q

    for nome in (
        "enfileirar", "job_id_de", "FilaCheia", "FilaIndisponivel",
        "marcar", "ler", "descartar",
    ):
        assert hasattr(q, nome), f"faltou {nome} na superficie publica"


def test_metering_nao_arrasta_sqlalchemy():
    originais = {
        modulo: sys.modules[modulo]
        for modulo in list(sys.modules)
        if modulo.startswith(("agent_ops", "sqlalchemy"))
    }
    for modulo in originais:
        del sys.modules[modulo]

    try:
        importlib.import_module("agent_ops.metering")

        assert "sqlalchemy" not in sys.modules, (
            "metering importou SQLAlchemy; os subpacotes devem ser independentes"
        )
    finally:
        # Sem restaurar o cache original, o SQLAlchemy fica com classes
        # duplicadas em memoria (a QueuePool recem-importada nao e a mesma
        # que o event registry conhece) e todo teste de decisions/queue que
        # roda depois deste na mesma sessao do pytest quebra com
        # "No such event 'connect' for target". Restaurar aqui devolve o
        # processo ao estado anterior ao teste, entao a ordem dos arquivos
        # de teste deixa de importar.
        for modulo in list(sys.modules):
            if modulo.startswith(("agent_ops", "sqlalchemy")) and modulo not in originais:
                del sys.modules[modulo]
        sys.modules.update(originais)
