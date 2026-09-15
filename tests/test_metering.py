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
from datetime import UTC

import pytest

from agent_ops import metering
from agent_ops.config import get_config


class FakePipeline:
    """Dublê de MULTI/EXEC, com a semantica que importa aqui.

    Uma transacao do Redis NAO faz rollback quando um comando falha em tempo de
    execucao: os outros rodam assim mesmo e o EXEC devolve o erro NA POSICAO
    daquele comando. E por isso que `consumir` chama `execute(raise_on_error=
    False)` e inspeciona cada slot em vez de confiar num `try` unico. Um dublê
    que levantasse tudo junto esconderia exatamente a distincao que o modulo
    precisa fazer: INCRBY que falhou (nada a desfazer, recusa) e EXPIRE que
    falhou (faxina, segue em frente).

    Queda de conexao e outra coisa e continua levantando do proprio `execute`,
    porque ai nenhum dos dois comandos chegou ao servidor.
    """

    def __init__(self, fake):
        self.fake = fake
        self.comandos: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def incrby(self, chave, quanto):
        self.comandos.append(("incrby", chave, quanto))
        return self

    def expire(self, chave, segundos):
        self.comandos.append(("expire", chave, segundos))
        return self

    async def execute(self, raise_on_error=True):
        if self.fake.explode:
            # Conexao caiu: o EXEC nunca chegou ao servidor, nenhum comando
            # foi aplicado.
            raise ConnectionError("redis fora do ar")

        resultados = []
        for nome, chave, valor in self.comandos:
            try:
                resultados.append(await getattr(self.fake, nome)(chave, valor))
            except Exception as exc:
                if raise_on_error:
                    raise
                resultados.append(exc)
        return resultados


class FakeRedis:
    """Dublê minimo: so o que `cotas.py` usa."""

    def __init__(self, explode=False, falha_no_expire=False):
        self.valores: dict[str, int] = {}
        self.expiracoes: dict[str, int] = {}
        self.explode = explode
        # Falha SO no expire, com o incrby passando. Sem esse controle
        # separado nao da para expressar a falha de rede entre as duas idas ao
        # Redis, que e justamente onde a invariante quebrava.
        self.falha_no_expire = falha_no_expire

    def pipeline(self, transaction=True):
        return FakePipeline(self)

    async def eval(self, script, numkeys, *args):
        """Modelo em Python do `_LUA_CONSUMIR`, nao o script de verdade.

        Um dublê nao roda Lua. O que ele faz e reproduzir a SEMANTICA para os
        testes rapidos continuarem rapidos, e o script de verdade e exercitado
        contra um Redis real em `tests/test_portabilidade_redis.py`. Mesmo
        arranjo do SQLite e do Postgres: o modelo e barato, o servidor de
        verdade e quem manda.

        A afirmacao abaixo evita o modo de falha obvio desse arranjo: alguem
        muda o script e o dublê segue respondendo pela versao antiga.
        """
        assert script is metering.cotas._LUA_CONSUMIR, (
            "o dublê so modela o script de reserva; apareceu outro script e o "
            "modelo aqui precisa acompanhar"
        )
        assert numkeys == 1

        if self.explode:
            raise ConnectionError("redis fora do ar")

        chave = args[0]
        unidades, limite, ttl = int(args[1]), int(args[2]), int(args[3])

        usado = self.valores.get(chave, 0) + unidades
        self.valores[chave] = usado

        # `pcall` no script: falhar aqui nao aborta nem recusa nada.
        ttl_ok = 1
        try:
            await self.expire(chave, ttl)
        except Exception:
            ttl_ok = 0

        if usado > limite:
            self.valores[chave] = usado - unidades
            return [1, usado, ttl_ok]
        return [0, usado, ttl_ok]

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


def test_consumir_aplica_o_ttl(redis_falso):
    asyncio.run(metering.consumir("chat", limite=10))
    chave = metering.cotas._chave("chat")
    assert redis_falso.expiracoes[chave] == metering.cotas._TTL_SEGUNDOS


