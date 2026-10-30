# core/tenant_db.py
import os
import re
import time
import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

import asyncpg
from asyncpg import Record

from auth.config import settings
from auth.db import db_cursor
from auth.tenancy import TenantContext
from core.secrets import get_secret_provider, SecretProvider
from services.db_core import cache as global_cache, SLOW_QUERY_MS, to_json

logger = logging.getLogger("leyia.tenant_db")

# ── Configuración del pool ────────────────────────────────────────────────
# FIX 2: min_size=4 evita handshakes SSL en el primer burst tras idle.
# FIX 2: max_size=10 absorbe el gather() paralelo de /overview (4 acquires
#        simultáneos por request) sin que los requests se apilen en cola.
# FIX 2: acquire_timeout → si el pool está lleno, fallar rápido (503) en
#        vez de colgar 55s en pool.acquire() sin límite.
TENANT_POOL_MIN_SIZE = int(os.getenv("TENANT_POOL_MIN_SIZE", "4"))
TENANT_POOL_MAX_SIZE = int(os.getenv("TENANT_POOL_MAX_SIZE", "10"))
TENANT_POOL_MAX_TENANTS = int(os.getenv("TENANT_POOL_MAX_TENANTS", "50"))
TENANT_POOL_IDLE_TTL = float(os.getenv("TENANT_POOL_IDLE_TTL", "600"))
TENANT_STATEMENT_TIMEOUT_MS = int(os.getenv("TENANT_STATEMENT_TIMEOUT_MS", "8000"))
TENANT_CONNECT_TIMEOUT = int(os.getenv("TENANT_CONNECT_TIMEOUT", "5"))
TENANT_COMMAND_TIMEOUT = float(os.getenv("TENANT_COMMAND_TIMEOUT", "15"))
TENANT_ACQUIRE_TIMEOUT = float(os.getenv("TENANT_ACQUIRE_TIMEOUT", "3"))

# search_path fijo — cubre tenant schemas (public + semantic).
# Se aplica en el STARTUP PACKET de cada conexión física del pool, así que es
# inmune al reordering de transaction pooler (Supabase :6543).
TENANT_SEARCH_PATH = os.getenv("TENANT_SEARCH_PATH", "public, semantic")


class TenantDSNError(Exception):
    pass


class TenantConnectionError(Exception):
    pass


_RETRYABLE = (
    asyncpg.ConnectionDoesNotExistError,
    asyncpg.ConnectionFailureError,
    asyncpg.InterfaceError,
    asyncpg.InvalidSQLStatementNameError,
)


@dataclass
class _PoolEntry:
    created_at: float
    pool: asyncpg.Pool


