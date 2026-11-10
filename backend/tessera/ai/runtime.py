"""Running an AI feature.

``run_feature`` is the single entry point business code uses::

    result = run_feature(env, "crm.followup_draft", {"tone": "warm"}, record=lead, sources=[...])

It checks the feature's kill switches (no gateway call when disabled), renders the feature's active
prompt version, runs a bounded tool loop through the gateway (tools execute as the requesting user,
read-only work is rolled back, writes become proposals), verifies citations against the sources the
model actually saw, and records everything in ``ai_core.run``. ``stream_feature`` does the same for
interactive drafting without tools.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from tessera.ai import prompts as T
from tessera.ai import proposals as P
from tessera.ai.client import CallContext, get_client
from tessera.ai.errors import AIError, FeatureDisabled, ModelRefused
from tessera.ai.tools import REGISTRY, PendingProposal, Source, ToolContext, ToolRejected, validate_arguments
from tessera.exceptions import AccessError, MissingError, UserError
from tessera.i18n import _

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

MAX_TOOL_RESULT = 8000
OnOutput = Callable[[ToolContext, str, Any], None]
Handler = Callable[["Environment", "Model | None", dict[str, Any]], dict[str, Any]]
HANDLERS: dict[str, Handler] = {}


def handler(name: str) -> Callable[[Handler], Handler]:
    """Register how the API prepares a feature: ``fn(env, record, inputs)`` returns extra keyword
    arguments for :func:`run_feature` (``sources``, ``on_output``)."""

    def decorator(func: Handler) -> Handler:
        HANDLERS[name] = func
        return func

    return decorator


@dataclass
class RunResult:
    run: Model
    text: str
    data: Any
    proposals: Model
    citations: list[dict[str, Any]] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run.ids[0],
            "feature": self.run.feature,
            "text": self.text,
            "data": self.data,
            "ai_generated": True,
            "citations": self.citations,
            "flags": self.flags,
            "proposals": [proposal_dict(p) for p in self.proposals],
            "status": self.run.status,
        }


def proposal_dict(p: Model) -> dict[str, Any]:
    return {
        "id": p.ids[0],
        "kind": p.kind,
        "title": p.title,
        "res_model": p.res_model,
        "res_id": p.res_id or None,
        "payload": p.edited_payload or p.payload,
        "original": p.original,
        "state": p.state,
        "reason": p.reason,
        "ai_generated": p.ai_generated,
        "run_id": p.run_id.ids[0] if p.run_id else None,
        "feature": p.feature,
        "expires_at": p.expires_at.isoformat() if p.expires_at else None,
    }


def get_feature(env: Environment, name: str) -> Model:
    feature = env["ai_core.feature"].elevated().search([("name", "=", name)], limit=1)
    if not feature:
        raise MissingError(_("Unknown AI feature '{name}'", name=name))
    return feature


def check_enabled(env: Environment, feature: Model) -> None:
    company = env.company.ids[0] if env.company else None
    if not feature.enabled_for(company):
        raise FeatureDisabled(_("The AI feature '{title}' is turned off", title=feature.title))
    if env.user.elevated().share:
        raise AccessError(_("AI features are available to internal users only"))


def _names_for(env: Environment, record: Model | None, sources: list[Source]) -> list[str]:
    """Person and company names present in the context, for redaction."""
    names: set[str] = set()
    for src in sources:
        if src.model in ("contacts.partner", "core.user"):
            names.add(src.title)
    if record is not None:
        from tessera.orm import fields as F

        rec = record.elevated()
        for f in rec._fields.values():
            person_link = isinstance(f, F.Many2one) and f.comodel_name in ("contacts.partner", "core.user")
            if (f.pii and isinstance(f, F.Char)) or person_link or f.name in ("contact_name", "partner_name"):
                value = rec[f.name]
                names.add(value.display_name if person_link and value else str(value or ""))
        if rec._name in ("contacts.partner", "core.user"):
            names.add(rec.display_name or "")
    names.add(env.user.elevated().name or "")
    # e-mail addresses and phone numbers are caught by the gateway's own patterns
    return sorted(n for n in names if n and len(n) >= 3 and "@" not in n and any(c.isalpha() for c in n))


def _prepare(
    env: Environment, feature: Model, inputs: dict[str, Any], record: Model | None, sources: list[Source]
) -> tuple[Model, str, str]:
    prompt = env["ai_core.prompt"].elevated().search([("name", "=", feature.prompt or feature.name)], limit=1)
    if not prompt:
        raise UserError(_("AI feature '{name}' has no prompt", name=feature.name))
    version = prompt.version_for(env.company.ids[0] if env.company else None, env.uid)
    T.validate_inputs(version.input_schema, inputs)
    variables = {
        "inputs": inputs,
        "sources": sources,
        "record": record.elevated() if record is not None else None,
        "user": env.user.elevated(),
        "company": env.company.elevated(),
        "today": env.today().isoformat(),
    }
    system = T.system_prompt(T.render(version.system, variables))
    task = T.render(version.template, variables)
    return version, system, task


def _new_run(
    env: Environment,
    feature: Model,
    version: Model,
    inputs: dict[str, Any],
    record: Model | None,
    sources: list[Source],
    system: str,
    task: str,
) -> Model:
    return (
        env["ai_core.run"]
        .elevated("ai run")
        .create(
            {
                "feature_id": feature.ids[0],
                "feature": feature.name,
                "user_id": env.uid,
                "company_id": env.company.ids[0] if env.company else None,
                "prompt_version": version.ref,
                "res_model": record._name if record is not None else None,
                "res_id": record.ids[0] if record is not None and record.ids else None,
                "input_ref": {
                    "inputs": json.loads(json.dumps(inputs, default=str)),
                    "sources": [s.id for s in sources],
                },
                "input_text": f"{system}\n\n---\n\n{task}",
            }
        )
    )


def _execute_tool(
    ctx: ToolContext, run: Model, step: int, name: str, args: dict[str, Any], allowed: list[str]
) -> tuple[str, bool]:
    """Run one tool call as the requesting user. Returns (result text, is_error)."""
    started = time.monotonic()
    calls = ctx.env["ai_core.tool_call"].elevated("ai tool call")
    tdef = REGISTRY.get(name)
    if name not in allowed or tdef is None:
        calls.create(
            {
                "run_id": run.ids[0],
                "step": step,
                "name": name,
                "arguments": args,
                "allowed": False,
                "error": "tool_not_allowed",
            }
        )
        return json.dumps(
            {"error": "tool_not_allowed", "message": f"The tool '{name}' is not available for this task."}
        ), True
    result: Any
    error = None
    proposals_before = len(ctx.proposals)
    try:
        validate_arguments(tdef, args)
        # every tool runs in a savepoint that is rolled back: tools read; writes only happen when a
        # person accepts a proposal later
        holder: dict[str, Any] = {}
        try:
            with ctx.env.cr.savepoint():
                holder["result"] = json.loads(json.dumps(tdef.func(ctx, **args), default=str))
                ctx.env.flush()
                raise _Rollback
        except _Rollback:
            result = holder["result"]
    except ToolRejected as exc:
        error, result = "invalid_arguments", {"error": "invalid_arguments", "message": str(exc)}
    except (AccessError, MissingError) as exc:
        error, result = "access_denied", {"error": "access_denied", "message": exc.message or "Not allowed"}
    except UserError as exc:
        error, result = "error", {"error": "error", "message": exc.message}
    if not tdef.writes and len(ctx.proposals) > proposals_before:
        del ctx.proposals[proposals_before:]  # read tools cannot propose
    text = json.dumps(result, default=str)[:MAX_TOOL_RESULT]
    calls.create(
        {
            "run_id": run.ids[0],
            "step": step,
            "name": name,
            "arguments": args,
            "result": json.loads(text) if len(text) < MAX_TOOL_RESULT else {"truncated": True},
            "allowed": True,
            "error": error,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    )
    return text, error is not None


class _Rollback(Exception):
    """Raised into the tool savepoint's ``__exit__`` so it rolls back."""


