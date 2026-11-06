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
#   • Hilos          → semantic.v_conversation_threads / semantic.v_conversation_messages
#
# Regla: el frontend PINTA. El backend CALCULA.
#
# Normalización de datos crudos (verificado en diagnóstico):
#   • conversations.channel: 'wa' + 'whatsapp' → 'whatsapp' · 'desconocido'/NULL → 'otro'
#   • conversations.status:  'abierto' → 'open'
#   • messages.role:         'human' → 'user' · 'ai' → 'assistant' (role_norm en la vista)
# ============================================================================

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


def _canal_norm(channel: Optional[str]) -> str:
    """Normaliza canal crudo → dominio del dashboard (whatsapp/web/cli/test/otro)."""
    if channel in ("wa", "whatsapp"):
        return "whatsapp"
    if channel in ("web", "cli", "test"):
        return channel
    return "otro"   # 'desconocido', NULL, valores futuros


def _status_norm(status: Optional[str]) -> str:
    """Normaliza status crudo → dominio del dashboard (open/closed/escalado/agendado)."""
    if status in (None, "abierto"):
        return "open"
    return status


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
"""

SQL_ESCALAMIENTOS_HOY = """
    SELECT total_hoy, con_telefono_hoy, sin_telefono_hoy
    FROM semantic.v_escalamientos_hoy
"""

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
"""

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

    # ── FIX 9: COUNT(*) OVER() — un solo acquire en vez de dos ────────────
    # El total viaja pegado en cada fila. Se elimina el segundo round-trip
    # (query_count) en el camino normal. Solo hay fallback al COUNT separado
    # cuando `offset` cae más allá del último registro (rows == []).
    query = f"""
        SELECT
            booking_id, thread_id, conversation_id, lead_id, evento_id,
            cliente_nombre, cliente_email,
            modalidad, modalidad_norm, canal,
            fecha_cita_local, hora_inicio_local, hora_fin,
            meet_link, html_link, tiene_meet,
            duracion_seg_estimada,
            inicio_cita, created_at,
            COUNT(*) OVER() AS total_count
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
        rows = _rows_to_dicts(await tenant_fetch_raw(
            tenant, query, *params_where, limite, offset,
            schema="semantic", label="citas_detalle_v2",
        ))
        if rows:
            # Extraer total del primer row y limpiar de todos.
            total = int(rows[0].pop("total_count", 0) or 0)
            for r in rows[1:]:
                r.pop("total_count", None)
        else:
            # Offset fuera del último registro: COUNT(*) OVER() devolvió 0
            # filas. Fallback al COUNT explícito (solo este edge case).
            row_count = await tenant_fetchrow_raw(
                tenant, query_count, *params_where,
                schema="semantic", label="citas_detalle_v2_count",
            )
            total = int((_row_to_dict(row_count) or {}).get("total") or 0)
        return {"rows": rows, "total": total}

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

    FIX 8: cacheado. Card KPI del dashboard — 4-5 dupes por reload del front.
    No requiere dato "live al segundo"; el feed live es /escalamientos/ultimos.
    """
    key = "escalamientos_hoy:v1"

    async def _load():
        return _row_to_dict(await tenant_fetchrow_raw(
            tenant, SQL_ESCALAMIENTOS_HOY,
            schema="semantic", label="escalamientos_hoy",
        )) or {}

    row = await tenant_cache.get_or_set(tenant, key, _load)

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
# OVERVIEW — 4 KPIs (secuencial + caché del payload completo)
# ============================================================================
def _build_overview_payload(
    conv: Dict[str, Any],
    ag:   Dict[str, Any],
    ttr:  Dict[str, Any],
    tasa: Dict[str, Any],
) -> Dict[str, Any]:
    """Construye el payload final del overview. Función pura (testeable)."""
    conv_subtitle = f"{conv['conversaciones']} conversaciones únicas"
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


