#!/usr/bin/env python3
# routes/dashboard_routes.py
# Dashboard LeyIA — endpoints de métricas multi-tenant.
# Base limpia: b_mensajes/b_sesiones/b_clientes → semantic.*

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from auth.tenancy import get_tenant_context, TenantContext
from services.dashboard_service import (
    get_conversaciones_diarias_service,
    get_conversaciones_total_service,
    get_clientes_total_service,
    get_agendas_diarias_service,
    get_agendas_detalle_service,
    get_agendas_total_service,
    get_citas_diarias_service,
    get_proximas_citas_service,
    get_escalaciones_service,
    get_ttr_bot_diario_service,
    get_ttr_bot_total_service,
    get_conversion_agenda_diaria_service,
    get_conversion_agenda_total_service,
    get_conversion_agenda_semanal_service,
    get_funnel_semanal_service,
    get_salud_semanal_service,
    get_operacion_diaria_service,
    get_mix_legal_service,
    get_mix_intent_semanal_service,
    get_dashboard_resumen_service,
    health_semantic_service,
)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


# ============================================================================
# RESUMEN EJECUTIVO
# ============================================================================

@router.get("/resumen", response_model=Dict[str, Any])
async def dashboard_resumen(tenant: TenantContext = Depends(get_tenant_context)):
    """KPIs totales: conversaciones, clientes, agendas, TTR (p50/p95), conversión, funnel última semana."""
    return await get_dashboard_resumen_service(tenant)


@router.get("/overview", response_model=Dict[str, Any])
async def dashboard_overview(tenant: TenantContext = Depends(get_tenant_context)):
    """Alias de /resumen para dashboards genéricos."""
    return await get_dashboard_resumen_service(tenant)


# ============================================================================
# 1. CONVERSACIONES (ahora = sesiones analíticas limpias)
# ============================================================================

@router.get("/conversaciones/diarias", response_model=List[Dict[str, Any]])
async def conversaciones_diarias(
    fecha_inicio: Optional[date] = Query(None, description="YYYY-MM-DD (hora Chile)"),
    fecha_fin: Optional[date] = Query(None),
    canal: Optional[str] = Query(None, description="web | whatsapp"),
    tenant: TenantContext = Depends(get_tenant_context),
):
    # BREAKING: ya no existe el filtro `status` (v_conversaciones_diarias lo perdió
    # al pasar de tabla conversations a sesiones analíticas)
    return await get_conversaciones_diarias_service(tenant, fecha_inicio, fecha_fin, canal)


@router.get("/conversaciones/total", response_model=Dict[str, Any])
async def conversaciones_total(tenant: TenantContext = Depends(get_tenant_context)):
    return await get_conversaciones_total_service(tenant)


@router.get("/clientes/total", response_model=Dict[str, Any])
async def clientes_total(tenant: TenantContext = Depends(get_tenant_context)):
    """Clientes únicos reales (threads válidos). Nuevo KPI base del funnel."""
    return await get_clientes_total_service(tenant)


# ============================================================================
# 2. AGENDAS (demanda: cuándo se agendó · detalle incluye dev_demo)
# ============================================================================

@router.get("/agendas/diarias", response_model=List[Dict[str, Any]])
async def agendas_diarias(
    fecha_inicio: Optional[date] = Query(None),
    fecha_fin: Optional[date] = Query(None),
    modalidad: Optional[str] = Query(None, description="'Presencial' | 'Online'"),
    tenant: TenantContext = Depends(get_tenant_context),
):
    return await get_agendas_diarias_service(tenant, fecha_inicio, fecha_fin, modalidad)


@router.get("/agendas/detalle", response_model=List[Dict[str, Any]])
async def agendas_detalle(
    fecha_inicio: Optional[date] = Query(None),
    fecha_fin: Optional[date] = Query(None),
    limite: int = Query(100, ge=1, le=500),
    tenant: TenantContext = Depends(get_tenant_context),
):
    return await get_agendas_detalle_service(tenant, fecha_inicio, fecha_fin, limite)


@router.get("/agendas/total", response_model=Dict[str, Any])
async def agendas_total(tenant: TenantContext = Depends(get_tenant_context)):
    return await get_agendas_total_service(tenant)


# ============================================================================
# 2b. CITAS (carga: cuándo OCURREN las citas)
# ============================================================================

@router.get("/citas/diarias", response_model=List[Dict[str, Any]])
async def citas_diarias(
    fecha_inicio: Optional[date] = Query(None),
    fecha_fin: Optional[date] = Query(None),
    modalidad: Optional[str] = Query(None),
    tenant: TenantContext = Depends(get_tenant_context),
):
    return await get_citas_diarias_service(tenant, fecha_inicio, fecha_fin, modalidad)