def _finish(
    ctx: ToolContext,
    run: Model,
    feature: Model,
    text: str,
    data: Any,
    usage: dict[str, int],
    meta: dict[str, Any],
    on_output: OnOutput | None,
) -> RunResult:
    env = ctx.env
    flags = list(meta.get("safety_flags") or [])
    cited = T.extract_citations(text)
    if isinstance(data, dict):
        cited += [c for c in data.get("citations", []) if isinstance(c, str)]
    valid = [c for c in dict.fromkeys(cited) if c in ctx.sources]
    invalid = {c for c in cited if c not in ctx.sources}
    if invalid:
        flags.append("fabricated_citation")
        text = T.strip_citations(text, invalid)
        if isinstance(data, dict) and isinstance(data.get("citations"), list):
            data = {**data, "citations": [c for c in data["citations"] if c not in invalid]}
    citations = []
    cit_model = env["ai_core.citation"].elevated("ai citation")
    for c in valid:
        src = ctx.sources[c]
        cit_model.create(
            {
                "run_id": run.ids[0],
                "source": c,
                "res_model": src.model,
                "res_id": src.res_id,
                "title": src.title,
                "snippet": src.text[:500],
            }
        )
        citations.append({"id": c, "res_model": src.model, "res_id": src.res_id, "title": src.title})
    if on_output is not None:
        on_output(ctx, text, data)
    created = env["ai_core.proposal"]
    for pending in ctx.proposals:
        created |= P.create(env, run, feature.name, pending)
    run.elevated().write(
        {
            "status": "ok",
            "output_text": text,
            "output_data": data,
            "safety_flags": flags,
            "decision": "pending" if created else "none",
            **usage,
            **{
                k: meta[k]
                for k in ("provider", "model", "inference_geo", "fallback_used", "cached", "redaction_applied")
                if k in meta
            },
        }
    )
    return RunResult(run, text, data, created, citations, flags)