async def get_overview_metrics_service(tenant: TenantContext, range_str: str) -> Dict[str, Any]:
    """
    FIX 6: secuencial (no asyncio.gather). Con queries de 3-15ms, paralelizar
    4 queries gana ~15ms pero consume 4 conexiones simultáneas del pool por
    request. Secuencial = 1 conexión, 4 acquires secuenciales.

    FIX 7: el payload final se cachea bajo 1 sola key. Los 4 requests
    concurrentes del front (React StrictMode / polling mal deduplicado)
    ejecutan el `_load` UNA vez; los otros 3 hacen cache hit instantáneo.
    """
    key = f"overview:v1:{range_str}"

    async def _load():
        # Secuencial. Cada `get_*_service` internamente ya tiene su propio
        # cache (conv_total:7d, agendas_total:7d, …), así que si el overview
        # fue invalidado pero esos no, los 4 calls son hits casi gratuitos.
        conv = await get_conversaciones_service(tenant, range_str)
        ag   = await get_agendas_service(tenant, range_str)
        ttr  = await get_ttr_service(tenant, range_str)
        tasa = await get_tasa_conversion_service(tenant, range_str)
        return _build_overview_payload(conv, ag, ttr, tasa)

    return await tenant_cache.get_or_set(tenant, key, _load)


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
    """
    Conversaciones por día, pivoteadas por canal.
    FIX 8: cacheado por (range_str). Antes se ejecutaba 6 veces en 2 min
    con la misma query — cada hit tomaba una conexión innecesarialmente.

    ⬅️ Normalización: el pivote usa _canal_norm() para que 'wa' y 'whatsapp'
    se fusionen en una sola serie 'whatsapp' (raw: 49 + 1 conversaciones).
    """
    days  = RANGES.get(range_str, 30)
    hoy   = datetime.now(TZ_CL).date()
    desde = hoy - timedelta(days=days)
    key   = f"conv_por_dia:v1:{range_str}"

    async def _load():
        rows = await tenant_fetch_raw(
            tenant, SQL_CONV_POR_DIA, desde, hoy,
            schema="semantic", label="conv_por_dia",
        )
        return _rows_to_dicts(rows)

    items = await tenant_cache.get_or_set(tenant, key, _load)

    # Pivote por fecha
    por_dia: Dict[str, Dict[str, Any]] = {}
    canales_ord: set = set()
    for r in items:
        f = r["fecha"]
        f_str = f.isoformat() if hasattr(f, "isoformat") else str(f)
        if f_str not in por_dia:
            por_dia[f_str] = {"fecha": f_str, "total": 0, "por_canal": {}}
        por_dia[f_str]["total"] += r["total"]
        # ⬅️ Normalizado + acumulado (raw podía traer 'wa' y 'whatsapp' por separado)
        canal = _canal_norm(r["channel"])
        por_dia[f_str]["por_canal"][canal] = (
            por_dia[f_str]["por_canal"].get(canal, 0) + r["total"]
        )
        canales_ord.add(canal)

    return {
        "period":  range_str,
        "inicio":  desde.isoformat(),
        "fin":     hoy.isoformat(),
        "canales": sorted(canales_ord),
        "por_dia": sorted(por_dia.values(), key=lambda x: x["fecha"]),
    }


async def get_conv_intenciones_service(tenant: TenantContext) -> Dict[str, Any]:
    """
    Distribución de intenciones.
    FIX 8: cacheado. Se ejecutaba 5 veces en 2 min.
    ⬅️ por_canal normalizado y acumulado con _canal_norm().
    """
    key = "conv_intenciones:v1"

    async def _load():
        rows = await tenant_fetch_raw(
            tenant, SQL_CONV_INTENCIONES,
            schema="semantic", label="conv_intenciones",
        )
        return _rows_to_dicts(rows)

    items = await tenant_cache.get_or_set(tenant, key, _load)

    # Consolidar por intent (ignorando el canal para el total principal)
    agg: Dict[str, Dict[str, Any]] = {}
    for r in items:
        k = r["intent"]
        if k not in agg:
            agg[k] = {"intent": k, "total": 0, "total_30d": 0, "total_hoy": 0, "por_canal": {}}
        agg[k]["total"]      += r["total"]
        agg[k]["total_30d"]  += r["total_30d"]
        agg[k]["total_hoy"]  += r["total_hoy"]
        c = _canal_norm(r["channel"])
        agg[k]["por_canal"][c] = agg[k]["por_canal"].get(c, 0) + r["total"]

    total = sum(v["total"] for v in agg.values()) or 1
    result = sorted(agg.values(), key=lambda x: x["total"], reverse=True)
    for v in result:
        v["pct"] = round(100.0 * v["total"] / total, 2)

    return {"total": total, "intenciones": result}


