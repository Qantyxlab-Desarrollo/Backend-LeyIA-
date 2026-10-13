-- Servidor backend: leyia_app puede leer y escribir todo
CREATE POLICY policy_app_all ON app_auth.tenants
    FOR ALL TO leyia_app USING (true) WITH CHECK (true);

CREATE POLICY policy_app_all ON app_auth.users
    FOR ALL TO leyia_app USING (true) WITH CHECK (true);

CREATE POLICY policy_app_all ON app_auth.tenant_users
    FOR ALL TO leyia_app USING (true) WITH CHECK (true);

CREATE POLICY policy_app_all ON app_auth.tenant_connections
    FOR ALL TO leyia_app USING (true) WITH CHECK (true);

CREATE POLICY policy_app_insert_select ON app_auth.audit_events
    FOR ALL TO leyia_app USING (true) WITH CHECK (true);
