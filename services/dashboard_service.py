#!/usr/bin/env python3
# services/dashboard_service.py
# Capa de servicios — Dashboard LeyIA
# Lee exclusivamente las vistas del esquema `semantic` de la BD del tenant.

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

from auth.tenancy import TenantContext
from core.tenant_db import tenant_cache, tenant_fetch, tenant_fetchrow

logger = logging.getLogger("leyia.dashboard")

TTL_TOTALS = 120.0
TTL_SERIES = 120.0
TTL_ESCALACIONES = 60.0
TTL_RESUMEN = 120.0


def _build_filters(
    fecha_inicio=None,
    fecha_fin=None,
    extras: Optional[Dict[str, Any]] = None,
) -> Tuple[str, list]:
    clauses, params = [], []
    if fecha_inicio:
        params.append(fecha_inicio)
        clauses.append(f"fecha >= ${len(params)}")
    if fecha_fin:
        params.append(fecha_fin)
        clauses.append(f"fecha <= ${len(params)}")
    for columna, valor in (extras or {}).items():
        if valor is None:
            continue
        params.append(valor)
        clauses.append(f"{columna} = ${len(params)}")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


# ============================================================================
# 1. CONVERSACIONES
# ============================================================================

async def get_conversaciones_diarias_service(
    tenant: TenantContext,
    fecha_inicio=None, fecha_fin=None, canal=None, status=None
) -> List[Dict[str, Any]]:
    where, params = _build_filters(fecha_inicio, fecha_fin, {"canal": canal, "status": status})
    query = f"""
        SELECT fecha, canal, status, total_conversaciones
        FROM semantic.v_conversaciones_diarias
        {where}
        ORDER BY fecha DESC, canal, status
    """
    key = f"conv_diarias:{fecha_inicio}:{fecha_fin}:{canal}:{status}"
    return await tenant_cache.get_or_set(
        tenant, key,
        lambda: tenant_fetch(tenant, query, *params, schema="semantic", label="conv_diarias")
    )


async def get_conversaciones_total_service(tenant: TenantContext) -> Dict[str, Any]:
    key = f"conv_total"
    return await tenant_cache.get_or_set(
        tenant, key,
        lambda: tenant_fetchrow(
            tenant,
            "SELECT total_conversaciones FROM semantic.v_conversaciones_total",
            schema="semantic",
            label="conv_total",
        ),
    )


# ============================================================================
# 2. AGENDAS
# ============================================================================

async def get_agendas_diarias_service(
    tenant: TenantContext,
    fecha_inicio=None, fecha_fin=None, modalidad=None
) -> List[Dict[str, Any]]:
    where, params = _build_filters(fecha_inicio, fecha_fin, {"modalidad": modalidad})
    query = f"""
        SELECT fecha, modalidad, modalidad_original, total_agendas
        FROM semantic.v_agendas_diarias
        {where}
        ORDER BY fecha DESC, modalidad
    """
    key = f"agendas_diarias:{fecha_inicio}:{fecha_fin}:{modalidad}"
    return await tenant_cache.get_or_set(
        tenant, key,
        lambda: tenant_fetch(tenant, query, *params, schema="semantic", label="agendas_diarias")
    )


async def get_agendas_total_service(tenant: TenantContext) -> Dict[str, Any]:
    key = f"agendas_total"
    return await tenant_cache.get_or_set(
        tenant, key,
        lambda: tenant_fetchrow(
            tenant,
            "SELECT total_agendas FROM semantic.v_agendas_total",
            schema="semantic",
            label="agendas_total",
        ),
    )


# ============================================================================
# 3. ESCALACIONES
# ============================================================================

async def get_escalaciones_service(
    tenant: TenantContext,
    limite: int = 100, solo_no_notificados: bool = False
) -> List[Dict[str, Any]]:
    query = """
        SELECT id, thread_id, conversation_id, lead_id, summary,
               notificado_whatsapp, notificado_wsp, created_at
        FROM semantic.v_escalaciones
    """
    params: list = []
    if solo_no_notificados:
        query += " WHERE notificado_whatsapp = FALSE"
    params.append(limite)
    query += f" LIMIT ${len(params)}"
    key = f"escalaciones:{limite}:{solo_no_notificados}"
    return await tenant_cache.get_or_set(
        tenant, key,
        lambda: tenant_fetch(tenant, query, *params, schema="semantic", label="escalaciones")
    )


# ============================================================================
# 4. TTR DEL BOT
# ============================================================================

