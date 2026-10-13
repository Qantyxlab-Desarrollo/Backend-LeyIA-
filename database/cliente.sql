-- En la base de datos operativa del cliente
CREATE ROLE leyia_metrics_reader
    LOGIN
    PASSWORD '<generar-contraseña-fuerte-única>';

-- Esquema semantic (vistas de métricas)
GRANT USAGE ON SCHEMA semantic TO leyia_metrics_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA semantic TO leyia_metrics_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA semantic
    GRANT SELECT ON TABLES TO leyia_metrics_reader;

-- Esquema public (tablas crudas que usa metrics_service.py)
GRANT USAGE ON SCHEMA public TO leyia_metrics_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO leyia_metrics_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO leyia_metrics_reader;

-- Asegurar que no pueda escribir ni cambiar esquema
ALTER ROLE leyia_metrics_reader SET default_transaction_read_only = on;
