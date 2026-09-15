"""O que o dublê nao consegue provar sobre o Redis.

A suite roda contra um dublê escrito a mao, de proposito: ela e rapida e nao
precisa de infraestrutura. O preco e que duas coisas ficam sem cobertura
justamente onde elas podem quebrar.

1. O SCRIPT LUA. Ele decide reserva, teto e compensacao num passo so, e um
   dublê em Python nao executa Lua: o que existe em `test_metering.py` e um
   MODELO da semantica. Modelo e util e nao e prova.
2. A ATOMICIDADE. A afirmacao de que uma chamada recusada nao custa cota so
   vale se reservar, checar e desfazer forem indivisiveis de verdade. Isso e
   propriedade do servidor, nao do cliente, e so aparece sob concorrencia
   real, que num dublê sequencial nao existe.

Rodar: `AGENT_OPS_TEST_REDIS_URL=redis://localhost:6379/15 pytest`.
Sem a variavel, tudo aqui e skip.
"""

import asyncio

import pytest

from agent_ops import metering
from agent_ops.metering import cotas


def test_o_script_reserva_e_devolve_o_restante(em_redis):
    async def cenario(cliente):
        restante = await metering.consumir("chat", limite=10, unidades=3)
        return restante, await cliente.get(cotas._chave("chat"))

    restante, contador = em_redis(cenario)

    assert restante == 7
    assert contador == "3"


def test_o_script_aplica_o_ttl(em_redis):
    async def cenario(cliente):
        await metering.consumir("chat", limite=10)
        return await cliente.ttl(cotas._chave("chat"))

    ttl = em_redis(cenario)

    assert 0 < ttl <= cotas._TTL_SEGUNDOS


def test_recusa_nao_deixa_o_contador_mexido(em_redis):
    """A invariante central, contra um servidor de verdade.

    O incremento e a compensacao acontecem dentro do mesmo script, entao nao
    existe janela em que o contador fique inflado nem chamada de rede que possa
    falhar entre as duas.
    """

    async def cenario(cliente):
        await metering.consumir("chat", limite=5, unidades=5)
        with pytest.raises(metering.TetoAtingido):
            await metering.consumir("chat", limite=5, unidades=1)
        return await cliente.get(cotas._chave("chat"))

    assert em_redis(cenario) == "5", "a chamada recusada mexeu no contador"


def test_o_teto_segura_sob_concorrencia_real(em_redis):
    """Cinquenta pedidos simultaneos, teto de 10: exatamente 10 passam.

    E o teste que o dublê nao consegue fazer, porque nele tudo e sequencial.
    Se reservar e checar nao fossem indivisiveis no servidor, varias chamadas
    leriam o mesmo total e passariam juntas, que e a rajada concorrente contra
    a qual o modulo inteiro foi escrito.
    """

    async def cenario(cliente):
        resultados = await asyncio.gather(
            *(metering.consumir("chat", limite=10) for _ in range(50)),
            return_exceptions=True,
        )
        concedidas = [r for r in resultados if not isinstance(r, Exception)]
        recusadas = [r for r in resultados if isinstance(r, metering.TetoAtingido)]
        return concedidas, recusadas, await cliente.get(cotas._chave("chat"))

    concedidas, recusadas, contador = em_redis(cenario)

    assert len(concedidas) == 10, f"passaram {len(concedidas)}, o teto era 10"
    assert len(recusadas) == 40
    assert contador == "10", f"o contador parou em {contador}, esperado 10"


def test_a_devolucao_nao_deixa_o_contador_negativo_no_servidor(em_redis):
    async def cenario(cliente):
        await metering.devolver("chat", unidades=3)
        return await cliente.get(cotas._chave("chat"))

    assert em_redis(cenario) == "0"


def test_panorama_le_o_que_o_script_escreveu(em_redis):
    async def cenario(_cliente):
        await metering.consumir("chat", limite=10, unidades=4)
        return await metering.panorama({"chat": 10})

    p = em_redis(cenario)

    assert p["degraded"] is False
    assert p["used"] == {"chat": 4}
    assert p["remaining"] == {"chat": 6}


def test_escopo_e_global_nao_se_consomem(em_redis):
    async def cenario(cliente):
        await metering.consumir("chat", limite=10, unidades=2)
        await metering.consumir("chat", limite=10, unidades=5, escopo="ip:1.2.3.4")
        return (
            await cliente.get(cotas._chave("chat")),
            await cliente.get(cotas._chave("chat", "ip:1.2.3.4")),
        )

    global_, escopado = em_redis(cenario)

    assert global_ == "2"
    assert escopado == "5"


def test_fechar_solta_o_cliente_de_verdade(em_redis):
    """`fechar` tem que funcionar contra um cliente real, nao so contra o dublê."""

    async def cenario(_cliente):
        await metering.consumir("chat", limite=10)
        cotas._cliente = await cotas._redis()
        await metering.fechar()
        return cotas._cliente

    assert em_redis(cenario) is None
