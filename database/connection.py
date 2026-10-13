# database/connection.py
import os

import psycopg2
from dotenv import load_dotenv
from psycopg2.pool import ThreadedConnectionPool

load_dotenv()

# ── Base CENTRAL de LeyIA (esquema app_auth: identidad, tenants, conexiones) ──
# No confundir con DATABASE_URL legacy: esa es la base OPERATIVA del cliente
# mono-tenant. Solo se usa para ser cifrada/registrada en tenant_connections,
# nunca para conexiones directas del backend.
DATABASE_URL = os.getenv("CENTRAL_DATABASE_URL") or os.getenv("DATABASE_URL", "")

if not DATABASE_URL or "tu-database-url" in DATABASE_URL.lower():
    # Fallback por partes (solo desarrollo local)
    DB_USER = os.getenv("DB_USER", "")
    DB_PASSWORD = os.getenv("DB_PASSWORD", "")
    DB_HOST = os.getenv("DB_HOST", "")
    DB_NAME = os.getenv("DB_NAME", "")
    DB_PORT = os.getenv("DB_PORT", "6543")

    if not all([DB_USER, DB_PASSWORD, DB_HOST, DB_NAME]):
        raise Exception(
            "❌ Falta CENTRAL_DATABASE_URL (o DB_USER/DB_PASSWORD/DB_HOST/DB_NAME) "
            "para la base central de LeyIA"
        )
    DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if "connect_timeout=" not in DATABASE_URL:
    sep = "&" if "?" in DATABASE_URL else "?"
    DATABASE_URL = f"{DATABASE_URL}{sep}connect_timeout=5"

_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        # ThreadedConnectionPool: preparado para mover deps sync a threadpool.
        # maxconn bajo: la central solo hace lookups cortos por request.
        _pool = ThreadedConnectionPool(minconn=1, maxconn=10, dsn=DATABASE_URL)
    return _pool


def get_connection():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        conn.rollback()  # C2: el ping abre una tx; no dejarla idle-in-transaction
    except Exception:
        # C1: NUNCA reconectar fuera del pool. Se descarta la conexión mala
        # y se pide otra creada POR el pool:
        pool.putconn(conn, close=True)
        conn = pool.getconn()
    return conn


def release_connection(conn):
    if conn is None:
        return
    try:
        conn.rollback()  # aborta cualquier tx abierta/inválida
    except Exception:
        pass
    _get_pool().putconn(conn)


def close_all_connections():
    global _pool
    if _pool:
        _pool.closeall()
        _pool = None
