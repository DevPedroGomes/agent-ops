"""Configuracao lida do ambiente, com cache.

Prefixo `AGENT_OPS_` para nao colidir com as envs da app hospedeira: o BrainHub
ja tem `REDIS_URL` propria e as duas podem apontar para instancias diferentes.

`extra="ignore"` porque a app hospedeira tem dezenas de envs que nao sao nossas.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_OPS_", extra="ignore")

    redis_url: str = "redis://localhost:6379"

    # Namespaceia toda chave no Redis. Sem isso, dois projetos do portfolio
    # dividindo o mesmo Redis dividiriam tambem o teto diario um do outro.
    projeto: str = "default"

    # Teto de profundidade da fila. Acima disso a API recusa com 429 em vez de
    # aceitar trabalho que nao va conseguir fazer — uma fila que so cresce e
    # indistinguivel de um servico fora do ar, mas mente para o cliente.
    profundidade_maxima: int = 500

    # Estanca gasto sem rebuild nem redeploy. Guardado aqui para o `panorama`
    # ter um default quando a env nao esta definida, mas quem MANDA e
    # `kill_switch_ligado()`: este campo congela na primeira leitura.
    kill_switch: bool = False


@lru_cache(maxsize=1)
def get_config() -> Config:
    return Config()


# Os mesmos textos que o pydantic aceita como verdadeiro, para ligar o switch
# com "1", "true" ou "on" dar o mesmo resultado por aqui e por `Config`.
_VERDADEIROS = frozenset({"1", "true", "t", "yes", "y", "on"})


def kill_switch_ligado() -> bool:
    """Le o kill switch AGORA, sem passar pelo cache de `get_config`.

    `get_config` e `lru_cache(maxsize=1)`: a env e lida uma vez por processo e
    congela. Isso esta certo para `redis_url` e `projeto`, que nao mudam com o
    servico em pe — e errado exatamente para o freio de emergencia. Um operador
    ligando `AGENT_OPS_KILL_SWITCH` no container em pe via o gasto continuar,
    enquanto o comentario do campo e o README prometiam "sem redeploy".

    Um `os.getenv` por chamada nao custa nada perto do round trip ao Redis que
    vem logo depois, entao nao ha motivo para cachear.

    Sem a env definida, cai no valor da `Config` (default `False`).
    """
    bruto = os.getenv("AGENT_OPS_KILL_SWITCH")
    if bruto is None:
        return get_config().kill_switch
    return bruto.strip().lower() in _VERDADEIROS
