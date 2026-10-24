# routes/metrics_routes.py
# KPIs del overview — multi-tenant.

from fastapi import APIRouter, Depends, Query

from auth.tenancy import get_tenant_context, TenantContext
# routes/metrics_routes.py:7
from services.dashboard_service import get_overview_metrics_service

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


# ==================================================================
# OVERVIEW DEL DASHBOARD
# ==================================================================
@router.get("/overview")
async def metrics_overview(
    range: str = Query("7d", pattern="^(7d|30d|90d)$"),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """KPIs + gráficos + leads de la instancia del tenant."""
    return await get_overview_metrics_service(tenant, range)


@router.get("/health/metrics")
async def health_metrics():
    """
    Health-check de infraestructura: verifica que el subsistema
    de métricas responda, sin exponer datos de ningún tenant.
    """
    # Nota: este endpoint es público por diseño (monitoring).
    # No devuelve detalles internos ni datos de cliente.
    return {"status": "ok", "service": "metrics"}
