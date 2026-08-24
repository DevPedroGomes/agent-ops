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

    async def _quebrado():
        return FakeRedis(explode=True)

    monkeypatch.setattr(metering.cotas, "_redis", _quebrado)

    with pytest.raises(metering.TetoAtingido):
        asyncio.run(metering.consumir("chat", limite=10))


def test_kill_switch_recusa_antes_de_tocar_no_redis(monkeypatch):
    monkeypatch.setenv("AGENT_OPS_KILL_SWITCH", "true")
    tocou = []

    async def _explode():
        # Registrar a passagem e obrigatorio: o `except` generico do `consumir`
        # converteria a excecao deste duble em TetoAtingido, e o teste passaria
        # mesmo com o kill switch desligado.
        tocou.append(True)
        raise AssertionError("nao deveria abrir conexao com o kill switch ligado")

    monkeypatch.setattr(metering.cotas, "_redis", _explode)

    with pytest.raises(metering.TetoAtingido):
        asyncio.run(metering.consumir("chat", limite=10))

    assert tocou == [], "o kill switch nao barrou: a chamada chegou no Redis"


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
    assert p["degraded"] is False


def test_panorama_nunca_reporta_restante_negativo(redis_falso):
    # O teto e checado depois do INCR, entao o contador pode passar do limite
    # por um instante. O selo publico nao pode mostrar "-1 restante".
    asyncio.run(metering.consumir("chat", limite=100, unidades=7))

    p = asyncio.run(metering.panorama({"chat": 5}))

    assert p["remaining"]["chat"] == 0


def test_panorama_degrada_sem_derrubar_o_health_check(monkeypatch):
    monkeypatch.setenv("AGENT_OPS_KILL_SWITCH", "false")

    async def _quebrado():
        return FakeRedis(explode=True)

    monkeypatch.setattr(metering.cotas, "_redis", _quebrado)

    p = asyncio.run(metering.panorama({"chat": 10}))

    assert p["degraded"] is True
    assert p["kill_switch"] is False


def test_redis_ilegivel_levanta_o_subtipo_de_indisponibilidade(monkeypatch):
    # "Voce esgotou o teto" e "o Redis caiu" sao 429 e 503. Com um tipo so, o
    # chamador respondia 429 para uma queda de infra e mandava o visitante
    # tentar de novo em 30s — prazo que nao conserta uma indisponibilidade.
    monkeypatch.setenv("AGENT_OPS_KILL_SWITCH", "false")

    async def _quebrado():
        return FakeRedis(explode=True)

    monkeypatch.setattr(metering.cotas, "_redis", _quebrado)

    with pytest.raises(metering.TetoIndisponivel):
        asyncio.run(metering.consumir("chat", limite=10))


def test_teto_indisponivel_continua_sendo_pego_por_teto_atingido(monkeypatch):
    # Compatibilidade: quem ja escreve `except TetoAtingido` nao pode passar a
    # deixar a excecao subir depois desta mudanca.
    monkeypatch.setenv("AGENT_OPS_KILL_SWITCH", "false")

    async def _quebrado():
        return FakeRedis(explode=True)

    monkeypatch.setattr(metering.cotas, "_redis", _quebrado)

    with pytest.raises(metering.TetoAtingido):
        asyncio.run(metering.consumir("chat", limite=10))


def test_teto_esgotado_nao_e_indisponibilidade(redis_falso):
    asyncio.run(metering.consumir("chat", limite=1))

    with pytest.raises(metering.TetoAtingido) as exc:
        asyncio.run(metering.consumir("chat", limite=1))

    assert not isinstance(exc.value, metering.TetoIndisponivel)


def test_kill_switch_nao_e_indisponibilidade(monkeypatch):
    # O kill switch e uma parada DELIBERADA, nao uma falha de backend: o
    # operador desligou a demo. Continua 429, nao 503.
    monkeypatch.setenv("AGENT_OPS_KILL_SWITCH", "true")

    tocou = []

    async def _registra():
        tocou.append(True)
        return FakeRedis()

    monkeypatch.setattr(metering.cotas, "_redis", _registra)

    with pytest.raises(metering.TetoAtingido) as exc:
        asyncio.run(metering.consumir("chat", limite=10))

    assert tocou == [], "o kill switch nao barrou: a chamada chegou no Redis"
    assert not isinstance(exc.value, metering.TetoIndisponivel)


