#!/usr/bin/env python3
# services/db_core.py
"""
db_core — Núcleo compartido de acceso a datos (LeyIA)

Responsabilidades:
- Pool asyncpg singleton, inmutable a race conditions de arranque.
- Compatible con Supabase Pooler / PgBouncer en transaction mode (statement_cache_size=0).
- Fail-fast: command_timeout + reciclaje de conexiones idle muertas silenciosamente.
- Retry transparente ante conexiones inválidas (1 reintento con conexión fresca).
- Timing por query (warning automático si supera SLOW_QUERY_MS).
- TTLCache con fallback a datos stale: si la BD falla, la API sigue respondiendo.
"""

import os
import time
import asyncio
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from uuid import UUID

import asyncpg

logger = logging.getLogger("leyia.db")

# ============================================================================
# CONFIG (override por .env)
# ============================================================================

_COMMAND_TIMEOUT = float(os.getenv("DB_COMMAND_TIMEOUT_SEG", "15"))
SLOW_QUERY_MS = float(os.getenv("DB_SLOW_QUERY_MS", "2000"))
CACHE_TTL_SEG = float(os.getenv("CACHE_TTL_SEG", "120"))          # frescura normal
CACHE_STALE_TTL_SEG = float(os.getenv("CACHE_STALE_TTL_SEG", "900"))  # fallback si BD cae

# ============================================================================
# POOL (singleton, race-safe)
# ============================================================================

_pool: Optional[asyncpg.Pool] = None
_pool_lock = asyncio.Lock()


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:  # solo UNA coroutine crea el pool aunque lleguen N en paralelo
        if _pool is None:
            dsn = os.getenv("DATABASE_URL")
            if not dsn:
                raise RuntimeError("DATABASE_URL no definida en .env")
            t0 = time.monotonic()
            _pool = await asyncpg.create_pool(
                dsn=dsn,
                min_size=1,
                max_size=10,
                # PgBouncer/Supavisor en transaction mode (puerto 6543) reasigna
                # backends entre transacciones → los prepared statements cacheados
                # dejan de existir. Desactivar la caché elimina InvalidSQLStatementNameError.
                statement_cache_size=0,
                # Ninguna query puede colgar el worker más de N segundos.
                command_timeout=_COMMAND_TIMEOUT,
                # Recicla conexiones idle que el pooler/NAT mató en silencio.
                max_inactive_connection_lifetime=60,
                # Reciclaje preventivo por volumen (evita bloat de memoria en backend).
                max_queries=50_000,
                server_settings={"application_name": "leyia-api"},
            )
            logger.info(
                f"✅ Pool asyncpg creado en {(time.monotonic() - t0) * 1000:.0f}ms "
                f"(min=1 max=10 stmt_cache=0 timeout={_COMMAND_TIMEOUT}s)"
            )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("🛑 Pool asyncpg cerrado")


# ============================================================================
# SERIALIZACIÓN (asyncpg → JSON-safe)
# ============================================================================

def json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return round(value.total_seconds(), 2)
    if isinstance(value, UUID):
        return str(value)
    return value


def to_json(record: asyncpg.Record) -> Dict[str, Any]:
    return {k: json_safe(v) for k, v in dict(record).items()}


# ============================================================================
# EJECUCIÓN (timing + retry de conexiones inválidas)
# ============================================================================

_RETRYABLE = (
    asyncpg.ConnectionDoesNotExistError,
    asyncpg.ConnectionFailureError,
    asyncpg.InterfaceError,
)


def _log_timing(label: str, query: str, dt_ms: float) -> None:
    resumen = " ".join(query.split())[:110]
    msg = f"⏱️ SQL [{label}] {dt_ms:.0f}ms | {resumen}"
    logger.warning(f"🐌 {msg}") if dt_ms > SLOW_QUERY_MS else logger.info(msg)


async def _run(label: str, query: str, params: tuple, many: bool):
    t0 = time.monotonic()
    last_exc: Optional[Exception] = None
    for intento in (1, 2):
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                result = await (
                    conn.fetch(query, *params) if many
                    else conn.fetchrow(query, *params)
                )
            _log_timing(label, query, (time.monotonic() - t0) * 1000)
            return result
        except _RETRYABLE as e:
            last_exc = e
            logger.warning(f"🔌 [{label}] conexión inválida (intento {intento}/2): {e}")
    raise last_exc  # type: ignore[misc]


async def fetch_raw(query: str, *params, label: str = "fetch") -> List[asyncpg.Record]:
    """Filas crudas (Record) — para lógica interna que necesita date/datetime."""
    return await _run(label, query, params, many=True)


async def fetchrow_raw(query: str, *params, label: str = "fetchrow") -> Optional[asyncpg.Record]:
    return await _run(label, query, params, many=False)


async def fetch(query: str, *params, label: str = "fetch") -> List[Dict[str, Any]]:
    records = await fetch_raw(query, *params, label=label)
    return [to_json(r) for r in records]


async def fetchrow(query: str, *params, label: str = "fetchrow") -> Dict[str, Any]:
    record = await fetchrow_raw(query, *params, label=label)
    return to_json(record) if record else {}


# ============================================================================
# CACHÉ TTL con fallback STALE (alta disponibilidad)
# ============================================================================

class TTLCache:
    """
    Caché en memoria por proceso con dos niveles:
      - fresh (< ttl):          se sirve directo, latencia ~0ms.
      - stale (< stale_ttl):    solo se sirve si la recomputación FALLA (BD caída).
    Lock por key: N requests concurrentes sobre la misma key disparan UN solo cómputo.

    NOTA: no mutar los objetos devueltos (se comparten entre requests).
    """

    def __init__(self, ttl: float = CACHE_TTL_SEG, stale_ttl: float = CACHE_STALE_TTL_SEG):
        self.ttl = ttl
        self.stale_ttl = stale_ttl
        self._store: Dict[str, Tuple[float, Any]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._guard:
            return self._locks.setdefault(key, asyncio.Lock())

    async def get_or_set(self, key: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        now = time.monotonic()
        entry = self._store.get(key)
        if entry and (now - entry[0]) < self.ttl:
            logger.info(f"🎯 CACHE HIT   | {key} | age={now - entry[0]:.1f}s")
            return entry[1]

        lock = await self._lock_for(key)
        async with lock:
            now = time.monotonic()
            entry = self._store.get(key)
            if entry and (now - entry[0]) < self.ttl:  # otra coroutine ya computó
                return entry[1]
            try:
                value = await factory()
                self._store[key] = (time.monotonic(), value)
                logger.info(f"📦 CACHE SET   | {key}")
                return value
            except Exception as e:
                if entry and (now - entry[0]) < self.stale_ttl:
                    logger.warning(
                        f"♻️ CACHE STALE | {key} | age={now - entry[0]:.1f}s | causa: {e}"
                    )
                    return entry[1]
                raise

    def invalidate(self, prefix: Optional[str] = None) -> int:
        """Invalida todo o por prefijo (ej. tras un webhook que escribe datos)."""
        keys = list(self._store) if prefix is None else [
            k for k in self._store if k.startswith(prefix)
        ]
        for k in keys:
            del self._store[k]
        if keys:
            logger.info(f"🧹 CACHE INVALIDATE | {prefix or '*'} | {len(keys)} keys")
        return len(keys)


# Instancia compartida del proceso
cache = TTLCache()