def _usage_add(total: dict[str, Any], resp: dict[str, Any]) -> None:
    u = resp.get("usage") or {}
    total["input_tokens"] = total.get("input_tokens", 0) + u.get("input_tokens", 0)
    total["output_tokens"] = total.get("output_tokens", 0) + u.get("output_tokens", 0)
    total["cache_read_tokens"] = total.get("cache_read_tokens", 0) + u.get("cache_read_tokens", 0)
    total["cost"] = round(total.get("cost", 0.0) + float(resp.get("cost") or 0), 6)
    total["latency_ms"] = total.get("latency_ms", 0) + int(resp.get("latency_ms") or 0)


def _fail(run: Model, exc: AIError | UserError, status: str = "error") -> None:
    run.elevated().write(
        {"status": status, "error_code": getattr(exc, "code", type(exc).__name__), "error": exc.message[:2000]}
    )


def run_feature(
    env: Environment,
    name: str,
    inputs: dict[str, Any] | None = None,
    *,
    record: Model | None = None,
    sources: list[Source] | None = None,
    on_output: OnOutput | None = None,
    raise_errors: bool = True,
) -> RunResult:
    inputs = inputs or {}
    sources = list(sources or [])
    feature = get_feature(env, name)
    check_enabled(env, feature)
    version, system, task = _prepare(env, feature, inputs, record, sources)
    run = _new_run(env, feature, version, inputs, record, sources, system, task)
    ctx = ToolContext(env=env, feature=feature, run=run, sources={s.id: s for s in sources})
    allowed = list(feature.tools or [])
    tools = [REGISTRY[t].spec() for t in allowed if t in REGISTRY]
    call = CallContext(env.company.ids[0] if env.company else 0, env.uid, feature.name)
    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
    usage: dict[str, Any] = {}
    client = get_client(env)
    names = _names_for(env, record, sources)
    meta: dict[str, Any] = {}
    try:
        for step in range(max(1, feature.max_steps or 1)):
            body = {
                "task_class": feature.task_class,
                "system": system,
                "messages": messages,
                "tools": tools,
                "output_schema": version.output_schema or None,
                "max_output_tokens": int((version.model_hints or {}).get("max_output_tokens", 4096)),
                "redaction_policy": feature.redaction or "redact",
                "metadata": {
                    "feature": feature.name,
                    "prompt_version": version.ref,
                    "run_id": f"run-{run.ids[0]}",
                    "cache_ttl": feature.cache_ttl or 0,
                    "cache_key_extra": str(record.elevated().write_date)
                    if record is not None and "write_date" in record._fields
                    else "",
                    "redaction_names": names,
                },
            }
            resp = client.complete(call, body)
            _usage_add(usage, resp)
            meta = resp
            run.elevated().write({"steps": step + 1})
            if resp.get("stop") == "refusal":
                raise ModelRefused(
                    _("The model declined this request ({category})", category=resp.get("refusal_category") or "policy")
                )
            calls = [b for b in resp.get("content", []) if b.get("type") == "tool_use"]
            if resp.get("stop") != "tool_use" or not calls:
                break
            messages.append({"role": "assistant", "content": resp["content"]})
            results = []
            for b in calls:
                out, is_error = _execute_tool(ctx, run, step, b.get("name", ""), dict(b.get("input") or {}), allowed)
                results.append(
                    {"type": "tool_result", "tool_use_id": b.get("id"), "content": out, "is_error": is_error}
                )
            messages.append({"role": "user", "content": results})
        text = "".join(b.get("text") or "" for b in meta.get("content", []) if b.get("type") == "text")
        return _finish(ctx, run, feature, text, meta.get("parsed"), usage, meta, on_output)
    except AIError as exc:
        run.elevated().write(usage)
        _fail(
            run,
            exc,
            {
                "model_refused": "refusal",
                "invalid_output": "invalid_output",
                "budget_exceeded": "blocked",
                "quota_exceeded": "blocked",
                "feature_disabled": "blocked",
                "policy_refused": "blocked",
            }.get(exc.code, "error"),
        )
        exc.context["run_id"] = run.ids[0]
        if raise_errors:
            raise
        return RunResult(run, "", None, env["ai_core.proposal"], [], [exc.code])


