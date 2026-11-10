"""Proposals: the only way an AI feature changes anything.

A proposal stores what the model suggested (and the current values, for the diff). Only the user who
asked may decide. Accepting applies the change *as that user* through the ORM, so every access right
and record rule applies again at that moment; nothing is ever auto-applied. The payload is signed
when the proposal is created, so a payload modified behind the API's back cannot be accepted.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from tessera.ai.tools import PendingProposal
from tessera.config import get_settings
from tessera.exceptions import AccessError, ConflictError, MissingError, UserError, ValidationError

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

DEFAULT_TTL = dt.timedelta(days=7)
ActionFn = Callable[["Environment", str, "int | None", dict[str, Any]], Any]
ACTIONS: dict[str, ActionFn] = {}


def action(name: str) -> Callable[[ActionFn], ActionFn]:
    """Register a named action a proposal of kind ``action`` may run (nothing else can be called)."""

    def decorator(func: ActionFn) -> ActionFn:
        ACTIONS[name] = func
        return func

    return decorator


def sign(payload: Any) -> str:
    body = json.dumps(payload, sort_keys=True, default=str).encode()
    return hmac.new(get_settings().secret_key.encode(), body, hashlib.sha256).hexdigest()


def _original(env: Environment, model: str, res_id: int | None, payload: dict[str, Any]) -> dict[str, Any] | None:
    if res_id is None or model not in env.registry.models:
        return None
    rec = env[model].browse([res_id])
    try:
        rec.check_access("read")
        names = [k for k in payload if k in rec._fields and rec._check_field_access(rec._fields[k], "read")]
        return rec.read(names)[0] if names else {}
    except (AccessError, MissingError):
        return None


def create(env: Environment, run: Model | None, feature: str, pending: PendingProposal) -> Model:
    if pending.kind not in ("create", "write", "transition", "message", "action"):
        raise ValidationError(f"Unknown proposal kind '{pending.kind}'")
    payload = pending.payload
    return (
        env["ai_core.proposal"]
        .elevated("ai proposal")
        .create(
            {
                "run_id": run.ids[0] if run else None,
                "feature": feature,
                "user_id": env.uid,
                "company_id": env.company.ids[0] if env.company else None,
                "kind": pending.kind,
                "title": pending.title[:250] if pending.title else None,
                "res_model": pending.res_model,
                "res_id": pending.res_id,
                "payload": payload,
                "original": _original(env, pending.res_model, pending.res_id, payload)
                if pending.kind == "write"
                else None,
                "payload_signature": sign(payload),
                "expires_at": env.now() + DEFAULT_TTL,
            }
        )
    )


def _check(env: Environment, proposal: Model) -> Model:
    p = proposal.elevated().exists()
    if not p:
        raise MissingError("This proposal no longer exists")
    if p.user_id.ids[0] != env.uid:
        raise AccessError("Only the person who asked for this proposal can decide on it")
    if p.state != "pending":
        raise ConflictError(f"This proposal is already {p.state}")
    if p.expires_at and p.expires_at < env.now():
        p.write({"state": "expired"})
        raise ConflictError("This proposal has expired")
    if not p.payload_signature or not hmac.compare_digest(p.payload_signature, sign(p.payload)):
        raise AccessError("This proposal was modified after it was made and cannot be accepted")
    return p


def apply(
    env: Environment, kind: str, model: str, res_id: int | None, payload: dict[str, Any], run_id: int | None
) -> int | None:
    """Apply a decided proposal as ``env``'s user. Returns the id of a created record."""
    if model not in env.registry.models:
        raise ValidationError(f"Unknown model '{model}'")
    if kind == "create":
        return int(env[model].create(dict(payload)).ids[0])
    rec = env[model].browse([res_id]) if res_id else env[model]
    if kind != "action" and not rec.exists():
        raise MissingError("The record this proposal is about no longer exists")
    if kind == "write":
        rec.write(dict(payload))
    elif kind == "message":
        from modules.chatter.thread import post_message

        post_message(
            env,
            model,
            int(res_id or 0),
            _as_html(str(payload.get("body", ""))),
            subject=payload.get("subject") or None,
            message_type="note" if payload.get("internal") else "comment",
            check_access=True,
            ai_run_id=run_id,
        )
    elif kind == "transition":
        from modules.workflow.models.workflow import workflow_for

        wf = workflow_for(env, model)
        if not wf:
            raise ValidationError(f"'{model}' has no workflow")
        transitions = (
            env["workflow.transition"]
            .elevated()
            .search([("definition_id", "=", wf.ids[0]), ("to_state_id.key", "=", payload.get("to_state"))])
        )
        chosen = next((t for t in transitions if t.blocked_reason(rec) is None), None)
        if chosen is None:
            raise ConflictError(f"Cannot move to '{payload.get('to_state')}' from the current state")
        chosen.apply(rec)
    elif kind == "action":
        name = str(payload.get("action", ""))
        if name not in ACTIONS:
            raise ValidationError(f"Unknown action '{name}'")
        ACTIONS[name](env, model, res_id, dict(payload.get("args") or {}))
    return None


def _as_html(body: str) -> str:
    """Drafts are plain text; the thread stores HTML."""
    if body.lstrip().startswith("<"):
        return body
    import html

    paragraphs = [p for p in body.replace("\r\n", "\n").split("\n\n") if p.strip()]
    return "".join(f"<p>{html.escape(p).replace(chr(10), '<br/>')}</p>" for p in paragraphs)


def _update_run_decision(env: Environment, run: Model) -> None:
    if not run:
        return
    states = set(run.elevated().proposal_ids.mapped("state"))
    if "pending" in states or not states:
        decision = "pending" if states else run.decision
    elif states <= {"rejected", "expired", "failed"}:
        decision = "rejected"
    elif states <= {"accepted"}:
        decision = "accepted"
    elif states <= {"accepted", "edited"}:
        decision = "edited"
    else:
        decision = "partial"
    run.elevated().write({"decision": decision, "decided_by": env.uid, "decided_at": env.now()})


def accept(env: Environment, proposal: Model, edited_payload: dict[str, Any] | None = None) -> Model:
    """Accept (or edit then accept). A failure while applying leaves the proposal ``failed`` with the
    reason; nothing else changes."""
    p = _check(env, proposal)
    edited = edited_payload is not None and edited_payload != p.payload
    payload = edited_payload if edited_payload is not None else p.payload
    try:
        with env.cr.savepoint():
            created = apply(env, p.kind, p.res_model, p.res_id or None, payload, p.run_id.ids[0] if p.run_id else None)
            env.flush()
    except UserError as exc:
        p.write({"state": "failed", "reason": exc.message[:250], "decided_by": env.uid, "decided_at": env.now()})
        _update_run_decision(env, p.run_id)
        return p
    p.write(
        {
            "state": "edited" if edited else "accepted",
            "edited_payload": payload if edited else None,
            "decided_by": env.uid,
            "decided_at": env.now(),
            "result_res_id": created,
            "ai_generated": False,
        }
    )
    _update_run_decision(env, p.run_id)
    return p


def reject(env: Environment, proposal: Model, reason: str = "") -> Model:
    p = _check(env, proposal)
    p.write({"state": "rejected", "reason": (reason or "")[:250], "decided_by": env.uid, "decided_at": env.now()})
    _update_run_decision(env, p.run_id)
    return p


def expire(env: Environment) -> int:
    stale = env["ai_core.proposal"].elevated().search([("state", "=", "pending"), ("expires_at", "<", env.now())])
    stale.write({"state": "expired"})
    for run in stale.mapped("run_id"):
        _update_run_decision(env, run)
    return len(stale)
