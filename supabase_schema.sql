-- ETL Conta Azul — Schema Supabase
-- Execute no SQL Editor do Supabase (uma vez por projeto).

CREATE TABLE IF NOT EXISTS clientes (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nome        TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS uploads (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cliente_id          UUID NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    nome_arquivo        TEXT NOT NULL,
    mes_referencia      TEXT,
    qtd_linhas_origem   INTEGER,
    qtd_linhas_saida    INTEGER,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS plano_contas (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cliente_id  UUID NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    id_conta    INTEGER NOT NULL,
    nivel       INTEGER NOT NULL,
    conta_pai   INTEGER,
    ordem       INTEGER NOT NULL,
    descricao   TEXT NOT NULL,
    sinal       INTEGER NOT NULL,
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (cliente_id, id_conta)
);

CREATE TABLE IF NOT EXISTS lancamentos (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    upload_id               UUID NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
    cliente_id              UUID NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    mes_referencia          TEXT,
    data_competencia        DATE,
    descricao               TEXT,
    fornecedor_cliente      TEXT,
    categoria_origem        TEXT,
    valor                   NUMERIC(15,2),
    centro_custo            TEXT,
    valor_total_lancamento  NUMERIC(15,2),
    id_linha_origem         INTEGER,
    status_validacao        TEXT,
    observacao_etl          TEXT
);

CREATE TABLE IF NOT EXISTS inconsistencias (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    upload_id               UUID NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
    mes_referencia          TEXT,
    id_linha_origem         INTEGER,
    data_competencia        DATE,
    descricao               TEXT,
    fornecedor_cliente      TEXT,
    valor_total_lancamento  NUMERIC(15,2),
    soma_valores_gerados    NUMERIC(15,2),
    diferenca               NUMERIC(15,2),
    observacao              TEXT
);

CREATE TABLE IF NOT EXISTS resumo_conferencia (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    upload_id           UUID NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
    cliente_id          UUID NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    mes_referencia      TEXT,
    total_original      NUMERIC(15,2),
    total_etl           NUMERIC(15,2),
    diferenca           NUMERIC(15,2),
    qtd_linhas_origem   INTEGER,
    qtd_linhas_saida    INTEGER,
    qtd_inconsistencias INTEGER,
    qtd_nao_mapeadas    INTEGER,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dre_mensal (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cliente_id      UUID NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    cliente         TEXT NOT NULL,
    mes_referencia  TEXT NOT NULL,
    ordem           INTEGER NOT NULL,
    nivel           INTEGER NOT NULL,
    descricao       TEXT NOT NULL,
    valor           NUMERIC(15,2),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- mapeamentos_cliente
-- Parametrização dinâmica de categorias, gerenciada pelo frontend (Lovable).
-- Resolução no ETL: Supabase mapeamentos → plano_contas CSV → não mapeada.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS mapeamentos_cliente (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cliente_id       UUID NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    categoria_origem TEXT NOT NULL,
    id_conta         TEXT,
    descricao_conta  TEXT NOT NULL,
    nivel_1          TEXT,
    nivel_2          TEXT,
    nivel_3          TEXT,
    sinal            INTEGER NOT NULL DEFAULT 1,
    ativo            BOOLEAN NOT NULL DEFAULT true,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

-- Busca rápida por cliente + categoria
CREATE INDEX IF NOT EXISTS idx_mapeamentos_cliente_lookup
    ON mapeamentos_cliente (cliente_id, categoria_origem);

-- Garante que existe no máximo um mapeamento ativo por cliente + categoria
CREATE UNIQUE INDEX IF NOT EXISTS idx_mapeamentos_cliente_unique_ativo
    ON mapeamentos_cliente (cliente_id, categoria_origem)
    WHERE ativo = true;

-- RLS: service role bypassa; policy permissiva para leitura pública temporária
ALTER TABLE mapeamentos_cliente ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_all_mapeamentos" ON mapeamentos_cliente
    FOR ALL
    USING (true)
    WITH CHECK (true);

-- ---------------------------------------------------------------------------
-- Migration: execute no SQL Editor do Supabase se as tabelas JÁ EXISTEM.
-- (Ignorar se estiver criando do zero com o CREATE TABLE acima.)
-- ---------------------------------------------------------------------------
-- ALTER TABLE uploads          ADD COLUMN IF NOT EXISTS mes_referencia TEXT;
-- ALTER TABLE resumo_conferencia ADD COLUMN IF NOT EXISTS mes_referencia TEXT;
-- ALTER TABLE inconsistencias   ADD COLUMN IF NOT EXISTS mes_referencia TEXT;
-- -- Adicionado em 2026-05: cliente_id direto em resumo_conferencia e dre_mensal
-- ALTER TABLE resumo_conferencia
--     ADD COLUMN IF NOT EXISTS cliente_id UUID REFERENCES clientes(id) ON DELETE CASCADE;
-- ALTER TABLE dre_mensal
--     ADD COLUMN IF NOT EXISTS cliente_id UUID REFERENCES clientes(id) ON DELETE CASCADE;
-- -- Backfill resumo_conferencia.cliente_id via upload:
-- UPDATE resumo_conferencia rc
--    SET cliente_id = u.cliente_id
--   FROM uploads u WHERE u.id = rc.upload_id AND rc.cliente_id IS NULL;
-- -- Backfill dre_mensal.cliente_id via nome:
-- UPDATE dre_mensal d
--    SET cliente_id = c.id
--   FROM clientes c WHERE c.nome = d.cliente AND d.cliente_id IS NULL;
-- ---------------------------------------------------------------------------