async def get_conv_categorias_service(tenant: TenantContext) -> Dict[str, Any]:
    """
    Distribución de categorías legales.
    FIX 8: cacheado.
    ⬅️ por_canal normalizado y acumulado con _canal_norm().
    """
    key = "conv_categorias:v1"

    async def _load():
        rows = await tenant_fetch_raw(
            tenant, SQL_CONV_CATEGORIAS,
            schema="semantic", label="conv_categorias",
        )
        return _rows_to_dicts(rows)

    items = await tenant_cache.get_or_set(tenant, key, _load)

    agg: Dict[str, Dict[str, Any]] = {}
    for r in items:
        k = r["categoria"]
        if k not in agg:
            agg[k] = {"categoria": k, "total": 0, "total_30d": 0, "total_hoy": 0, "por_canal": {}}
        agg[k]["total"]      += r["total"]
        agg[k]["total_30d"]  += r["total_30d"]
        agg[k]["total_hoy"]  += r["total_hoy"]
        c = _canal_norm(r["channel"])
        agg[k]["por_canal"][c] = agg[k]["por_canal"].get(c, 0) + r["total"]

    total = sum(v["total"] for v in agg.values()) or 1
    result = sorted(agg.values(), key=lambda x: x["total"], reverse=True)
    for v in result:
        v["pct"] = round(100.0 * v["total"] / total, 2)

    return {"total": total, "categorias": result}


async def get_conv_status_service(tenant: TenantContext) -> Dict[str, Any]:
    """
    Distribución por estado (open/closed/escalado/agendado).
    FIX 8: cacheado.
    ⬅️ Normalización con FUSIÓN de buckets: 'abierto' se suma dentro de
    'open', no basta con renombrar la etiqueta (raw: open=61 + abierto=30).
    """
    key = "conv_status:v1"

    async def _load():
        rows = await tenant_fetch_raw(
            tenant, SQL_CONV_STATUS,
            schema="semantic", label="conv_status",
        )
        return _rows_to_dicts(rows)

    items = await tenant_cache.get_or_set(tenant, key, _load)

    agg: Dict[str, int] = {}
    for r in items:
        k = _status_norm(r["status"])
        agg[k] = agg.get(k, 0) + int(r["total"])

    total = sum(agg.values()) or 1
    status_list = [
        {"status": k, "total": v, "pct": round(100.0 * v / total, 2)}
        for k, v in sorted(agg.items(), key=lambda x: x[1], reverse=True)
    ]
    return {"total": total, "status": status_list}


async def get_conv_kpis_service(tenant: TenantContext) -> Dict[str, Any]:
    """
    KPIs consolidados de conversaciones.
    FIX 8: cacheado. Se ejecutaba 7 veces en 2 min (16s cada una por cola
    de pool, no por SQL — el SQL tarda 1.8ms según pg_stat_statements).

    ⚠️ PENDIENTE: las columnas whatsapp/abiertas/etc. vienen precomputadas
    de semantic.v_conversations_kpis. Si esa vista filtra channel='whatsapp'
    o status='open' sin incluir 'wa'/'abierto', subcuenta — no es corregible
    aquí, hay que ajustar el DDL de la vista.
    """
    key = "conv_kpis:v1"

    async def _load():
        return _row_to_dict(await tenant_fetchrow_raw(
            tenant, SQL_CONV_KPIS,
            schema="semantic", label="conv_kpis",
        )) or {}

    row = await tenant_cache.get_or_set(tenant, key, _load)

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




