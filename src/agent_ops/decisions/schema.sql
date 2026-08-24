-- A decisao do agente vira registro consultavel, nao so texto na tela.
--
-- MOTIVACAO: a cada execucao o pipeline decide bastante coisa e nada disso
-- sobrevivia ao fim do stream. O painel mostra o caminho enquanto acontece e
-- some quando a pagina recarrega. Com a decisao persistida da para responder
-- depois "por que ele decidiu isso?", comparar execucoes ao longo do tempo, e
-- achar o caso que sempre cai na rede de seguranca — que e o sinal de que
-- falta insumo, nao de que o modelo esta ruim.
--
-- METADADO, NUNCA CONTEUDO: `input_digest` e hash da entrada. O texto da
-- entrada nao entra aqui. Sem essa regra a trilha vira uma segunda copia do
-- acervo, e uma copia sem as protecoes de isolamento da tabela original.
--
-- ISOLAMENTO: `tenant_id` em toda linha. A leitura da trilha filtra por tenant,
-- nunca so por `correlation_id`.
--
-- Tipos propositalmente portateis (TEXT/INTEGER, sem UUID nem JSONB nativos):
-- o mesmo DDL roda em Postgres na producao e em SQLite nos testes, sem um
-- segundo schema para manter em sincronia.

CREATE TABLE IF NOT EXISTS decisions (
    id              TEXT PRIMARY KEY,

    -- Qual app gerou a linha. Os tres projetos podem dividir um banco.
    project         TEXT NOT NULL,
    tenant_id       TEXT NOT NULL,

    -- Amarra todas as decisoes de UMA execucao, do orquestrador aos workers.
    correlation_id  TEXT NOT NULL,

    -- Hash da entrada, nunca a entrada. Serve para deduplicar e para provar
    -- que duas execucoes viram exatamente o mesmo insumo.
    input_digest    TEXT NOT NULL,

    -- O campo que sustenta a tese: a resposta a "por que decidiu isso?" e um
    -- codigo de regra, nao um paragrafo gerado.
    rule_code       TEXT NOT NULL,

    -- O que o modelo EXTRAIU (com ponteiro para a origem) e o que a regra
    -- DECIDIU. Separados de proposito: trocar a regra re-decide o historico
    -- inteiro sem gastar um token.
    evidence        TEXT NOT NULL DEFAULT '{}',
    outcome         TEXT NOT NULL DEFAULT '{}',

    model           TEXT,
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    cost_cents      INTEGER NOT NULL DEFAULT 0,

    -- Trilha orquestrador -> worker. Nulo na raiz.
    parent_id       TEXT,

    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Listagem da trilha do tenant, do mais recente para o mais antigo.
CREATE INDEX IF NOT EXISTS idx_decisions_tenant_created
    ON decisions (tenant_id, created_at DESC);

-- Reconstruir uma execucao inteira em ordem.
CREATE INDEX IF NOT EXISTS idx_decisions_correlation
    ON decisions (correlation_id, created_at);

-- Descer do orquestrador para os workers dele.
CREATE INDEX IF NOT EXISTS idx_decisions_parent
    ON decisions (parent_id);

-- Semear o golden set do eval: agrupar por regra e achar a que sempre cai na
-- rede de seguranca.
CREATE INDEX IF NOT EXISTS idx_decisions_project_rule
    ON decisions (project, rule_code);
