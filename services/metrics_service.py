#!/usr/bin/env python3
# services/metrics_service.py
# KPIs del overview — capa limpia (b_clientes / b_sesiones) en TZ America/Santiago.

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict
from zoneinfo import ZoneInfo

from auth.tenancy import TenantContext
from core.tenant_db import tenant_cache, tenant_fetch_raw, tenant_fetchrow_raw

logger = logging.getLogger("leyia.metrics")

RANGES = {"7d": 7, "30d": 30, "90d": 90}
DIAS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
C_RESUELTO, C_ESCALADO = "#3b82f6", "#f59e0b"
TZ = ZoneInfo("America/Santiago")

TTL_OVERVIEW = 120.0

# ---- Funnel + deltas por cliente (cohorte: primer contacto) ----
SQL_FUNNEL = """
    SELECT
      count(*) FILTER (WHERE primer_contacto >= $2)  AS cli_now,
      count(*) FILTER (WHERE primer_contacto <  $2)  AS cli_prev,
      count(*) FILTER (WHERE primer_contacto >= $2 AND inicio_ficha)   AS fini_now,
      count(*) FILTER (WHERE primer_contacto <  $2 AND inicio_ficha)   AS fini_prev,
      count(*) FILTER (WHERE primer_contacto >= $2 AND completo_ficha) AS fcom_now,
      count(*) FILTER (WHERE primer_contacto <  $2 AND completo_ficha) AS fcom_prev,
      count(*) FILTER (WHERE primer_contacto >= $2 AND agendo_cita)    AS cita_now,
      count(*) FILTER (WHERE primer_contacto <  $2 AND agendo_cita)    AS cita_prev,
      count(*) FILTER (WHERE primer_contacto >= $2
                       AND motivo_escalacion = 'hablar_abogada')       AS esc_now,
      count(*) FILTER (WHERE primer_contacto <  $2
                       AND motivo_escalacion = 'hablar_abogada')       AS esc_prev,
      count(*) FILTER (WHERE primer_contacto >= $2
                       AND turnos_pidio_humano > 0)                    AS ped_now,
      count(*) FILTER (WHERE primer_contacto <  $2
                       AND turnos_pidio_humano > 0)                    AS ped_prev
    FROM b_clientes
    WHERE primer_contacto >= $1 AND primer_contacto < $3
"""

# ---- Sesiones + mensajes procesados ----
SQL_SES_MSGS = """
    SELECT
      count(*) FILTER (WHERE inicio >= $2) AS ses_now,
      count(*) FILTER (WHERE inicio <  $2) AS ses_prev,
      coalesce(sum(msg_humano + msg_ai) FILTER (WHERE inicio >= $2), 0) AS msgs_now,
      coalesce(sum(msg_humano + msg_ai) FILTER (WHERE inicio <  $2), 0) AS msgs_prev
    FROM b_sesiones
    WHERE inicio >= $1 AND inicio < $3
"""

# ---- TTR 1ra respuesta (p50/p95, sin duplicados ni gaps) ----
SQL_TTR = """
    SELECT percentile_cont(0.5)  WITHIN GROUP (ORDER BY ttr_1ra_respuesta_seg) AS p50_s,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY ttr_1ra_respuesta_seg) AS p95_s
    FROM b_sesiones
    WHERE inicio >= $1 AND inicio < $2 AND ttr_1ra_respuesta_seg IS NOT NULL
"""

# ---- Sesiones por día (bucket en hora Chile) ----
SQL_BY_DAY = """
    SELECT fecha_local AS d, count(*) AS n
    FROM b_sesiones
    WHERE fecha_local >= ($1 AT TIME ZONE 'America/Santiago')::date
      AND fecha_local <= ($2 AT TIME ZONE 'America/Santiago')::date
    GROUP BY 1
"""

# ---- Mix de intents (top 5) ----
SQL_TOP = """
    SELECT COALESCE(intent_dominante, 'sin_clasificar') AS intent, count(*) AS n
    FROM b_sesiones
    WHERE inicio >= $1 AND inicio < $2
    GROUP BY 1 ORDER BY n DESC LIMIT 5
"""

# ---- Leads recientes (solo threads válidos vía join a b_clientes) ----
SQL_RECENT = """
    SELECT COALESCE(NULLIF(l.nombre, ''), l.telefono, 'Sin nombre') AS nombre,
           COALESCE(NULLIF(l.categoria, ''), 'General') AS area,
           COALESCE(NULLIF(l.etapa_proceso, ''), 'nuevo') AS status,
           l.created_at
    FROM leads l
    JOIN b_clientes bc ON bc.thread_id = l.thread_id
    ORDER BY l.created_at DESC LIMIT 5
"""


def _delta(actual: float, anterior: float) -> int:
    return round((actual - anterior) / anterior * 100) if anterior else 0


def _pct(parte: float, total: float) -> int:
    return round(parte / total * 100) if total else 0


