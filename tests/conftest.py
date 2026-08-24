"""Isolamento da configuracao entre testes.

`get_config` e `lru_cache`, e o `monkeypatch` restaura a env — nao o cache. Sem
este autouse, um teste que liga o kill switch deixa `kill_switch=True` gravado
no processo para todos os que rodarem depois, e a suite so passa porque cada
teste que precisa de config fresca lembra de chamar `cache_clear()` a mao. Isso
e disciplina, nao estrutura.

O caso perigoso e preciso: um teste NOVO de metering que espera `TetoAtingido`
PASSA com o switch preso ligado, porque `consumir` recusa antes de chegar no
codigo sob teste. A asserção fica verde sem ter provado nada.

Limpa antes E depois: antes protege este teste do anterior, depois protege o
proximo deste (inclusive quando este falha no meio).
"""

import pytest

from agent_ops.config import get_config


@pytest.fixture(autouse=True)
def config_isolada():
    get_config.cache_clear()
    yield
    get_config.cache_clear()
