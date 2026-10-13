#!/usr/bin/env python3
# api.py — LeyIA Central (portal multi-tenant)
# v3.0.0 — Auth central + tenancy + métricas por tenant + provisión admin
# Un despliegue = TODOS los clientes.
# SIN agente, SIN webhook WhatsApp (eso vive en agent_api.py, por cliente).

import os
import sys
import time
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Any, Dict, List
from uuid import uuid4

BACKEND_DIR = Path(__file__).parent.absolute()
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv
env_loaded = load_dotenv(BACKEND_DIR / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("leyia.central")
logger.info(f"[STARTUP] .env cargado: {env_loaded}")

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from auth.config import settings

# ============================================================================
# VALIDACIÓN DE ARRANQUE (fail-fast)
# ============================================================================

def validate_startup() -> None:
    faltan = []
    if not settings.CENTRAL_DATABASE_URL:
        faltan.append("CENTRAL_DATABASE_URL")
    if not settings.CENTRAL_SUPABASE_URL:
        faltan.append("CENTRAL_SUPABASE_URL")
    if not settings.TENANT_SECRETS_KEY:
        faltan.append("TENANT_SECRETS_KEY")
    if faltan:
        raise RuntimeError(f"❌ Faltan variables críticas: {', '.join(faltan)}")

    if settings.ENVIRONMENT == "production":
        if settings.DEV_TENANT_ID:
            raise RuntimeError("❌ DEV_TENANT_ID debe estar VACÍO en producción")
        if not settings.SECURE_COOKIES:
            logger.warning("⚠️ SECURE_COOKIES=False en producción (activar con HTTPS)")
    elif settings.dev_tenant_bypass_enabled:
        logger.warning("⚠️ DEV TENANT BYPASS activo — solo desarrollo")


validate_startup()

# ============================================================================
# ROUTERS (todos consumen el servicio central)
# ============================================================================

from auth.router import router as auth_router               # /me, /session, /logout
from routes.admin_routes import router as admin_router      # provisión tenants/usuarios
from routes.dashboard_routes import router as dashboard_router
from routes.metrics_routes import router as metrics_router
from routes.public_routes import router as public_router    # branding por tenant

app = FastAPI(
    title="LeyIA Central",
    version="3.0.0",
    description="API central multi-tenant: identidad, autorización y métricas.",
)

ALLOWED_ORIGINS = [
    settings.FRONTEND_URL,                          # principal (variable)
    "https://frontend-leyia.vercel.app",            # dominio Vercel actual
    "https://app.leyia.com",                        # futuro dominio custom
    "http://localhost:5173", "http://127.0.0.1:5173",
    "http://localhost:3000", "http://127.0.0.1:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,   # necesario para la cookie httpOnly
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(dashboard_router)
app.include_router(metrics_router)
app.include_router(public_router)


def _list_api_routes() -> List[Dict[str, Any]]:
    return [
        {"path": r.path, "name": r.name, "methods": list(r.methods)}
        for r in app.routes
        if hasattr(r, "methods") and hasattr(r, "path")
    ]


# ============================================================================
# MIDDLEWARE DE LOGGING
# ============================================================================

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    client = request.client.host if request.client else "unknown"
    method, path = request.method, request.url.path
    logger.info(f"➡️ {method} {path} | client={client}")
    try:
        response = await call_next(request)
        logger.info(
            f"⬅️ {method} {path} | status={response.status_code} | "
            f"{(time.time() - start) * 1000:.1f}ms"
        )
        return response
    except Exception as e:
        logger.error(f"💥 {method} {path} | {type(e).__name__}: {e}")
        raise


# ============================================================================
# ENDPOINTS DE SISTEMA
# ============================================================================

@app.get("/")
async def root():
    return {"service": "LeyIA Central", "status": "ok", "docs": "/docs"}


@app.get("/api/debug/routes")
async def debug_routes():
    routes = [r for r in _list_api_routes() if r["path"].startswith("/api")]
    return {"total": len(routes), "routes": routes}


@app.get("/api/v1/system/status")
async def system_status():
    return {
        "status": "ok",
        "service": "central",
        "version": "3.0.0",
        "environment": settings.ENVIRONMENT,
        "dev_bypass": settings.dev_tenant_bypass_enabled,
    }


# ============================================================================
# MANEJO DE ERRORES (500 sanitizado con error_id)
# ============================================================================

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail, "timestamp": datetime.now().isoformat()},
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    error_id = uuid4().hex[:12]
    logger.error(
        f"💥 Error no manejado [{error_id}] | {request.method} {request.url.path} | "
        f"{exc.__class__.__name__}: {exc}",
        exc_info=True,
    )
    return JSONResponse(
        status_code=500,
        content={"error": "Error interno del servidor", "error_id": error_id},
    )


# ============================================================================
# LIFESPAN
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 70)
    logger.info(f"🟢 LeyIA Central iniciando | env={settings.ENVIRONMENT}")
    logger.info(f"🟢 Rutas /api: {len([r for r in _list_api_routes() if r['path'].startswith('/api')])}")
    logger.info("=" * 70)

    # Warm-up SOLO en desarrollo: calienta pool + caché del tenant demo.
    # En producción el pool de cada tenant se crea lazy en su primer request.
    if settings.dev_tenant_bypass_enabled:
        async def _warmup():
            try:
                from auth.tenancy import _dev_context
                from services.dashboard_service import warmup as dashboard_warmup
                tenant = _dev_context()
                await dashboard_warmup(tenant)
                logger.info("🔥 Warm-up demo OK")
            except Exception as e:
                logger.warning(f"⚠️ Warm-up demo falló (no fatal): {e}")

        asyncio.create_task(_warmup())

    yield

    from core.tenant_db import close_tenant_pools
    from database.connection import close_all_connections
    await close_tenant_pools()
    close_all_connections()
    logger.info("🛑 LeyIA Central finalizada")


app.router.lifespan_context = lifespan

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8001)),
        reload=True,
    )
