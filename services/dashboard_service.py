#!/usr/bin/env python3
# services/dashboard_service.py
# Capa de servicios — Dashboard LeyIA (semantic v2 sobre base limpia)

import asyncio
import logging
import traceback
from collections import OrderedDict
from datetime import date, datetime, time, timezone
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

from auth.tenancy import TenantContext
from core.tenant_db import tenant_cache, tenant_fetch, tenant_fetchrow, tenant_fetch_raw, tenant_fetchrow_raw

logger = logging.getLogger("leyia.dashboard")

TTL_TOTALS = 120.0
TTL_SERIES = 120.0
TTL_ESCALACIONES = 60.0
TTL_RESUMEN = 120.0

MESES_ES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
DIAS_ES = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]


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
        if valor is None or columna is None:
            continue
        params.append(valor)
        clauses.append(f"{columna} = ${len(params)}")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def _parse_fecha_texto(fecha_raw) -> Optional[date]:
    """Parsea fechas en formatos DD/MM/YYYY o YYYY-MM-DD."""
    if not fecha_raw:
        return None
    if isinstance(fecha_raw, date) and not isinstance(fecha_raw, datetime):
        return fecha_raw
    if isinstance(fecha_raw, datetime):
        return fecha_raw.date()
    if not isinstance(fecha_raw, str):
        return None

    s = fecha_raw.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def _normalizar_modalidad(modalidad_raw) -> str:
    """Normaliza modalidades con emojis a Online/Presencial."""
    if not modalidad_raw:
        return "Sin modalidad"
    lower = str(modalidad_raw).lower()
    if any(k in lower for k in ("online", "meet", "zoom", "videollamada", "virtual", "google meet")):
        return "Online"
    if any(k in lower for k in ("presencial", "oficina", "presencialmente")):
        return "Presencial"
    return str(modalidad_raw).strip()[:30] or "Sin modalidad"


# ============================================================================
# 1. CONVERSACIONES
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
# 2b. CITAS (por día de ocurrencia)
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
# 2c. PRÓXIMAS CITAS — desde public.bookings de la BD del cliente
# ============================================================================

async def get_proximas_citas_service(
    tenant: TenantContext,
    fecha_inicio: Optional[date] = None,
    fecha_fin: Optional[date] = None,
    modalidad: Optional[str] = None,
    limite: int = 100,
) -> Dict[str, Any]:
    """
    Próximas citas desde public.bookings de la base de datos del cliente.
    Usa el mismo mecanismo de conexión multi-tenant que el resto del dashboard
    (core.tenant_db), leyendo directamente del esquema public.
    """
    hoy = date.today()

    # 1. Query de bookings según modalidad
    if modalidad:
        query_sql = """
            SELECT id, thread_id, lead_id, evento_id, fecha, hora_inicio, hora_fin,
                   modalidad, meet_link, html_link, created_at, inicio_cita,
                   cliente_nombre, cliente_email
            FROM public.bookings
            WHERE modalidad = $1
            ORDER BY inicio_cita ASC
        """
        raw_rows = await tenant_fetch_raw(
            tenant, query_sql, modalidad,
            schema="public", label="proximas_citas_bookings"
        )
    else:
        query_sql = """
            SELECT id, thread_id, lead_id, evento_id, fecha, hora_inicio, hora_fin,
                   modalidad, meet_link, html_link, created_at, inicio_cita,
                   cliente_nombre, cliente_email
            FROM public.bookings
            ORDER BY inicio_cita ASC
        """
        raw_rows = await tenant_fetch_raw(
            tenant, query_sql,
            schema="public", label="proximas_citas_bookings"
        )

    # 2. Contar total en base del cliente
    total_db = 0
    try:
        count_row = await tenant_fetchrow_raw(
            tenant,
            "SELECT COUNT(*)::int AS n FROM public.bookings",
            schema="public", label="proximas_citas_count"
        )
        total_db = count_row.get("n", 0) if count_row else 0
    except Exception:
        pass

    # 3. Parsear y normalizar filas
    rows: List[Dict[str, Any]] = []
    for r in raw_rows:
        fecha_date = _parse_fecha_texto(r.get("fecha"))
        inicio_cita = r.get("inicio_cita")
        hora_inicio = None

        if isinstance(inicio_cita, datetime):
            hora_inicio = inicio_cita.strftime("%H:%M")
            if not fecha_date:
                fecha_date = inicio_cita.date()
        else:
            hora_raw = r.get("hora_inicio")
            hora_inicio = hora_raw[:5] if isinstance(hora_raw, str) and len(hora_raw) >= 5 else hora_raw

        if not fecha_date:
            continue

        rows.append({
            "id": r.get("id"),
            "thread_id": r.get("thread_id"),
            "lead_id": r.get("lead_id"),
            "evento_id": r.get("evento_id"),
            "fecha_cita": fecha_date,
            "hora": hora_inicio,
            "hora_fin": r.get("hora_fin"),
            "cliente_nombre": r.get("cliente_nombre"),
            "cliente_email": r.get("cliente_email"),
            "modalidad": _normalizar_modalidad(r.get("modalidad")),
            "modalidad_original": r.get("modalidad") or "Sin modalidad",
            "meet_link": r.get("meet_link"),
            "html_link": r.get("html_link"),
            "created_at": r.get("created_at"),
            "inicio_cita": inicio_cita,
        })

    # 4. Filtrar por rango de fechas
    fecha_desde = fecha_inicio or hoy
    fecha_hasta = fecha_fin

    rows_filtradas = [
        r for r in rows
        if r["fecha_cita"] >= fecha_desde
        and (not fecha_hasta or r["fecha_cita"] <= fecha_hasta)
    ]

    fallback_used = False
    if not rows_filtradas and not fecha_inicio and not fecha_fin:
        fallback_used = True
        rows_filtradas = [r for r in rows if r["fecha_cita"]]

    rows_filtradas.sort(key=lambda x: (x["fecha_cita"], x["hora"] or ""))
    rows_filtradas = rows_filtradas[:limite]

    total = len(rows_filtradas)
    online = sum(1 for r in rows_filtradas if r["modalidad"] == "Online")
    presencial = sum(1 for r in rows_filtradas if r["modalidad"] == "Presencial")

    # 5. Agrupar por día
    dias: "OrderedDict[date, Dict[str, Any]]" = OrderedDict()
    for r in rows_filtradas:
        fc = r["fecha_cita"]
        if fc not in dias:
            dias[fc] = {
                "fecha": fc.isoformat(),
                "fecha_cita": fc,
                "dia_semana": DIAS_ES[fc.weekday()],
                "dia": fc.day,
                "mes": MESES_ES[fc.month - 1],
                "modalidad_resumen": {},
                "total_dia": 0,
                "citas": [],
            }
        dia = dias[fc]
        mod = r["modalidad"]
        dia["modalidad_resumen"][mod] = dia["modalidad_resumen"].get(mod, 0) + 1
        dia["total_dia"] += 1
        dia["citas"].append(dict(r))

    return {
        "total_citas": total,
        "online": online,
        "presencial": presencial,
        "orden": "cronologico",
        "fecha_inicio": fecha_inicio.isoformat() if fecha_inicio else hoy.isoformat(),
        "fecha_fin": fecha_fin.isoformat() if fecha_fin else None,
        "por_dia": list(dias.values()),
    }


# ============================================================================
# 3. ESCALACIONES
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
# 4. TTR DEL BOT (p50/p95)
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
# 6. NEGOCIO
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