# ============================================================================
# 2c. PRÓXIMAS CITAS — lectura directa de la tabla bookings
# ============================================================================

@router.get("/citas/proximas", response_model=Dict[str, Any])
async def citas_proximas(
    fecha_inicio: Optional[date] = Query(None, description="Por defecto hoy. Puedes poner una fecha pasada si lo necesitas."),
    fecha_fin: Optional[date] = Query(None),
    modalidad: Optional[str] = Query(None, description="'Presencial' | 'Online'"),
    limite: int = Query(100, ge=1, le=500),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """
    Próximas citas desde la tabla `bookings` (no la vista).
    Devuelve totales, conteo por modalidad y citas agrupadas por día.
    """
    return await get_proximas_citas_service(tenant, fecha_inicio, fecha_fin, modalidad, limite)


# ============================================================================
# 3. ESCALACIONES (ahora con motivo y split demanda real vs silenciosa)
# ============================================================================

@router.get("/escalaciones", response_model=List[Dict[str, Any]])
async def escalaciones(
    limite: int = Query(100, ge=1, le=500),
    solo_no_notificados: bool = Query(False),
    solo_demanda_real: bool = Query(False, description="Solo motivo='hablar_abogada'"),
    tenant: TenantContext = Depends(get_tenant_context),
):
    return await get_escalaciones_service(tenant, limite, solo_no_notificados, solo_demanda_real)


# ============================================================================
# 4. TTR DEL BOT (ahora honesto: p50/p95, sin duplicados ni gaps)
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
# 5. CONVERSIÓN (cohorte del primer contacto)
# ============================================================================

@router.get("/conversion/diaria", response_model=List[Dict[str, Any]])
async def conversion_diaria(
    fecha_inicio: Optional[date] = Query(None),
    fecha_fin: Optional[date] = Query(None),
    tenant: TenantContext = Depends(get_tenant_context),
):
    return await get_conversion_agenda_diaria_service(tenant, fecha_inicio, fecha_fin)


@router.get("/conversion/semanal", response_model=List[Dict[str, Any]])
async def conversion_semanal(
    semanas: int = Query(12, ge=1, le=52),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """RECOMENDADA sobre la diaria: con bajo volumen la diaria salta 0%↔100%."""
    return await get_conversion_agenda_semanal_service(tenant, semanas)


@router.get("/conversion/total", response_model=Dict[str, Any])
async def conversion_total(tenant: TenantContext = Depends(get_tenant_context)):
    return await get_conversion_agenda_total_service(tenant)


# ============================================================================
# 6. NUEVO — NEGOCIO
# ============================================================================

@router.get("/funnel/semanal", response_model=List[Dict[str, Any]])
async def funnel_semanal(
    semanas: int = Query(12, ge=1, le=52),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Cohorte: clientes → ficha iniciada → ficha completa → cita. Tasas por paso."""
    return await get_funnel_semanal_service(tenant, semanas)


@router.get("/salud/semanal", response_model=List[Dict[str, Any]])
async def salud_semanal(
    semanas: int = Query(12, ge=1, le=52),
    tenant: TenantContext = Depends(get_tenant_context),
):
    return await get_salud_semanal_service(tenant, semanas)


@router.get("/operacion/diaria", response_model=List[Dict[str, Any]])
async def operacion_diaria(
    fecha_inicio: Optional[date] = Query(None),
    fecha_fin: Optional[date] = Query(None),
    canal: Optional[str] = None,
    tenant: TenantContext = Depends(get_tenant_context),
):
    return await get_operacion_diaria_service(tenant, fecha_inicio, fecha_fin, canal)


@router.get("/mix/legal", response_model=List[Dict[str, Any]])
async def mix_legal(tenant: TenantContext = Depends(get_tenant_context)):
    return await get_mix_legal_service(tenant)


@router.get("/mix/intent-semanal", response_model=List[Dict[str, Any]])
async def mix_intent_semanal(
    semanas: int = Query(12, ge=1, le=52),
    tenant: TenantContext = Depends(get_tenant_context),
):
    return await get_mix_intent_semanal_service(tenant, semanas)


# ============================================================================
# HEALTH / STATUS
# ============================================================================

@router.get("/health", response_model=Dict[str, Any])
async def health(tenant: TenantContext = Depends(get_tenant_context)):
    return await health_semantic_service(tenant)


@router.get("/status", response_model=Dict[str, Any])
async def dashboard_status(tenant: TenantContext = Depends(get_tenant_context)):
    return {
        "service": "dashboard",
        "status": "ok",
        "authenticated": True,
        "tenant_id": tenant.tenant_id,
        "tenant_slug": tenant.tenant_slug,
        "user_id": tenant.user_id,
        "user_email": tenant.user_email,
        "capa_datos": "semantic v2 (b_mensajes/b_sesiones/b_clientes)",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
