# auth/tenancy.py
# Resolución de tenant por membresía — la fuente de verdad del acceso.
# Nunca confiar en tenant_id enviado por el cliente.

import logging
from typing import Optional

from fastapi import Depends, HTTPException, status
from pydantic import BaseModel

from auth.config import settings
from auth.db import db_cursor
from auth.dependencies import get_current_user, get_current_user_optional
from auth.models import User

logger = logging.getLogger("tenancy")
_dev_warning_emitted = False


class TenantContext(BaseModel):
    tenant_id: str
    tenant_nombre: str
    tenant_slug: str
    tenant_role: str = "owner"       # rol DENTRO del estudio: owner | admin | viewer
    user_id: Optional[str] = None
    user_email: Optional[str] = None
    is_dev_fallback: bool = False


def get_active_membership(user_id: str) -> Optional[dict]:
    """
    v1: un usuario = una membresía activa (garantizado por el índice único
    parcial uq_tenant_users_one_active creado en el SQL central).
    """
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT tu.tenant_id::text AS tenant_id,
                   tu.role            AS tenant_role,
                   t.nombre           AS tenant_nombre,
                   t.slug             AS tenant_slug,
                   t.status           AS tenant_status,
                   t.is_active        AS tenant_is_active
            FROM app_auth.tenant_users tu
            JOIN app_auth.tenants t ON t.id = tu.tenant_id
            WHERE tu.user_id = %s
              AND tu.status = 'active'          -- 👈 T1: membresía revocada no entra
            ORDER BY tu.created_at ASC          -- 👈 T2: determinista
            LIMIT 1
            """,
            (user_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def _dev_context() -> TenantContext:
    """Contexto fijo solo para desarrollo (frontend aún sin login)."""
    global _dev_warning_emitted

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT id::text, nombre, slug, status, is_active
            FROM app_auth.tenants
            WHERE id = %s
            """,
            (settings.DEV_TENANT_ID,),
        )
        row = cur.fetchone()

    if not row:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "DEV_TENANT_ID apunta a un tenant inexistente. Corre scripts/seed_demo.py",
        )
    if row["status"] != "active" or not row["is_active"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Tenant demo suspendido")

    if not _dev_warning_emitted:                 # 👈 loguear una vez, no por request
        logger.warning("⚠️ DEV TENANT BYPASS activo — jamás habilitar en producción")
        _dev_warning_emitted = True

    return TenantContext(
        tenant_id=row["id"],
        tenant_nombre=row["nombre"],
        tenant_slug=row["slug"],
        is_dev_fallback=True,
    )


async def get_tenant_context(
    user: Optional[User] = Depends(get_current_user_optional),
) -> TenantContext:
    # ── Sin token ────────────────────────────────────────────────────
    if user is None:
        if settings.dev_tenant_bypass_enabled:   # 👈 usa la property, no lógica duplicada
            return _dev_context()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No autenticado")

    # ── Con token: tenant desde la membresía (fuente de verdad) ──────
    m = get_active_membership(user.id)
    if not m:
        raise HTTPException(status.HTTP_428_PRECONDITION_REQUIRED, "onboarding_required")

    if m["tenant_status"] != "active" or not m["tenant_is_active"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Firma suspendida")

    return TenantContext(
        tenant_id=m["tenant_id"],
        tenant_nombre=m["tenant_nombre"],
        tenant_slug=m["tenant_slug"],
        tenant_role=m["tenant_role"],
        user_id=user.id,
        user_email=user.email,
    )


# ============================================================================
# GUARDS ADICIONALES
# ============================================================================

async def require_tenant_owner(
    tenant: TenantContext = Depends(get_tenant_context),
) -> TenantContext:
    """Para acciones de gestión del estudio (miembros, configuración)."""
    if tenant.tenant_role not in ("owner", "admin"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Requiere rol owner/admin dentro del estudio",
        )
    return tenant


async def require_platform_admin(
    user: User = Depends(get_current_user),      # 👈 ahora sí está importado
) -> User:
    """Para operaciones de staff LeyIA (provisión de tenants, etc.)."""
    if user.role != "admin":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Requiere administrador de plataforma",
        )
    return user
