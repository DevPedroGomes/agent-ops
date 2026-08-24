"""Configuracao lida do ambiente, com cache.

Prefixo `AGENT_OPS_` para nao colidir com as envs da app hospedeira: o BrainHub
ja tem `REDIS_URL` propria e as duas podem apontar para instancias diferentes.

`extra="ignore"` porque a app hospedeira tem dezenas de envs que nao sao nossas.

O cache vale para o que nao muda com o servico no ar. O kill switch NAO e um
desses: ele tem funcao propria, `kill_switch_ligado()`, que le a env a cada
chamada — ver o docstring dela.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def exigir_sem_dois_pontos(valor: str, campo: str) -> str:
    """Recusa `:` num pedaco de chave. Devolve o proprio valor, para encadear.

    `:` e o separador das chaves do Redis e dos job ids deste pacote, e as
    juntas nao escapam nada. Com um `:` dentro de um campo, dois pares
    diferentes viram a MESMA chave — `projeto="a:b"` + `digest="c"` colide com
    `projeto="a"` + `tenant="b"` + `digest="c"`. Colisao de chave aqui nao da
    erro: ela silenciosamente mistura o teto de dois projetos ou entrega a um
    tenant o job do outro. Como o id ganhou um terceiro campo (`tenant`), a
    ambiguidade deixou de ser teorica.

    `ValueError` porque e erro de configuracao/programacao: aparece no boot ou
    no primeiro teste, nunca no meio de um pico de trafego.
    """
    if ":" in valor:
        raise ValueError(
            f"{campo} nao pode conter ':' (recebeu {valor!r}): e o separador "
            "das chaves do Redis e dos job ids, e um ':' aqui faz duas chaves "
            "diferentes virarem a mesma"
        )
    return valor


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

    # Estanca gasto sem rebuild de imagem e sem deploy de codigo: basta a env
    # chegar ao processo. Guardado aqui para valer de default quando ela nao
    # esta definida, mas quem MANDA e `kill_switch_ligado()` — este campo
    # congela na primeira leitura, e um freio de emergencia congelado nao freia.
    kill_switch: bool = False

    @field_validator("projeto")
    @classmethod
    def _projeto_sem_dois_pontos(cls, valor: str) -> str:
        return exigir_sem_dois_pontos(valor, "AGENT_OPS_PROJETO")


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
