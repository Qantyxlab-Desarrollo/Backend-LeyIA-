#!/usr/bin/env python3
# services/dashboard_service.py
# Capa de servicios — Dashboard LeyIA (semantic v2 sobre base limpia)

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
    campo_fecha: str = "fecha",
) -> Tuple[str, list]:
    clauses, params = [], []
    if fecha_inicio:
        params.append(fecha_inicio)
        clauses.append(f"{campo_fecha} >= ${len(params)}")
    if fecha_fin:
        params.append(fecha_fin)
        clauses.append(f"{campo_fecha} <= ${len(params)}")
    for columna, valor in (extras or {}).items():
        if valor is None:
            continue
        params.append(valor)
        clauses.append(f"{columna} = ${len(params)}")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


# ============================================================================
# 1. CONVERSACIONES (sesiones analíticas)
# ============================================================================

async def get_conversaciones_diarias_service(
    tenant: TenantContext, fecha_inicio=None, fecha_fin=None, canal=None
) -> List[Dict[str, Any]]:
    where, params = _build_filters(fecha_inicio, fecha_fin, {"canal": canal})
    query = f"""
        SELECT fecha, canal, total_conversaciones
        FROM semantic.v_conversaciones_diarias
        {where}
        ORDER BY fecha DESC, canal
    """
    key = f"conv_diarias:{fecha_inicio}:{fecha_fin}:{canal}"
    return await tenant_cache.get_or_set(
        tenant, key,
        lambda: tenant_fetch(tenant, query, *params, schema="semantic", label="conv_diarias")
    )


async def get_conversaciones_total_service(tenant: TenantContext) -> Dict[str, Any]:
    return await tenant_cache.get_or_set(
        tenant, "conv_total",
        lambda: tenant_fetchrow(
            tenant,
            "SELECT total_conversaciones FROM semantic.v_conversaciones_total",
            schema="semantic", label="conv_total"),
    )


async def get_clientes_total_service(tenant: TenantContext) -> Dict[str, Any]:
    return await tenant_cache.get_or_set(
        tenant, "clientes_total",
        lambda: tenant_fetchrow(
            tenant,
            "SELECT total_clientes FROM semantic.v_clientes_total",
            schema="semantic", label="clientes_total"),
    )


# ============================================================================
# 2. AGENDAS
# ============================================================================

async def get_agendas_diarias_service(
    tenant: TenantContext, fecha_inicio=None, fecha_fin=None, modalidad=None
) -> List[Dict[str, Any]]:
    where, params = _build_filters(fecha_inicio, fecha_fin, {"modalidad": modalidad})
    query = f"""
        SELECT fecha, modalidad, modalidad_original, total_agendas, clientes
        FROM semantic.v_agendas_diarias
        {where}
        ORDER BY fecha DESC, modalidad
    """
    key = f"agendas_diarias:{fecha_inicio}:{fecha_fin}:{modalidad}"
    return await tenant_cache.get_or_set(
        tenant, key,
        lambda: tenant_fetch(tenant, query, *params, schema="semantic", label="agendas_diarias")
    )


async def get_agendas_detalle_service(
    tenant: TenantContext, fecha_inicio=None, fecha_fin=None, limite: int = 100
) -> List[Dict[str, Any]]:
    where, params = _build_filters(fecha_inicio, fecha_fin, campo_fecha="fecha_cita")
    params.append(limite)
    query = f"""
        SELECT fecha_cita, hora, cliente_nombre, cliente_email, modalidad,
               meet_link, html_link, thread_id, created_at, canal, cliente_telefono
        FROM semantic.v_agendas_detalle
        {where}
        ORDER BY fecha_cita DESC, hora DESC
        LIMIT ${len(params)}
    """
    key = f"agendas_detalle:{fecha_inicio}:{fecha_fin}:{limite}"
    return await tenant_cache.get_or_set(
        tenant, key,
        lambda: tenant_fetch(tenant, query, *params, schema="semantic", label="agendas_detalle")
    )


async def get_agendas_total_service(tenant: TenantContext) -> Dict[str, Any]:
    return await tenant_cache.get_or_set(
        tenant, "agendas_total",
        lambda: tenant_fetchrow(
            tenant,
            "SELECT total_agendas FROM semantic.v_agendas_total",
            schema="semantic", label="agendas_total"),
    )


# ============================================================================
# 2b. CITAS (carga por día de ocurrencia)
# ============================================================================

async def get_citas_diarias_service(
    tenant: TenantContext, fecha_inicio=None, fecha_fin=None, modalidad=None
) -> List[Dict[str, Any]]:
    where, params = _build_filters(
        fecha_inicio, fecha_fin, {"modalidad": modalidad}, campo_fecha="fecha_cita"
    )
    query = f"""
        SELECT fecha_cita, modalidad, modalidad_original, total_citas, clientes
        FROM semantic.v_citas_diarias
        {where}
        ORDER BY fecha_cita DESC, modalidad
    """
    key = f"citas_diarias:{fecha_inicio}:{fecha_fin}:{modalidad}"
    return await tenant_cache.get_or_set(
        tenant, key,
        lambda: tenant_fetch(tenant, query, *params, schema="semantic", label="citas_diarias")
    )


