# routes/admin_routes.py
# Endpoints de provisión de LeyIA — SOLO staff de plataforma (users.role='admin').
#
# - POST /api/admin/tenants   → crea tenant + guarda DSN cifrado + invita owner
# - POST /api/admin/users     → invita usuario adicional a un tenant existente
#
# Todas las operaciones escriben en la DB CENTRAL mediante auth.db (rol leyia_app)
# y hablan con Supabase Auth Admin API vía CENTRAL_SUPABASE_SERVICE_ROLE_KEY.

import re
from typing import Literal, Optional

import httpx
import psycopg2
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr

from auth.config import settings
from auth.db import db_cursor, get_user_by_email
from auth.dependencies import get_current_user
from auth.models import User
from core.secrets import save_tenant_connection

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ============================================================================
# DEPENDENCIA: solo staff LeyIA
# ============================================================================

def require_platform_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Solo staff LeyIA")
    return user


# ============================================================================
# HELPERS
# ============================================================================

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")


def _auth_admin_headers() -> dict:
    key = settings.CENTRAL_SUPABASE_SERVICE_ROLE_KEY
    if not key:
        raise HTTPException(500, "CENTRAL_SUPABASE_SERVICE_ROLE_KEY no configurada")
    return {"apikey": key, "Authorization": f"Bearer {key}"}


def _verify_dsn(dsn: str) -> dict:
    """
    Prueba el DSN del cliente ANTES de guardarlo.
    - Conexión obligatoria (si falla → 400 y no se crea nada).
    - Schema `semantic` recomendado (si falta → warning, no bloquea).
    """
    if dsn.startswith("postgres://"):
        dsn = dsn.replace("postgres://", "postgresql://", 1)
    test_dsn = dsn + ("&" if "?" in dsn else "?") + "connect_timeout=5"

    try:
        conn = psycopg2.connect(test_dsn)
    except psycopg2.OperationalError as e:
        raise HTTPException(
            400,
            f"No se pudo conectar a la base del cliente: {type(e).__name__}. "
            "Verifica host, usuario, password y SSL.",
        )

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.schemata "
                "WHERE schema_name = 'semantic')"
            )
            has_semantic = cur.fetchone()[0]
    finally:
        conn.close()

    return {"connection": "ok", "semantic_schema": bool(has_semantic)}


def _invite_user_by_email(email: str, full_name: Optional[str]) -> str:
    """Invita vía Supabase Auth (POST /auth/v1/invite). Envía el correo de invitación."""
    r = httpx.post(
        f"{settings.CENTRAL_SUPABASE_URL}/auth/v1/invite",
        headers=_auth_admin_headers(),
        json={
            "email": email,
            "data": {"full_name": full_name} if full_name else {},
            "redirect_to": f"{settings.FRONTEND_URL}/definir-password",
        },
        timeout=15,
    )
    if r.status_code == 422:
        raise HTTPException(
            409,
            "Este email ya existe en Supabase Auth. "
            "Usa la gestión de usuarios existentes (v1) o recupera su id.",
        )
    if r.status_code >= 400:
        raise HTTPException(
            502, f"Supabase rechazó la invitación ({r.status_code}): {r.text[:200]}"
        )
    return r.json()["id"]


