"""Teto diario de chamadas pagas, para demos abertas na internet.

Por que existe separado do rate limit: o rate limit limita o que UM chamador
faz por minuto. Nenhum rate limit limita o que TODOS fazem juntos, nem alguem
criando contas novas — e com cadastro aberto "exigir login" nao e teto de gasto.

Decisoes, todas herdadas do `budget.py` do BrainHub:

- A cota e consumida ANTES da chamada ao provider, nunca depois. Contar depois
  deixa uma rajada concorrente passar toda junta, porque nenhuma delas foi
  contada ainda.
- O dia vira em UTC, para o reset nao andar com o fuso do servidor.
- INCR primeiro e checa depois: duas requisicoes concorrentes enxergam cada uma
  o proprio total pos-incremento, entao nenhuma escapa do teto. Quem perde
  desfaz o proprio incremento. As tres coisas (reservar, checar, desfazer)
  acontecem num script Lua, num passo indivisivel: enquanto a compensacao era
  uma segunda ida ao Redis, uma falha nela deixava a chamada RECUSADA cobrando
  cota, que e a unica coisa que este modulo existe para impedir.
- Se o Redis nao responde, RECUSA. Um teto ilegivel nao e um teto ausente —
  mas o tipo levantado e `TetoIndisponivel`, para o chamador separar 503 de 429.
- O kill switch e configuracao de ambiente, para estancar sem redeploy. Ele e
  parada deliberada, nao falha de backend: continua `TetoAtingido` puro. E lido
  a CADA chamada (`kill_switch_ligado`), nao pelo cache de `get_config` — um
  freio de emergencia que so pega depois de reiniciar o processo nao e freio de
  emergencia.

O que mudou na extracao: a chave ganhou `projeto` (dois apps no mesmo Redis
dividiriam o teto um do outro) e `escopo` opcional (teto por IP convivendo com
o teto global sem que um consuma o do outro).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any, cast

import redis.asyncio as aioredis

from agent_ops.config import (
    exigir_sem_dois_pontos,
    get_config,
    kill_switch_ligado,
)

logger = logging.getLogger(__name__)

_cliente: aioredis.Redis | None = None

# 48h: sobrevive a virada de dia UTC sem acumular chave velha para sempre.
_TTL_SEGUNDOS = 172_800


class TetoAtingido(Exception):
    """Recusa segura de mostrar ao visitante. O chamador responde 429."""

    def __init__(self, mensagem: str):
        self.mensagem = mensagem
        super().__init__(mensagem)


class TetoIndisponivel(TetoAtingido):
    """Recusa porque o teto nao pode ser LIDO, nao porque acabou.

    Subclasse e nao irma: `except TetoAtingido` ja escrito continua pegando
    esta, entao a distincao pode ser adotada aos poucos.

    Existe porque "voce esgotou a cota de hoje" e "o Redis caiu" sao 429 e 503,
    e ate aqui o unico jeito de separar os dois era comparar a mensagem em
    ingles. Todo chamador respondia 429 para uma queda de infra e mandava o
    visitante voltar em 30s — prazo que nao conserta indisponibilidade, e
    status que nao acorda ninguem de plantao.
    """


def _hoje_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def segundos_ate_meia_noite_utc() -> int:
    """Quanto falta para o teto resetar. Serve ao header `Retry-After`.

    Piso de 1 segundo de proposito, e o momento em que ele importa NAO e a
    virada: na meia-noite exata o `+ 86_400` ja devolve 86400 sozinho. Quem
    precisa do piso e o ultimo fragmento de segundo ANTES da virada — em
    23:59:59.5 o valor real e meio segundo, o `int()` trunca para 0, e
    `Retry-After: 0` convida o cliente a repetir imediatamente, que e o laco
    que o teto existe para impedir.
    """
    agora = datetime.now(UTC)
    inicio_do_dia = agora.replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int(inicio_do_dia.timestamp() + 86_400 - agora.timestamp()))


def _chave(tipo: str, escopo: str | None = None, dia: str | None = None) -> str:
    """Monta a chave do contador. Recusa `:` dentro de `tipo`.

    Sem essa recusa, `_chave("a", escopo="b")` e `_chave("a:b")` produziam a
    MESMA chave: um teto por IP passaria a somar no contador de outro tipo de
    cota, sem erro nenhum.

    `escopo` fica livre de proposito: ele e o ULTIMO pedaco da chave, entao um
    `:` la dentro nao cria ambiguidade — e o caso de uso documentado do escopo
    e justamente `ip:1.2.3.4`. Barrar `tipo` (vocabulario pequeno e escolhido
    pela app: "chat", "ingest") fecha a colisao sem tirar isso.
    """
    exigir_sem_dois_pontos(tipo, "tipo")
    base = f"ao:budget:{get_config().projeto}:{dia or _hoje_utc()}:{tipo}"
    return f"{base}:{escopo}" if escopo else base


async def _redis() -> aioredis.Redis:
    global _cliente
    if _cliente is None:
        # `from_url` do redis-py nao e anotada, entao o mypy em strict recusa
        # a chamada. O tipo do retorno esta certo e e o que importa aqui.
        _cliente = aioredis.from_url(  # type: ignore[no-untyped-call]
            get_config().redis_url, decode_responses=True
        )
    return _cliente


async def fechar() -> None:
    """Fecha o cliente do Redis e solta o singleton. Chame no shutdown da app.

    O cliente e criado na primeira chamada e vivia ate o processo morrer. Isso
    e barato num servico em pe, e atrapalha em dois lugares concretos:

    - desligamento. O pool fica aberto, e um interpretador saindo sob carga
      reclama de conexao nao fechada no meio do log de shutdown;
    - mais de um event loop no mesmo processo. O cliente amarra no loop que o
      tocou primeiro, entao um script que chama `asyncio.run` duas vezes
      encontra na segunda um cliente preso a um loop ja fechado. Sem uma forma
      de soltar o singleton, nao havia saida a nao ser reiniciar o processo.

    NAO LEVANTA, e o singleton sai mesmo quando o `aclose` falha: o backend ja
    estar fora do ar e justamente uma das razoes de estar desligando, e um
    cliente que ninguem consegue fechar nem substituir e pior que um socket
    vazado. Mesmo contrato do `devolver`.

    Depois desta chamada, a proxima operacao abre um cliente novo.
    """
    global _cliente
    if _cliente is None:
        return

    cliente, _cliente = _cliente, None
    try:
        await cliente.aclose()
    except Exception:
        logger.warning("metering.fechamento_falhou", exc_info=True)


# Reserva e checagem de teto num passo indivisivel, no servidor.
#
# POR QUE NAO DA PARA FAZER ISSO EM DUAS IDAS AO REDIS: a invariante central do
# modulo e "uma chamada recusada nao custa cota ao proximo visitante". Contando
# primeiro e compensando depois, essa promessa fica dependendo de uma SEGUNDA
# chamada dar certo. Quando ela nao da (rede piscou, processo morreu no meio),
# o contador fica inflado ate a virada do dia e visitantes seguintes sao
# recusados por cota que ninguem gastou. Nao havia como consertar isso do lado
# do cliente: qualquer compensacao e uma nova viagem que pode falhar.
#
# `redis.call` no INCRBY e `redis.pcall` no EXPIRE, e a diferenca e deliberada.
# Script de Redis NAO desfaz o que ja escreveu quando aborta, igual a MULTI/
# EXEC: um erro no EXPIRE com `call` derrubaria o script DEPOIS de o INCRBY ter
# valido, e seria o bug herdado do `budget.py` de volta, agora escondido dentro
# do Lua. Com `pcall` o EXPIRE erra sem abortar nada, que e o tratamento certo
# para uma faxina. Um erro no INCRBY, esse sim, aborta antes de qualquer
# escrita: nao ha o que desfazer e o chamador recusa.
#
# Devolve {recusado, usado, ttl_aplicado}.
_LUA_CONSUMIR = """
local usado = redis.call('INCRBY', KEYS[1], ARGV[1])
local ttl_ok = 1
local ttl = redis.pcall('EXPIRE', KEYS[1], ARGV[3])
if type(ttl) == 'table' and ttl.err then ttl_ok = 0 end
if usado > tonumber(ARGV[2]) then
  redis.call('INCRBY', KEYS[1], -tonumber(ARGV[1]))
  return {1, usado, ttl_ok}
