"""O pacote importa e a configuracao le do ambiente.

Prende aqui: importar `agent_ops` nao pode abrir conexao nenhuma. Se um dia
alguem criar o cliente Redis no topo de um modulo, este teste quebra — e e
para quebrar, porque import com efeito colateral transforma `pytest --collect`
e o `--help` da app em chamada de rede.
"""

import agent_ops
from agent_ops.config import Config, get_config


def test_versao_exposta():
    assert agent_ops.__version__ == "0.1.0"


def test_config_le_do_ambiente(monkeypatch):
    monkeypatch.setenv("AGENT_OPS_REDIS_URL", "redis://exemplo:6379")
    monkeypatch.setenv("AGENT_OPS_PROJETO", "brainhub")
    get_config.cache_clear()

    cfg = get_config()

    assert isinstance(cfg, Config)
    assert cfg.redis_url == "redis://exemplo:6379"
    assert cfg.projeto == "brainhub"
    assert cfg.kill_switch is False


def test_kill_switch_desliga_por_ambiente(monkeypatch):
    monkeypatch.setenv("AGENT_OPS_KILL_SWITCH", "true")
    get_config.cache_clear()

    assert get_config().kill_switch is True
