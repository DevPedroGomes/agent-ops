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
3. O `pcall` DO EXPIRE. O modelo em Python engole a falha do TTL por conta
   propria, entao trocar `redis.pcall` por `redis.call` no script nao quebrava
   nenhum teste rapido. Os dois testes de falha no TTL daqui fazem o EXPIRE
   errar dentro do script de verdade.

Rodar: `AGENT_OPS_TEST_REDIS_URL=redis://localhost:6379/15 pytest`.
Sem a variavel, tudo aqui e skip.
"""

import asyncio
import logging

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


def _ttl_invalido(monkeypatch):
    """Faz o EXPIRE do script falhar no servidor, sem mexer no servidor.

    `consumir` manda `str(_TTL_SEGUNDOS)` como ARGV[3]. Com "x" ali, o EXPIRE
    responde "value is not an integer or out of range" dentro do Lua, depois
    de o INCRBY ja ter escrito. E a mesma forma da falha original: o
    incremento vale e a faxina que vem em seguida erra.
    """
    monkeypatch.setattr(cotas, "_TTL_SEGUNDOS", "x")


def test_ttl_que_falha_dentro_do_script_nao_recusa_a_chamada(
    em_redis, monkeypatch, caplog
):
    """Regressao do bug do EXPIRE, contra o script de verdade.

    Com `redis.call` no EXPIRE, o script aborta depois do INCRBY, o Redis nao
    desfaz o incremento e `consumir` levanta `TetoIndisponivel`: a chamada e
    recusada e a cota fica cobrada. Com `redis.pcall`, a reserva fica de pe e
    so o TTL se perde.
    """
    _ttl_invalido(monkeypatch)

    async def cenario(cliente):
        restante = await metering.consumir("chat", limite=10)
        chave = cotas._chave("chat")
        return restante, await cliente.get(chave), await cliente.ttl(chave)

    with caplog.at_level(logging.WARNING, logger=cotas.__name__):
        restante, contador, ttl = em_redis(cenario)

    assert restante == 9, "a chamada foi recusada por causa do TTL"
    assert contador == "1", "a reserva nao ficou de pe"
    # -1 e "a chave existe e nao expira": prova que o EXPIRE errou de verdade
    # no servidor, e que o teste nao passou pelo caminho feliz.
    assert ttl == -1
    # O log e o que `ttl_ok=0` vindo do script produz em `consumir`.
    assert "metering.ttl_nao_aplicado" in caplog.text


def test_recusa_com_ttl_falhando_dentro_do_script_nao_cobra_cota(
    em_redis, monkeypatch, caplog
):
    """Teto estourado e EXPIRE com erro na mesma chamada.

    A compensacao tem que acontecer mesmo com o TTL falhando no meio do
    script. Se o EXPIRE abortasse o script, ela nunca rodaria e o contador
    ficaria inflado ate a virada do dia.
    """

    async def cenario(cliente):
        await metering.consumir("chat", limite=5, unidades=5)
        _ttl_invalido(monkeypatch)
        with pytest.raises(metering.TetoAtingido) as recusa:
            await metering.consumir("chat", limite=5, unidades=1)
        return recusa.value, await cliente.get(cotas._chave("chat"))

    with caplog.at_level(logging.WARNING, logger=cotas.__name__):
        recusa, contador = em_redis(cenario)

    # Tipo exato: `TetoIndisponivel` tambem e `TetoAtingido`, e e ele que sobe
    # quando o script aborta no EXPIRE.
    assert type(recusa) is metering.TetoAtingido, (
        f"recusou como {type(recusa).__name__}, nao por teto"
    )
    assert contador == "5", f"a chamada recusada cobrou cota: contador em {contador}"
    assert "metering.ttl_nao_aplicado" in caplog.text
