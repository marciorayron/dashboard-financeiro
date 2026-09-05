-- =====================================================================
-- schema.sql
-- Dashboard Financeiro Pessoal
--
-- Modelo de dados relacional (SQLite) com:
--   * Multi-tenant:  `user_id` em todas as tabelas core com FK.
--   * Multi-empresa: `company_name`/`company_tax_id` por holerite.
--   * Rubricas itemizadas por documento.
--   * Índices compostos para acelerar consultas por usuário.
-- =====================================================================

-- ---------------------------------------------------------------------
-- USERS
-- Identifica cada usuário da aplicação (até 10 iniciais).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    email         TEXT    NOT NULL UNIQUE,
    password_hash TEXT,                 -- opcional; autenticação futura
    role          TEXT    NOT NULL DEFAULT 'user'
                         CHECK (role IN ('admin', 'user')),
    plan          TEXT    NOT NULL DEFAULT 'free'
                         CHECK (plan IN ('free', 'pro')),
    is_active     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------
-- AI_USAGE_LOGS
-- Log de auditoria de chamadas à IA (DeepSeek) por usuário, usado para
-- aplicar o controle de consumo (rate limiting) do modelo Freemium:
--   * Plano Gratuito  -> máx. 5 consultas por janela de 24h.
--   * Plano Pró       -> máx. 15 consultas por janela de 1h.
-- Cada linha representa UMA chamada válida a `/api/analytics/ask-ai` ou
-- `/api/analytics/explain-anomaly`.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_usage_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_user_created
    ON ai_usage_logs (user_id, created_at);

-- ---------------------------------------------------------------------
-- AI_BLOCKED_LOGS
-- Auditoria de tentativas de prompt injection / entradas inválidas
-- rejeitadas pelo `ai_service`. Alimenta a governança de IA (FinOps)
-- no painel administrativo.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_blocked_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reason     TEXT    NOT NULL,
    detail     TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ai_blocked_user_created
    ON ai_blocked_logs (user_id, created_at);

