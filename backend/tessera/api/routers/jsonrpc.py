"""JSON-RPC 2.0 endpoint mirroring the ORM (``/jsonrpc``), with batch support."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from tessera.api.deps import RequestContext, authenticated_any
from tessera.api.routers.records import _call, get_model
from tessera.api.serializers import clean_input_values, default_read_fields
from tessera.exceptions import TesseraError, UserError, ValidationError
from tessera.orm.model import Model

router = APIRouter(tags=["jsonrpc"])

ERR_PARSE, ERR_REQUEST, ERR_METHOD, ERR_PARAMS, ERR_INTERNAL = -32700, -32600, -32601, -32602, -32603


def _execute(ctx: RequestContext, method: str, params: dict[str, Any]) -> Any:
    model_name = params.get("model")
    if method == "ref":
        rec = ctx.env.ref(str(params["external_id"]), raise_if_not_found=False)
        return {"model": rec._name, "id": rec.ids[0]} if rec else None
    if not model_name:
        raise ValidationError("params.model is required")
    model: Model = get_model(ctx, str(model_name), params.get("context"))
    if method == "search_read":
        records = model.search(
            params.get("domain"),
            offset=int(params.get("offset", 0)),
            limit=params.get("limit"),
            order=params.get("order"),
        )
        return records.read(params.get("fields") or default_read_fields(model))
    if method == "search":
        return model.search(
            params.get("domain"),
            offset=int(params.get("offset", 0)),
            limit=params.get("limit"),
            order=params.get("order"),
        ).ids
    if method == "search_count":
        return model.search_count(params.get("domain"))
    if method == "read":
        return model.browse(list(params.get("ids", []))).read(params.get("fields") or default_read_fields(model))
    if method == "create":
        values = params.get("values", {})
        batch = values if isinstance(values, list) else [values]
        records = model.create([clean_input_values(model, v) for v in batch])
        return records.ids if isinstance(values, list) else records.ids[0]
    if method == "write":
        model.browse(list(params.get("ids", []))).write(clean_input_values(model, params.get("values", {})))
        return True
    if method == "unlink":
        model.browse(list(params.get("ids", []))).unlink()
        return True
    if method == "read_group":
        return model.read_group(
            params.get("domain"),
            params.get("aggregates", []),
            params.get("groupby", []),
            offset=int(params.get("offset", 0)),
            limit=params.get("limit"),
            orderby=params.get("orderby"),
        )
    if method == "fields_get":
        return model.fields_get(params.get("fields"))
    if method == "name_search":
        return [
            {"id": i, "display_name": n}
            for i, n in model.name_search(params.get("name", ""), params.get("domain"), limit=params.get("limit", 20))
        ]
    if method == "default_get":
        return model.default_get(params.get("fields") or list(model._fields))
    if method == "call":
        records = model.browse(list(params.get("ids", [])))
        return _call(records, str(params.get("method")), list(params.get("args", [])), dict(params.get("kwargs", {})))
    raise TesseraError(f"Unknown JSON-RPC method '{method}'")


def _handle(ctx: RequestContext, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0" or "method" not in payload:
        return {
            "jsonrpc": "2.0",
            "id": payload.get("id") if isinstance(payload, dict) else None,
            "error": {"code": ERR_REQUEST, "message": "Invalid request"},
        }
    rid = payload.get("id")
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": ERR_PARAMS, "message": "params must be an object"}}
    try:
        with ctx.cr.savepoint():
            result = _execute(ctx, str(payload["method"]), params)
        return {"jsonrpc": "2.0", "id": rid, "result": result}
    except UserError as exc:
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "error": {
                "code": ERR_PARAMS if exc.http_status in (400, 422) else -32000 - exc.http_status,
                "message": exc.message,
                "data": exc.to_dict(),
            },
        }
    except TesseraError as exc:
        code = ERR_METHOD if "Unknown JSON-RPC method" in exc.message else ERR_INTERNAL
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "error": {"code": code, "message": exc.message if code == ERR_METHOD else "Internal error"},
        }


@router.post("/jsonrpc")
async def jsonrpc(request: Request, ctx: RequestContext = Depends(authenticated_any)) -> JSONResponse:
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": ERR_PARSE, "message": "Parse error"}})
    if isinstance(payload, list):
        return JSONResponse([_handle(ctx, item) for item in payload])
    return JSONResponse(_handle(ctx, payload))
