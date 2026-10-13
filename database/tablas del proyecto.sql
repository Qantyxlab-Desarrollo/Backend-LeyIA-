-- En el proyecto Supabase CENTRAL de LeyIA
CREATE TABLE IF NOT EXISTS app_auth.tenant_connections (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL UNIQUE REFERENCES app_auth.tenants(id) ON DELETE RESTRICT,
    provider        text NOT NULL DEFAULT 'supabase_postgres',
    project_ref     text,
    encrypted_dsn   text NOT NULL,           -- DSN cifrado con Fernet
    secret_ref      text,                    -- referencia humana/auditoría
    status          text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('active', 'pending', 'disabled', 'error')),
    last_verified_at timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tenant_connections_tenant
    ON app_auth.tenant_connections (tenant_id);

ALTER TABLE app_auth.tenant_connections ENABLE ROW LEVEL SECURITY;

-- En cada base de datos OPERATIVA de cliente (NO en la central)
CREATE ROLE leyia_metrics_reader
    LOGIN
    PASSWORD '<generar-contraseña-fuerte>';

GRANT USAGE ON SCHEMA semantic TO leyia_metrics_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA semantic TO leyia_metrics_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA semantic
    GRANT SELECT ON TABLES TO leyia_metrics_reader;

GRANT USAGE ON SCHEMA public TO leyia_metrics_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO leyia_metrics_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO leyia_metrics_reader;
