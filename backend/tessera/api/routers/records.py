"""REST API generated from the registry: ``/api/v1/models/{model}``.

Permissions are the ORM's; this layer only parses and serialises.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from tessera.api.deps import RequestContext, authenticated_any
from tessera.api.serializers import clean_input_values, default_read_fields, etag_for, parse_fields_param
from tessera.exceptions import AccessError, ConcurrencyError, MissingError, ValidationError
from tessera.orm.model import Model

router = APIRouter(prefix="/models", tags=["records"])

MAX_LIMIT = 1000


class CreateBody(BaseModel):
    values: dict[str, Any] | list[dict[str, Any]]
    fields: list[str] | None = None


class UpdateBody(BaseModel):
    values: dict[str, Any]
    fields: list[str] | None = None


class CallBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    args: list[Any] = Field(default_factory=list)
    keyword_args: dict[str, Any] = Field(default_factory=dict, alias="kwargs")
    context: dict[str, Any] | None = None


class ReadGroupBody(BaseModel):
    domain: Any = None
    aggregates: list[str] = Field(default_factory=list)
    groupby: list[str] = Field(default_factory=list)
    limit: int | None = None
    offset: int = 0
    orderby: str | None = None


def get_model(ctx: RequestContext, model_name: str, context: dict[str, Any] | None = None) -> Model:
    if model_name not in ctx.registry.models:
        raise MissingError(f"Unknown model '{model_name}'", model=model_name)
    model = ctx.env[model_name]
    if context:
        safe = {k: v for k, v in context.items() if not k.startswith("_")}
        model = model.with_context(**safe)
    return model


def _parse_domain(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid domain JSON: {exc.msg}") from exc


def _parse_context(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        ctx = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError("Invalid context JSON") from exc
    if not isinstance(ctx, dict):
        raise ValidationError("context must be an object")
    return ctx


def _serialize(records: Model, fields: list[str] | None) -> list[dict[str, Any]]:
    return records.read(fields or default_read_fields(records))


@router.get("/{model_name}")
def list_records(
    model_name: str,
    ctx: RequestContext = Depends(authenticated_any),
    domain: str | None = Query(None, description="Domain as JSON"),
    fields: str | None = Query(None, description="Comma-separated field names"),
    order: str | None = None,
    limit: int = Query(80, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    context: str | None = Query(None, description="Context as JSON"),
    count: bool = Query(True, description="Include the total count"),
) -> dict[str, Any]:
    model = get_model(ctx, model_name, _parse_context(context))
    node = _parse_domain(domain)
    records = model.search(node, offset=offset, limit=limit, order=order)
    total = model.search_count(node) if count else None
    return {
        "items": _serialize(records, parse_fields_param(fields, model)),
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/{model_name}", status_code=201)
def create_record(
    model_name: str, body: CreateBody, ctx: RequestContext = Depends(authenticated_any)
) -> dict[str, Any]:
    model = get_model(ctx, model_name)
    batch = body.values if isinstance(body.values, list) else [body.values]
    records = model.create([clean_input_values(model, v) for v in batch])
    items = _serialize(records, body.fields)
    return {"items": items} if isinstance(body.values, list) else items[0]


@router.post("/{model_name}/read_group")
def read_group(
    model_name: str, body: ReadGroupBody, ctx: RequestContext = Depends(authenticated_any)
) -> dict[str, Any]:
    model = get_model(ctx, model_name)
    groups = model.read_group(
        body.domain, body.aggregates, body.groupby, offset=body.offset, limit=body.limit, orderby=body.orderby
    )
    return {"groups": groups}


@router.get("/{model_name}/fields")
def fields_get(model_name: str, ctx: RequestContext = Depends(authenticated_any)) -> dict[str, Any]:
    model = get_model(ctx, model_name)
    model.check_access_rights("read")
    return {
        "model": model._name,
        "description": model._description,
        "rec_name": model._rec_name,
        "order": model._order,
        "fields": model.fields_get(),
        "methods": sorted(type(model)._exposed_methods),
    }


@router.get("/{model_name}/name_search")
def name_search(
    model_name: str,
    ctx: RequestContext = Depends(authenticated_any),
    q: str = "",
    domain: str | None = None,
    limit: int = Query(20, ge=1, le=200),
) -> list[dict[str, Any]]:
    model = get_model(ctx, model_name)
    return [{"id": i, "display_name": n} for i, n in model.name_search(q, _parse_domain(domain), limit=limit)]


@router.post("/{model_name}/call/{method}")
def call_model_method(
    model_name: str, method: str, body: CallBody, ctx: RequestContext = Depends(authenticated_any)
) -> dict[str, Any]:
    model = get_model(ctx, model_name, body.context)
    return {"result": _call(model, method, body.args, body.keyword_args)}


@router.get("/{model_name}/{record_id}")
def get_record(
    model_name: str,
    record_id: int,
    response: Response,
    ctx: RequestContext = Depends(authenticated_any),
    fields: str | None = None,
    context: str | None = None,
) -> dict[str, Any]:
    model = get_model(ctx, model_name, _parse_context(context))
    record = model.browse([record_id])
    data = _serialize(record, parse_fields_param(fields, model))
    if not data:
        raise MissingError(f"{model_name}({record_id}) not found")
    etag = etag_for(record)
    if etag:
        response.headers["ETag"] = etag
    return data[0]


@router.patch("/{model_name}/{record_id}")
def update_record(
    model_name: str,
    record_id: int,
    body: UpdateBody,
    response: Response,
    ctx: RequestContext = Depends(authenticated_any),
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    model = get_model(ctx, model_name)
    record = model.browse([record_id]).exists()
    if not record:
        raise MissingError(f"{model_name}({record_id}) not found")
    if if_match:
        current = etag_for(record)
        if current and current != if_match:
            raise ConcurrencyError("The record was modified by someone else; reload and retry")
    record.write(clean_input_values(model, body.values))
    etag = etag_for(record)
    if etag:
        response.headers["ETag"] = etag
    return _serialize(record, body.fields)[0]


@router.delete("/{model_name}/{record_id}", status_code=204, response_class=Response)
def delete_record(model_name: str, record_id: int, ctx: RequestContext = Depends(authenticated_any)) -> Response:
    model = get_model(ctx, model_name)
    record = model.browse([record_id]).exists()
    if not record:
        raise MissingError(f"{model_name}({record_id}) not found")
    record.unlink()
    return Response(status_code=204)


@router.post("/{model_name}/{record_id}/call/{method}")
def call_record_method(
    model_name: str, record_id: int, method: str, body: CallBody, ctx: RequestContext = Depends(authenticated_any)
) -> dict[str, Any]:
    model = get_model(ctx, model_name, body.context)
    record = model.browse([record_id]).exists()
    if not record:
        raise MissingError(f"{model_name}({record_id}) not found")
    return {"result": _call(record, method, body.args, body.keyword_args)}


def _call(records: Model, method: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
    if method.startswith("_") or method not in type(records)._exposed_methods:
        raise AccessError(f"Method '{method}' is not exposed on {records._name}", method=method)
    result = getattr(records, method)(*args, **kwargs)
    if isinstance(result, Model):
        return {"model": result._name, "ids": result.ids}
    return result


def _unused(request: Request) -> None:  # keep Request import meaningful for extensions
    return None