def _audit(event_type: str, actor_id: str, tenant_id: Optional[str], success: bool, meta: dict):
    try:
        with db_cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO app_auth.audit_events
                    (actor_user_id, tenant_id, event_type, success, metadata_json)
                VALUES (%s, %s, %s, %s, %s::jsonb)
                """,
                (actor_id, tenant_id, event_type, success, __import__("json").dumps(meta)),
            )
    except Exception:
        pass  # la auditoría nunca debe tumbar la operación


# ============================================================================
# ENDPOINTS
# ============================================================================

class CreateTenantRequest(BaseModel):
    nombre: str
    slug: str
    dsn: str                        # operativa del cliente (idealmente leyia_metrics_reader)
    owner_email: EmailStr
    owner_full_name: Optional[str] = None


@router.post("/tenants", status_code=201)
def create_tenant(
    body: CreateTenantRequest,
    admin: User = Depends(require_platform_admin),
):
    slug = body.slug.strip().lower()
    if not _SLUG_RE.match(slug):
        raise HTTPException(
            400, "slug inválido: minúsculas, números y guiones (3-64 chars)"
        )

    # 1. Verificar el DSN ANTES de crear nada
    check = _verify_dsn(body.dsn)

    with db_cursor() as cur:
        cur.execute("SELECT 1 FROM app_auth.tenants WHERE slug = %s", (slug,))
        if cur.fetchone():
            raise HTTPException(409, f"Ya existe un tenant con slug '{slug}'")

    # 2. Crear tenant
    with db_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO app_auth.tenants (nombre, slug, status, is_active)
            VALUES (%s, %s, 'active', true)
            RETURNING id
            """,
            (body.nombre, slug),
        )
        tenant_id = str(cur.fetchone()["id"])

    try:
        # 3. Conexión cifrada
        save_tenant_connection(
            tenant_id=tenant_id,
            dsn=body.dsn,
            secret_ref=f"production/tenants/{tenant_id}/metrics-db",
        )

        # 4. Invitar al dueño + espejo local + membresía owner
        uid = _invite_user_by_email(body.owner_email, body.owner_full_name)
        with db_cursor(commit=True) as cur:
            cur.execute(
                "INSERT INTO app_auth.users (id, email, full_name, role)"
                " VALUES (%s, %s, %s, 'user')",
                (uid, body.owner_email.lower(), body.owner_full_name),
            )
            cur.execute(
                "INSERT INTO app_auth.tenant_users (user_id, tenant_id, role)"
                " VALUES (%s, %s, 'owner')",
                (uid, tenant_id),
            )
    except Exception:
        # Compensación: dejar la central limpia si algo falló a mitad de camino
        with db_cursor(commit=True) as cur:
            cur.execute("DELETE FROM app_auth.tenant_connections WHERE tenant_id = %s", (tenant_id,))
            cur.execute("DELETE FROM app_auth.tenants WHERE id = %s", (tenant_id,))
        raise

    _audit("tenant_created", admin.id, tenant_id, True,
           {"slug": slug, "owner_email": body.owner_email})

    return {
        "tenant_id": tenant_id,
        "slug": slug,
        "owner_user_id": uid,
        "invite_sent_to": body.owner_email,
        "dsn_check": check,   # incluye warning si falta schema 'semantic'
    }


class InviteUserRequest(BaseModel):
    email: EmailStr
    full_name: Optional[str] = None
    tenant_id: str
    tenant_role: Literal["owner", "admin", "viewer"] = "viewer"


@router.post("/users", status_code=201)
def invite_user(
    body: InviteUserRequest,
    admin: User = Depends(require_platform_admin),
):
    # Tenant debe existir y estar activo
    with db_cursor() as cur:
        cur.execute(
            "SELECT status, is_active FROM app_auth.tenants WHERE id = %s",
            (body.tenant_id,),
        )
        tenant = cur.fetchone()
    if not tenant:
        raise HTTPException(404, "Tenant no encontrado")
    if tenant["status"] != "active" or not tenant["is_active"]:
        raise HTTPException(409, "Tenant no está activo")

    # v1: una membresía activa por usuario — si ya existe, no invitar de nuevo
    existing = get_user_by_email(body.email)
    if existing:
        raise HTTPException(
            409,
            "Este email ya tiene cuenta en LeyIA (v1: un usuario = un tenant). "
            "Para moverlo, revoca primero su membresía actual.",
        )

    uid = _invite_user_by_email(body.email, body.full_name)

    with db_cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO app_auth.users (id, email, full_name, role)"
            " VALUES (%s, %s, %s, 'user')",
            (uid, body.email.lower(), body.full_name),
        )
        cur.execute(
            "INSERT INTO app_auth.tenant_users (user_id, tenant_id, role)"
            " VALUES (%s, %s, %s)",
            (uid, body.tenant_id, body.tenant_role),
        )

    _audit("user_invited", admin.id, body.tenant_id, True,
           {"email": body.email, "tenant_role": body.tenant_role})

    return {
        "user_id": uid,
        "tenant_id": body.tenant_id,
        "tenant_role": body.tenant_role,
        "invite_sent_to": body.email,
    }


# ─── Consultas básicas de gestión (staff) ────────────────────────────────────

@router.get("/tenants")
def list_tenants(_: User = Depends(require_platform_admin)):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT t.id::text, t.nombre, t.slug, t.status, t.is_active,
                   tc.status AS connection_status,
                   (SELECT count(*) FROM app_auth.tenant_users tu
                     WHERE tu.tenant_id = t.id AND tu.status = 'active') AS members
            FROM app_auth.tenants t
            LEFT JOIN app_auth.tenant_connections tc ON tc.tenant_id = t.id
            ORDER BY t.created_at DESC
            """
        )
        return {"tenants": [dict(r) for r in cur.fetchall()]}
