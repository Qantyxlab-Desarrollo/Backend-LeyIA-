#!/usr/bin/env python3
# services/mview_refresh_service.py
# Refresco programado de la capa b_* (matviews) por tenant.

import asyncio
import logging
import time
from typing import Dict

from auth.tenancy import TenantContext
from core.tenant_db import tenant_fetch

logger = logging.getLogger("leyia.mviews")

REFRESH_EVERY_SEG = 15 * 60          # cada 15 min
_ORDEN = ["b_mensajes", "b_sesiones", "b_clientes"]  # orden de dependencia

_loop_task: asyncio.Task | None = None


async def refresh_matviews(tenant: TenantContext) -> Dict[str, float]:
    """Refresca las matviews en orden. Devuelve duración por matview."""
    tiempos: Dict[str, float] = {}
    for mv in _ORDEN:
        t0 = time.monotonic()
        # NOTA: REFRESH debe correr fuera de transacción; tenant_fetch ya opera así.
        await tenant_fetch(
            tenant,
            f"REFRESH MATERIALIZED VIEW public.{mv}",
            schema="public",
            label=f"mview_refresh.{mv}",
        )
        tiempos[mv] = round(time.monotonic() - t0, 2)
        logger.info(f"♻️ REFRESH {mv} OK en {tiempos[mv]}s (tenant {tenant.tenant_id[:8]})")
    return tiempos


async def _refresh_loop(tenant: TenantContext, every_seg: int) -> None:
    """Bucle de fondo: refresco periódico + manejo de fallos sin caerse."""
    while True:
        try:
            await refresh_matviews(tenant)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"❌ refresh_matviews falló (sigue el loop): {e}")
        await asyncio.sleep(every_seg)


def start_refresh_loop(tenant: TenantContext, every_seg: int = REFRESH_EVERY_SEG) -> None:
    """Llamar una vez en el startup de la app (lifespan/startup event)."""
    global _loop_task
    if _loop_task is None or _loop_task.done():
        _loop_task = asyncio.create_task(_refresh_loop(tenant, every_seg))
        logger.info(f"🌀 mview refresh loop iniciado (cada {every_seg // 60} min)")


async def stop_refresh_loop() -> None:
    global _loop_task
    if _loop_task and not _loop_task.done():
        _loop_task.cancel()
