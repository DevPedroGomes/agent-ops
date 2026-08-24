-- Progresso do job fora da memoria do processo.
--
-- MOTIVACAO: com o progresso em memoria, um redeploy no meio de uma ingestao
-- longa apaga a barra de progresso e o cliente reconecta no SSE sem saber se o
-- trabalho morreu ou continua. Persistido, a reconexao retoma a leitura.
--
-- Por que nao guardar no Redis junto com a fila: o resultado do arq expira
-- (`keep_result`, 1h por padrao) e o progresso precisa sobreviver a isso para
-- a UI conseguir explicar um job que falhou ontem.
--
-- `descartado` e o dead-letter: esgotou as tentativas e ninguem vai tentar de
-- novo sozinho. `detalhe` carrega o motivo legivel que a UI mostra.

CREATE TABLE IF NOT EXISTS job_progress (
    job_id      TEXT PRIMARY KEY,
    estado      TEXT NOT NULL,
    percentual  INTEGER NOT NULL DEFAULT 0,
    detalhe     TEXT,
    tentativas  INTEGER NOT NULL DEFAULT 0,
    atualizado  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Varrer a dead-letter na UI de operacao.
CREATE INDEX IF NOT EXISTS idx_job_progress_estado
    ON job_progress (estado, atualizado DESC);
