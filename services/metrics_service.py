#!/usr/bin/env python3
# services/metrics_service.py
# KPIs del overview — lectura sobre la BD operativa del tenant (esquema public).

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from auth.tenancy import TenantContext
from core.tenant_db import tenant_cache, tenant_fetch_raw, tenant_fetchrow_raw

logger = logging.getLogger("leyia.metrics")

RANGES = {"7d": 7, "30d": 30, "90d": 90}
DIAS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
C_RESUELTO, C_ESCALADO = "#3b82f6", "#f59e0b"
ROLE_IN, ROLE_OUT = ["human", "user"], ["ai", "assistant"]

TTL_OVERVIEW = 120.0

SQL_CONV = """
    SELECT
        count(*) FILTER (WHERE c.created_at >= $2) AS conv_now,
        count(*) FILTER (WHERE c.created_at <  $2) AS conv_prev,
        count(*) FILTER (WHERE c.created_at >= $2 AND EXISTS (
            SELECT 1 FROM public.escalations e WHERE e.conversation_id = c.id
        )) AS esc_now,
        count(*) FILTER (WHERE c.created_at <  $2 AND EXISTS (
            SELECT 1 FROM public.escalations e WHERE e.conversation_id = c.id
        )) AS esc_prev
    FROM public.conversations c
    WHERE c.created_at >= $1 AND c.created_at < $3
"""

SQL_LEADS = """
    SELECT
        count(*) FILTER (WHERE consentimiento IS TRUE AND created_at >= $2) AS leads_now,
        count(*) FILTER (WHERE consentimiento IS TRUE AND created_at <  $2) AS leads_prev
    FROM public.leads
    WHERE created_at >= $1 AND created_at < $3
"""

SQL_MSGS = """
    SELECT
        count(*) FILTER (WHERE created_at >= $2) AS msgs_now,
        count(*) FILTER (WHERE created_at <  $2) AS msgs_prev
    FROM public.messages
    WHERE created_at >= $1 AND created_at < $3
"""

SQL_TTR = """
    WITH first_in AS (
        SELECT conversation_id, MIN(created_at) AS t
        FROM public.messages
        WHERE role = ANY($1::text[]) AND created_at >= $2 AND created_at < $3
        GROUP BY 1
    ),
    first_out AS (
        SELECT m.conversation_id, MIN(m.created_at) AS t
        FROM public.messages m
        JOIN first_in fi USING (conversation_id)
        WHERE m.role = ANY($4::text[]) AND m.created_at > fi.t
        GROUP BY 1
    )
    SELECT AVG(EXTRACT(EPOCH FROM (fo.t - fi.t))) AS avg_s
    FROM first_in fi
    JOIN first_out fo USING (conversation_id)
"""

SQL_BY_DAY = """
    SELECT created_at::date AS d, count(*) AS n
    FROM public.conversations
    WHERE created_at >= $1 AND created_at < $2
    GROUP BY 1
"""

SQL_TOP = """
    SELECT COALESCE(NULLIF(category, ''), 'General') AS area, count(*) AS n
    FROM public.conversations
    WHERE created_at >= $1 AND created_at < $2
    GROUP BY 1 ORDER BY n DESC LIMIT 5
"""

SQL_RECENT = """
    SELECT COALESCE(NULLIF(l.nombre, ''), l.telefono, 'Sin nombre') AS nombre,
           COALESCE(NULLIF(l.categoria, ''), c.category, 'General') AS area,
           COALESCE(NULLIF(l.etapa_proceso, ''), 'nuevo') AS status,
           l.created_at
    FROM public.leads l
    LEFT JOIN public.conversations c ON c.id = l.conversation_id
    ORDER BY l.created_at DESC LIMIT 5
"""


def _delta(actual: float, anterior: float) -> int:
    return round((actual - anterior) / anterior * 100) if anterior else 0


