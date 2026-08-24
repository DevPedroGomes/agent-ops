"""Testes do teto de gasto.

O que se prende aqui:
- a cota e consumida ANTES da chamada paga, nunca depois. Contar depois deixa
  uma rajada concorrente passar inteira, porque nenhuma foi contada ainda;
- quem estoura o teto DESFAZ o proprio incremento, senao uma recusa cobraria
  cota do proximo visitante;
- Redis ilegivel RECUSA. Um teto ilegivel nao e um teto ausente;
- o escopo entra na chave, para teto por IP conviver com teto global sem que
  um consuma o do outro;
- o projeto namespaceia tudo, senao dois apps no mesmo Redis dividem o teto.
"""

import asyncio

import pytest

from agent_ops import metering
from agent_ops.config import get_config


class FakeRedis:
    """Dublê minimo: so o que `cotas.py` usa."""

    def __init__(self, explode=False, falha_no_expire=False):
        self.valores: dict[str, int] = {}
        self.expiracoes: dict[str, int] = {}
        self.explode = explode
        # Falha SO no expire, com o incrby passando. Sem esse controle
        # separado nao da para expressar a falha de rede entre as duas idas ao
        # Redis — que e justamente onde a invariante quebrava.
        self.falha_no_expire = falha_no_expire

    async def incrby(self, chave, quanto):
        if self.explode:
            raise ConnectionError("redis fora do ar")
        self.valores[chave] = self.valores.get(chave, 0) + quanto
        return self.valores[chave]

    async def expire(self, chave, segundos):
        if self.explode or self.falha_no_expire:
            raise ConnectionError("redis fora do ar")
        self.expiracoes[chave] = segundos

    async def get(self, chave):
        if self.explode:
            raise ConnectionError("redis fora do ar")
        return self.valores.get(chave)


@pytest.fixture
def redis_falso(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setenv("AGENT_OPS_PROJETO", "testes")
    monkeypatch.setenv("AGENT_OPS_KILL_SWITCH", "false")
    get_config.cache_clear()

    async def _fake():
        return fake

    monkeypatch.setattr(metering.cotas, "_redis", _fake)
    return fake


def test_chave_carrega_projeto_e_dia(redis_falso):
    chave = metering.cotas._chave("chat", dia="2026-08-24")
    assert chave == "ao:budget:testes:2026-08-24:chat"


def test_escopo_entra_na_chave(redis_falso):
    chave = metering.cotas._chave("chat", escopo="ip:abc", dia="2026-08-24")
    assert chave == "ao:budget:testes:2026-08-24:chat:ip:abc"


def test_consumir_devolve_o_restante(redis_falso):
    restante = asyncio.run(metering.consumir("chat", limite=10))
    assert restante == 9


def test_consumir_marca_expiracao_so_na_primeira(redis_falso):
    asyncio.run(metering.consumir("chat", limite=10))
    assert len(redis_falso.expiracoes) == 1
    asyncio.run(metering.consumir("chat", limite=10))
    assert len(redis_falso.expiracoes) == 1


def test_estourar_o_teto_recusa_e_desfaz_o_proprio_incremento(redis_falso):
    asyncio.run(metering.consumir("chat", limite=1))
    chave = metering.cotas._chave("chat")

    with pytest.raises(metering.TetoAtingido):
        asyncio.run(metering.consumir("chat", limite=1))

    # O incremento da chamada recusada foi desfeito: sobrou so o da que passou.
    assert redis_falso.valores[chave] == 1


def test_falha_no_ttl_nao_recusa_a_chamada(monkeypatch):
    # O TTL e faxina, nao corretude. Recusar por causa dele seria ruim; recusar
    # SEM devolver o incremento — que era o comportamento herdado do budget.py
    # original — cobra cota de uma chamada que nunca aconteceu.
    monkeypatch.setenv("AGENT_OPS_PROJETO", "testes")
    monkeypatch.setenv("AGENT_OPS_KILL_SWITCH", "false")
    get_config.cache_clear()
    fake = FakeRedis(falha_no_expire=True)

    async def _fake():
        return fake

    monkeypatch.setattr(metering.cotas, "_redis", _fake)

    restante = asyncio.run(metering.consumir("chat", limite=10))

    assert restante == 9
    assert fake.valores[metering.cotas._chave("chat")] == 1
    assert fake.expiracoes == {}  # o TTL realmente nao foi aplicado


def test_redis_ilegivel_recusa(monkeypatch):
    monkeypatch.setenv("AGENT_OPS_KILL_SWITCH", "false")
    get_config.cache_clear()

    async def _quebrado():
        return FakeRedis(explode=True)

    monkeypatch.setattr(metering.cotas, "_redis", _quebrado)

    with pytest.raises(metering.TetoAtingido):
        asyncio.run(metering.consumir("chat", limite=10))


def test_kill_switch_recusa_antes_de_tocar_no_redis(monkeypatch):
    monkeypatch.setenv("AGENT_OPS_KILL_SWITCH", "true")
    get_config.cache_clear()

    async def _explode():
        raise AssertionError("nao deveria abrir conexao com o kill switch ligado")

    monkeypatch.setattr(metering.cotas, "_redis", _explode)

    with pytest.raises(metering.TetoAtingido):
        asyncio.run(metering.consumir("chat", limite=10))


def test_devolver_reduz_o_contador(redis_falso):
    asyncio.run(metering.consumir("chat", limite=10, unidades=3))
    asyncio.run(metering.devolver("chat", unidades=3))
    assert redis_falso.valores[metering.cotas._chave("chat")] == 0


def test_devolver_engole_falha_do_redis(monkeypatch):
    async def _quebrado():
        return FakeRedis(explode=True)

    monkeypatch.setattr(metering.cotas, "_redis", _quebrado)
    # Nao levanta: uma devolucao perdida custa folga, nao dinheiro.
    asyncio.run(metering.devolver("chat"))


def test_panorama_relata_uso_e_restante(redis_falso):
    asyncio.run(metering.consumir("chat", limite=10, unidades=4))

    p = asyncio.run(metering.panorama({"chat": 10, "ingest": 5}))

    assert p["used"] == {"chat": 4, "ingest": 0}
    assert p["limits"] == {"chat": 10, "ingest": 5}
    assert p["remaining"] == {"chat": 6, "ingest": 5}
    assert p["kill_switch"] is False
    assert "degraded" not in p


def test_panorama_nunca_reporta_restante_negativo(redis_falso):
    # O teto e checado depois do INCR, entao o contador pode passar do limite
    # por um instante. O selo publico nao pode mostrar "-1 restante".
    asyncio.run(metering.consumir("chat", limite=100, unidades=7))

    p = asyncio.run(metering.panorama({"chat": 5}))

    assert p["remaining"]["chat"] == 0


def test_panorama_degrada_sem_derrubar_o_health_check(monkeypatch):
    monkeypatch.setenv("AGENT_OPS_KILL_SWITCH", "false")
    get_config.cache_clear()

    async def _quebrado():
        return FakeRedis(explode=True)

    monkeypatch.setattr(metering.cotas, "_redis", _quebrado)

    p = asyncio.run(metering.panorama({"chat": 10}))

    assert p["degraded"] is True
    assert p["kill_switch"] is False