def test_o_ttl_nao_depende_do_contador_passar_por_unidades(redis_falso):
    """Toda chave que existe tem TTL, qualquer que seja o estado do contador.

    O gatilho antigo era `usado == unidades`, um proxy de "esta e a primeira
    escrita". Ele so acerta quando o contador comeca em zero, e nada garante
    isso: uma devolucao orfa o deixava negativo, uma correcao manual de
    operador o deixa em qualquer valor. Dai um passo de 2 unidades produz 1, 3,
    5... e nunca 2, o `EXPIRE` nunca acontece e sobra uma chave imortal por
    (projeto, dia, tipo, escopo).

    O contador e semeado direto no dublê de proposito: a origem do valor nao
    importa, e a rota da devolucao orfa esta fechada pelo piso em zero. A
    propriedade que se quer prender e a invariante, nao um caminho para ela.
    """
    chave = metering.cotas._chave("chat")
    redis_falso.valores[chave] = 1

    asyncio.run(metering.consumir("chat", limite=100, unidades=2))  # 1 -> 3

    assert redis_falso.valores[chave] == 3, "o cenario tem que pular o 2"
    assert redis_falso.expiracoes.get(chave) == metering.cotas._TTL_SEGUNDOS, (
        "a chave ficou sem TTL: ela nunca sai do Redis"
    )


def test_devolver_tambem_aplica_o_ttl(redis_falso):
    """Uma devolucao pode CRIAR a chave, e uma chave criada sem TTL e imortal."""
    asyncio.run(metering.devolver("chat", unidades=1))

    chave = metering.cotas._chave("chat")
    assert redis_falso.expiracoes.get(chave) == metering.cotas._TTL_SEGUNDOS


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


def test_tipo_com_dois_pontos_e_recusado(redis_falso):
    # `_chave("a", escopo="b")` e `_chave("a:b")` davam a MESMA chave: um teto
    # por IP passava a somar no contador de outro tipo de cota, sem erro.
    with pytest.raises(ValueError):
        metering.cotas._chave("chat:ip")


def test_escopo_pode_ter_dois_pontos(redis_falso):
    # `escopo` e o ULTIMO pedaco da chave, entao `:` la dentro nao cria
    # ambiguidade — e `ip:1.2.3.4` e justamente o uso documentado dele.
    assert metering.cotas._chave("chat", escopo="ip:1.2.3.4", dia="2026-08-24") == (
        "ao:budget:testes:2026-08-24:chat:ip:1.2.3.4"
    )


def test_segundos_ate_meia_noite_esta_dentro_do_dia():
    s = metering.segundos_ate_meia_noite_utc()
    assert 1 <= s <= 86_400


def test_segundos_ate_meia_noite_na_virada_exata(monkeypatch):
    # Na meia-noite exata o `+ 86_400` ja corrige sozinho: este teste prende o
    # valor, nao o piso. Quem prende o piso e o teste seguinte.
    from datetime import datetime

    class MeiaNoiteExata(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 25, 0, 0, 0, tzinfo=UTC)

    monkeypatch.setattr(metering.cotas, "datetime", MeiaNoiteExata)

    assert metering.segundos_ate_meia_noite_utc() == 86_400


def test_o_piso_salva_o_ultimo_fragmento_de_segundo(monkeypatch):
    # 23:59:59.5 -> faltam 0,5s -> `int()` trunca para 0 -> `Retry-After: 0`
    # manda o cliente repetir na mesma hora. Sem `max(1, ...)` este teste falha
    # com 0, e ele e o UNICO que morre se alguem apagar o piso achando que o
    # `+ 86_400` ja cobre tudo.
    from datetime import datetime

    class QuaseVirada(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 24, 23, 59, 59, 500_000, tzinfo=UTC)

    monkeypatch.setattr(metering.cotas, "datetime", QuaseVirada)

    assert metering.segundos_ate_meia_noite_utc() == 1


def test_segundos_ate_meia_noite_conta_o_que_falta(monkeypatch):
    from datetime import datetime

    class VinteETres(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 24, 23, 0, 0, tzinfo=UTC)

    monkeypatch.setattr(metering.cotas, "datetime", VinteETres)

    assert metering.segundos_ate_meia_noite_utc() == 3_600


