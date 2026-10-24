#!/usr/bin/env python3
# services/dashboard_service.py
# Capa de servicios — Dashboard LeyIA
# ============================================================================
# Fuentes:
#   • KPIs          → public.conversations / public.bookings / semantic.v_ttr_items
#                     / semantic.v_tasa_conversion_diaria
#   • Citas detalle → semantic.v_citas_detalle
#   • Citas resumen → semantic.v_citas_creadas_diarias / semantic.v_citas_agendadas_diarias
#   • Escalamientos → semantic.v_ultimos_escalamientos / semantic.v_escalamientos_hoy
#   • Conversaciones → semantic.v_conversations_por_dia / _por_intencion
#                     / _por_categoria / _por_status / _kpis
#
# Regla: el frontend PINTA. El backend CALCULA.
# ============================================================================

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from auth.tenancy import TenantContext
from core.tenant_db import (
    tenant_cache,
    tenant_fetch_raw,
    tenant_fetchrow_raw,
)

logger = logging.getLogger("leyia.dashboard")

# ============================================================================
# CONSTANTES
# ============================================================================
RANGES = {"7d": 7, "30d": 30, "90d": 90}
TZ_CL  = ZoneInfo("America/Santiago")


# ============================================================================
# HELPERS
# ============================================================================
def _delta(actual: float, anterior: float) -> int:
    return round((actual - anterior) / anterior * 100) if anterior else 0


def _row_to_dict(row) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return dict(row) if not isinstance(row, dict) else row


def _rows_to_dicts(rows) -> List[Dict[str, Any]]:
    if not rows:
        return []
    return [dict(r) if not isinstance(r, dict) else r for r in rows]


def _rango_fechas(range_str: str) -> Dict[str, date]:
    """
    Bordes de la ventana actual y la previa (hora Chile).
    Para KPIs de conversaciones / agendas / ttr / conversión.
    """
    days = RANGES.get(range_str, 7)
    _now = datetime.now(TZ_CL)
    fin  = _now.date()
    inicio = fin - timedelta(days=days)
    return {
        "inicio":      inicio,
        "fin":         fin,
        "prev_inicio": inicio - timedelta(days=days),
        "prev_fin":    inicio,
    }


def _fechas_modo(modo: str, range_str: str) -> Dict[str, date]:
    """
    Bordes según el modo de citas:
      • agendadas → [fin - range, fin]      (created_at en la vista)
      • pasadas   → [fin - range, fin]      (inicio_cita en la vista)
      • proximas  → [fin, fin + range]      (inicio_cita futuro)
    """
    days = RANGES.get(range_str, 7)
    hoy = datetime.now(TZ_CL).date()
    if modo == "proximas":
        return {"desde": hoy, "hasta": hoy + timedelta(days=days)}
    return {"desde": hoy - timedelta(days=days), "hasta": hoy}


# ============================================================================
# SQL — KPIs (conversaciones / agendas / ttr / conversion)
# ============================================================================
SQL_CONVERSACIONES = """
    WITH actual AS (
        SELECT COUNT(*)::int AS total
        FROM public.conversations c
        WHERE (c.created_at AT TIME ZONE 'America/Santiago')::date >= $1::date
          AND (c.created_at AT TIME ZONE 'America/Santiago')::date <= $2::date
    ),
    previo AS (
        SELECT COUNT(*)::int AS total
        FROM public.conversations c
        WHERE (c.created_at AT TIME ZONE 'America/Santiago')::date >= $3::date
          AND (c.created_at AT TIME ZONE 'America/Santiago')::date <  $4::date
    )
    SELECT actual.total AS conversaciones, previo.total AS conversaciones_prev
    FROM actual, previo
"""

SQL_AGENDAS = """
    WITH actual AS (
        SELECT COUNT(*)::int AS total
        FROM public.bookings b
        WHERE (b.created_at AT TIME ZONE 'America/Santiago')::date >= $1::date
          AND (b.created_at AT TIME ZONE 'America/Santiago')::date <= $2::date
    ),
    previo AS (
        SELECT COUNT(*)::int AS total
        FROM public.bookings b
        WHERE (b.created_at AT TIME ZONE 'America/Santiago')::date >= $3::date
          AND (b.created_at AT TIME ZONE 'America/Santiago')::date <  $4::date
    )
    SELECT actual.total AS agendas, previo.total AS agendas_prev
    FROM actual, previo
"""

