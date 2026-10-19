# backend/scripts/diagnose_tenant_db.py
import asyncio
import asyncpg
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).parent.parent.absolute()
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv
load_dotenv(BACKEND_DIR / ".env")

from auth.db import db_cursor
from core.secrets import get_secret_provider


TENANT_ID = "f569a82a-067d-4c43-895a-dd9aaff200f8"


def get_dsn():
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT tc.encrypted_dsn, t.slug
            FROM app_auth.tenant_connections tc
            JOIN app_auth.tenants t ON t.id = tc.tenant_id
            WHERE tc.tenant_id = %s
            """,
            (TENANT_ID,),
        )
        row = cur.fetchone()
    if not row:
        raise SystemExit("No hay conexión para este tenant")
    sp = get_secret_provider()
    return row["slug"], sp.decrypt(row["encrypted_dsn"])


async def main():
    slug, dsn = get_dsn()
    print(f"\n🔑 Tenant: {slug}")
    print(f"🔗 DSN completo: {dsn}\n")

    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        info = await conn.fetchrow(
            "SELECT current_database(), current_user, inet_server_addr(), inet_server_port()"
        )
        print("📍 Conectado a:", dict(info))

        tables = await conn.fetch(
            "SELECT schemaname, tablename FROM pg_tables WHERE tablename = 'bookings'"
        )
        print("\n📋 Tablas 'bookings' encontradas:")
        for t in tables:
            print(f"   - {t['schemaname']}.{t['tablename']}")

        if tables:
            count = await conn.fetchrow("SELECT COUNT(*)::int AS n FROM public.bookings")
            print(f"\n🔢 Total filas en public.bookings: {count['n']}")

            sample = await conn.fetch("SELECT id, fecha, modalidad, inicio_cita FROM public.bookings LIMIT 5")
            print("\n📝 Muestra:")
            for r in sample:
                print(f"   {dict(r)}")
        else:
            print("\n❌ No existe public.bookings en esta base de datos")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