end
return {0, usado, ttl_ok}
"""


async def _incrementar_com_ttl(
    r: aioredis.Redis, chave: str, quanto: int
) -> tuple[Any, Any]:
    """`INCRBY` e `EXPIRE` numa transacao so. Devolve os dois resultados.

    Uma ida ao Redis em vez de duas, e o TTL deixa de depender de adivinhar
    qual chamada e a primeira. O gatilho anterior era `usado == unidades`, que
    so acerta quando o contador comeca em zero: bastava uma devolucao orfa
    deixar a chave negativa para o passo pular o valor reservado (-1, 1, 3, 5
    com `unidades=2`) e o `EXPIRE` nunca acontecer. A chave entao sobrevivia a
    todas as viradas de dia, uma por (projeto, dia, tipo, escopo), para sempre.

    Renovar o TTL a cada chamada e de proposito e nao atrapalha: o nome da
    chave ja carrega o dia, entao renovar so faz a chave viver 48h depois do
    ultimo uso, que e exatamente a faxina desejada.

    `raise_on_error=False` porque o Redis NAO desfaz uma transacao quando um
    comando falha: os outros rodam, e o erro volta na posicao do que falhou.
    Levantar tudo junto colocaria o `EXPIRE` de volta no mesmo escopo de erro
    do `INCRBY`, que e a forma exata do bug herdado do `budget.py`. Quem chama
    inspeciona cada slot.
    """
    async with r.pipeline(transaction=True) as pipe:
        pipe.incrby(chave, quanto)
        pipe.expire(chave, _TTL_SEGUNDOS)
        usado, ttl = await pipe.execute(raise_on_error=False)
    return usado, ttl


async def _pisar_em_zero(
    r: aioredis.Redis, chave: str, observado: int
) -> None:
    """Desfaz exatamente o excesso abaixo de zero. Nao zera a chave.

    Some o proprio excesso (`-observado`) em vez de `SET chave 0` porque um
    consumo concorrente pode ter entrado no meio: o `SET` apagaria esse consumo
    e daria a chamada dele de graca. Somar de volta so o que se observou abaixo
    de zero e comutativo com qualquer `incrby` paralelo.

    A correcao nao e atomica com a leitura, e sob concorrencia ela pode sobrar:
    duas devolucoes orfas simultaneas veem -1 e -2, somam +1 e +2, e o contador
    para em +1 em vez de 0. Isso e um erro para CIMA, ou seja, uma unidade de
    cota a menos para os visitantes. A direcao importa: o modulo inteiro existe
    para nunca errar para baixo, que e o lado que gasta dinheiro. Tornar isso
    exato exigiria script Lua no servidor, e o preco nao se paga para consertar
    um caso que ja e patologico.
    """
    try:
        await r.incrby(chave, -observado)
    except Exception:
        logger.warning("metering.piso_nao_aplicado chave=%s", chave, exc_info=True)


async def consumir(
    tipo: str,
    limite: int,
    unidades: int = 1,
    escopo: str | None = None,
) -> int:
    """Gasta `unidades` da cota de hoje. Devolve o quanto sobrou.

    Levanta `TetoAtingido` sem ter consumido nada, e isso e garantido pelo
    servidor e nao pela sorte: reservar, checar e desfazer sao um script so.
    Uma chamada recusada nao custa cota ao proximo visitante. Quando a recusa
    vem de o Redis estar ilegivel, o tipo e `TetoIndisponivel` (subclasse),
    para o chamador poder responder 503 em vez de 429 sem inspecionar a
    mensagem.

    `ValueError` para `unidades` nao positiva, mesma excecao a regra do
    `marcar`: e erro de programacao e aparece no primeiro teste.
    """
    # `unidades` e onde entra valor CALCULADO — paginas de um PDF, estimativa
    # de tokens. Um bug de parsing ali (0 ou negativo) fazia o modulo que existe
    # para NEGAR gasto conceder orcamento: `incrby` com valor negativo empurra o
    # contador para baixo de zero e libera as proximas chamadas de todo mundo,
    # sem nenhum erro. Zero e igualmente invalido: passaria pelo teto sempre sem
    # reservar nada, quebrando a invariante de nao haver chamada paga sem cota
    # reservada antes.
    if unidades <= 0:
        raise ValueError(f"unidades deve ser positiva; recebeu {unidades!r}")

    if kill_switch_ligado():
        logger.warning("metering.kill_switch_ativo tipo=%s", tipo)
        raise TetoAtingido("This demo is paused right now. Please try again later.")

    chave = _chave(tipo, escopo)
    try:
        r = await _redis()
        # Argumentos como texto porque e assim que eles viajam no protocolo:
        # todo ARGV chega no Lua como string de qualquer jeito, e o script ja
        # faz `tonumber` onde precisa de numero.
        # O `cast` existe porque os stubs do redis-py descrevem `eval` com o
        # retorno de sincrono e assincrono unidos (`Awaitable[str] | str`), e
        # nao da para dar `await` nisso. O tipo de verdade e o do script: os
        # tres inteiros que ele devolve.
        recusado, usado, ttl_ok = await cast(
            "Awaitable[list[int]]",
            r.eval(
                _LUA_CONSUMIR,
                1,
                chave,
                str(unidades),
                str(limite),
                str(_TTL_SEGUNDOS),
            ),
        )
    except Exception as exc:
        # O script e indivisivel: ou ele reservou, ou nao escreveu nada. Nao ha
        # incremento pendente para desfazer neste caminho.
        logger.error("metering.estado_ilegivel tipo=%s erro=%s", tipo, exc)
        raise TetoIndisponivel("This demo is temporarily unavailable.") from exc

    if not ttl_ok:
        # Faxina, nao corretude: o contador ja esta certo sem TTL e a chave de
        # amanha tem outro nome. O `pcall` la dentro garantiu que isto nao
        # recusou nada.
        logger.warning("metering.ttl_nao_aplicado chave=%s", chave)

    if recusado:
        # A devolucao ja aconteceu dentro do script, junto com o incremento.
        logger.warning(
            "metering.esgotado tipo=%s usado=%d limite=%d", tipo, usado, limite
        )
        raise TetoAtingido(
            "This demo reached today's usage cap. It resets at midnight UTC."
        )

    return limite - int(usado)


async def devolver(tipo: str, unidades: int = 1, escopo: str | None = None) -> None:
    """Devolve cota quando a chamada paga nao chegou a acontecer.

    Melhor esforco: uma devolucao perdida custa um pouco de folga, um gasto nao
    registrado custa dinheiro. A excecao e `unidades` nao positiva, que levanta
    `ValueError`: `devolver(-1)` e um `incrby(+1)` disfarcado, ou seja, cobrar
    cota pela via da devolucao — o mesmo erro de sinal do `consumir`, do outro
    lado.

    NAO DEIXA O CONTADOR NEGATIVO. A chave e calculada com o dia de HOJE, entao
    uma chamada reservada as 23:59:59 cuja devolucao acontece as 00:00:01 cai
    na chave de AMANHA, que ainda nao existe — e um `incrby` negativo CRIA a
    chave em -N. O teto do dia seguinte entao sobe por esse valor, para todo
    mundo, e nao se corrige sozinho ate a virada seguinte. Devolver o que nunca
    foi consumido tem que ser inofensivo, nao virar credito.
    """
    if unidades <= 0:
        raise ValueError(f"unidades deve ser positiva; recebeu {unidades!r}")

    chave = _chave(tipo, escopo)
    try:
        r = await _redis()
        # Com TTL tambem aqui: uma devolucao pode CRIAR a chave (a que
        # atravessa a virada do dia UTC cai na chave de amanha, que ainda nao
        # existe), e uma chave criada sem TTL fica no Redis para sempre.
        novo, _ = await _incrementar_com_ttl(r, chave, -unidades)
        if isinstance(novo, int) and novo < 0:
            await _pisar_em_zero(r, chave, novo)
    except Exception as exc:
        logger.error("metering.devolucao_falhou tipo=%s erro=%s", tipo, exc)


async def panorama(
    limites: dict[str, int], escopo: str | None = None
) -> dict[str, Any]:
    """Uso de hoje. Serve ao health check e a um selo na UI.

    Recebe os limites em vez de le-los da configuracao: cada projeto tem os
    proprios tipos de cota, e o pacote nao deve conhecer os nomes deles.

    Nunca levanta. Um health check que quebra quando o Redis cai transforma
    uma degradacao parcial em pagina fora do ar.

    UMA FORMA SO nos dois caminhos, sempre com `degraded`. Enquanto o caminho
    degradado devolvia um dicionario menor, quem lia `p["remaining"][tipo]`
    levantava KeyError exatamente quando o Redis caia — ou seja, a funcao que
    existe para sobreviver a degradacao virava o 500 que ela deveria evitar.

    Degradado, os contadores vem zerados e `degraded` avisa que eles nao valem.
    Zero e nao `limite` porque, com o Redis ilegivel, `consumir` RECUSA: dizer
    "restam 300" prometeria uma folga que o visitante nao tem.

    `escopo` le a MESMA conta que `consumir(escopo=...)` move. Sem ele, um teto
    por IP existia, recusava chamadas e nao aparecia em lugar nenhum: nem no
    health check nem num selo dizendo ao visitante quanto lhe resta. O escopo
    volta no resultado para o chamador nao confundir o relato global com o de
    alguem, ja que as duas contas sao separadas de proposito.

    Nao existe "panorama de todos os escopos": a chave de escopo e por IP, e
    varrer o Redis para enumera-los seria um SCAN de tamanho imprevisivel no
    caminho do health check. Pergunte pelo escopo que interessa.
    """
    dia = _hoje_utc()
    # Leitura viva, a mesma que o `consumir` usa: o health check nao pode dizer
    # "kill switch desligado" enquanto o `consumir` ja esta recusando por ele.
    ligado = kill_switch_ligado()

    degradado = False
    try:
        r = await _redis()
        usados = {
            tipo: int(await r.get(_chave(tipo, escopo, dia)) or 0)
            for tipo in limites
        }
    except Exception:
        logger.warning("metering.panorama_degradado", exc_info=True)
        degradado = True
        usados = dict.fromkeys(limites, 0)

    return {
        "date": dia,
        "escopo": escopo,
        "kill_switch": ligado,
        "degraded": degradado,
        "used": usados,
        "limits": limites,
        # `max(0, ...)`: o teto e checado depois do INCR, entao o contador pode
        # passar do limite por um instante. O selo publico nao mostra negativo.
        # Degradado, o restante e 0 pela mesma razao que `consumir` recusa.
        "remaining": (
            dict.fromkeys(limites, 0)
            if degradado
            else {t: max(0, limites[t] - usados[t]) for t in limites}
        ),
    }
