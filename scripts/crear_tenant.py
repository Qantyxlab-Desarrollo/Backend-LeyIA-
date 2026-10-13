#!/usr/bin/env python3
# scripts/crear_tenant.py
"""
Crea un tenant manualmente usando service_role.
Maneja usuario ya existente en Auth central (recovery link).
"""

import getpass
import os
import re
import sys

import httpx

load_dotenv_done = False
try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv_done = True
except ImportError:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth.config import settings
from auth.db import db_cursor
from core.secrets import get_secret_provider

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")


def _normalize_dsn(dsn: str) -> str:
    dsn = dsn.strip().strip('"').strip("'")
    if dsn.startswith("postgres://"):
        dsn = dsn.replace("postgres://", "postgresql://", 1)
    return dsn


def _mask_dsn(dsn: str) -> str:
    try:
        rest = dsn.split("://", 1)[1]
        user_part = rest.split("@", 1)[0]
        if ":" in user_part:
            pwd = user_part.split(":", 1)[1]
            return dsn.replace(pwd, "****")
    except Exception:
        pass
    return dsn[:55] + "..."


def _verify_dsn(dsn: str) -> dict:
    import psycopg2
    try:
        conn = psycopg2.connect(dsn, connect_timeout=5)
    except psycopg2.OperationalError as e:
        raise RuntimeError(f"No se pudo conectar: {e}") from e

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = 'semantic')"
            )
            has_semantic = cur.fetchone()[0]
    finally:
        conn.close()
    return {"connection": "ok", "semantic_schema": bool(has_semantic)}


def _get_existing_user_id(supabase_url: str, service_role: str, email: str) -> str:
    r = httpx.get(
        f"{supabase_url}/auth/v1/admin/users",
        headers={"apikey": service_role, "Authorization": f"Bearer {service_role}"},
        params={"page": "1", "perPage": "100"},
        timeout=15,
    )
    r.raise_for_status()
    for u in r.json().get("users", []):
        if (u.get("email") or "").lower() == email.lower():
            return u["id"]
    raise RuntimeError(f"Usuario {email} reportado como existente pero no encontrado en lista")