def test_devolver_nao_deixa_o_contador_negativo(redis_falso):
    """Devolver o que nunca foi consumido nao pode virar credito.

    `devolver` calcula a chave com o dia de HOJE, entao uma chamada reservada
    as 23:59:59 cuja devolucao acontece as 00:00:01 cai na chave de AMANHA, que
    ainda nao existe. O `incrby` negativo criava essa chave em -N.
    """
    asyncio.run(metering.devolver("chat", unidades=3))

    assert redis_falso.valores[metering.cotas._chave("chat")] == 0


def test_devolucao_orfa_nao_eleva_o_teto_do_dia(redis_falso):
    """A consequencia que da o tamanho do defeito.

    Com o contador comecando negativo, o teto do dia sobe pelo valor devolvido
    e nao se corrige sozinho antes da virada seguinte. Medido antes da
    correcao: com `unidades=3` e duas devolucoes orfas, 15 unidades servidas
    contra um limite de 10.
    """
    asyncio.run(metering.devolver("chat", unidades=3))
    asyncio.run(metering.devolver("chat", unidades=3))

    gastas = 0
    while True:
        try:
            asyncio.run(metering.consumir("chat", limite=10, unidades=3))
        except metering.TetoAtingido:
            break
        gastas += 3

    assert gastas <= 10, f"o teto de 10 deixou passar {gastas} unidades"


def test_devolucao_normal_continua_devolvendo(redis_falso):
    """O piso nao pode atrapalhar o caminho que importa."""
    asyncio.run(metering.consumir("chat", limite=10, unidades=4))
    asyncio.run(metering.devolver("chat", unidades=4))

    assert redis_falso.valores[metering.cotas._chave("chat")] == 0

    # e a cota devolvida esta de fato disponivel de novo
    restante = asyncio.run(metering.consumir("chat", limite=10, unidades=10))
    assert restante == 0


def test_o_piso_nao_apaga_consumo_de_outra_chamada(redis_falso):
    """Zerar a chave seria errado: o piso corrige so o proprio excesso."""
    asyncio.run(metering.consumir("chat", limite=10, unidades=2))
    asyncio.run(metering.devolver("chat", unidades=5))

    # 2 consumidas, 5 devolvidas: o correto e 0, nao -3, e nem "zerar tudo"
    # de um jeito que perdesse um consumo concorrente.
    assert redis_falso.valores[metering.cotas._chave("chat")] == 0


class FakeClienteFechavel:
    def __init__(self, explode_ao_fechar=False):
        self.fechado = False
        self.explode_ao_fechar = explode_ao_fechar

    async def aclose(self):
        if self.explode_ao_fechar:
            raise ConnectionError("ja estava fora do ar")
        self.fechado = True


def test_fechar_devolve_o_cliente_e_limpa_o_singleton(monkeypatch):
    """Sem isto nao ha desligamento limpo: o pool fica aberto ate o processo cair."""
    cliente = FakeClienteFechavel()
    monkeypatch.setattr(metering.cotas, "_cliente", cliente)

    asyncio.run(metering.fechar())

    assert cliente.fechado
    assert metering.cotas._cliente is None, (
        "o singleton tem que sair junto, senao a proxima chamada usa um "
        "cliente fechado em vez de abrir outro"
    )


def test_fechar_sem_cliente_aberto_nao_faz_nada(monkeypatch):
    monkeypatch.setattr(metering.cotas, "_cliente", None)

    asyncio.run(metering.fechar())  # nao levanta


def test_fechar_engole_falha_do_backend(monkeypatch):
    """Desligamento nao pode falhar por causa do que ja estava quebrado."""
    cliente = FakeClienteFechavel(explode_ao_fechar=True)
    monkeypatch.setattr(metering.cotas, "_cliente", cliente)

    asyncio.run(metering.fechar())

    assert metering.cotas._cliente is None, (
        "mesmo com erro o singleton tem que sair, senao fica preso um cliente "
        "que ninguem consegue fechar nem substituir"
    )