# ============================================================================
# CONVERSATIONS · SUMMARY — 1 sola request, 1 conexión a la vez
# ============================================================================
async def get_conv_summary_service(
    tenant: TenantContext,
    range_str: str = "30d",
) -> Dict[str, Any]:
    """
    Payload consolidado de la página ConversationsPage.

    v2: se removió `intenciones` porque el panel "Intención detectada"
    ya no se muestra en la UI. Se ahorra una query (~180ms exec) por
    carga de página.

    El endpoint INDIVIDUAL /conversations/intenciones sigue activo
    (no se tocó el route, SQL, ni el service) — solo se dejó de llamar
    desde acá.
    """
    key = f"conv_summary:v2:{range_str}"   # ⬅️ v1 → v2 = invalida cache al instante

    async def _load():
        kpis       = await get_conv_kpis_service(tenant)
        por_dia    = await get_conv_por_dia_service(tenant, range_str)
        categorias = await get_conv_categorias_service(tenant)
        status     = await get_conv_status_service(tenant)
        return {
            "kpis":       kpis,
            "por_dia":    por_dia,
            "categorias": categorias,
            "status":     status,
        }

    return await tenant_cache.get_or_set(tenant, key, _load)


# ============================================================================
# CONVERSATIONS · HILOS — listado paginado + detalle del hilo completo
# ⬅️ NUEVO. Fuentes:
#   • semantic.v_conversation_threads   (1 fila por thread_id, con stats y
#                                        channel_norm/status_norm)
#   • semantic.v_conversation_messages  (mensajes con role_norm, orden ASC)
# ============================================================================
SQL_CONV_THREADS_COUNT = """
    SELECT COUNT(*)::int AS total
    FROM semantic.v_conversation_threads
    WHERE {where_sql}
"""

SQL_CONV_THREAD_HEAD = """
    SELECT
        conversation_id, thread_id,
        channel, channel_norm, status, status_norm,
        intent_first, category, summary,
        fecha_creacion_local, created_at, updated_at, closed_at,
        message_count, user_messages, assistant_messages,
        first_message_at, last_message_at
    FROM semantic.v_conversation_threads
    WHERE thread_id = $1
"""

SQL_CONV_THREAD_MESSAGES = """
    SELECT
        message_id, role, role_norm, content, route,
        intent, sentiment, urgency, category,
        created_at,
        COUNT(*) OVER() AS total_count
    FROM semantic.v_conversation_messages
    WHERE thread_id = $1
    ORDER BY created_at ASC, message_id ASC
    LIMIT $2 OFFSET $3
"""

SQL_CONV_THREAD_MESSAGES_COUNT = """
    SELECT COUNT(*)::int AS total
    FROM semantic.v_conversation_messages
    WHERE thread_id = $1
"""