# ============================================================================
# 3. ESCALACIONES (con motivo y split demanda real)
# ============================================================================

async def get_escalaciones_service(
    tenant: TenantContext,
    limite: int = 100,
    solo_no_notificados: bool = False,
    solo_demanda_real: bool = False,
) -> List[Dict[str, Any]]:
    condiciones, params = [], []
    if solo_no_notificados:
        condiciones.append("notificado_whatsapp = FALSE")
    if solo_demanda_real:
        condiciones.append("es_demanda_real = TRUE")
    where = f" WHERE {' AND '.join(condiciones)}" if condiciones else ""
    params.append(limite)
    query = f"""
        SELECT id, thread_id, conversation_id, lead_id, summary,
               notificado_whatsapp, notificado_wsp, created_at, motivo, es_demanda_real
        FROM semantic.v_escalaciones
        {where}
        ORDER BY created_at DESC
        LIMIT ${len(params)}
    """
    key = f"escalaciones:{limite}:{solo_no_notificados}:{solo_demanda_real}"
    return await tenant_cache.get_or_set(
        tenant, key,
        lambda: tenant_fetch(tenant, query, *params, schema="semantic", label="escalaciones")
    )


# ============================================================================
# 4. TTR DEL BOT (honesto: p50/p95)
# ============================================================================