async def _compute_overview(tenant: TenantContext, range_str: str) -> Dict[str, Any]:
    days = RANGES.get(range_str, 7)
    end = datetime.now(TZ)
    start = end - timedelta(days=days)
    prev_start = start - timedelta(days=days)

    (funnel_row, ses_row,
     ttr_now_row, ttr_prev_row,
     by_day_rows, top_rows, recent_rows) = await asyncio.gather(
        tenant_fetchrow_raw(tenant, SQL_FUNNEL, prev_start, start, end,
                            schema="public", label="metrics.funnel"),
        tenant_fetchrow_raw(tenant, SQL_SES_MSGS, prev_start, start, end,
                            schema="public", label="metrics.ses_msgs"),
        tenant_fetchrow_raw(tenant, SQL_TTR, start, end,
                            schema="public", label="metrics.ttr_now"),
        tenant_fetchrow_raw(tenant, SQL_TTR, prev_start, start,
                            schema="public", label="metrics.ttr_prev"),
        tenant_fetch_raw(tenant, SQL_BY_DAY, start, end,
                         schema="public", label="metrics.by_day"),
        tenant_fetch_raw(tenant, SQL_TOP, start, end,
                         schema="public", label="metrics.top"),
        tenant_fetch_raw(tenant, SQL_RECENT,
                         schema="public", label="metrics.recent"),
    )

    g = lambda row, k: (row[k] if row and row[k] is not None else 0)

    cli_now, cli_prev = g(funnel_row, "cli_now"), g(funnel_row, "cli_prev")
    ses_now, ses_prev = g(ses_row, "ses_now"), g(ses_row, "ses_prev")
    msgs_now, msgs_prev = g(ses_row, "msgs_now"), g(ses_row, "msgs_prev")
    cita_now, cita_prev = g(funnel_row, "cita_now"), g(funnel_row, "cita_prev")
    fini_now, fini_prev = g(funnel_row, "fini_now"), g(funnel_row, "fini_prev")
    fcom_now, fcom_prev = g(funnel_row, "fcom_now"), g(funnel_row, "fcom_prev")
    esc_now, esc_prev = g(funnel_row, "esc_now"), g(funnel_row, "esc_prev")
    ped_now = g(funnel_row, "ped_now")

    conv_now, conv_prev = _pct(cita_now, cli_now), _pct(cita_prev, cli_prev)
    ttr_now = float(g(ttr_now_row, "p50_s"))
    ttr_prev = float(g(ttr_prev_row, "p50_s"))
    ttr_p95_now = float(g(ttr_now_row, "p95_s"))

    # ---- gráfico por día (fechas locales) ----
    by_day = {r["d"]: r["n"] for r in by_day_rows}
    conversations_by_day = []
    for i in range(days):
        d = (start + timedelta(days=i)).date()
        label = DIAS[d.weekday()] if days == 7 else f"{d.day}/{d.month}"
        conversations_by_day.append({"label": label, "value": by_day.get(d, 0)})

    top_intents = [{"label": r["intent"], "value": r["n"]} for r in top_rows]

    def fmt(dt: datetime) -> str:
        dt_local = dt.astimezone(TZ) if dt.tzinfo else dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ)
        if dt_local.date() == end.date():
            return dt_local.strftime("%H:%M")
        if dt_local.date() == (end - timedelta(days=1)).date():
            return "Ayer"
        return dt_local.strftime("%d/%m")

    recent_leads = [
        {"name": r["nombre"], "area": r["area"], "status": r["status"],
         "time": fmt(r["created_at"])}
        for r in recent_rows
    ]

    gestion_sin_humano = cli_now - esc_now  # resueltas sin derivación demandada

    return {
        "kpis": [
            {"id": "conversations", "label": "Sesiones",
             "value": ses_now, "delta": _delta(ses_now, ses_prev)},
            {"id": "clientes", "label": "Clientes nuevos",
             "value": cli_now, "delta": _delta(cli_now, cli_prev)},
            {"id": "conversion", "label": "Conversión a cita",
             "value": f"{conv_now}%", "delta": conv_now - conv_prev},  # puntos, no relativo
            {"id": "agendas", "label": "Citas agendadas",
             "value": cita_now, "delta": _delta(cita_now, cita_prev)},
            {"id": "fichas", "label": "Fichas completas",
             "value": fcom_now, "delta": _delta(fcom_now, fcom_prev)},
            {"id": "response_time", "label": "TTR mediana 1ra respuesta",
             "value": f"{ttr_now:.1f}s" if ttr_now else "—",
             "delta": _delta(ttr_now, ttr_prev)},
            {"id": "escalations", "label": "Escaladas a abogado (demanda)",
             "value": esc_now, "delta": _delta(esc_now, esc_prev)},
            {"id": "messages", "label": "Mensajes procesados",
             "value": msgs_now, "delta": _delta(msgs_now, msgs_prev)},
        ],
        "conversationsByDay": conversations_by_day,
        "resolution": [
            {"label": "Gestionadas sin humano", "value": gestion_sin_humano, "color": C_RESUELTO},
            {"label": "Escaladas a abogado", "value": esc_now, "color": C_ESCALADO},
        ],
        "topIntents": top_intents,
        "recentLeads": recent_leads,
        # ---- secciones nuevas ----
        "funnel": {
            "clientes_nuevos": cli_now,
            "fichas_iniciadas": fini_now,
            "fichas_completas": fcom_now,
            "citas": cita_now,
            "conversion_cita_pct": conv_now,
            "tasa_completado_ficha_pct": _pct(fcom_now, fini_now),
        },
        "gapHumano": {
            "pidieron_humano": ped_now,
            "escalados": esc_now,
            "ttr_p95_s": round(ttr_p95_now, 1),
        },
    }


async def get_overview_metrics_service(tenant: TenantContext, range_str: str) -> dict:
    return await tenant_cache.get_or_set(
        tenant,
        f"overview:{range_str}",
        lambda: _compute_overview(tenant, range_str),
    )
