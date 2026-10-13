#!/usr/bin/env python3
# routes/dashboard_routes.py
# Dashboard LeyIA — endpoints de métricas multi-tenant.
# Cada endpoint resuelve el tenant desde la membresía del usuario autenticado
# y consulta la base de datos operativa de ese estudio jurídico.

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from auth.tenancy import get_tenant_context, TenantContext
from services.dashboard_service import (
    get_conversaciones_diarias_service,
    get_conversaciones_total_service,
    get_agendas_diarias_service,
    get_agendas_total_service,
    get_escalaciones_service,
    get_ttr_bot_diario_service,
    get_ttr_bot_total_service,
    get_conversion_agenda_diaria_service,
    get_conversion_agenda_total_service,
    get_dashboard_resumen_service,
    health_semantic_service,
)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


# ============================================================================
# RESUMEN EJECUTIVO — GET /api/dashboard/resumen
# ============================================================================

@router.get("/resumen", response_model=Dict[str, Any])
async def dashboard_resumen(tenant: TenantContext = Depends(get_tenant_context)):
    """KPIs totales en una sola llamada: conversaciones, agendas, TTR, conversión."""
    return await get_dashboard_resumen_service(tenant)


# ============================================================================
# ALIAS: /api/dashboard/overview (compatibilidad con frontends/dashboards)
# ============================================================================

@router.get("/overview", response_model=Dict[str, Any])
async def dashboard_overview(tenant: TenantContext = Depends(get_tenant_context)):
    """Alias de /resumen para facilitar la integración con dashboards genéricos."""
    return await get_dashboard_resumen_service(tenant)


# ============================================================================
# 1. CONVERSACIONES
# ============================================================================

@router.get("/conversaciones/diarias", response_model=List[Dict[str, Any]])
async def conversaciones_diarias(
    fecha_inicio: Optional[date] = Query(None, description="YYYY-MM-DD"),
    fecha_fin: Optional[date] = Query(None, description="YYYY-MM-DD"),
    canal: Optional[str] = None,
    status: Optional[str] = None,
    tenant: TenantContext = Depends(get_tenant_context),
):
    return await get_conversaciones_diarias_service(
        tenant, fecha_inicio, fecha_fin, canal, status
    )


@router.get("/conversaciones/total", response_model=Dict[str, Any])
async def conversaciones_total(tenant: TenantContext = Depends(get_tenant_context)):
    return await get_conversaciones_total_service(tenant)


# ============================================================================
# 2. AGENDAS
# ============================================================================

@router.get("/agendas/diarias", response_model=List[Dict[str, Any]])
async def agendas_diarias(
    fecha_inicio: Optional[date] = Query(None),
    fecha_fin: Optional[date] = Query(None),
    modalidad: Optional[str] = Query(
        None, description="'Presencial' u 'Online' (etiqueta normalizada)"
    ),
    tenant: TenantContext = Depends(get_tenant_context),
):
    return await get_agendas_diarias_service(tenant, fecha_inicio, fecha_fin, modalidad)


@router.get("/agendas/total", response_model=Dict[str, Any])
async def agendas_total(tenant: TenantContext = Depends(get_tenant_context)):
    return await get_agendas_total_service(tenant)


# ============================================================================
# 3. ESCALACIONES
# ============================================================================

@router.get("/escalaciones", response_model=List[Dict[str, Any]])
async def escalaciones(
    limite: int = Query(100, ge=1, le=500),
    solo_no_notificados: bool = Query(False, description="Solo casos sin notificar a WhatsApp"),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Ordenadas de más reciente a más antigua (orden definido en la vista)."""
    return await get_escalaciones_service(tenant, limite, solo_no_notificados)


# ============================================================================
# 4. TTR — TIEMPO MEDIO DE RESPUESTA
# ============================================================================

@router.get("/ttr/diario", response_model=List[Dict[str, Any]])
async def ttr_diario(
    fecha_inicio: Optional[date] = Query(None),
    fecha_fin: Optional[date] = Query(None),
    tenant: TenantContext = Depends(get_tenant_context),
):
    return await get_ttr_bot_diario_service(tenant, fecha_inicio, fecha_fin)


@router.get("/ttr/total", response_model=Dict[str, Any])
async def ttr_total(tenant: TenantContext = Depends(get_tenant_context)):
    return await get_ttr_bot_total_service(tenant)


# ============================================================================
# 5. CONVERSIÓN A AGENDA
# ============================================================================

@router.get("/conversion/diaria", response_model=List[Dict[str, Any]])
async def conversion_diaria(
    fecha_inicio: Optional[date] = Query(None),
    fecha_fin: Optional[date] = Query(None),
    tenant: TenantContext = Depends(get_tenant_context),
):
    return await get_conversion_agenda_diaria_service(tenant, fecha_inicio, fecha_fin)


@router.get("/conversion/total", response_model=Dict[str, Any])
async def conversion_total(tenant: TenantContext = Depends(get_tenant_context)):
    return await get_conversion_agenda_total_service(tenant)


# ============================================================================
# HEALTH — GET /api/dashboard/health
# ============================================================================

@router.get("/health", response_model=Dict[str, Any])
async def health(tenant: TenantContext = Depends(get_tenant_context)):
    return await health_semantic_service(tenant)


# ============================================================================
# STATUS — GET /api/dashboard/status (estado rápido del dashboard)
# ============================================================================

@router.get("/status", response_model=Dict[str, Any])
async def dashboard_status(tenant: TenantContext = Depends(get_tenant_context)):
    """Estado rápido del dashboard/API para el frontend."""
    return {
        "service": "dashboard",
        "status": "ok",
        "authenticated": True,
        "tenant_id": tenant.tenant_id,
        "tenant_slug": tenant.tenant_slug,
        "user_id": tenant.user_id,
        "user_email": tenant.user_email,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