class TenantPoolManager:
    def __init__(self, secret_provider: SecretProvider):
        self._secret = secret_provider
        self._pools: "OrderedDict[str, _PoolEntry]" = OrderedDict()
        self._lock = asyncio.Lock()

    def _fetch_encrypted_dsn(self, tenant_id: str) -> str:
        with db_cursor() as cur:
            cur.execute(
                """
                SELECT tc.encrypted_dsn, t.status AS tenant_status, tc.status AS conn_status
                FROM app_auth.tenant_connections tc
                JOIN app_auth.tenants t ON t.id = tc.tenant_id
                WHERE tc.tenant_id = %s
                LIMIT 1
                """,
                (tenant_id,),
            )
            row = cur.fetchone()
            if not row:
                raise TenantDSNError("Tenant no tiene conexión registrada")
            if row["tenant_status"] != "active":
                raise TenantConnectionError("Tenant inactivo")
            if row["conn_status"] != "active":
                raise TenantConnectionError("Conexión inactiva")
            return row["encrypted_dsn"]

    def _normalize_dsn(self, dsn: str) -> str:
        if dsn.startswith("postgres://"):
            dsn = dsn.replace("postgres://", "postgresql://", 1)

        options = f"-c statement_timeout={TENANT_STATEMENT_TIMEOUT_MS}"
        if "options=" in dsn:
            dsn = re.sub(r"options=[^&]*", f"options={options}", dsn)
        else:
            sep = "&" if "?" in dsn else "?"
            dsn = f"{dsn}{sep}options={options}"

        if "connect_timeout=" not in dsn:
            sep = "&" if "?" in dsn else "?"
            dsn = f"{dsn}{sep}connect_timeout={TENANT_CONNECT_TIMEOUT}"

        if "sslmode=" not in dsn and "sslmode" not in dsn:
            sep = "&" if "?" in dsn else "?"
            dsn = f"{dsn}{sep}sslmode=require"

        return dsn

    async def get_pool(self, tenant_id: str) -> asyncpg.Pool:
        # ── FIX 5: fast path sin lock ────────────────────────────────────
        # Si el pool ya existe, retornarlo SIN adquirir el lock global.
        # En asyncio single-thread NO hay await entre .get() / .move_to_end()
        # y la asignación de created_at, así que la operación es atómica.
        # Esto elimina la serialización de ~100 req/s concurrentes contra el
        # mismo tenant (que antes pasaban una por una por self._lock).
        entry = self._pools.get(tenant_id)
        if entry is not None:
            self._pools.move_to_end(tenant_id)
            entry.created_at = time.monotonic()
            return entry.pool

        # ── Slow path: crear pool (bajo lock) ────────────────────────────
        async with self._lock:
            # Double-check: otro coroutine pudo crearlo mientras esperábamos.
            entry = self._pools.get(tenant_id)
            if entry is not None:
                self._pools.move_to_end(tenant_id)
                entry.created_at = time.monotonic()
                return entry.pool

            now = time.monotonic()

            # Evicción por inactividad
            stale = [
                tid for tid, e in self._pools.items()
                if now - e.created_at > TENANT_POOL_IDLE_TTL
            ]
            for tid in stale:
                await self._evict(tid)

            if len(self._pools) >= TENANT_POOL_MAX_TENANTS:
                oldest_tid, _ = next(iter(self._pools.items()))
                await self._evict(oldest_tid)

        # Crear pool fuera del lock (I/O de red: SSL handshake + auth)
        encrypted = self._fetch_encrypted_dsn(tenant_id)
        plaintext = self._secret.decrypt(encrypted)
        dsn = self._normalize_dsn(plaintext)

        try:
            pool = await asyncpg.create_pool(
                dsn=dsn,
                min_size=TENANT_POOL_MIN_SIZE,
                max_size=TENANT_POOL_MAX_SIZE,
                # statement_cache_size=0 obligatorio con transaction pooler
                statement_cache_size=0,
                command_timeout=TENANT_COMMAND_TIMEOUT,
                # FIX 3: 60s → 300s. Con min_size=4 las 4 conexiones
                # calientes se mantienen vivas entre bursts y no se paga
                # handshake SSL en cada ráfaga.
                max_inactive_connection_lifetime=300,
                max_queries=50_000,
                server_settings={
                    "application_name": f"leyia-tenant-{tenant_id[:8]}",
                    "search_path": TENANT_SEARCH_PATH,
                    # NO setear default_transaction_read_only — bloquearía
                    # REFRESH MATERIALIZED VIEW del loop de matviews.
                },
            )
        except Exception as exc:
            logger.error(f"Fallo creando pool para tenant {tenant_id[:8]}: {exc}")
            raise TenantConnectionError("Base de datos del cliente no disponible") from exc

        async with self._lock:
            if tenant_id in self._pools:
                # Otro coroutine ganó la carrera mientras creábamos el pool.
                await pool.close()
                return self._pools[tenant_id].pool
            self._pools[tenant_id] = _PoolEntry(created_at=time.monotonic(), pool=pool)
            self._pools.move_to_end(tenant_id)
            return pool

    async def _evict(self, tenant_id: str) -> None:
        entry = self._pools.pop(tenant_id, None)
        if entry:
            try:
                await entry.pool.close()
            except Exception:
                pass

    async def close_all(self) -> None:
        async with self._lock:
            for entry in self._pools.values():
                try:
                    await entry.pool.close()
                except Exception:
                    pass
            self._pools.clear()


# ── Singleton por proceso ─────────────────────────────────────────────────
_manager: Optional[TenantPoolManager] = None
_manager_lock = asyncio.Lock()


async def get_tenant_manager() -> TenantPoolManager:
    global _manager
    if _manager is None:
        async with _manager_lock:
            if _manager is None:
                _manager = TenantPoolManager(get_secret_provider())
    return _manager


async def close_tenant_pools() -> None:
    global _manager
    if _manager is not None:
        await _manager.close_all()
        _manager = None
        logger.info("🛑 Tenant pools cerrados")


# ── FIX 4: logging separa exec_ms de acq_ms ───────────────────────────────
# Antes: dt_ms medía acquire + fetch, y se reportaba como "SQL-TENANT ...ms".
# Se diagnosticaba falsamente como "query lenta" cuando en realidad era cola
# del pool. Ahora se reportan ambos por separado para observabilidad real.
def _log_timing(label: str, query: str, exec_ms: float, total_ms: float) -> None:
    resumen = " ".join(query.split())[:110]
    acq_ms = max(0.0, total_ms - exec_ms)
    msg = (
        f"⏱️ SQL-TENANT [{label}] total={total_ms:.0f}ms "
        f"exec={exec_ms:.0f}ms acq={acq_ms:.0f}ms | {resumen}"
    )
    if total_ms > SLOW_QUERY_MS or acq_ms > SLOW_QUERY_MS:
        logger.warning(f"🐌 {msg}")
    else:
        logger.info(msg)


