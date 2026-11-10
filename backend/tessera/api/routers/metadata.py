"""Model metadata for clients: which models the caller can read, their fields and methods."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from tessera.api.deps import RequestContext, authenticated
from tessera.exceptions import MissingError

router = APIRouter(prefix="/metadata", tags=["metadata"])


@router.get("/models")
def list_models(ctx: RequestContext = Depends(authenticated)) -> list[dict[str, Any]]:
    out = []
    for name, cls in sorted(ctx.registry.models.items()):
        if not ctx.env[name].check_access_rights("read", raise_exception=False):
            continue
        out.append({"name": name, "description": cls._description, "module": cls._module, "studio": cls._studio})
    return out


@router.get("/models/{model_name}")
def describe_model(model_name: str, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    if model_name not in ctx.registry.models:
        raise MissingError(f"Unknown model '{model_name}'")
    model = ctx.env[model_name]
    model.check_access_rights("read")
    desc = ctx.registry.describe_model(model_name)
    desc["fields"] = model.fields_get()
    desc["methods"] = sorted(type(model)._exposed_methods)
    desc["access"] = {
        op: model.check_access_rights(op, raise_exception=False) for op in ("read", "write", "create", "unlink")
    }
    return desc
