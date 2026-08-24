"""Nucleo compartilhado dos agentes do portfolio.

Os tres subpacotes nao se importam entre si de proposito: quem so precisa do
teto de gasto nao carrega o resto. Na pratica, medido:

- `agent_ops.metering` — Redis apenas. Nao arrasta SQLAlchemy nem arq.
- `agent_ops.decisions` — SQLAlchemy apenas.
- `agent_ops.queue` — arq (logo, Redis) E SQLAlchemy. A fila tem duas metades e
  a de progresso duravel e uma tabela no Postgres, entao `queue/__init__.py`
  importa `execucao` junto com `fila`.

Ou seja: o isolamento vale entre os subpacotes, nao dentro do `queue`. Este
paragrafo existe porque a versao anterior dele prometia que "um projeto que so
precisa da fila nao arrasta SQLAlchemy junto" — o que nunca foi verdade, e um
docstring que mente sobre uma propriedade e pior que a ausencia dele.

Um dia da para carregar `execucao` so quando ele for tocado (`__getattr__` de
modulo, PEP 562), e ai quem so enfileira nao paga o ORM. Nao vale a indirecao
numa v0.1.0: o custo e um import, o risco e um caminho de resolucao de nome que
so quebra em producao.
"""

__version__ = "0.1.0"