def stream_feature(
    env: Environment,
    name: str,
    inputs: dict[str, Any] | None = None,
    *,
    record: Model | None = None,
    sources: list[Source] | None = None,
    on_output: OnOutput | None = None,
) -> Iterator[dict[str, Any]]:
    """Stream a draft: yields ``{"event": "start", "run_id"}``, ``delta`` events, then ``done`` with the
    run result (proposals included) or ``error``. Kill switches are checked before anything is sent."""
    inputs = inputs or {}
    sources = list(sources or [])
    feature = get_feature(env, name)
    check_enabled(env, feature)
    version, system, task = _prepare(env, feature, inputs, record, sources)
    run = _new_run(env, feature, version, inputs, record, sources, system, task)
    ctx = ToolContext(env=env, feature=feature, run=run, sources={s.id: s for s in sources})
    yield {"event": "start", "run_id": run.ids[0], "feature": feature.name, "ai_generated": True}
    body = {
        "task_class": feature.task_class,
        "system": system,
        "messages": [{"role": "user", "content": task}],
        "output_schema": version.output_schema or None,
        "max_output_tokens": int((version.model_hints or {}).get("max_output_tokens", 4096)),
        "redaction_policy": feature.redaction or "redact",
        "metadata": {
            "feature": feature.name,
            "prompt_version": version.ref,
            "run_id": f"run-{run.ids[0]}",
            "redaction_names": _names_for(env, record, sources),
        },
    }
    call = CallContext(env.company.ids[0] if env.company else 0, env.uid, feature.name)
    try:
        for ev in get_client(env).stream(call, body):
            kind = ev.get("event")
            if kind == "delta":
                yield {"event": "delta", "text": ev.get("text", "")}
            elif kind == "error":
                err = ev.get("error") or {}
                from tessera.ai.errors import from_code

                raise from_code(err.get("code", "ai_unavailable"), err.get("message", ""))
            elif kind == "done":
                resp = ev.get("response") or {}
                usage: dict[str, Any] = {}
                _usage_add(usage, resp)
                if resp.get("stop") == "refusal":
                    raise ModelRefused(_("The model declined this request"))
                text = "".join(b.get("text") or "" for b in resp.get("content", []) if b.get("type") == "text")
                result = _finish(ctx, run, feature, text, resp.get("parsed"), usage, resp, on_output)
                yield {"event": "done", "result": result.to_dict()}
                return
    except AIError as exc:
        _fail(run, exc)
        yield {"event": "error", "error": {"code": exc.code, "message": exc.message}, "run_id": run.ids[0]}


def propose_from_output(kind: str, model: str, res_id: int | None, payload: dict[str, Any], title: str) -> OnOutput:
    """Convenience ``on_output`` hook: turn the final output into one proposal."""

    def hook(ctx: ToolContext, text: str, data: Any) -> None:
        ctx.proposals.append(
            PendingProposal(
                kind,
                model,
                res_id,
                {**payload, **({"body": text} if kind == "message" and "body" not in payload else {})},
                title,
            )
        )

    return hook