async def _compute_overview(tenant: TenantContext, range_str: str) -> Dict[str, Any]:
    days = RANGES.get(range_str, 7)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    prev_start = start - timedelta(days=days)

    (conv_row, leads_row, msgs_row,
     ttr_now_row, ttr_prev_row,
     by_day_rows, top_rows, recent_rows) = await asyncio.gather(
        tenant_fetchrow_raw(tenant, SQL_CONV, prev_start, start, end,
                            schema="public", label="metrics.conv"),
        tenant_fetchrow_raw(tenant, SQL_LEADS, prev_start, start, end,
                            schema="public", label="metrics.leads"),
        tenant_fetchrow_raw(tenant, SQL_MSGS, prev_start, start, end,
                            schema="public", label="metrics.msgs"),
        tenant_fetchrow_raw(tenant, SQL_TTR, ROLE_IN, start, end, ROLE_OUT,
                            schema="public", label="metrics.ttr_now"),
        tenant_fetchrow_raw(tenant, SQL_TTR, ROLE_IN, prev_start, start, ROLE_OUT,
                            schema="public", label="metrics.ttr_prev"),
        tenant_fetch_raw(tenant, SQL_BY_DAY, start, end,
                         schema="public", label="metrics.by_day"),
        tenant_fetch_raw(tenant, SQL_TOP, start, end,
                         schema="public", label="metrics.top_areas"),
        tenant_fetch_raw(tenant, SQL_RECENT,
                         schema="public", label="metrics.recent_leads"),
    )

    now_p = {
        "conv": (conv_row["conv_now"] if conv_row else 0) or 0,
        "escalated": (conv_row["esc_now"] if conv_row else 0) or 0,
        "leads": (leads_row["leads_now"] if leads_row else 0) or 0,
        "msgs": (msgs_row["msgs_now"] if msgs_row else 0) or 0,
        "avg_response_s": float(ttr_now_row["avg_s"] or 0) if ttr_now_row else 0,
    }
    prev_p = {
        "conv": (conv_row["conv_prev"] if conv_row else 0) or 0,
        "escalated": (conv_row["esc_prev"] if conv_row else 0) or 0,
        "leads": (leads_row["leads_prev"] if leads_row else 0) or 0,
        "msgs": (msgs_row["msgs_prev"] if msgs_row else 0) or 0,
        "avg_response_s": float(ttr_prev_row["avg_s"] or 0) if ttr_prev_row else 0,
    }
    now_p["resolved"] = now_p["conv"] - now_p["escalated"]
    prev_p["resolved"] = prev_p["conv"] - prev_p["escalated"]

    by_day = {r["d"]: r["n"] for r in by_day_rows}
    conversations_by_day = []
    for i in range(days):
        d = (start + timedelta(days=i)).date()
        label = DIAS[d.weekday()] if days == 7 else f"{d.day}/{d.month}"
        conversations_by_day.append({"label": label, "value": by_day.get(d, 0)})

    top_intents = [{"label": r["area"], "value": r["n"]} for r in top_rows]

    def fmt(dt: datetime) -> str:
        if dt.date() == end.date():
            return dt.strftime("%H:%M")
        if dt.date() == (end - timedelta(days=1)).date():
            return "Ayer"
        return dt.strftime("%d/%m")

    recent_leads = [
        {"name": r["nombre"], "area": r["area"], "status": r["status"], "time": fmt(r["created_at"])}
        for r in recent_rows
    ]

    res_pct = round(now_p["resolved"] / now_p["conv"] * 100) if now_p["conv"] else 0
    res_prev = round(prev_p["resolved"] / prev_p["conv"] * 100) if prev_p["conv"] else 0
    tnow, tprev = now_p["avg_response_s"], prev_p["avg_response_s"]

    return {
        "kpis": [
            {"id": "conversations", "label": "Conversaciones", "value": now_p["conv"], "delta": _delta(now_p["conv"], prev_p["conv"])},
            {"id": "messages", "label": "Mensajes procesados", "value": now_p["msgs"], "delta": _delta(now_p["msgs"], prev_p["msgs"])},
            {"id": "resolution", "label": "Resolución automática", "value": f"{res_pct}%", "delta": res_pct - res_prev},
            {"id": "response_time", "label": "Tiempo medio de respuesta", "value": f"{tnow:.1f}s" if tnow else "—", "delta": _delta(tnow, tprev)},
            {"id": "escalations", "label": "Escaladas a abogado", "value": now_p["escalated"], "delta": _delta(now_p["escalated"], prev_p["escalated"])},
            {"id": "leads", "label": "Leads cualificados", "value": now_p["leads"], "delta": _delta(now_p["leads"], prev_p["leads"])},
        ],
        "conversationsByDay": conversations_by_day,
        "resolution": [
            {"label": "Resueltas por el agente", "value": now_p["resolved"], "color": C_RESUELTO},
            {"label": "Escaladas a abogado", "value": now_p["escalado"], "color": C_ESCALADO},
        ],
        "topIntents": top_intents,
        "recentLeads": recent_leads,
    }


async def get_overview_metrics_service(tenant: TenantContext, range_str: str) -> dict:
    return await tenant_cache.get_or_set(
        tenant,
        f"overview:{range_str}",
        lambda: _compute_overview(tenant, range_str),
    )