SQL_TTR = """
    WITH actual AS (
        SELECT
            ROUND(percentile_cont(0.5)
                  WITHIN GROUP (ORDER BY ttr_seg::double precision)::numeric, 2) AS p50_s,
            ROUND(percentile_cont(0.95)
                  WITHIN GROUP (ORDER BY ttr_seg::double precision)::numeric, 2) AS p95_s,
            COUNT(*)::int AS muestras
        FROM semantic.v_ttr_items
        WHERE fecha >= $1::date AND fecha <= $2::date
    ),
    previo AS (
        SELECT
            ROUND(percentile_cont(0.5)
                  WITHIN GROUP (ORDER BY ttr_seg::double precision)::numeric, 2) AS p50_s
        FROM semantic.v_ttr_items
        WHERE fecha >= $3::date AND fecha < $4::date
    )
    SELECT
        COALESCE(actual.p50_s, 0)    AS p50_s,
        COALESCE(actual.p95_s, 0)    AS p95_s,
        COALESCE(actual.muestras, 0) AS muestras,
        COALESCE(previo.p50_s, 0)    AS p50_prev
    FROM actual, previo
"""

SQL_TASA_CONVERSION = """
    WITH actual AS (
        SELECT
            COALESCE(SUM(conversaciones), 0)::int AS conversaciones,
            COALESCE(SUM(citas), 0)::int          AS citas
        FROM semantic.v_tasa_conversion_diaria
        WHERE fecha >= $1::date AND fecha <= $2::date
    ),
    previo AS (
        SELECT
            COALESCE(SUM(conversaciones), 0)::int AS conversaciones,
            COALESCE(SUM(citas), 0)::int          AS citas
        FROM semantic.v_tasa_conversion_diaria
        WHERE fecha >= $3::date AND fecha < $4::date
    )
    SELECT
        actual.conversaciones                              AS conversaciones,
        actual.citas                                       AS citas,
        CASE WHEN actual.conversaciones > 0
             THEN ROUND(100.0 * actual.citas::numeric / actual.conversaciones::numeric, 2)
             ELSE 0.0 END                                  AS tasa_pct,
        previo.conversaciones                              AS conversaciones_prev,
        previo.citas                                       AS citas_prev,
        CASE WHEN previo.conversaciones > 0
             THEN ROUND(100.0 * previo.citas::numeric / previo.conversaciones::numeric, 2)
             ELSE 0.0 END                                  AS tasa_pct_prev
    FROM actual, previo
"""

# ============================================================================
# SQL — ESCALAMIENTOS (capa semántica)
# ============================================================================
# ✅ Vistas en `semantic`. Tabla base `public.escalations` solo para el COUNT.
# ⚡ Sin caché: estos datos deben verse "apenas entra uno". La vista tiene
#    ORDER BY created_at DESC + índice BRIN → consulta instantánea.
SQL_ESCALAMIENTOS_ULTIMOS = """
    SELECT
        id,
        creado_cl,
        thread_id,
        telefono,
        nombre,
        correo,
        mensaje,
        motivo,
        lead_id,
        notificado_whatsapp
    FROM semantic.v_ultimos_escalamientos
    LIMIT $1
"""  # ⬅️ ACTUALIZADO: thread_id, telefono, nombre, correo

SQL_ESCALAMIENTOS_HOY = """
    SELECT total_hoy, con_telefono_hoy, sin_telefono_hoy
    FROM semantic.v_escalamientos_hoy
"""  # ⬅️ ACTUALIZADO: nuevas columnas de la vista v2

SQL_ESCALAMIENTOS_TODOS = """
    SELECT
        id,
        creado_cl,
        thread_id,
        telefono,
        nombre,
        correo,
        mensaje,
        motivo,
        lead_id,
        notificado_whatsapp
    FROM semantic.v_ultimos_escalamientos
    LIMIT $1 OFFSET $2
"""  # ⬅️ ACTUALIZADO: misma estructura que /ultimos

SQL_ESCALAMIENTOS_TODOS_COUNT = """
    SELECT COUNT(*)::int AS total
    FROM public.escalations
"""


