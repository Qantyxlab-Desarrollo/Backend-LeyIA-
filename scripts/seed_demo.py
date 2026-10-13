#!/usr/bin/env python3
# scripts/seed_demo.py
"""
Crea el tenant demo en la base CENTRAL y registra su conexión operativa
(el DATABASE_URL legacy) cifrada en app_auth.tenant_connections.

Idempotente: puede ejecutarse varias veces sin romper nada.
Uso: python scripts/seed_demo.py
"""

import os
import sys
from uuid import UUID

from dotenv import load_dotenv

load_dotenv()

# Permite ejecutar desde cualquier cwd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth.config import settings          # noqa: E402
from auth.db import db_cursor             # noqa: E402
from core.secrets import save_tenant_connection, get_secret_provider  # noqa: E402


def main() -> None:
    # ── 0. Validaciones ─────────────────────────────────────────────
    if not settings.DEV_TENANT_ID:
        raise SystemExit("❌ DEV_TENANT_ID no configurado en .env")
    try:
        tenant_id = str(UUID(settings.DEV_TENANT_ID))
    except ValueError:
        raise SystemExit("❌ DEV_TENANT_ID no es un UUID válido")

    if not settings.TENANT_SECRETS_KEY:
        raise SystemExit(
            "❌ TENANT_SECRETS_KEY no configurada. "
            "Generar: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )

    legacy_dsn = os.getenv("DATABASE_URL") or os.getenv("DEMO_DATABASE_URL")

    if legacy_dsn and legacy_dsn.strip() == (settings.CENTRAL_DATABASE_URL or "").strip():
        print("⚠️  DATABASE_URL == CENTRAL_DATABASE_URL: apuntarías el tenant demo")
        print("    a la base CENTRAL. Funciona para la demo, pero no es la arquitectura")
        print("    objetivo (central ≠ operativa del cliente).\n")

    # ── 1. Tenant demo ──────────────────────────────────────────────
    with db_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO app_auth.tenants (id, nombre, slug, status, is_active, config_json)
            VALUES (%s, %s, %s, 'active', true, %s::jsonb)
            ON CONFLICT (id) DO UPDATE SET
                nombre    = EXCLUDED.nombre,
                slug      = EXCLUDED.slug,
                status    = 'active',
                is_active = true
            """,
            (
                tenant_id,
                "Estudio Demo",
                "demo",
                '{"firm_name": "LeyIA Demo", "agent_name": "LeyIA"}',
            ),
        )
    print(f"✅ Tenant demo listo: {tenant_id}")

    # ── 2. Conexión operativa cifrada ───────────────────────────────
    if legacy_dsn:
        save_tenant_connection(
            tenant_id=tenant_id,
            dsn=legacy_dsn,
            provider="supabase_postgres",
            secret_ref="dev/demo-database",
        )
        print("✅ DSN operativo cifrado y guardado en app_auth.tenant_connections")
    else:
        print("⚠️  No hay DATABASE_URL/DEMO_DATABASE_URL: tenant sin conexión.")

    # ── 3. Verificación ─────────────────────────────────────────────
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT t.slug, t.status, tc.status AS conn_status,
                   left(tc.encrypted_dsn, 14) || '...' AS dsn_cifrado_preview
            FROM app_auth.tenants t
            LEFT JOIN app_auth.tenant_connections tc ON tc.tenant_id = t.id
            WHERE t.id = %s
            """,
            (tenant_id,),
        )
        row = cur.fetchone()
        print("\n📋 Estado en la central:")
        for k, v in dict(row).items():
            print(f"   {k}: {v}")

    # Roundtrip de cifrado: lo guardado debe descifrar al DSN original
    if legacy_dsn:
        with db_cursor() as cur:
            cur.execute(
                "SELECT encrypted_dsn FROM app_auth.tenant_connections WHERE tenant_id = %s",
                (tenant_id,),
            )
            enc = cur.fetchone()
        plain = get_secret_provider().decrypt(enc["encrypted_dsn"])
        assert plain == legacy_dsn, "❌ El DSN descifrado no coincide con el original"
        print("🔐 Roundtrip de cifrado OK (encrypt → DB → decrypt coincide)")

    print("\n🎉 Seed completado. Siguiente: uvicorn + curl /api/dashboard/status")


if __name__ == "__main__":
    main()