-- ---------------------------------------------------------------------
-- HOLERITES
-- Documento financeiro (contracheque) de um usuário.
--
--  * `company_name` e `company_tax_id` são extraídos dinamicamente do PDF,
--    suportando qualquer empregador (ex.: Lear Corporation).
--  * `totals` e `bases` guardam valores agregados em JSON para consulta
--    rápida sem precisar reprocessar as rubricas.
--  * `tipo_documento` distingue, ex., 'holerite', 'férias', '13º'.
--  * `mes_referencia` identifica o mês/ano de competência (ex.: 2024-06).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS holerites (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    company_name    TEXT    NOT NULL,
    company_tax_id  TEXT,                          -- CNPJ/CPF do empregador
    funcionario     TEXT,                          -- nome do funcionário
    mes_referencia  TEXT,                          -- ex.: '2024-06'
    tipo_documento  TEXT    NOT NULL DEFAULT 'holerite',
    -- JSON com totais (ex.: {"salario_bruto": ..., "descontos": ...})
    totals          TEXT,
    -- JSON com bases de cálculo (INSS, IRRF, FGTS, etc.)
    bases           TEXT,
    file_path       TEXT,                          -- caminho do PDF original
    file_hash       TEXT,                          -- hash para evitar duplicados
    raw_text        TEXT,                          -- texto bruto extraído do PDF
    parsed_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Índices compostos: isolamento + buscas comuns por usuário.
CREATE INDEX IF NOT EXISTS idx_holerites_user_company
    ON holerites (user_id, company_name);
CREATE INDEX IF NOT EXISTS idx_holerites_user_mes
    ON holerites (user_id, mes_referencia);
CREATE INDEX IF NOT EXISTS idx_holerites_user_tipo
    ON holerites (user_id, tipo_documento);

-- ---------------------------------------------------------------------
-- RUBRICAS_HOLERITE
-- Linhas itemizadas de cada holerite (verbas e descontos).
--   * `codigo`:    código da rubrica no contracheque.
--   * `descricao`: ex.: "Salário Base", "INSS", "Vale Transporte".
--   * `tipo`:      'provento' (provento) | 'desconto' (dedução).
--   * `valor`:     valor monetário da rubrica.
--   * `referencia`: opcional, ex.: "220:00:00" (horas) ou "30 DIAS".
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rubricas_holerite (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    holerite_id  INTEGER NOT NULL REFERENCES holerites(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    codigo       TEXT,
    descricao    TEXT    NOT NULL,
    tipo         TEXT    NOT NULL CHECK (tipo IN ('provento', 'desconto')),
    valor        REAL    NOT NULL DEFAULT 0,
    referencia   TEXT,                            -- opcional
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Índices para agregação por usuário e por documento.
CREATE INDEX IF NOT EXISTS idx_rubricas_user_tipo
    ON rubricas_holerite (user_id, tipo);
CREATE INDEX IF NOT EXISTS idx_rubricas_holerite
    ON rubricas_holerite (holerite_id);
CREATE INDEX IF NOT EXISTS idx_rubricas_user_descricao
    ON rubricas_holerite (user_id, descricao);

-- ---------------------------------------------------------------------
-- CATALOGO_RUBRICAS
-- Catálogo oficial de rubricas (ex.: Lear Corporation). Fonte de verdade
-- para classificação determinística PROVENTO/DESCONTO, independentemente
-- de extração por LLM, regex ou posição de coluna.
--   * `codigo`:    código oficial da rubrica (chave primária).
--   * `descricao`: descrição oficial (referência).
--   * `tipo`:      'PROVENTO' | 'DESCONTO'.
-- Populado no startup (ver `db_service.init_rubrica_catalog`).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS catalogo_rubricas (
    codigo    VARCHAR(20) PRIMARY KEY,
    descricao VARCHAR(255) NOT NULL,
    tipo      VARCHAR(10)  NOT NULL CHECK (tipo IN ('PROVENTO', 'DESCONTO'))
);

-- ---------------------------------------------------------------------
-- USER_PROFILES
-- Perfil financeiro do usuário (dados de contratação/benefícios) usado
-- para métricas personalizadas (ex.: salário líquido recorrente teórico).
-- Populado via `models/profile.py` e `routes/profile.py`.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id                  INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    admission_date           TEXT,
    job_title                TEXT,
    contract_type            TEXT NOT NULL DEFAULT 'HORISTA'
                             CHECK (contract_type IN ('HORISTA', 'MENSALISTA')),
    base_rate                REAL NOT NULL DEFAULT 0.0,
    monthly_hours            REAL NOT NULL DEFAULT 220.0,
    irrf_dependents          INTEGER NOT NULL DEFAULT 0,
    fixed_benefits_deduction REAL NOT NULL DEFAULT 0.0,
    overtime_tier1_rate      REAL NOT NULL DEFAULT 1.70,
    overtime_tier1_limit     REAL NOT NULL DEFAULT 30,
    overtime_tier2_rate      REAL NOT NULL DEFAULT 2.00,
    updated_at               TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------
-- USER_PROFILE_HISTORY
-- Histórico de revisões do perfil de contratação (versionamento). Cada
-- linha representa o snapshot dos termos de contrato vigentes a partir de
-- `effective_date`. Permite ao analytics resolver a taxa de pagamento ativa
-- na data de cada paystub, em vez de usar sempre o valor atual do perfil.
-- Populado automaticamente por `models/profile.save_profile`.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_profile_history (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    effective_date           TEXT    NOT NULL DEFAULT (date('now')),
    job_title                TEXT,
    contract_type            TEXT    NOT NULL DEFAULT 'HORISTA',
    base_rate                REAL    NOT NULL DEFAULT 0.0,
    monthly_hours            REAL    NOT NULL DEFAULT 220.0,
    irrf_dependents          INTEGER NOT NULL DEFAULT 0,
    fixed_benefits_deduction REAL    NOT NULL DEFAULT 0.0,
    created_at               TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_profile_history_user_date
    ON user_profile_history (user_id, effective_date);

-- ---------------------------------------------------------------------
-- APP_SETTINGS
-- Configurações globais editáveis pelo painel administrativo (sem deploy):
-- faixas progressivas de INSS/IRRF, dedução por dependente, etc. O valor é
-- armazenado em JSON na coluna `value` (chaveado por `key`).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------
-- PARSE_ERRORS
-- Log de auditoria de erros de parsing de PDFs. Permite ao administrador
-- visualizar/uploads quebrados com contexto do usuário, timestamp e
-- detalhes da exceção (mensagem + traceback) e marcá-los como resolvidos.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS parse_errors (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    filename       TEXT,
    error_type     TEXT,
    error_message  TEXT,
    traceback      TEXT,
    resolved       INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    resolved_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_parse_errors_user
    ON parse_errors (user_id);
CREATE INDEX IF NOT EXISTS idx_parse_errors_unresolved
    ON parse_errors (resolved, created_at);