# ============================================================================
# KPI 1: CONVERSACIONES
# ============================================================================
async def get_conversaciones_service(tenant: TenantContext, range_str: str = "7d") -> Dict[str, Any]:
    fechas = _rango_fechas(range_str)
    key = f"conv_total:{range_str}"

    async def _load():
        return await tenant_fetchrow_raw(
            tenant, SQL_CONVERSACIONES,
            fechas["inicio"], fechas["fin"],
            fechas["prev_inicio"], fechas["prev_fin"],
            schema="public", label="conv_total",
        )

    row = _row_to_dict(await tenant_cache.get_or_set(tenant, key, _load)) or {}
    now  = int(row.get("conversaciones") or 0)
    prev = int(row.get("conversaciones_prev") or 0)
    return {
        "period":         range_str,
        "inicio":         fechas["inicio"].isoformat(),
        "fin":            fechas["fin"].isoformat(),
        "conversaciones": now,
        "delta":          _delta(now, prev),
        "prev": {
            "inicio":         fechas["prev_inicio"].isoformat(),
            "fin":            fechas["prev_fin"].isoformat(),
            "conversaciones": prev,
        },
    }


# ============================================================================
# KPI 2: AGENDAS
# ============================================================================
async def get_agendas_service(tenant: TenantContext, range_str: str = "7d") -> Dict[str, Any]:
    fechas = _rango_fechas(range_str)
    key = f"agendas_total:{range_str}"

    async def _load():
        return await tenant_fetchrow_raw(
            tenant, SQL_AGENDAS,
            fechas["inicio"], fechas["fin"],
            fechas["prev_inicio"], fechas["prev_fin"],
            schema="public", label="agendas_total",
        )

    row = _row_to_dict(await tenant_cache.get_or_set(tenant, key, _load)) or {}
    now  = int(row.get("agendas") or 0)
    prev = int(row.get("agendas_prev") or 0)
    return {
        "period":  range_str,
        "inicio":  fechas["inicio"].isoformat(),
        "fin":     fechas["fin"].isoformat(),
        "agendas": now,
        "delta":   _delta(now, prev),
        "prev": {
            "inicio":  fechas["prev_inicio"].isoformat(),
            "fin":     fechas["prev_fin"].isoformat(),
            "agendas": prev,
        },
    }


# ============================================================================
# KPI 3: TTR
# ============================================================================
async def get_ttr_service(tenant: TenantContext, range_str: str = "7d") -> Dict[str, Any]:
    fechas = _rango_fechas(range_str)
    key = f"ttr:{range_str}"

    async def _load():
        return await tenant_fetchrow_raw(
            tenant, SQL_TTR,
            fechas["inicio"], fechas["fin"],
            fechas["prev_inicio"], fechas["prev_fin"],
            schema="semantic", label="ttr",
        )

    row = _row_to_dict(await tenant_cache.get_or_set(tenant, key, _load)) or {}
    p50      = float(row.get("p50_s")    or 0.0)
    p95      = float(row.get("p95_s")    or 0.0)
    prev     = float(row.get("p50_prev") or 0.0)
    muestras = int(row.get("muestras")   or 0)
    return {
        "period":   range_str,
        "inicio":   fechas["inicio"].isoformat(),
        "fin":      fechas["fin"].isoformat(),
        "p50_s":    p50,
        "p95_s":    p95,
        "muestras": muestras,
        "delta":    _delta(p50, prev),
        "prev": {
            "inicio": fechas["prev_inicio"].isoformat(),
            "fin":    fechas["prev_fin"].isoformat(),
            "p50_s":  prev,
        },
    }


