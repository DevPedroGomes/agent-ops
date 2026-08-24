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
  desfaz o proprio incremento.
- Se o Redis nao responde, RECUSA. Um teto ilegivel nao e um teto ausente —
  mas o tipo levantado e `TetoIndisponivel`, para o chamador separar 503 de 429.
- O kill switch e configuracao de ambiente, para estancar sem redeploy. Ele e
  parada deliberada, nao falha de backend: continua `TetoAtingido` puro.

O que mudou na extracao: a chave ganhou `projeto` (dois apps no mesmo Redis
dividiriam o teto um do outro) e `escopo` opcional (teto por IP convivendo com
o teto global sem que um consuma o do outro).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis

from agent_ops.config import get_config

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
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _chave(tipo: str, escopo: str | None = None, dia: str | None = None) -> str:
    base = f"ao:budget:{get_config().projeto}:{dia or _hoje_utc()}:{tipo}"
    return f"{base}:{escopo}" if escopo else base


async def _redis() -> aioredis.Redis:
    global _cliente
    if _cliente is None:
        _cliente = aioredis.from_url(get_config().redis_url, decode_responses=True)
    return _cliente


async def consumir(
    tipo: str,
    limite: int,
    unidades: int = 1,
    escopo: str | None = None,
) -> int:
    """Gasta `unidades` da cota de hoje. Devolve o quanto sobrou.

    Levanta `TetoAtingido` sem ter consumido nada — uma chamada recusada nao
    custa cota ao proximo visitante. Quando a recusa vem de o Redis estar
    ilegivel, o tipo e `TetoIndisponivel` (subclasse), para o chamador poder
    responder 503 em vez de 429 sem inspecionar a mensagem.
    """
    if get_config().kill_switch:
        logger.warning("metering.kill_switch_ativo tipo=%s", tipo)
        raise TetoAtingido("This demo is paused right now. Please try again later.")

    chave = _chave(tipo, escopo)
    try:
        r = await _redis()
        usado = await r.incrby(chave, unidades)
    except Exception as exc:
        # Falhou ANTES de contar: nao ha incremento para desfazer.
        logger.error("metering.estado_ilegivel tipo=%s erro=%s", tipo, exc)
        raise TetoIndisponivel("This demo is temporarily unavailable.") from exc

    if usado == unidades:
        # O TTL e faxina, nao corretude: o contador ja esta certo sem ele, e a
        # chave de amanha tem outro nome. Falhar aqui NAO pode recusar a
        # chamada — e muito menos recusar sem devolver o incremento que acabou
        # de acontecer. Esse era o bug herdado do `budget.py`: com o `expire`
        # dentro do mesmo `try` do `incrby`, uma falha de rede entre as duas
        # idas ao Redis mandava a execucao para o `except` generico, que
        # levantava TetoAtingido sem nunca alcancar o rollback logo abaixo.
        # Chamada recusada cobrando cota e exatamente a invariante que este
        # modulo existe para garantir.
        try:
            await r.expire(chave, _TTL_SEGUNDOS)
        except Exception:
            logger.warning("metering.ttl_nao_aplicado chave=%s", chave, exc_info=True)

    if usado > limite:
        # Perdeu a corrida: devolve o proprio incremento e recusa.
        try:
            await r.incrby(chave, -unidades)
        except Exception:
            logger.exception("metering.rollback_falhou tipo=%s", tipo)
        logger.warning(
            "metering.esgotado tipo=%s usado=%d limite=%d", tipo, usado, limite
        )
        raise TetoAtingido(
            "This demo reached today's usage cap. It resets at midnight UTC."
        )

    return limite - usado


async def devolver(tipo: str, unidades: int = 1, escopo: str | None = None) -> None:
    """Devolve cota quando a chamada paga nao chegou a acontecer.

    Melhor esforco: uma devolucao perdida custa um pouco de folga, um gasto nao
    registrado custa dinheiro.
    """
    try:
        r = await _redis()
        await r.incrby(_chave(tipo, escopo), -unidades)
    except Exception as exc:
        logger.error("metering.devolucao_falhou tipo=%s erro=%s", tipo, exc)


async def panorama(limites: dict[str, int]) -> dict:
    """Uso de hoje. Serve ao health check e a um selo na UI.

    Recebe os limites em vez de le-los da configuracao: cada projeto tem os
    proprios tipos de cota, e o pacote nao deve conhecer os nomes deles.

    Nunca levanta. Um health check que quebra quando o Redis cai transforma
    uma degradacao parcial em pagina fora do ar.
    """
    dia = _hoje_utc()
    ligado = get_config().kill_switch

    try:
        r = await _redis()
        usados = {
            tipo: int(await r.get(_chave(tipo, dia=dia)) or 0) for tipo in limites
        }
    except Exception:
        logger.warning("metering.panorama_degradado", exc_info=True)
        return {"date": dia, "kill_switch": ligado, "degraded": True}

    return {
        "date": dia,
        "kill_switch": ligado,
        "used": usados,
        "limits": limites,
        # `max(0, ...)`: o teto e checado depois do INCR, entao o contador pode
        # passar do limite por um instante. O selo publico nao mostra negativo.
        "remaining": {t: max(0, limites[t] - usados[t]) for t in limites},
    }