async def get_conv_threads_service(
    tenant: TenantContext,
    range_str: str = "30d",
    status: Optional[str] = None,
    canal: Optional[str] = None,
    q: Optional[str] = None,
    include_test: bool = False,
    limite: int = 20,
    offset: int = 0,
) -> Dict[str, Any]:
    """
    Bandeja de conversaciones: 1 fila por thread_id, ordenada por última
    actividad (last_message_at DESC). Cacheada (FIX 8) + COUNT(*) OVER() (FIX 9).

    Los filtros `status` y `canal` se aplican sobre las columnas NORMALIZADAS
    (status_norm / channel_norm), por eso los patterns de la ruta no cambian:
    pedir canal=whatsapp trae tanto 'wa' como 'whatsapp'; status=open trae
    también los 'abierto'.
    """
    limite = max(1, min(100, limite))
    offset = max(0, offset)
    days  = RANGES.get(range_str, 30)
    hoy   = datetime.now(TZ_CL).date()
    desde = hoy - timedelta(days=days)

    filtros = ["fecha_creacion_local >= $1::date", "fecha_creacion_local <= $2::date"]
    params: List[Any] = [desde, hoy]
    p = 3

    if not include_test:
        filtros.append("channel IS DISTINCT FROM 'test'")
    if status:
        filtros.append(f"status_norm = ${p}")
        params.append(status)
        p += 1
    if canal:
        filtros.append(f"channel_norm = ${p}")
        params.append(canal)
        p += 1
    if q:
        filtros.append(f"(thread_id ILIKE ${p} OR summary ILIKE ${p})")
        params.append(f"%{q}%")
        p += 1

    where_sql = " AND ".join(filtros)

    query = f"""
        SELECT
            thread_id, conversation_id,
            channel, channel_norm, status, status_norm,
            intent_first, category, summary,
            fecha_creacion_local, created_at, updated_at, closed_at,
            message_count, user_messages, assistant_messages,
            first_message_at, last_message_at,
            last_message_preview, last_message_role,
            COUNT(*) OVER() AS total_count
        FROM semantic.v_conversation_threads
        WHERE {where_sql}
        ORDER BY last_message_at DESC NULLS LAST, created_at DESC
        LIMIT ${p} OFFSET ${p + 1}
    """
    query_count = SQL_CONV_THREADS_COUNT.format(where_sql=where_sql)

    key = f"conv_threads:v1:{range_str}:{status or ''}:{canal or ''}:{q or ''}:{include_test}:{limite}:{offset}"

    async def _load():
        rows = _rows_to_dicts(await tenant_fetch_raw(
            tenant, query, *params, limite, offset,
            schema="semantic", label="conv_threads",
        ))
        if rows:
            total = int(rows[0].pop("total_count", 0) or 0)
            for r in rows[1:]:
                r.pop("total_count", None)
        else:
            # Offset fuera del último registro → fallback COUNT explícito (FIX 9)
            row_count = await tenant_fetchrow_raw(
                tenant, query_count, *params,
                schema="semantic", label="conv_threads_count",
            )
            total = int((_row_to_dict(row_count) or {}).get("total") or 0)
        return {"rows": rows, "total": total}

    payload = await tenant_cache.get_or_set(tenant, key, _load)

    return {
        "period": range_str,
        "inicio": desde.isoformat(),
        "fin":    hoy.isoformat(),
        "filtros": {
            "status":       status or "all",
            "canal":        canal or "all",
            "q":            q or "",
            "include_test": include_test,
        },
        "paginacion": {
            "total":   payload["total"],
            "limite":  limite,
            "offset":  offset,
            "paginas": (payload["total"] + limite - 1) // limite if limite > 0 else 0,
        },
        "rows": payload["rows"],
    }


async def get_conv_thread_detail_service(
    tenant: TenantContext,
    thread_id: str,
    limite: int = 200,
    offset: int = 0,
) -> Optional[Dict[str, Any]]:
    """
    Hilo completo: cabecera (conversations) + mensajes ordenados ASC.

    SIN caché a propósito (patrón escalamientos/ultimos): un thread 'open'
    cambia en vivo y el front hará polling.

    Retorna None si el thread no existe ni en conversations ni en messages
    → la ruta lo traduce a 404. Si hay mensajes "huérfanos" sin fila en
    conversations, responde con conversacion=None + mensajes (no 404).
    """
    limite = max(1, min(1000, limite))
    offset = max(0, offset)

    head = _row_to_dict(await tenant_fetchrow_raw(
        tenant, SQL_CONV_THREAD_HEAD, thread_id,
        schema="semantic", label="conv_thread_head",
    ))

    mensajes = _rows_to_dicts(await tenant_fetch_raw(
        tenant, SQL_CONV_THREAD_MESSAGES, thread_id, limite, offset,
        schema="semantic", label="conv_thread_messages",
    ))

    if mensajes:
        total_msgs = int(mensajes[0].pop("total_count", 0) or 0)
        for r in mensajes[1:]:
            r.pop("total_count", None)
    elif offset > 0:
        # offset fuera de rango → COUNT(*) OVER() devolvió 0 filas (fallback FIX 9)
        row_count = await tenant_fetchrow_raw(
            tenant, SQL_CONV_THREAD_MESSAGES_COUNT, thread_id,
            schema="semantic", label="conv_thread_messages_count",
        )
        total_msgs = int((_row_to_dict(row_count) or {}).get("total") or 0)
    else:
        total_msgs = 0

    if head is None and total_msgs == 0:
        return None

    return {
        "thread_id":    thread_id,
        "conversacion": head,
        "paginacion": {
            "total":   total_msgs,
            "limite":  limite,
            "offset":  offset,
            "paginas": (total_msgs + limite - 1) // limite if limite > 0 else 0,
        },
        "mensajes": mensajes,
    }
