"""Um relogio so para o pacote inteiro.

Existe porque o default do banco NAO serve. `CURRENT_TIMESTAMP` gravado numa
coluna sem fuso e resolvido pelo banco: o SQLite devolve sempre UTC, o Postgres
devolve a hora do fuso DA SESSAO. Com o mesmo DDL nos dois, um banco de
producao configurado em `America/Sao_Paulo` guarda hora local enquanto a suite
de teste, em SQLite, guarda UTC. Nada disso levanta erro, e nenhum teste em
SQLite consegue ver a diferenca.

O estrago e concreto: achar job travado compara `atualizado` com o UTC de
agora, e o dia da cota vira a meia-noite UTC. Um offset de tres horas faz um
job parado ha muito tempo parecer recem-atualizado e desalinha a trilha do
periodo de cobranca.

A gravacao passa a carimbar aqui, e o carimbo e INGENUO (sem `tzinfo`) porque
as colunas sao `TIMESTAMP` sem fuso nos dois bancos. Um datetime com fuso
mandado para uma coluna dessas volta a ser convertido pelo Postgres, que e
exatamente o problema de novo. Ingenuo e em UTC, os digitos gravados sao os
mesmos nos dois bancos e a leitura nao precisa saber de onde veio.

O default no DDL continua la de proposito, para quem roda INSERT na mao contra
o banco. Ele erra do mesmo jeito, so que agora ninguem do pacote depende dele.
"""

from __future__ import annotations

from datetime import UTC, datetime


def agora_utc() -> datetime:
    """UTC de agora, sem `tzinfo`. Ver o docstring do modulo."""
    return datetime.now(UTC).replace(tzinfo=None)