# ============================================================================
# KPI 4: CONVERSIÓN
# ============================================================================
async def get_tasa_conversion_service(tenant: TenantContext, range_str: str = "7d") -> Dict[str, Any]:
    fechas = _rango_fechas(range_str)
    key = f"tasa_conversion:{range_str}"

    async def _load():
        return await tenant_fetchrow_raw(
            tenant, SQL_TASA_CONVERSION,
            fechas["inicio"], fechas["fin"],
            fechas["prev_inicio"], fechas["prev_fin"],
            schema="semantic", label="tasa_conversion",
        )

    row = _row_to_dict(await tenant_cache.get_or_set(tenant, key, _load)) or {}
    conv_now   = int(row.get("conversaciones") or 0)
    citas_now  = int(row.get("citas") or 0)
    tasa_now   = float(row.get("tasa_pct") or 0.0)
    conv_prev  = int(row.get("conversaciones_prev") or 0)
    citas_prev = int(row.get("citas_prev") or 0)
    tasa_prev  = float(row.get("tasa_pct_prev") or 0.0)

    return {
        "period":         range_str,
        "inicio":         fechas["inicio"].isoformat(),
        "fin":            fechas["fin"].isoformat(),
        "conversaciones": conv_now,
        "citas":          citas_now,
        "tasa_pct":       tasa_now,
        "delta_puntos":   round(tasa_now - tasa_prev),
        "prev": {
            "inicio":         fechas["prev_inicio"].isoformat(),
            "fin":            fechas["prev_fin"].isoformat(),
            "conversaciones": conv_prev,
            "citas":          citas_prev,
            "tasa_pct":       tasa_prev,
        },
    }

