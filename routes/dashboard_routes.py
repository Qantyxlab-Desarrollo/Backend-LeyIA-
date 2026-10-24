#!/usr/bin/env python3
# routes/dashboard_routes.py
# Dashboard LeyIA — endpoints activos

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query

from auth.tenancy import get_tenant_context, TenantContext
from services.dashboard_service import (
    get_conversaciones_service,
    get_agendas_service,
    get_ttr_service,
    get_tasa_conversion_service,
    get_overview_metrics_service,
    get_citas_detalle_service,
    get_citas_resumen_service,
    get_escalamientos_ultimos_service,
    get_escalamientos_hoy_service,
    get_escalamientos_todos_service,
    # ⬅️ NUEVO — Conversations analytics
    get_conv_por_dia_service,
    get_conv_intenciones_service,
    get_conv_categorias_service,
    get_conv_status_service,
    get_conv_kpis_service,
    health_semantic_service,
)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


# ── KPIs ───────────────────────────────────────────────────────────────────
@router.get("/conversaciones", response_model=Dict[str, Any])
async def conversaciones_total(
    range: str = Query("7d", pattern="^(7d|30d|90d)$"),
    tenant: TenantContext = Depends(get_tenant_context),
):
    return await get_conversaciones_service(tenant, range)


@router.get("/agendas", response_model=Dict[str, Any])
async def agendas_total(
    range: str = Query("7d", pattern="^(7d|30d|90d)$"),
    tenant: TenantContext = Depends(get_tenant_context),
):
    return await get_agendas_service(tenant, range)


@router.get("/ttr", response_model=Dict[str, Any])
async def ttr_total(
    range: str = Query("7d", pattern="^(7d|30d|90d)$"),
    tenant: TenantContext = Depends(get_tenant_context),
):
    return await get_ttr_service(tenant, range)


@router.get("/tasa-conversion", response_model=Dict[str, Any])
async def tasa_conversion(
    range: str = Query("7d", pattern="^(7d|30d|90d)$"),
    tenant: TenantContext = Depends(get_tenant_context),
):
    return await get_tasa_conversion_service(tenant, range)


@router.get("/overview", response_model=Dict[str, Any])
async def dashboard_overview(
    range: str = Query("7d", pattern="^(7d|30d|90d)$"),
    tenant: TenantContext = Depends(get_tenant_context),
):
    return await get_overview_metrics_service(tenant, range)


# ── CITAS — detalle y resumen ──────────────────────────────────────────────
@router.get("/citas/detalle", response_model=Dict[str, Any])
async def citas_detalle(
    range: str = Query("30d", pattern="^(7d|30d|90d)$"),
    modo: str = Query("agendadas", pattern="^(agendadas|pasadas|proximas)$"),
    modalidad: Optional[str] = Query(None, pattern="^(Online|Presencial)$"),
    canal: Optional[str] = Query(None, pattern="^(whatsapp|web|cli|otro)$"),
    limite: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """
    Listado paginado de citas.
    `modo`:
      • agendadas → created_at (coincide con el KPI de agendas)
      • pasadas   → inicio_cita (citas que se llevaron a cabo)
      • proximas  → inicio_cita (citas que van a ocurrir)
    """
    return await get_citas_detalle_service(
        tenant, range, modo, modalidad, canal, limite, offset
    )


@router.get("/citas/resumen", response_model=Dict[str, Any])
async def citas_resumen(
    range: str = Query("30d", pattern="^(7d|30d|90d)$"),
    modo: str = Query("agendadas", pattern="^(agendadas|pasadas|proximas)$"),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Total, por modalidad, por canal, por día, leads/threads únicos."""
    return await get_citas_resumen_service(tenant, range, modo)


@router.get("/citas/proximas", response_model=Dict[str, Any])
async def citas_proximas(
    range: str = Query("7d", pattern="^(7d|30d|90d)$"),
    limite: int = Query(20, ge=1, le=100),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Atajo: próximas citas (inicio_cita >= hoy) con límite bajo."""
    return await get_citas_detalle_service(
        tenant, range, "proximas", None, None, limite, 0
    )


# ── ESCALAMIENTOS — vista semantic.v_ultimos_escalamientos ─────────────────
@router.get("/escalamientos/ultimos", response_model=Dict[str, Any])
async def escalamientos_ultimos(
    limite: int = Query(10, ge=1, le=100),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """
    Últimos registros, más recientes primero.
    `mensaje` ya viene formateado desde la vista.
    Sin caché: datos live para polling cada 15-30s desde el front.
    """
    return await get_escalamientos_ultimos_service(tenant, limite)


@router.get("/escalamientos/hoy", response_model=Dict[str, Any])
async def escalamientos_hoy(
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Card 'Escalamientos hoy' — conteo del día en hora Chile."""
    return await get_escalamientos_hoy_service(tenant)


@router.get("/escalamientos/todos", response_model=Dict[str, Any])
async def escalamientos_todos(
    limite: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Listado completo paginado, más recientes primero."""
    return await get_escalamientos_todos_service(tenant, limite, offset)


# ── CONVERSATIONS · ANALYTICS ─────────────────────────────────────────────
@router.get("/conversations/kpis", response_model=Dict[str, Any])
async def conversations_kpis(tenant: TenantContext = Depends(get_tenant_context)):
    """KPIs consolidados (excluye channel='test')."""
    return await get_conv_kpis_service(tenant)


@router.get("/conversations/por-dia", response_model=Dict[str, Any])
async def conversations_por_dia(
    range: str = Query("30d", pattern="^(7d|30d|90d)$"),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Volumen por día pivoteado por canal."""
    return await get_conv_por_dia_service(tenant, range)


@router.get("/conversations/intenciones", response_model=Dict[str, Any])
async def conversations_intenciones(tenant: TenantContext = Depends(get_tenant_context)):
    """Distribución de intenciones (intent_first)."""
    return await get_conv_intenciones_service(tenant)


@router.get("/conversations/categorias", response_model=Dict[str, Any])
async def conversations_categorias(tenant: TenantContext = Depends(get_tenant_context)):
    """Distribución de categorías legales."""
    return await get_conv_categorias_service(tenant)


@router.get("/conversations/status", response_model=Dict[str, Any])
async def conversations_status(tenant: TenantContext = Depends(get_tenant_context)):
    """Distribución por estado (open/closed/escalado/agendado)."""
    return await get_conv_status_service(tenant)


# ── Health / Status ────────────────────────────────────────────────────────
@router.get("/health", response_model=Dict[str, Any])
async def health(tenant: TenantContext = Depends(get_tenant_context)):
    return await health_semantic_service(tenant)


@router.get("/status", response_model=Dict[str, Any])
async def dashboard_status(tenant: TenantContext = Depends(get_tenant_context)):
    return {
        "service": "dashboard",
        "status": "ok",
        "tenant_id": tenant.tenant_id,
        "tenant_slug": tenant.tenant_slug,
        "kpis_activos": [
            "conversations", "conversion", "agendas", "response_time",
            "citas_detalle", "citas_resumen", "citas_proximas",
            "escalamientos_ultimos", "escalamientos_hoy", "escalamientos_todos",
            # ⬅️ NUEVO — Conversations analytics
            "conversations_kpis", "conversations_por_dia", "conversations_intenciones",
            "conversations_categorias", "conversations_status",
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
