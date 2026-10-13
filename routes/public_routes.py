# routes/public_routes.py
# Endpoints públicos — lo que el frontend necesita ANTES del login.
# El branding vive en app_auth.tenants.config_json (DB central).

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from auth.config import settings
from auth.db import db_cursor

router = APIRouter(prefix="/api/public", tags=["public"])

_DEFAULTS = {
    "firm_name": "LeyIA",
    "agent_name": "LeyIA",
    "tono": "profesional",
    "areas_legales": [],
}


@router.get("/branding")
def branding(slug: Optional[str] = Query(None, description="Slug del estudio, ej. 'demo'")):
    """
    Branding del tenant para la pantalla de login.

    - Producción: el front pasa ?slug=estudio-x (a futuro: por subdominio).
    - Desarrollo: si no hay slug, cae al DEV_TENANT_ID.
    """
    cfg: dict = {}

    if slug:
        with db_cursor() as cur:
            cur.execute(
                "SELECT config_json FROM app_auth.tenants"
                " WHERE slug = %s AND is_active AND status = 'active'",
                (slug.lower(),),
            )
            row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Estudio no encontrado")
        cfg = row["config_json"] or {}
    elif settings.dev_tenant_bypass_enabled:
        with db_cursor() as cur:
            cur.execute(
                "SELECT config_json FROM app_auth.tenants WHERE id = %s",
                (settings.DEV_TENANT_ID,),
            )
            row = cur.fetchone()
        cfg = (row["config_json"] if row else {}) or {}

    return {
        "firm_name": cfg.get("firm_name", _DEFAULTS["firm_name"]),
        "agent_name": cfg.get("agent_name", _DEFAULTS["agent_name"]),
        "tono": cfg.get("tono", _DEFAULTS["tono"]),
        "areas_legales": cfg.get("areas_legales", _DEFAULTS["areas_legales"]),
    }