async def get_ttr_bot_diario_service(
    tenant: TenantContext, fecha_inicio=None, fecha_fin=None
) -> List[Dict[str, Any]]:
    where, params = _build_filters(fecha_inicio, fecha_fin)
    query = f"""
        SELECT fecha, respuestas_bot, ttr_promedio_seg, ttr_promedio, ttr_p50_seg, ttr_p95_seg
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
    return await tenant_cache.get_or_set(
        tenant, "ttr_total",
        lambda: tenant_fetchrow(
            tenant,
            "SELECT respuestas_bot, ttr_promedio_seg, ttr_promedio, ttr_p50_seg, ttr_p95_seg "
            "FROM semantic.v_ttr_bot_total",
            schema="semantic", label="ttr_total"),
    )


# ============================================================================
# 5. CONVERSIÓN (cohorte primer contacto)
# ============================================================================

async def get_conversion_agenda_diaria_service(
    tenant: TenantContext, fecha_inicio=None, fecha_fin=None
) -> List[Dict[str, Any]]:
    where, params = _build_filters(fecha_inicio, fecha_fin)
    query = f"""
        SELECT fecha, leads_con_intencion, leads_convertidos, tasa_conversion_pct, canal
        FROM semantic.v_conversion_agenda_diaria
        {where}
        ORDER BY fecha DESC
    """
    key = f"conversion_diaria:{fecha_inicio}:{fecha_fin}"
    return await tenant_cache.get_or_set(
        tenant, key,
        lambda: tenant_fetch(tenant, query, *params, schema="semantic", label="conversion_diaria")
    )


async def get_conversion_agenda_semanal_service(
    tenant: TenantContext, semanas: int = 12
) -> List[Dict[str, Any]]:
    query = """
        SELECT semana, clientes_nuevos, con_cita, tasa_pct
        FROM semantic.v_conversion_agenda_semanal
        ORDER BY semana DESC LIMIT $1
    """
    return await tenant_cache.get_or_set(
        tenant, f"conversion_semanal:{semanas}",
        lambda: tenant_fetch(tenant, query, semanas, schema="semantic", label="conversion_semanal")
    )


async def get_conversion_agenda_total_service(tenant: TenantContext) -> Dict[str, Any]:
    return await tenant_cache.get_or_set(
        tenant, "conversion_total",
        lambda: tenant_fetchrow(
            tenant,
            "SELECT leads_con_intencion, leads_convertidos, tasa_conversion_pct "
            "FROM semantic.v_conversion_agenda_total",
            schema="semantic", label="conversion_total"),
    )


# ============================================================================
# 6. NUEVO — NEGOCIO
# ============================================================================

async def get_funnel_semanal_service(tenant: TenantContext, semanas: int = 12) -> List[Dict[str, Any]]:
    query = """
        SELECT semana, clientes_nuevos, fichas_iniciadas, fichas_completas,
               tasa_completado_ficha_pct, citas, conv_cita_pct,
               demanda_humano, pidieron_humano_texto, mediana_horas_a_cita
        FROM semantic.v_funnel_semanal
        ORDER BY semana DESC LIMIT $1
    """
    return await tenant_cache.get_or_set(
        tenant, f"funnel_semanal:{semanas}",
        lambda: tenant_fetch(tenant, query, semanas, schema="semantic", label="funnel_semanal")
    )


async def get_salud_semanal_service(tenant: TenantContext, semanas: int = 12) -> List[Dict[str, Any]]:
    query = """
        SELECT semana, sesiones, cierran_negativo, pct_cierre_negativo,
               con_urgencia_alta, pct_urgencia_alta, sin_respuesta, piden_humano,
               con_errores_validacion, total_errores_validacion,
               msgs_prom_sesion, ttr_1ra_p50_seg
        FROM semantic.v_salud_semanal
        ORDER BY semana DESC LIMIT $1
    """
    return await tenant_cache.get_or_set(
        tenant, f"salud_semanal:{semanas}",
        lambda: tenant_fetch(tenant, query, semanas, schema="semantic", label="salud_semanal")
    )


async def get_operacion_diaria_service(
    tenant: TenantContext, fecha_inicio=None, fecha_fin=None, canal=None
) -> List[Dict[str, Any]]:
    where, params = _build_filters(fecha_inicio, fecha_fin, {"canal": canal})
    query = f"""
        SELECT fecha, canal, sesiones, sin_respuesta, con_errores,
               inicia_fuera_horario, msgs_prom
        FROM semantic.v_operacion_diaria
        {where}
        ORDER BY fecha DESC, canal
    """
    key = f"operacion_diaria:{fecha_inicio}:{fecha_fin}:{canal}"
    return await tenant_cache.get_or_set(
        tenant, key,
        lambda: tenant_fetch(tenant, query, *params, schema="semantic", label="operacion_diaria")
    )


async def get_mix_legal_service(tenant: TenantContext) -> List[Dict[str, Any]]:
    return await tenant_cache.get_or_set(
        tenant, "mix_legal",
        lambda: tenant_fetch(
            tenant,
            "SELECT materia, clientes, fichas_iniciadas, citas, conv_pct "
            "FROM semantic.v_mix_legal",
            schema="semantic", label="mix_legal")
    )


async def get_mix_intent_semanal_service(tenant: TenantContext, semanas: int = 12) -> List[Dict[str, Any]]:
    query = """
        SELECT semana, intent, sesiones
        FROM semantic.v_mix_intent_semanal
        ORDER BY semana DESC, sesiones DESC
        LIMIT $1 * 10
    """
    return await tenant_cache.get_or_set(
        tenant, f"mix_intent:{semanas}",
        lambda: tenant_fetch(tenant, query, semanas, schema="semantic", label="mix_intent")
    )


# ============================================================================
# RESUMEN EJECUTIVO (extendido)
# ============================================================================

async def _safe(coro, nombre: str) -> Dict[str, Any]:
    try:
        return await coro
    except Exception as e:
        logger.warning(f"⚠️ KPI '{nombre}' no disponible, se omite del resumen: {e}")
        return {}


async def _funnel_ultima_semana(tenant: TenantContext) -> Dict[str, Any]:
    row = await tenant_fetchrow(
        tenant,
        "SELECT * FROM semantic.v_funnel_semanal ORDER BY semana DESC LIMIT 1",
        schema="semantic", label="funnel_ultima_semana")
    return {"funnel_ultima_semana": row}


async def get_dashboard_resumen_service(tenant: TenantContext) -> Dict[str, Any]:
    async def _compute():
        resultados = await asyncio.gather(
            _safe(get_conversaciones_total_service(tenant), "conversaciones"),
            _safe(get_clientes_total_service(tenant), "clientes"),
            _safe(get_agendas_total_service(tenant), "agendas"),
            _safe(get_ttr_bot_total_service(tenant), "ttr"),
            _safe(get_conversion_agenda_total_service(tenant), "conversion"),
            _safe(tenant_fetchrow(
                tenant,
                "SELECT COUNT(*)::int AS escalaciones_sin_notificar "
                "FROM semantic.v_escalaciones WHERE notificado_whatsapp = FALSE",
                schema="semantic", label="esc_sin_notificar"), "esc_pendientes"),
            _safe(tenant_fetchrow(
                tenant,
                "SELECT COUNT(*)::int AS escalaciones_demanda_real "
                "FROM semantic.v_escalaciones WHERE es_demanda_real",
                schema="semantic", label="esc_demanda"), "esc_demanda"),
            _safe(_funnel_ultima_semana(tenant), "funnel"),
        )
        merged: Dict[str, Any] = {}
        for r in resultados:
            merged.update(r)
        return merged

    return await tenant_cache.get_or_set(tenant, "resumen", _compute)


# ============================================================================
# WARM-UP / HEALTH
# ============================================================================

async def warmup(tenant: TenantContext) -> None:
    try:
        await get_dashboard_resumen_service(tenant)
        logger.info(f"🔥 Warm-up dashboard OK para tenant {tenant.tenant_id[:8]}")
    except Exception as e:
        logger.warning(f"⚠️ Warm-up dashboard falló (no fatal): {e}")


async def health_semantic_service(tenant: TenantContext) -> Dict[str, Any]:
    try:
        row = await tenant_fetchrow(
            tenant,
            "SELECT total_conversaciones FROM semantic.v_conversaciones_total",
            schema="semantic", label="health_semantic")
        return {"status": "ok", "esquema": "semantic v2", "muestra": row}
    except Exception as e:
        return {"status": "error", "error": str(e)}
