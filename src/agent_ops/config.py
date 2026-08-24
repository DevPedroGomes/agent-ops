"""Configuracao lida do ambiente, com cache.

Prefixo `AGENT_OPS_` para nao colidir com as envs da app hospedeira: o BrainHub
ja tem `REDIS_URL` propria e as duas podem apontar para instancias diferentes.

`extra="ignore"` porque a app hospedeira tem dezenas de envs que nao sao nossas.
"""

from __future__ import annotations

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

    # Estanca gasto sem rebuild nem redeploy.
    kill_switch: bool = False


@lru_cache(maxsize=1)
def get_config() -> Config:
    return Config()
