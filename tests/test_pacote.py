"""O pacote importa e a configuracao le do ambiente.

Prende aqui: importar `agent_ops` nao pode abrir conexao nenhuma. Se um dia
alguem criar o cliente Redis no topo de um modulo, este teste quebra — e e
para quebrar, porque import com efeito colateral transforma `pytest --collect`
e o `--help` da app em chamada de rede.
"""

import pytest
from pydantic import ValidationError

import agent_ops
from agent_ops.config import Config, get_config


def test_versao_exposta():
    assert agent_ops.__version__ == "0.2.0"


def test_a_versao_tem_fonte_unica():
    # Ate a v0.1.1 o numero estava escrito em dois lugares e eles divergiram: a
    # tag dizia 0.1.1 e o metadado dizia 0.1.0. Este teste falha se alguem
    # reintroduzir o literal no __init__.
    import pathlib

    fonte = (
        pathlib.Path(agent_ops.__file__).read_text(encoding="utf-8")
    )
    assert '__version__ = "' not in fonte, (
        "versao voltou a ser literal no __init__; a fonte e o pyproject"
    )


def test_config_le_do_ambiente(monkeypatch):
    monkeypatch.setenv("AGENT_OPS_REDIS_URL", "redis://exemplo:6379")
    monkeypatch.setenv("AGENT_OPS_PROJETO", "brainhub")

    cfg = get_config()

    assert isinstance(cfg, Config)
    assert cfg.redis_url == "redis://exemplo:6379"
    assert cfg.projeto == "brainhub"
    assert cfg.kill_switch is False


def test_kill_switch_desliga_por_ambiente(monkeypatch):
    monkeypatch.setenv("AGENT_OPS_KILL_SWITCH", "true")

    assert get_config().kill_switch is True


def test_kill_switch_ligado_aqui_nao_pode_vazar_para_o_proximo(monkeypatch):
    # Este teste liga o switch DE PROPOSITO e nao limpa o cache no fim. Junto
    # com o de baixo, prende o autouse do conftest.
    monkeypatch.setenv("AGENT_OPS_KILL_SWITCH", "true")

    assert get_config().kill_switch is True


def test_o_kill_switch_do_teste_anterior_nao_vazou():
    # Sem o autouse do conftest, o lru_cache de get_config atravessaria a
    # fronteira entre os dois testes e este leria True.
    assert get_config().kill_switch is False


def test_projeto_com_dois_pontos_e_recusado(monkeypatch):
    # `:` e o separador das chaves e dos job ids, e as juntas nao escapam nada:
    # projeto="a:b" + digest="c" produz o mesmo id que projeto="a" +
    # tenant="b" + digest="c". Colisao de chave aqui nao levanta erro nenhum —
    # ela mistura o teto de dois projetos e entrega a um tenant o job do outro.
    monkeypatch.setenv("AGENT_OPS_PROJETO", "a:b")

    with pytest.raises(ValidationError):
        get_config()


def test_projeto_sem_dois_pontos_continua_valendo(monkeypatch):
    monkeypatch.setenv("AGENT_OPS_PROJETO", "brainhub")

    assert get_config().projeto == "brainhub"
