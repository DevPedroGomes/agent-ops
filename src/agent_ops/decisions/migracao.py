"""Aplicacao do schema da trilha.

Um `.sql` empacotado em vez de DDL em string Python: o arquivo e revisavel
como SQL, colorizado no editor, e pode ser rodado a mao contra o banco quando
alguem precisa investigar producao sem subir a app.
"""

from __future__ import annotations

from importlib import resources

from sqlalchemy import text
from sqlalchemy.engine import Engine

SQL_SCHEMA: str = (
    resources.files("agent_ops.decisions").joinpath("schema.sql").read_text("utf-8")
)


def aplicar(engine: Engine) -> None:
    """Cria tabela e indices. Idempotente: pode rodar em todo boot.

    Cada statement vai numa execucao propria porque o driver do SQLite recusa
    varios comandos num `execute()` so — e os testes rodam em SQLite.
    """
    statements = [s.strip() for s in SQL_SCHEMA.split(";") if s.strip()]
    with engine.begin() as conexao:
        for statement in statements:
            conexao.execute(text(statement))
