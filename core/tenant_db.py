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

TENANT_POOL_MIN_SIZE = int(os.getenv("TENANT_POOL_MIN_SIZE", "1"))
TENANT_POOL_MAX_SIZE = int(os.getenv("TENANT_POOL_MAX_SIZE", "5"))
TENANT_POOL_MAX_TENANTS = int(os.getenv("TENANT_POOL_MAX_TENANTS", "50"))
TENANT_POOL_IDLE_TTL = float(os.getenv("TENANT_POOL_IDLE_TTL", "600"))
TENANT_STATEMENT_TIMEOUT_MS = int(os.getenv("TENANT_STATEMENT_TIMEOUT_MS", "8000"))
TENANT_CONNECT_TIMEOUT = int(os.getenv("TENANT_CONNECT_TIMEOUT", "5"))
TENANT_COMMAND_TIMEOUT = float(os.getenv("TENANT_COMMAND_TIMEOUT", "15"))


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
        async with self._lock:
            now = time.monotonic()

            # Evicción por inactividad
            stale = [
                tid for tid, entry in self._pools.items()
                if now - entry.created_at > TENANT_POOL_IDLE_TTL
            ]
            for tid in stale:
                await self._evict(tid)

            if tenant_id in self._pools:
                entry = self._pools.pop(tenant_id)
                self._pools[tenant_id] = _PoolEntry(created_at=now, pool=entry.pool)
                return entry.pool

            if len(self._pools) >= TENANT_POOL_MAX_TENANTS:
                oldest_tid, _ = next(iter(self._pools.items()))
                await self._evict(oldest_tid)

        # Crear pool fuera del lock
        encrypted = self._fetch_encrypted_dsn(tenant_id)
        plaintext = self._secret.decrypt(encrypted)
        dsn = self._normalize_dsn(plaintext)

        try:
            pool = await asyncpg.create_pool(
                dsn=dsn,
                min_size=TENANT_POOL_MIN_SIZE,
                max_size=TENANT_POOL_MAX_SIZE,
                statement_cache_size=0,
                command_timeout=TENANT_COMMAND_TIMEOUT,
                max_inactive_connection_lifetime=60,
                max_queries=50_000,
                server_settings={"application_name": f"leyia-tenant-{tenant_id[:8]}"},
            )
        except Exception as exc:
            logger.error(f"Fallo creando pool para tenant {tenant_id[:8]}: {exc}")
            raise TenantConnectionError("Base de datos del cliente no disponible") from exc

        async with self._lock:
            if tenant_id in self._pools:
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


# ── Singleton por proceso ──────────────────────────────────────────────────
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


# ── Setup/reset de conexión (solo lectura + search_path) ────────────────────
async def _setup_connection(conn: asyncpg.Connection, schema: str) -> None:
    await conn.execute("SET default_transaction_read_only = on")
    await conn.execute(f"SET search_path TO {schema}, pg_catalog")


async def _reset_connection(conn: asyncpg.Connection) -> None:
    try:
        await conn.execute("ROLLBACK")
    except Exception:
        pass
    try:
        await conn.execute("RESET ALL")
    except Exception:
        pass


def _log_timing(label: str, query: str, dt_ms: float) -> None:
    resumen = " ".join(query.split())[:110]
    msg = f"⏱️ SQL-TENANT [{label}] {dt_ms:.0f}ms | {resumen}"
    logger.warning(f"🐌 {msg}") if dt_ms > SLOW_QUERY_MS else logger.info(msg)


# ── Ejecutores con retry ─────────────────────────────────────────────────
async def tenant_fetch_raw(
    tenant: TenantContext,
    query: str,
    *params,
    schema: str = "semantic",
    label: str = "tenant_fetch",
) -> List[Record]:
    mgr = await get_tenant_manager()
    pool = await mgr.get_pool(tenant.tenant_id)
    t0 = time.monotonic()
    last_exc: Optional[Exception] = None

    for intento in (1, 2):
        async with pool.acquire() as conn:
            await _setup_connection(conn, schema)
            try:
                result = await conn.fetch(query, *params)
                _log_timing(label, query, (time.monotonic() - t0) * 1000)
                return result
            except _RETRYABLE as e:
                last_exc = e
                logger.warning(f"🔌 [{label}] conexión inválida (intento {intento}/2): {e}")
            except Exception:
                raise
            finally:
                await _reset_connection(conn)

    raise last_exc  # type: ignore[arg-type]


async def tenant_fetchrow_raw(
    tenant: TenantContext,
    query: str,
    *params,
    schema: str = "semantic",
    label: str = "tenant_fetchrow",
) -> Optional[Record]:
    mgr = await get_tenant_manager()
    pool = await mgr.get_pool(tenant.tenant_id)
    t0 = time.monotonic()
    last_exc: Optional[Exception] = None

    for intento in (1, 2):
        async with pool.acquire() as conn:
            await _setup_connection(conn, schema)
            try:
                result = await conn.fetchrow(query, *params)
                _log_timing(label, query, (time.monotonic() - t0) * 1000)
                return result
            except _RETRYABLE as e:
                last_exc = e
                logger.warning(f"🔌 [{label}] conexión inválida (intento {intento}/2): {e}")
            except Exception:
                raise
            finally:
                await _reset_connection(conn)

    raise last_exc  # type: ignore[arg-type]


async def tenant_fetch(
    tenant: TenantContext,
    query: str,
    *params,
    schema: str = "semantic",
    label: str = "tenant_fetch",
) -> List[Dict[str, Any]]:
    records = await tenant_fetch_raw(tenant, query, *params, schema=schema, label=label)
    return [to_json(r) for r in records]


async def tenant_fetchrow(
    tenant: TenantContext,
    query: str,
    *params,
    schema: str = "semantic",
    label: str = "tenant_fetchrow",
) -> Dict[str, Any]:
    record = await tenant_fetchrow_raw(tenant, query, *params, schema=schema, label=label)
    return to_json(record) if record else {}


# ── Caché con prefijo de tenant ───────────────────────────────────────────
class TenantScopedCache:
    async def get_or_set(
        self,
        tenant: TenantContext,
        key: str,
        factory: Callable[[], Awaitable[Any]],
    ) -> Any:
        scoped_key = f"t:{tenant.tenant_id}:{key}"
        return await global_cache.get_or_set(scoped_key, factory)


tenant_cache = TenantScopedCache()