async def get_ttr_bot_diario_service(
    tenant: TenantContext, fecha_inicio=None, fecha_fin=None
) -> List[Dict[str, Any]]:
    where, params = _build_filters(fecha_inicio, fecha_fin)
    query = f"""
        SELECT fecha, respuestas_bot, ttr_promedio_seg, ttr_promedio
        FROM semantic.v_ttr_bot_diario
        {where}
        ORDER BY fecha DESC
    """
    key = f"ttr_diario:{fecha_inicio}:{fecha_fin}"
    return await tenant_cache.get_or_set(
        tenant, key,
        lambda: tenant_fetch(tenant, query, *params, schema="semantic", label="ttr_diario")
    )


async def get_ttr_bot_total_service(tenant: TenantContext) -> Dict[str, Any]:
    key = f"ttr_total"
    return await tenant_cache.get_or_set(
        tenant, key,
        lambda: tenant_fetchrow(
            tenant,
            "SELECT respuestas_bot, ttr_promedio_seg, ttr_promedio "
            "FROM semantic.v_ttr_bot_total",
            schema="semantic",
            label="ttr_total",
        ),
    )


# ============================================================================
# 5. CONVERSIÓN A AGENDA
# ============================================================================

async def get_conversion_agenda_diaria_service(
    tenant: TenantContext, fecha_inicio=None, fecha_fin=None
) -> List[Dict[str, Any]]:
    where, params = _build_filters(fecha_inicio, fecha_fin)
    query = f"""
        SELECT fecha, leads_con_intencion, leads_convertidos, tasa_conversion_pct
        FROM semantic.v_conversion_agenda_diaria
        {where}
        ORDER BY fecha DESC
    """
    key = f"conversion_diaria:{fecha_inicio}:{fecha_fin}"
    return await tenant_cache.get_or_set(
        tenant, key,
        lambda: tenant_fetch(tenant, query, *params, schema="semantic", label="conversion_diaria")
    )


async def get_conversion_agenda_total_service(tenant: TenantContext) -> Dict[str, Any]:
    key = f"conversion_total"
    return await tenant_cache.get_or_set(
        tenant, key,
        lambda: tenant_fetchrow(
            tenant,
            "SELECT leads_con_intencion, leads_convertidos, tasa_conversion_pct "
            "FROM semantic.v_conversion_agenda_total",
            schema="semantic",
            label="conversion_total",
        ),
    )


# ============================================================================
# RESUMEN EJECUTIVO
# ============================================================================

async def _safe(coro, nombre: str) -> Dict[str, Any]:
    try:
        return await coro
    except Exception as e:
        logger.warning(f"⚠️ KPI '{nombre}' no disponible, se omite del resumen: {e}")
        return {}


async def get_dashboard_resumen_service(tenant: TenantContext) -> Dict[str, Any]:
    async def _compute():
        resultados = await asyncio.gather(
            _safe(get_conversaciones_total_service(tenant), "conversaciones"),
            _safe(get_agendas_total_service(tenant), "agendas"),
            _safe(get_ttr_bot_total_service(tenant), "ttr"),
            _safe(get_conversion_agenda_total_service(tenant), "conversion"),
            _safe(
                tenant_fetchrow(
                    tenant,
                    "SELECT COUNT(*)::int AS escalaciones_sin_notificar "
                    "FROM semantic.v_escalaciones WHERE notificado_whatsapp = FALSE",
                    schema="semantic",
                    label="esc_sin_notificar",
                ),
                "escalaciones",
            ),
        )
        merged: Dict[str, Any] = {}
        for r in resultados:
            merged.update(r)
        return merged

    return await tenant_cache.get_or_set(tenant, "resumen", _compute)


# ============================================================================
# WARM-UP
# ============================================================================

async def warmup(tenant: TenantContext) -> None:
    try:
        await get_dashboard_resumen_service(tenant)
        logger.info(f"🔥 Warm-up dashboard OK para tenant {tenant.tenant_id[:8]}")
    except Exception as e:
        logger.warning(f"⚠️ Warm-up dashboard falló (no fatal): {e}")


# ============================================================================
# HEALTH
# ============================================================================

async def health_semantic_service(tenant: TenantContext) -> Dict[str, Any]:
    try:
        row = await tenant_fetchrow(
            tenant,
            "SELECT total_conversaciones FROM semantic.v_conversaciones_total",
            schema="semantic",
            label="health_semantic",
        )
        return {"status": "ok", "esquema": "semantic", "muestra": row}
    except Exception as e:
        return {"status": "error", "error": str(e)}