def main() -> None:
    if not load_dotenv_done:
        print("⚠️  dotenv no disponible")
        return

    service_role = settings.CENTRAL_SUPABASE_SERVICE_ROLE_KEY
    if not service_role:
        raise SystemExit("❌ CENTRAL_SUPABASE_SERVICE_ROLE_KEY no configurada")
    supabase_url = settings.CENTRAL_SUPABASE_URL.rstrip("/")

    nombre = input("Nombre del estudio: ").strip()
    slug_raw = input("Slug (solo minúsculas, números y guiones): ").strip()
    owner_email = input("Email del dueño: ").strip().lower()
    owner_name = input("Nombre del dueño: ").strip()
    dsn_raw = getpass.getpass("DSN de solo lectura: ").strip()

    dsn = _normalize_dsn(dsn_raw)

    print("\n📋 Resumen:")
    print(f"   Nombre: {nombre}")
    print(f"   Slug:   {slug_raw}")
    print(f"   Email:  {owner_email}")
    print(f"   DSN:    {_mask_dsn(dsn)}")

    slug = slug_raw.lower().replace(" ", "-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    if not _SLUG_RE.match(slug):
        print(f"\n❌ Slug inválido: '{slug_raw}' → '{slug}'")
        return
    print(f"   Slug normalizado: {slug}")

    print("\n🔌 Verificando conexión al DSN...")
    check = _verify_dsn(dsn)
    print(f"   ✅ OK: {check}")

    # Verificar que no exista el tenant
    with db_cursor() as cur:
        cur.execute("SELECT id FROM app_auth.tenants WHERE slug = %s", (slug,))
        if cur.fetchone():
            raise SystemExit(f"❌ Ya existe un tenant con slug '{slug}'")

    # ── Crear/invitar usuario en Supabase Auth central ──────────
    print("\n📧 Procesando usuario en Supabase Auth central...")
    action_link = None
    user_id = None
    email_verified = False

    r = httpx.post(
        f"{supabase_url}/auth/v1/invite",
        headers={"apikey": service_role, "Authorization": f"Bearer {service_role}"},
        json={
            "email": owner_email,
            "data": {"full_name": owner_name},
            "redirect_to": f"{settings.FRONTEND_URL}/definir-password",
        },
        timeout=15,
    )

    if r.status_code == 200:
        user_id = r.json()["id"]
        print(f"   ✅ Usuario invitado: {user_id}")
    elif r.status_code == 422 and "email_exists" in r.text:
        print(f"   ⚠️  Email ya registrado. Recuperando usuario...")
        user_id = _get_existing_user_id(supabase_url, service_role, owner_email)
        print(f"   ✅ Usuario existente: {user_id}")

        print("   🔄 Generando recovery link...")
        r2 = httpx.post(
            f"{supabase_url}/auth/v1/admin/generate_link",
            headers={"apikey": service_role, "Authorization": f"Bearer {service_role}"},
            json={
                "type": "recovery",
                "email": owner_email,
                "redirect_to": f"{settings.FRONTEND_URL}/definir-password",
            },
            timeout=15,
        )
        if r2.status_code >= 400:
            raise SystemExit(f"❌ Error generando recovery link: {r2.status_code} {r2.text[:300]}")
        link_data = r2.json()
        action_link = link_data.get("action_link")
        email_verified = bool(link_data.get("email_confirmed_at"))
        print(f"   ✅ Recovery link generado")
    else:
        raise SystemExit(f"❌ Error invitando: {r.status_code} {r.text[:300]}")

    # ── Guardar en la base central ──────────────────────────────
    print("\n💾 Guardando tenant y vinculación...")
    with db_cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO app_auth.tenants (nombre, slug, status, is_active) VALUES (%s, %s, 'active', true) RETURNING id",
            (nombre, slug),
        )
        tenant_id = str(cur.fetchone()["id"])

        encrypted = get_secret_provider().encrypt(dsn)
        cur.execute(
            """
            INSERT INTO app_auth.tenant_connections
                (tenant_id, provider, encrypted_dsn, secret_ref, status, last_verified_at)
            VALUES (%s, 'supabase_postgres', %s, %s, 'active', NOW())
            """,
            (tenant_id, encrypted, f"production/tenants/{tenant_id}/metrics-db"),
        )

        cur.execute(
            """
            INSERT INTO app_auth.users (id, email, full_name, role, email_verified)
            VALUES (%s, %s, %s, 'user', %s)
            ON CONFLICT (id) DO UPDATE SET
                email = EXCLUDED.email,
                full_name = COALESCE(EXCLUDED.full_name, app_auth.users.full_name),
                role = COALESCE(app_auth.users.role, EXCLUDED.role),
                email_verified = EXCLUDED.email_verified,
                updated_at = NOW()
            """,
            (user_id, owner_email, owner_name, email_verified),
        )

        cur.execute(
            """
            INSERT INTO app_auth.tenant_users (user_id, tenant_id, role, status)
            VALUES (%s, %s, 'owner', 'active')
            ON CONFLICT (user_id, tenant_id)
            DO UPDATE SET role = 'owner', status = 'active', updated_at = NOW()
            """,
            (user_id, tenant_id),
        )

    print(f"\n🎉 Tenant '{slug}' creado: {tenant_id}")
    print(f"   ✅ Owner '{owner_email}' vinculado")

    if action_link:
        print(f"\n   ⬇️  ENVÍA ESTE LINK A MANZZO (WhatsApp/email) para que defina su contraseña:")
        print(f"   {action_link}")
    else:
        print(f"\n   📧 Invitación enviada a {owner_email}")

    print(f"\n   🔍 DSN check: {check}")


if __name__ == "__main__":
    main()
