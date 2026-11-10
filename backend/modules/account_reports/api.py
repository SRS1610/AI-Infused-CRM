"""Financial reports API.

``GET /api/v1/account/reports`` lists the reports; ``GET /api/v1/account/reports/{code}`` runs one with query
filters (lists are comma-separated: ``company_ids=1,2&unfold=line:12,line:14``); ``POST
/api/v1/account/reports/run`` takes a :class:`ReportParams` body; ``GET …/{code}/export?format=csv|xlsx|pdf``
downloads it; ``GET …/schema`` returns the parameter object's JSON schema.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import ValidationError as PydanticError

from modules.account_reports.engine import list_reports, run_report
from modules.account_reports.export import export_report
from modules.account_reports.params import ReportParams
from tessera.api.deps import RequestContext, authenticated
from tessera.api.registry import register_router
from tessera.exceptions import UserError
from tessera.i18n import _

router = register_router(APIRouter(prefix="/account/reports", tags=["account_reports"]))


def _ints(value: str | None) -> list[int]:
    try:
        return [int(x) for x in (value or "").split(",") if x.strip()]
    except ValueError as exc:
        raise UserError(_("Ids are comma-separated numbers")) from exc


def _strs(value: str | None) -> list[str]:
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def _date(value: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value) if value else None
    except ValueError as exc:
        raise UserError(_("Dates use YYYY-MM-DD")) from exc


def query_params(
    code: str,
    date_from: str | None = None,
    date_to: str | None = None,
    comparison: str = "none",
    periods: int = 1,
    journal_ids: str | None = None,
    partner_ids: str | None = None,
    company_ids: str | None = None,
    currency: str | None = None,
    include_draft: bool = False,
    account_type: str = "all",
    unfold: str | None = None,
    unfold_all: bool = False,
    expand: str | None = None,
    offset: int = 0,
    limit: int = 80,
    hide_zero: bool = True,
) -> ReportParams:
    try:
        return ReportParams(
            report=code,
            date_from=_date(date_from),
            date_to=_date(date_to),
            comparison=comparison,
            periods=periods,
            journal_ids=_ints(journal_ids),
            partner_ids=_ints(partner_ids),
            company_ids=_ints(company_ids),
            currency=currency or None,
            include_draft=include_draft,
            account_type=account_type,
            unfold=_strs(unfold),
            unfold_all=unfold_all,
            expand=expand or None,
            offset=offset,
            limit=limit,
            hide_zero=hide_zero,
        )
    except PydanticError as exc:
        first = exc.errors()[0]
        raise UserError(
            _("Invalid report parameter {name}: {message}", name=".".join(map(str, first["loc"])), message=first["msg"])
        ) from exc


@router.get("")
def reports(ctx: RequestContext = Depends(authenticated)) -> list[dict[str, Any]]:
    return list_reports(ctx.env)


@router.get("/schema")
def schema(ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    return ReportParams.model_json_schema()


@router.post("/run")
def run(params: ReportParams, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    return run_report(ctx.env, params)


@router.get("/{code}")
def report(
    params: ReportParams = Depends(query_params), ctx: RequestContext = Depends(authenticated)
) -> dict[str, Any]:
    return run_report(ctx.env, params)


@router.get("/{code}/export")
def export(
    format: str = Query("xlsx", pattern="^(csv|xlsx|pdf|html)$"),
    params: ReportParams = Depends(query_params),
    ctx: RequestContext = Depends(authenticated),
) -> Response:
    content, mimetype, filename = export_report(ctx.env, params, format)
    disposition = "inline" if format in ("pdf", "html") else "attachment"
    return Response(
        content, media_type=mimetype, headers={"Content-Disposition": f'{disposition}; filename="{filename}"'}
    )
