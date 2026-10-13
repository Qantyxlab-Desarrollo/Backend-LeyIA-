#!/usr/bin/env python3
# scripts/migrate_dev_tenant_connection.py
import os
from dotenv import load_dotenv

from auth.config import settings
from core.secrets import save_tenant_connection

load_dotenv()

tenant_id = settings.DEV_TENANT_ID
dsn = os.getenv("DATABASE_URL") or os.getenv("DEMO_DATABASE_URL")

if not tenant_id:
    raise SystemExit("DEV_TENANT_ID no configurado")
if not dsn:
    raise SystemExit("DATABASE_URL o DEMO_DATABASE_URL no configurados")

save_tenant_connection(
    tenant_id=tenant_id,
    dsn=dsn,
    secret_ref="dev/local-database",
    provider="supabase_postgres",
)
print(f"✅ DSN cifrado y guardado para tenant {tenant_id[:8]}")