# ============================================================================
# CITAS · DETALLE — vista semantic.v_citas_detalle
# ============================================================================
async def get_citas_detalle_service(
    tenant: TenantContext,
    range_str: str = "30d",
    modo: str = "agendadas",
    modalidad: Optional[str] = None,
    canal: Optional[str] = None,
    limite: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    modo = modo if modo in ("agendadas", "pasadas", "proximas") else "agendadas"
    limite = max(1, min(500, limite))
    offset = max(0, offset)

    fechas = _fechas_modo(modo, range_str)

    if modo == "agendadas":
        col_fecha = "fecha_creacion_local"
        orden     = "fecha_creacion_local DESC, created_at DESC"
    elif modo == "pasadas":
        col_fecha = "fecha_cita_local"
        orden     = "fecha_cita_local DESC, inicio_cita DESC"
    else:  # proximas
        col_fecha = "fecha_cita_local"
        orden     = "fecha_cita_local ASC, inicio_cita ASC"

    filtros = [f"{col_fecha} >= $1::date", f"{col_fecha} <= $2::date"]
    params_where: List[Any] = [fechas["desde"], fechas["hasta"]]
    p = 3

    if modalidad:
        filtros.append(f"modalidad_norm = ${p}")
        params_where.append(modalidad)
        p += 1
    if canal:
        filtros.append(f"canal = ${p}")
        params_where.append(canal)
        p += 1

    where_sql = " AND ".join(filtros)

    query = f"""
        SELECT
            booking_id, thread_id, conversation_id, lead_id, evento_id,
            cliente_nombre, cliente_email,
            modalidad, modalidad_norm, canal,
            fecha_cita_local, hora_inicio_local, hora_fin,
            meet_link, html_link, tiene_meet,
            duracion_seg_estimada,
            inicio_cita, created_at
        FROM semantic.v_citas_detalle
        WHERE {where_sql}
        ORDER BY {orden}
        LIMIT ${p} OFFSET ${p + 1}
    """

    query_count = f"""
        SELECT COUNT(*)::int AS total
        FROM semantic.v_citas_detalle
        WHERE {where_sql}
    """

    key = f"citas_detalle_v2:{range_str}:{modo}:{modalidad or ''}:{canal or ''}:{limite}:{offset}"

    async def _load():
        rows = await tenant_fetch_raw(
            tenant, query, *params_where, limite, offset,
            schema="semantic", label="citas_detalle_v2",
        )
        row_count = await tenant_fetchrow_raw(
            tenant, query_count, *params_where,
            schema="semantic", label="citas_detalle_v2_count",
        )
        return {
            "rows": _rows_to_dicts(rows),
            "total": (row_count or {}).get("total", 0),
        }

    payload = await tenant_cache.get_or_set(tenant, key, _load)

    return {
        "period": range_str,
        "modo":   modo,
        "inicio": fechas["desde"].isoformat(),
        "fin":    fechas["hasta"].isoformat(),
        "filtros": {
            "modalidad": modalidad or "all",
            "canal":     canal or "all",
        },
        "paginacion": {
            "total":   payload["total"],
            "limite":  limite,
            "offset":  offset,
            "paginas": (payload["total"] + limite - 1) // limite if limite > 0 else 0,
        },
        "rows": payload["rows"],
    }



# ============================================================================
# CITAS · RESUMEN — vistas v_citas_creadas_diarias / v_citas_agendadas_diarias
# ============================================================================
async def get_citas_resumen_service(
    tenant: TenantContext,
    range_str: str = "30d",
    modo: str = "agendadas",
) -> Dict[str, Any]:
    modo = modo if modo in ("agendadas", "pasadas", "proximas") else "agendadas"
    fechas = _fechas_modo(modo, range_str)

    vista = (
        "semantic.v_citas_creadas_diarias"
        if modo == "agendadas"
        else "semantic.v_citas_agendadas_diarias"
    )

    query_tot = f"""
        SELECT
            COUNT(*)::int                                                                 AS dias,
            COALESCE(SUM(total), 0)::int                                                  AS total,
            COALESCE(SUM(total) FILTER (WHERE modalidad_norm = 'Online'), 0)::int        AS online,
            COALESCE(SUM(total) FILTER (WHERE modalidad_norm = 'Presencial'), 0)::int    AS presencial,
            COALESCE(SUM(total) FILTER (WHERE modalidad_norm = 'Otra/Sin dato'), 0)::int AS sin_modalidad,
            COALESCE(SUM(con_meet), 0)::int                                               AS con_meet,
            COALESCE(SUM(leads_unicos), 0)::int                                           AS leads_unicos,
            COALESCE(SUM(total) FILTER (WHERE canal = 'whatsapp'), 0)::int                AS whatsapp,
            COALESCE(SUM(total) FILTER (WHERE canal = 'web'), 0)::int                     AS web,
            COALESCE(SUM(total) FILTER (WHERE canal = 'cli'), 0)::int                     AS cli,
            COALESCE(SUM(total) FILTER (WHERE canal = 'otro'), 0)::int                    AS otro
        FROM {vista}
        WHERE fecha >= $1::date AND fecha <= $2::date
    """

    query_dia = f"""
        SELECT fecha, total, modalidad_norm, canal
        FROM {vista}
        WHERE fecha >= $1::date AND fecha <= $2::date
        ORDER BY fecha DESC
    """

    key = f"citas_resumen_v2:{range_str}:{modo}"

    async def _load():
        row_tot = await tenant_fetchrow_raw(
            tenant, query_tot, fechas["desde"], fechas["hasta"],
            schema="semantic", label="citas_resumen_v2_tot",
        )
        rows_dia = await tenant_fetch_raw(
            tenant, query_dia, fechas["desde"], fechas["hasta"],
            schema="semantic", label="citas_resumen_v2_dia",
        )
        return {"tot": _row_to_dict(row_tot) or {}, "filas_dia": _rows_to_dicts(rows_dia)}

    payload = await tenant_cache.get_or_set(tenant, key, _load)
    tot = payload["tot"] or {}

    total      = int(tot.get("total") or 0)
    online     = int(tot.get("online") or 0)
    presencial = int(tot.get("presencial") or 0)
    sin_mod    = int(tot.get("sin_modalidad") or 0)

    por_dia: Dict[str, Dict[str, Any]] = {}
    for r in payload["filas_dia"]:
        f = r["fecha"]
        f_str = f.isoformat() if hasattr(f, "isoformat") else str(f)
        if f_str not in por_dia:
            por_dia[f_str] = {"fecha": f_str, "total": 0, "online": 0, "presencial": 0}
        por_dia[f_str]["total"] += r["total"]
        if r["modalidad_norm"] == "Online":
            por_dia[f_str]["online"] += r["total"]
        if r["modalidad_norm"] == "Presencial":
            por_dia[f_str]["presencial"] += r["total"]

    return {
        "period": range_str,
        "modo":   modo,
        "inicio": fechas["desde"].isoformat(),
        "fin":    fechas["hasta"].isoformat(),
        "total":  total,
        "por_modalidad": {
            "online":         online,
            "presencial":     presencial,
            "sin_modalidad":  sin_mod,
            "online_pct":     round(100.0 * online / total, 2) if total else 0.0,
            "presencial_pct": round(100.0 * presencial / total, 2) if total else 0.0,
        },
        "por_canal": {
            "whatsapp": int(tot.get("whatsapp") or 0),
            "web":      int(tot.get("web") or 0),
            "cli":      int(tot.get("cli") or 0),
            "otro":     int(tot.get("otro") or 0),
        },
        "con_meet":     int(tot.get("con_meet") or 0),
        "leads_unicos": int(tot.get("leads_unicos") or 0),
        "por_dia": sorted(por_dia.values(), key=lambda x: x["fecha"], reverse=True),
    }


# ============================================================================
# ESCALAMIENTOS · ÚLTIMOS — vista semantic.v_ultimos_escalamientos
# ============================================================================
async def get_escalamientos_ultimos_service(
    tenant: TenantContext,
    limite: int = 10,
) -> Dict[str, Any]:
    """
    Feed 'últimos registros' para el dashboard.
    Sin caché a propósito: el front hace polling y debe ver el dato apenas
    se inserta. La vista ya trae ORDER BY created_at DESC.
    """
    limite = max(1, min(100, limite))

    rows = await tenant_fetch_raw(
        tenant, SQL_ESCALAMIENTOS_ULTIMOS, limite,
        schema="semantic", label="escalamientos_ultimos",
    )
    items = _rows_to_dicts(rows)

    return {
        "limite": limite,
        "total":  len(items),
        "rows":   items,
    }


# ============================================================================
# ESCALAMIENTOS · HOY — vista semantic.v_escalamientos_hoy
# ============================================================================
async def get_escalamientos_hoy_service(tenant: TenantContext) -> Dict[str, Any]:
    """
    Conteo de escalamientos del día (hora Chile).
    La vista retorna 1 fila siempre (COUNT = 0 si no hay).
    """
    row = _row_to_dict(await tenant_fetchrow_raw(
        tenant, SQL_ESCALAMIENTOS_HOY,
        schema="semantic", label="escalamientos_hoy",
    )) or {}

    return {
        "fecha":            datetime.now(TZ_CL).date().isoformat(),
        "total_hoy":        int(row.get("total_hoy") or 0),
        "con_telefono_hoy": int(row.get("con_telefono_hoy") or 0),
        "sin_telefono_hoy": int(row.get("sin_telefono_hoy") or 0),
    }


# ============================================================================
# ESCALAMIENTOS · TODOS — listado completo paginado
# ============================================================================
async def get_escalamientos_todos_service(
    tenant: TenantContext,
    limite: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """
    Todos los escalamientos registrados, paginados (más recientes primero).
    El COUNT va directo a public.escalations (más barato que contar la vista).
    """
    limite = max(1, min(500, limite))
    offset = max(0, offset)

    rows = await tenant_fetch_raw(
        tenant, SQL_ESCALAMIENTOS_TODOS, limite, offset,
        schema="semantic", label="escalamientos_todos",
    )
    row_count = await tenant_fetchrow_raw(
        tenant, SQL_ESCALAMIENTOS_TODOS_COUNT,
        schema="public", label="escalamientos_todos_count",
    )

    total = int((_row_to_dict(row_count) or {}).get("total") or 0)

    return {
        "paginacion": {
            "total":   total,
            "limite":  limite,
            "offset":  offset,
            "paginas": (total + limite - 1) // limite if limite > 0 else 0,
        },
        "rows": _rows_to_dicts(rows),
    }


# ============================================================================
# OVERVIEW — 4 KPIs en paralelo
# ============================================================================
async def get_overview_metrics_service(tenant: TenantContext, range_str: str) -> Dict[str, Any]:
    conv, ag, ttr, tasa = await asyncio.gather(
        get_conversaciones_service(tenant, range_str),
        get_agendas_service(tenant, range_str),
        get_ttr_service(tenant, range_str),
        get_tasa_conversion_service(tenant, range_str),
    )

    conv_subtitle    = f"{conv['conversaciones']} conversaciones únicas"
    agendas_subtitle = (
        f"{ag['agendas']} citas registradas en el período"
        if ag["agendas"] > 0 else "Sin citas registradas"
    )
    conversion_subtitle = (
        f"{tasa['citas']} de {tasa['conversaciones']} lograron el propósito"
        if tasa["conversaciones"] > 0 else "Sin conversaciones registradas"
    )
    ttr_subtitle = (
        f"{ttr['muestras']} respuestas · p95 {ttr['p95_s']:.1f}s"
        if ttr["muestras"] > 0 else "Sin respuestas medidas"
    )

    return {
        "kpis": [
            {"id": "conversations", "label": "Conversaciones totales",
             "value": conv["conversaciones"], "subtitle": conv_subtitle, "delta": conv["delta"]},
            {"id": "conversion", "label": "Conversión por conversación",
             "value": f"{tasa['tasa_pct']:.0f}%",
             "numerator": tasa["citas"], "denominator": tasa["conversaciones"],
             "subtitle": conversion_subtitle, "delta": tasa["delta_puntos"]},
            {"id": "agendas", "label": "Agendas realizadas",
             "value": ag["agendas"], "subtitle": agendas_subtitle, "delta": ag["delta"]},
            {"id": "response_time", "label": "TTR mediana 1ra respuesta",
             "value": f"{ttr['p50_s']:.2f}s" if ttr["p50_s"] else "—",
             "subtitle": ttr_subtitle, "delta": ttr["delta"]},
        ],
        "conversion":     tasa,
        "conversaciones": conv,
        "agendas":        ag,
        "ttr":            ttr,
    }


# ============================================================================
# WARM-UP / HEALTH
# ============================================================================
async def warmup(tenant: TenantContext) -> None:
    try:
        await get_overview_metrics_service(tenant, "7d")
        logger.info(f"🔥 Warm-up dashboard OK para tenant {tenant.tenant_id[:8]}")
    except Exception as e:
        logger.warning(f"⚠️ Warm-up dashboard falló (no fatal): {e}")


async def health_semantic_service(tenant: TenantContext) -> Dict[str, Any]:
    try:
        row = await tenant_fetchrow_raw(
            tenant,
            "SELECT COUNT(*)::int AS filas FROM semantic.v_tasa_conversion_diaria",
            schema="semantic", label="health_semantic")
        return {"status": "ok", "esquema": "semantic + public",
                "muestra": _row_to_dict(row)}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ============================================================================
# CONVERSATIONS · ANALYTICS
# ============================================================================
SQL_CONV_POR_DIA = """
    SELECT fecha, channel, status, total
    FROM semantic.v_conversations_por_dia
    WHERE fecha >= $1::date AND fecha <= $2::date
    ORDER BY fecha ASC
"""

SQL_CONV_INTENCIONES = """
    SELECT intent, channel, total, total_30d, total_hoy
    FROM semantic.v_conversations_por_intencion
    ORDER BY total DESC, intent
"""

SQL_CONV_CATEGORIAS = """
    SELECT categoria, channel, total, total_30d, total_hoy
    FROM semantic.v_conversations_por_categoria
    ORDER BY total DESC, categoria
"""

SQL_CONV_STATUS = """
    SELECT status, total
    FROM semantic.v_conversations_por_status
    ORDER BY total DESC
"""

SQL_CONV_KPIS = """
    SELECT
        fecha_hoy, total, hoy, ultimos_7d, ultimos_30d,
        cerradas, abiertas, escaladas, agendadas,
        whatsapp, web, cli, otros_canales, canales_unicos
    FROM semantic.v_conversations_kpis
"""


async def get_conv_por_dia_service(
    tenant: TenantContext, range_str: str = "30d",
) -> Dict[str, Any]:
    """Conversaciones por día, pivoteadas por canal."""
    days  = RANGES.get(range_str, 30)
    hoy   = datetime.now(TZ_CL).date()
    desde = hoy - timedelta(days=days)

    rows = await tenant_fetch_raw(
        tenant, SQL_CONV_POR_DIA, desde, hoy,
        schema="semantic", label="conv_por_dia",
    )
    items = _rows_to_dicts(rows)

    # Pivote por fecha
    por_dia: Dict[str, Dict[str, Any]] = {}
    canales_ord: set = set()
    for r in items:
        f = r["fecha"]
        f_str = f.isoformat() if hasattr(f, "isoformat") else str(f)
        if f_str not in por_dia:
            por_dia[f_str] = {"fecha": f_str, "total": 0, "por_canal": {}}
        por_dia[f_str]["total"] += r["total"]
        por_dia[f_str]["por_canal"][r["channel"]] = (
            por_dia[f_str]["por_canal"].get(r["channel"], 0) + r["total"]
        )
        canales_ord.add(r["channel"])

    return {
        "period":  range_str,
        "inicio":  desde.isoformat(),
        "fin":     hoy.isoformat(),
        "canales": sorted(canales_ord),
        "por_dia": sorted(por_dia.values(), key=lambda x: x["fecha"]),
    }


async def get_conv_intenciones_service(tenant: TenantContext) -> Dict[str, Any]:
    rows = await tenant_fetch_raw(
        tenant, SQL_CONV_INTENCIONES,
        schema="semantic", label="conv_intenciones",
    )
    items = _rows_to_dicts(rows)

    # Consolidar por intent (ignorando el canal para el total principal)
    agg: Dict[str, Dict[str, Any]] = {}
    for r in items:
        k = r["intent"]
        if k not in agg:
            agg[k] = {"intent": k, "total": 0, "total_30d": 0, "total_hoy": 0, "por_canal": {}}
        agg[k]["total"]      += r["total"]
        agg[k]["total_30d"]  += r["total_30d"]
        agg[k]["total_hoy"]  += r["total_hoy"]
        agg[k]["por_canal"][r["channel"]] = r["total"]

    total = sum(v["total"] for v in agg.values()) or 1
    result = sorted(agg.values(), key=lambda x: x["total"], reverse=True)
    for v in result:
        v["pct"] = round(100.0 * v["total"] / total, 2)

    return {"total": total, "intenciones": result}


async def get_conv_categorias_service(tenant: TenantContext) -> Dict[str, Any]:
    rows = await tenant_fetch_raw(
        tenant, SQL_CONV_CATEGORIAS,
        schema="semantic", label="conv_categorias",
    )
    items = _rows_to_dicts(rows)

    agg: Dict[str, Dict[str, Any]] = {}
    for r in items:
        k = r["categoria"]
        if k not in agg:
            agg[k] = {"categoria": k, "total": 0, "total_30d": 0, "total_hoy": 0, "por_canal": {}}
        agg[k]["total"]      += r["total"]
        agg[k]["total_30d"]  += r["total_30d"]
        agg[k]["total_hoy"]  += r["total_hoy"]
        agg[k]["por_canal"][r["channel"]] = r["total"]

    total = sum(v["total"] for v in agg.values()) or 1
    result = sorted(agg.values(), key=lambda x: x["total"], reverse=True)
    for v in result:
        v["pct"] = round(100.0 * v["total"] / total, 2)

    return {"total": total, "categorias": result}


async def get_conv_status_service(tenant: TenantContext) -> Dict[str, Any]:
    rows = await tenant_fetch_raw(
        tenant, SQL_CONV_STATUS,
        schema="semantic", label="conv_status",
    )
    items = _rows_to_dicts(rows)
    total = sum(r["total"] for r in items) or 1
    for r in items:
        r["pct"] = round(100.0 * r["total"] / total, 2)
    return {"total": total, "status": items}


async def get_conv_kpis_service(tenant: TenantContext) -> Dict[str, Any]:
    row = _row_to_dict(await tenant_fetchrow_raw(
        tenant, SQL_CONV_KPIS,
        schema="semantic", label="conv_kpis",
    )) or {}

    return {
        "fecha_hoy": str(row.get("fecha_hoy") or ""),
        "total":     int(row.get("total")     or 0),
        "hoy":       int(row.get("hoy")       or 0),
        "ultimos_7d":  int(row.get("ultimos_7d")  or 0),
        "ultimos_30d": int(row.get("ultimos_30d") or 0),
        "por_status": {
            "cerradas":   int(row.get("cerradas")  or 0),
            "abiertas":   int(row.get("abiertas")  or 0),
            "escaladas":  int(row.get("escaladas") or 0),
            "agendadas":  int(row.get("agendadas") or 0),
        },
        "por_canal": {
            "whatsapp": int(row.get("whatsapp")      or 0),
            "web":      int(row.get("web")           or 0),
            "cli":      int(row.get("cli")           or 0),
            "otros":    int(row.get("otros_canales") or 0),
        },
        "canales_unicos": int(row.get("canales_unicos") or 0),
    }
