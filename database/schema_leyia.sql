-- ============================================================================
-- ESQUEMA CENTRAL LEYIA
-- ============================================================================
CREATE SCHEMA IF NOT EXISTS app_auth;

-- ============================================================================
-- 1. TENANTS — cada estudio jurídico cliente
-- ============================================================================
CREATE TABLE IF NOT EXISTS app_auth.tenants (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    nombre          text NOT NULL,
    slug            text NOT NULL UNIQUE,
    status          text NOT NULL DEFAULT 'provisioning'
        CHECK (status IN ('active', 'suspended', 'provisioning')),
    is_active       boolean NOT NULL DEFAULT true,
    config_json     jsonb DEFAULT '{}',      -- branding, tono, áreas legales, etc.
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tenants_slug
    ON app_auth.tenants (slug);

COMMENT ON TABLE app_auth.tenants IS
'Una fila por estudio jurídico. La fuente de verdad del tenant_id es esta tabla, no el navegador.';

-- ============================================================================
-- 2. USERS — espejo local de identidades Supabase Auth
-- ============================================================================
-- NO almacena contraseñas. Las credenciales viven exclusivamente en auth.users.
-- Esta tabla decide SI un usuario autenticado puede entrar a LeyIA (registro cerrado).
CREATE TABLE IF NOT EXISTS app_auth.users (
    id              uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE RESTRICT,
    email           text NOT NULL,
    full_name       text,
    role            text NOT NULL DEFAULT 'user'
        CHECK (role IN ('admin', 'user')),
    is_active       boolean NOT NULL DEFAULT true,
    email_verified  boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email
    ON app_auth.users (lower(email));

COMMENT ON TABLE app_auth.users IS
'Espejo local de Supabase Auth. No almacena contraseñas. Los usuarios deben ser provisionados por invitación.';

-- ============================================================================
-- 3. TENANT_USERS — membresías (qué usuario entra a qué tenant y con qué rol)
-- ============================================================================
CREATE TABLE IF NOT EXISTS app_auth.tenant_users (
    user_id     uuid NOT NULL REFERENCES app_auth.users(id) ON DELETE RESTRICT,
    tenant_id   uuid NOT NULL REFERENCES app_auth.tenants(id) ON DELETE RESTRICT,
    role        text NOT NULL DEFAULT 'viewer'
        CHECK (role IN ('owner', 'admin', 'viewer')),
    status      text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'revoked')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, tenant_id)
);

-- v1: un usuario solo puede tener UNA membresía activa a la vez.
CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_users_one_active
    ON app_auth.tenant_users (user_id) WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_tenant_users_tenant
    ON app_auth.tenant_users (tenant_id);

COMMENT ON TABLE app_auth.tenant_users IS
'Relación usuario-tenant. La membresía activa determina a qué base de datos operativa accede el usuario.';

-- ============================================================================
-- 4. TENANT_CONNECTIONS — DSN cifrado a la base operativa del cliente
-- ============================================================================
CREATE TABLE IF NOT EXISTS app_auth.tenant_connections (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL UNIQUE REFERENCES app_auth.tenants(id) ON DELETE RESTRICT,
    provider        text NOT NULL DEFAULT 'supabase_postgres'
        CHECK (provider IN ('supabase_postgres', 'postgres')),
    project_ref     text,
    encrypted_dsn   text NOT NULL,           -- cifrado con Fernet (TENANT_SECRETS_KEY)
    secret_ref      text,                    -- referencia humana/auditoría
    status          text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('active', 'pending', 'disabled', 'error')),
    last_verified_at timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tenant_connections_tenant
    ON app_auth.tenant_connections (tenant_id);

COMMENT ON TABLE app_auth.tenant_connections IS
'Referencia cifrada a la base de datos operativa de cada cliente. El backend descifra el DSN en memoria.';

-- ============================================================================
-- 5. AUDIT_EVENTS — registro de accesos y operaciones administrativas
-- ============================================================================
CREATE TABLE IF NOT EXISTS app_auth.audit_events (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_user_id   uuid REFERENCES app_auth.users(id) ON DELETE SET NULL,
    tenant_id       uuid REFERENCES app_auth.tenants(id) ON DELETE SET NULL,
    event_type      text NOT NULL,
    request_id      text,
    success         boolean NOT NULL,
    metadata_json   jsonb DEFAULT '{}',      -- sin tokens, contraseñas ni mensajes privados
    ip_address      inet,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_tenant_created
    ON app_auth.audit_events (tenant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_actor_created
    ON app_auth.audit_events (actor_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_event_type
    ON app_auth.audit_events (event_type, created_at DESC);

COMMENT ON TABLE app_auth.audit_events IS
'Auditoría de login, consultas de métricas, cambios administrativos y fallos de conexión. No almacenar tokens ni datos sensibles.';

-- ============================================================================
-- RLS: activar en todas las tablas centrales
-- ============================================================================
ALTER TABLE app_auth.tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_auth.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_auth.tenant_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_auth.tenant_connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_auth.audit_events ENABLE ROW LEVEL SECURITY;

-- ============================================================================
-- TRIGGER: updated_at automático
-- ============================================================================
CREATE OR REPLACE FUNCTION app_auth.set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_tenants_updated_at') THEN
        CREATE TRIGGER trg_tenants_updated_at BEFORE UPDATE ON app_auth.tenants
        FOR EACH ROW EXECUTE FUNCTION app_auth.set_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_users_updated_at') THEN
        CREATE TRIGGER trg_users_updated_at BEFORE UPDATE ON app_auth.users
        FOR EACH ROW EXECUTE FUNCTION app_auth.set_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_tenant_users_updated_at') THEN
        CREATE TRIGGER trg_tenant_users_updated_at BEFORE UPDATE ON app_auth.tenant_users
        FOR EACH ROW EXECUTE FUNCTION app_auth.set_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_tenant_connections_updated_at') THEN
        CREATE TRIGGER trg_tenant_connections_updated_at BEFORE UPDATE ON app_auth.tenant_connections
        FOR EACH ROW EXECUTE FUNCTION app_auth.set_updated_at();
    END IF;
END $$;