def test_depois_de_fechar_a_proxima_chamada_abre_outro(monkeypatch):
    monkeypatch.setattr(metering.cotas, "_cliente", FakeClienteFechavel())
    asyncio.run(metering.fechar())

    criados = []

    def _from_url(*args, **kwargs):
        criados.append(1)
        return FakeClienteFechavel()

    monkeypatch.setattr(metering.cotas.aioredis, "from_url", _from_url)
    asyncio.run(metering.cotas._redis())

    assert criados == [1], "o cliente novo nao foi criado"


def test_panorama_enxerga_o_escopo_pedido(redis_falso):
    """Sem isto, cota por IP e invisivel para quem observa.

    `consumir` aceita `escopo` justamente para um teto por IP conviver com o
    global, e o `panorama` so lia a chave sem escopo. O teto por IP existia,
    recusava chamadas, e nao aparecia em lugar nenhum: nem no health check, nem
    num selo que diga ao visitante quanto lhe resta.
    """
    asyncio.run(metering.consumir("chat", limite=20, unidades=3, escopo="ip:1.2.3.4"))

    p = asyncio.run(metering.panorama({"chat": 20}, escopo="ip:1.2.3.4"))

    assert p["used"] == {"chat": 3}
    assert p["remaining"] == {"chat": 17}
    assert p["escopo"] == "ip:1.2.3.4"


def test_panorama_sem_escopo_nao_ve_o_contador_escopado(redis_falso):
    """As duas contas sao separadas de proposito, e o relato tem que refletir isso."""
    asyncio.run(metering.consumir("chat", limite=20, unidades=3, escopo="ip:1.2.3.4"))

    p = asyncio.run(metering.panorama({"chat": 20}))

    assert p["used"] == {"chat": 0}
    assert p["escopo"] is None


def test_panorama_escopado_degrada_com_a_mesma_forma(monkeypatch):
    monkeypatch.setenv("AGENT_OPS_KILL_SWITCH", "false")
    get_config.cache_clear()

    async def _quebrado():
        return FakeRedis(explode=True)

    monkeypatch.setattr(metering.cotas, "_redis", _quebrado)

    p = asyncio.run(metering.panorama({"chat": 20}, escopo="ip:1.2.3.4"))

    assert p["degraded"] is True
    assert p["escopo"] == "ip:1.2.3.4"
    assert p["used"] == {"chat": 0}
    assert p["remaining"] == {"chat": 0}


class RedisComRollbackQuebrado(FakeRedis):
    """Deixa o incremento passar e derruba a devolucao que viria depois.

    E a janela entre as duas idas ao Redis: a chamada ja foi contada e a
    compensacao nao acontece. Nao e hipotese remota, e o mesmo tipo de falha de
    rede que o bug herdado do `budget.py` explorava, e vale tambem para o
    processo morrer entre uma coisa e outra.
    """

    def __init__(self):
        super().__init__()
        self.ja_incrementou = False

    async def incrby(self, chave, quanto):
        if quanto < 0 and self.ja_incrementou:
            raise ConnectionError("redis caiu antes da compensacao")
        self.ja_incrementou = True
        return await super().incrby(chave, quanto)


def test_recusa_nao_pode_cobrar_cota_quando_a_compensacao_falha(monkeypatch):
    """A invariante central do modulo, no caminho em que ela ainda quebrava.

    "Uma chamada recusada nao custa cota ao proximo visitante" e a razao de o
    `consumir` existir. Contar primeiro e compensar depois deixa essa promessa
    dependendo de uma SEGUNDA ida ao Redis dar certo, e quando ela nao da, o
    contador fica inflado ate a virada do dia: visitantes seguintes sao
    recusados por cota que ninguem gastou.
    """
    monkeypatch.setenv("AGENT_OPS_PROJETO", "testes")
    monkeypatch.setenv("AGENT_OPS_KILL_SWITCH", "false")
    get_config.cache_clear()
    fake = RedisComRollbackQuebrado()

    async def _fake():
        return fake

    monkeypatch.setattr(metering.cotas, "_redis", _fake)

    with pytest.raises(metering.TetoAtingido):
        asyncio.run(metering.consumir("chat", limite=0, unidades=1))

    assert fake.valores[metering.cotas._chave("chat")] == 0, (
        "a chamada recusada cobrou cota: o contador ficou inflado ate a virada"
    )
