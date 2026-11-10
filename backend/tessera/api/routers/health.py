from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response

from tessera import __version__
from tessera.api.deps import database_url_for
from tessera.db.engine import get_engine
from tessera.orm.registry import Registry

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"status": "ok", "version": __version__}


@router.get("/readyz")
def readyz(request: Request, response: Response) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    ok = True
    url = database_url_for(request)
    try:
        with get_engine(url).connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
        ok = False
    try:
        registry = Registry.get(url)
        checks["registry"] = {
            "ready": registry.ready,
            "modules": len(registry.module_order),
            "models": len(registry.models),
        }
        ok = ok and registry.ready
    except Exception as exc:
        checks["registry"] = f"error: {type(exc).__name__}"
        ok = False
    response.status_code = 200 if ok else 503
    return {"status": "ok" if ok else "degraded", "checks": checks}
