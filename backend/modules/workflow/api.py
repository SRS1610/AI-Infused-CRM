"""Workflow API: status bar for a record, taking a transition, incoming automation webhooks."""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Body, Depends, Request

from modules.workflow.models.workflow import workflow_for
from tessera.api.deps import RequestContext, authenticated, request_context
from tessera.api.registry import register_router
from tessera.exceptions import AccessDenied, MissingError, UserError
from tessera.i18n import _

router = register_router(APIRouter(prefix="/workflow", tags=["workflow"]))


def statusbar_payload(ctx: RequestContext, model: str, res_id: int) -> dict[str, Any]:
    if model not in ctx.registry.models:
        raise UserError(_("Unknown model '{model}'", model=model))
    record = ctx.env[model].browse([res_id])
    record.check_access("read")
    wf = workflow_for(ctx.env, model)
    if not wf:
        return {"workflow": None, "states": [], "transitions": []}
    current = wf.state_key(record)
    can_write = record.check_access_rights("write", raise_exception=False)
    transitions = []
    for tr in wf.transition_ids:
        tr_user = tr.with_env(ctx.env) if can_write else None
        reason = tr_user.blocked_reason(record) if tr_user is not None else _("No write access")
        if reason is None:
            transitions.append(
                {
                    "id": tr.ids[0],
                    "name": tr.name,
                    "to": tr.to_state_id.key,
                    "confirm": tr.confirm,
                    "highlight": tr.highlight,
                }
            )
    return {
        "workflow": {"id": wf.ids[0], "name": wf.name, "field": wf.state_field, "strict": wf.strict},
        "current": current,
        "states": [
            {"key": s.key, "name": s.name, "fold": s.fold, "final": s.is_final, "current": s.key == current}
            for s in wf.state_ids
        ],
        "transitions": transitions,
    }


@router.get("/{model}/{res_id}/statusbar")
def statusbar(model: str, res_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """States of the record's workflow and the transitions the caller can take now (guards evaluated)."""
    return statusbar_payload(ctx, model, res_id)


@router.post("/{model}/{res_id}/transitions/{transition_id}")
def take_transition(
    model: str, res_id: int, transition_id: int, ctx: RequestContext = Depends(authenticated)
) -> dict[str, Any]:
    """409 when the transition is not available (wrong state or guard), 403 when the caller lacks the group."""
    wf = workflow_for(ctx.env, model)
    tr = ctx.env["workflow.transition"].elevated().browse([transition_id]).exists()
    if not wf or not tr or tr.definition_id.ids[0] != wf.ids[0]:
        raise MissingError(_("Transition not found"))
    record = ctx.env[model].browse([res_id]).exists()
    if not record:
        raise MissingError(_("Record not found"))
    tr.with_env(ctx.env).apply(record)
    return statusbar_payload(ctx, model, res_id)


@router.post("/hooks/{token}")
def incoming_webhook(
    token: str,
    request: Request,
    payload: dict[str, Any] = Body(default_factory=dict),
    ctx: RequestContext = Depends(request_context),
) -> dict[str, Any]:
    """Trigger an ``on_webhook`` automation. The URL token is the secret. ``record_id`` in the JSON body
    selects the record; the whole body is available to expressions as ``payload``."""
    from modules.workflow.engine import run_automation

    autos = ctx.env["workflow.automation"].elevated().search([("trigger", "=", "on_webhook"), ("active", "=", True)])
    auto = next((a for a in autos if a.webhook_token and hmac.compare_digest(a.webhook_token, token)), None)
    if auto is None:
        raise AccessDenied(_("Unknown webhook"))
    env = ctx.env.elevated(f"automation webhook {auto.ids[0]}")
    rid = payload.get("record_id")
    if rid is None:
        raise UserError(_("record_id is required"))
    record = env[auto.model_name].browse([int(rid)]).exists()
    if not record:
        raise MissingError(_("Record not found"))
    ran = run_automation(auto, record, "webhook", extra={"payload": payload})
    return {"ran": ran}
