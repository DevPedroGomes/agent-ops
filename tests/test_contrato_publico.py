"""Prende a superficie publica do pacote.

Por que existe: a fase 2 troca a linha de import do BrainHub para apontar aqui.
Se um rename silencioso mudar `consumir` para `consume`, o BrainHub quebra no
deploy, nao no CI. Este teste faz a quebra acontecer aqui.

Prende tambem o custo de import de CADA subpacote — o real, nao o desejado.
`metering` e livre de SQLAlchemy e de arq. `queue` carrega SQLAlchemy porque a
metade de progresso duravel dele e uma tabela no Postgres. Prender so a
primeira metade deixava o docstring da raiz afirmar uma propriedade que nenhum
teste checava (e que era falsa para dois dos tres subpacotes). Se alguem um dia
tornar o `execucao` preguicoso, e o teste do `queue` que registra a mudanca.
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
        "marcar", "ler", "descartar", "MAX_TENTATIVAS",
    ):
        assert hasattr(q, nome), f"faltou {nome} na superficie publica"


def _importa_isolado(subpacote: str) -> set[str]:
    """Importa `subpacote` do zero e devolve os pacotes de topo carregados.

    Existe para os dois testes abaixo poderem medir a mesma coisa sem repetir a
    danca de salvar e restaurar `sys.modules` — ver o comentario no `finally`.
    """
    prefixos = ("agent_ops", "sqlalchemy", "arq")
    originais = {
        modulo: sys.modules[modulo]
        for modulo in list(sys.modules)
        if modulo.startswith(prefixos)
    }
    for modulo in originais:
        del sys.modules[modulo]

    try:
        importlib.import_module(subpacote)
        return {m for m in ("sqlalchemy", "arq") if m in sys.modules}
    finally:
        # Sem restaurar o cache original, o SQLAlchemy fica com classes
        # duplicadas em memoria (a QueuePool recem-importada nao e a mesma
        # que o event registry conhece) e todo teste de decisions/queue que
        # roda depois deste na mesma sessao do pytest quebra com
        # "No such event 'connect' for target". Restaurar aqui devolve o
        # processo ao estado anterior ao teste, entao a ordem dos arquivos
        # de teste deixa de importar.
        for modulo in list(sys.modules):
            if modulo.startswith(prefixos) and modulo not in originais:
                del sys.modules[modulo]
        sys.modules.update(originais)


def test_metering_nao_arrasta_sqlalchemy_nem_arq():
    assert _importa_isolado("agent_ops.metering") == set(), (
        "metering deve depender so do Redis; SQLAlchemy e arq ficam de fora"
    )


def test_queue_carrega_sqlalchemy_e_isso_e_esperado():
    # Assercao simetrica, e ela prende o que E, nao o que se gostaria que
    # fosse: `queue/__init__.py` importa `execucao`, e progresso duravel e uma
    # tabela no Postgres. Se um dia o carregamento virar preguicoso (PEP 562),
    # este teste falha e obriga a atualizar o docstring da raiz junto.
    assert "sqlalchemy" in _importa_isolado("agent_ops.queue"), (
        "queue deixou de carregar SQLAlchemy; atualize o docstring de "
        "agent_ops/__init__.py, que descreve esse custo de import"
    )