def test_kill_switch_engaja_sem_reiniciar_o_processo(monkeypatch):
    # O operador liga a env no container EM PE e espera o gasto parar. Como
    # `get_config` e lru_cache, a leitura ficava congelada na primeira chamada
    # e o switch so valia depois de reiniciar — enquanto config.py e o README
    # prometiam "sem rebuild nem redeploy". E o freio de emergencia: o prazo
    # documentado dele nao pode estar errado.
    monkeypatch.setenv("AGENT_OPS_KILL_SWITCH", "false")
    assert get_config().kill_switch is False  # config carregada e congelada

    monkeypatch.setenv("AGENT_OPS_KILL_SWITCH", "true")  # sem cache_clear
    tocou = []

    async def _explode():
        tocou.append(True)
        raise AssertionError("nao deveria abrir conexao com o kill switch ligado")

    monkeypatch.setattr(metering.cotas, "_redis", _explode)

    with pytest.raises(metering.TetoAtingido):
        asyncio.run(metering.consumir("chat", limite=10))

    assert tocou == [], "o kill switch nao barrou: a chamada chegou no Redis"


def test_panorama_relata_o_kill_switch_vivo(monkeypatch):
    # O health check nao pode dizer "kill switch desligado" enquanto o consumir
    # ja esta recusando por causa dele.
    monkeypatch.setenv("AGENT_OPS_KILL_SWITCH", "false")
    assert get_config().kill_switch is False

    monkeypatch.setenv("AGENT_OPS_KILL_SWITCH", "true")

    async def _quebrado():
        return FakeRedis(explode=True)

    monkeypatch.setattr(metering.cotas, "_redis", _quebrado)

    assert asyncio.run(metering.panorama({"chat": 10}))["kill_switch"] is True


def test_kill_switch_sem_env_cai_no_valor_da_config():
    # Sem a env definida, vale o que a Config carregou (default False).
    from agent_ops.config import kill_switch_ligado

    assert kill_switch_ligado() is False


def test_panorama_degradado_mantem_a_forma_do_saudavel(monkeypatch, redis_falso):
    # Sem `used`/`limits`/`remaining` no caminho degradado, um health check que
    # le `p["remaining"]["chat"]` levanta KeyError EXATAMENTE quando o Redis
    # cai: a funcao que existe para sobreviver a degradacao parcial virava um
    # 500. Uma forma so, com o `degraded` dizendo se da para confiar nos
    # numeros, e o que torna a leitura segura nos dois casos.
    saudavel = asyncio.run(metering.panorama({"chat": 10}))

    async def _quebrado():
        return FakeRedis(explode=True)

    monkeypatch.setattr(metering.cotas, "_redis", _quebrado)
    degradado = asyncio.run(metering.panorama({"chat": 10}))

    assert degradado.keys() == saudavel.keys()
    assert degradado["degraded"] is True
    assert degradado["limits"] == {"chat": 10}
    # Zero, nao `limite`: com o Redis ilegivel o `consumir` RECUSA, entao dizer
    # "10 restantes" prometeria ao visitante uma folga que ele nao tem.
    assert degradado["remaining"] == {"chat": 0}
    assert degradado["used"] == {"chat": 0}


def test_unidades_negativa_nao_pode_dar_credito(redis_falso):
    # `unidades` e onde entra valor CALCULADO (paginas do PDF, estimativa de
    # tokens). Um bug de parsing ali fazia o modulo que existe para NEGAR gasto
    # conceder orcamento: com limite=10 e unidades=-5 o retorno era 15 e o
    # contador ia para -5, liberando as proximas chamadas de todo mundo.
    with pytest.raises(ValueError):
        asyncio.run(metering.consumir("chat", limite=10, unidades=-5))

    assert redis_falso.valores == {}


def test_unidades_zero_nao_reserva_nada_e_e_recusada(redis_falso):
    # Zero passa pelo teto sempre e nao reserva nada: seria uma chamada paga
    # sem cota reservada, que e a invariante 1 do spec.
    with pytest.raises(ValueError):
        asyncio.run(metering.consumir("chat", limite=10, unidades=0))

    assert redis_falso.valores == {}


def test_devolver_recusa_unidades_nao_positiva(redis_falso):
    # `devolver(-1)` seria um `incrby(+1)` disfarçado: cobrar cota pela via de
    # devolucao e o mesmo erro de sinal do outro lado.
    asyncio.run(metering.consumir("chat", limite=10, unidades=2))

    for invalida in (0, -1):
        with pytest.raises(ValueError):
            asyncio.run(metering.devolver("chat", unidades=invalida))

    assert redis_falso.valores[metering.cotas._chave("chat")] == 2
