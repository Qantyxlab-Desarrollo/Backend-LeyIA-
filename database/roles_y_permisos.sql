-- Ejecutar como postgres / superuser en el SQL Editor de Supabase
CREATE ROLE leyia_app WITH LOGIN PASSWORD '<GENERAR-CONTRASEÑA-FUERTE>';

-- Conexión
GRANT USAGE ON SCHEMA app_auth TO leyia_app;

-- Tablas
GRANT SELECT, INSERT, UPDATE, DELETE ON app_auth.tenants TO leyia_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON app_auth.users TO leyia_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON app_auth.tenant_users TO leyia_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON app_auth.tenant_connections TO leyia_app;
GRANT SELECT, INSERT ON app_auth.audit_events TO leyia_app;

-- Secuencias (para INSERTs con uuid default no hace falta, pero por si acaso)
GRANT USAGE ON ALL SEQUENCES IN SCHEMA app_auth TO leyia_app;

-- No conceder BYPASSRLS ni privilegios sobre auth.* ni sobre public.*