# ── Ejecutores con retry + acquire timeout ───────────────────────────────
# El parámetro `schema=` se mantiene por compatibilidad con los callers pero
# ya no se usa (search_path = "public, semantic" cubre ambos).
async def tenant_fetch_raw(
    tenant: TenantContext,
    query: str,
    *params,
    schema: str = "semantic",        # deprecated: ignorado
    label: str = "tenant_fetch",
) -> List[Record]:
    mgr = await get_tenant_manager()
    pool = await mgr.get_pool(tenant.tenant_id)
    last_exc: Optional[Exception] = None

    for intento in (1, 2):
        t0 = time.monotonic()                      # FIX: reset por intento
        try:
            # FIX 2: timeout en el acquire → fallar rápido si pool saturado.
            async with pool.acquire(timeout=TENANT_ACQUIRE_TIMEOUT) as conn:
                t_exec = time.monotonic()
                result = await conn.fetch(query, *params)
                exec_ms = (time.monotonic() - t_exec) * 1000
                total_ms = (time.monotonic() - t0) * 1000
                _log_timing(label, query, exec_ms, total_ms)
                return result
        except _RETRYABLE as e:
            last_exc = e
            logger.warning(
                f"🔌 [{label}] conexión inválida (intento {intento}/2): {e}"
            )
        except asyncio.TimeoutError:
            waited_ms = (time.monotonic() - t0) * 1000
            logger.error(
                f"⏱️ [{label}] POOL SATURADO: sin conexión en {waited_ms:.0f}ms "
                f"(límite {TENANT_ACQUIRE_TIMEOUT * 1000:.0f}ms, "
                f"max_size={TENANT_POOL_MAX_SIZE})"
            )
            raise TenantConnectionError(
                f"Pool saturado [{label}]: sin conexión en {TENANT_ACQUIRE_TIMEOUT}s"
            ) from None

    raise last_exc  # type: ignore[arg-type]


async def tenant_fetchrow_raw(
    tenant: TenantContext,
    query: str,
    *params,
    schema: str = "semantic",        # deprecated: ignorado
    label: str = "tenant_fetchrow",
) -> Optional[Record]:
    mgr = await get_tenant_manager()
    pool = await mgr.get_pool(tenant.tenant_id)
    last_exc: Optional[Exception] = None

    for intento in (1, 2):
        t0 = time.monotonic()
        try:
            async with pool.acquire(timeout=TENANT_ACQUIRE_TIMEOUT) as conn:
                t_exec = time.monotonic()
                result = await conn.fetchrow(query, *params)
                exec_ms = (time.monotonic() - t_exec) * 1000
                total_ms = (time.monotonic() - t0) * 1000
                _log_timing(label, query, exec_ms, total_ms)
                return result
        except _RETRYABLE as e:
            last_exc = e
            logger.warning(
                f"🔌 [{label}] conexión inválida (intento {intento}/2): {e}"
            )
        except asyncio.TimeoutError:
            waited_ms = (time.monotonic() - t0) * 1000
            logger.error(
                f"⏱️ [{label}] POOL SATURADO: sin conexión en {waited_ms:.0f}ms "
                f"(límite {TENANT_ACQUIRE_TIMEOUT * 1000:.0f}ms, "
                f"max_size={TENANT_POOL_MAX_SIZE})"
            )
            raise TenantConnectionError(
                f"Pool saturado [{label}]: sin conexión en {TENANT_ACQUIRE_TIMEOUT}s"
            ) from None

    raise last_exc  # type: ignore[arg-type]


async def tenant_fetch(
    tenant: TenantContext,
    query: str,
    *params,
    schema: str = "semantic",        # deprecated: ignorado
    label: str = "tenant_fetch",
) -> List[Dict[str, Any]]:
    records = await tenant_fetch_raw(tenant, query, *params, schema=schema, label=label)
    return [to_json(r) for r in records]


async def tenant_fetchrow(
    tenant: TenantContext,
    query: str,
    *params,
    schema: str = "semantic",        # deprecated: ignorado
    label: str = "tenant_fetchrow",
) -> Dict[str, Any]:
    record = await tenant_fetchrow_raw(tenant, query, *params, schema=schema, label=label)
    return to_json(record) if record else {}


# ── FIX 1: caché con single-flight por (tenant, key) ────────────────────
# Antes: 4 requests concurrentes del mismo endpoint ejecutaban el factory()
# en paralelo → 4 × cola de pool para la misma query. Logs mostraban
# "CACHE SET" seguido de "CACHE HIT age=0.0s" (stampede clásico).
# Ahora: 1 ejecuta, los otros 3 esperan el lock y ven el hit → 1 × coste.
class TenantScopedCache:
    def __init__(self):
        # Lock por scoped_key. El número de keys está acotado por
        # (#tenants × #endpoints × #ranges), típicamente <100 por proceso.
        self._locks: Dict[str, asyncio.Lock] = {}

    async def get_or_set(
        self,
        tenant: TenantContext,
        key: str,
        factory: Callable[[], Awaitable[Any]],
    ) -> Any:
        scoped_key = f"t:{tenant.tenant_id}:{key}"
        lock = self._locks.setdefault(scoped_key, asyncio.Lock())
        async with lock:
            return await global_cache.get_or_set(scoped_key, factory)


tenant_cache = TenantScopedCache()
