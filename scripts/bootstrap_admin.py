#!/usr/bin/env python3
# scripts/bootstrap_admin.py
"""
Crea el PRIMER usuario staff de LeyIA en Supabase Auth central
y lo registra en app_auth.users + app_auth.tenant_users (owner del demo).
Se ejecuta UNA sola vez por proyecto.
"""

import getpass
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth.config import settings
from auth.db import db_cursor


def main() -> None:
    email = input("Email del admin LeyIA: ").strip().lower()
    full_name = input("Nombre completo: ").strip()
    password = getpass.getpass("Contraseña temporal (mínimo 10 caracteres): ")

    if len(password) < 10:
        raise SystemExit("❌ La contraseña debe tener al menos 10 caracteres.")

    key = settings.CENTRAL_SUPABASE_SERVICE_ROLE_KEY
    if not key:
        raise SystemExit("❌ CENTRAL_SUPABASE_SERVICE_ROLE_KEY no está configurada.")

    supabase_url = settings.CENTRAL_SUPABASE_URL.rstrip("/")

    # 1. Crear usuario en Supabase Auth central (Admin API)
    r = httpx.post(
        f"{supabase_url}/auth/v1/admin/users",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        json={
            "email": email,
            "password": password,
            "email_confirm": True,
            "user_metadata": {"full_name": full_name},
        },
        timeout=15,
    )

    if r.status_code == 422:
        raise SystemExit("❌ Email inválido o contraseña muy débil.")
    if r.status_code >= 400:
        raise SystemExit(f"❌ Supabase respondió {r.status_code}: {r.text[:300]}")

    user_id = r.json()["id"]
    print(f"✅ Usuario creado en Supabase Auth central: {user_id}")

    # 2. Espejo local como admin de plataforma + owner del tenant demo
    with db_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO app_auth.users (id, email, full_name, role, email_verified)
            VALUES (%s, %s, %s, 'admin', true)
            ON CONFLICT (id) DO UPDATE SET
                email = EXCLUDED.email,
                full_name = COALESCE(EXCLUDED.full_name, app_auth.users.full_name),
                role = 'admin',
                email_verified = true,
                updated_at = NOW()
            """,
            (user_id, email, full_name),
        )

        if settings.DEV_TENANT_ID:
            cur.execute(
                """
                INSERT INTO app_auth.tenant_users (user_id, tenant_id, role, status)
                VALUES (%s, %s, 'owner', 'active')
                ON CONFLICT (user_id, tenant_id)
                DO UPDATE SET role = 'owner', status = 'active', updated_at = NOW()
                """,
                (user_id, settings.DEV_TENANT_ID),
            )

    print("✅ Espejo local creado:")
    print("   - app_auth.users.role = 'admin' (plataforma)")
    if settings.DEV_TENANT_ID:
        print("   - app_auth.tenant_users.role = 'owner' (tenant demo)")
    print("\n🎉 Bootstrap completo. Puedes hacer login con:")
    print(f"   Email:    {email}")
    print(f"   Password: {'*' * len(password)}")
    print(f"\n   Endpoint: {supabase_url}/auth/v1/token?grant_type=password")


if __name__ == "__main__":
    main()
