"""Automation engine: matching, execution, once-only time triggers, recursion guard."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from tessera.expr import evaluate, standard_names
from tessera.logging import get_logger
from tessera.orm.domain import Domain

if TYPE_CHECKING:
    from tessera.orm.model import Model

log = get_logger(__name__)

MAX_DEPTH = 3
SKIP_MODELS_PREFIX = ("workflow.", "core.job", "core.audit", "core.notification", "chatter.", "core.mail_queue")


def automations_for(records: Model, triggers: tuple[str, ...]) -> Any:
    """Active automations of ``records``' model for these triggers (an empty list when none apply)."""
    if records._name.startswith(SKIP_MODELS_PREFIX) or "workflow.automation" not in records.env.registry.models:
        return []
    return (
        records.env["workflow.automation"]
        .elevated()
        .search([("model_name", "=", records._name), ("trigger", "in", list(triggers)), ("active", "=", True)])
    )


def _matching(auto: Model, records: Model, domain: Any) -> Model:
    if not domain or not records:
        return records
    return records.with_context(active_test=False).search([("id", "in", records.ids), Domain(domain).node])


def run_automation(
    auto: Model,
    records: Model,
    event: str,
    *,
    old: dict[int, dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
    once_key: str | None = None,
) -> int:
    """Run ``auto``'s actions on each record that passes its filters. Returns how many ran."""
    if not records:
        return 0
    env = records.env
    depth = int(env.context.get("automation_depth", 0))
    chain = tuple(env.context.get("automation_chain", ()))
    if depth >= MAX_DEPTH or auto.ids[0] in chain:
        return 0  # an automation never re-triggers itself, and chains stop at MAX_DEPTH
    scoped = records.with_context(automation_depth=depth + 1, automation_chain=(*chain, auto.ids[0])).elevated(
        f"automation {auto.ids[0]}"
    )
    targets = _matching(auto, scoped, auto.filter_domain) if event != "manual" else scoped
    runs = env["workflow.automation_run"].elevated()
    count = 0
    for rec in targets:
        base = {"automation_id": auto.ids[0], "model": rec._name, "res_id": rec.ids[0], "event": event}
        run = None
        if once_key:
            if runs.search_count([("once_key", "=", once_key)]):
                continue
            run = runs.create({**base, "status": "skipped", "once_key": once_key})  # claim (unique key)
        names = standard_names(env, rec, old=(old or {}).get(rec.ids[0], {}), **(extra or {}))
        try:
            if auto.condition and not evaluate(auto.condition, names):
                continue
            with env.cr.savepoint():
                for action in auto.actions or []:
                    _execute(env, auto, rec, action, names)
            if run is not None:
                run.write({"status": "done"})
            else:
                runs.create({**base, "status": "done"})
            count += 1
        except Exception as exc:
            log.warning("automation failed", automation=auto.ids[0], record=rec.ids[0], error=str(exc)[:300])
            outcome = {"status": "error", "error": str(exc)[:4000]}
            if run is not None:
                run.write(outcome)
            else:
                runs.create({**base, **outcome})
            if auto.on_error == "raise":
                raise
    if count:
        env.cr.execute(
            "UPDATE workflow_automation SET run_count = COALESCE(run_count, 0) + :n WHERE id = :id",
            {"n": count, "id": auto.ids[0]},
        )
    return count


def _value(expr: Any, names: dict[str, Any]) -> Any:
    value = evaluate(str(expr), names)
    if hasattr(value, "_ids"):  # a record assigned to a many2one
        return value.ids[0] if value.ids else None
    return value


def _execute(env: Any, auto: Model, rec: Model, action: dict[str, Any], names: dict[str, Any]) -> None:
    kind = action["type"]
    if kind == "update":
        vals = {f: _value(expr, names) for f, expr in action["values"].items()}
        rec.write(vals)
    elif kind == "post_message":
        from modules.chatter.thread import is_thread, post_message

        if is_thread(rec):
            post_message(
                env,
                rec._name,
                rec.ids[0],
                str(_value(action["body"], names) or ""),
                message_type="note" if action.get("internal", True) else "comment",
            )
    elif kind == "schedule_activity":
        from modules.chatter.thread import schedule_activity

        user = _value(action["user"], names) if action.get("user") else None
        days = int(action.get("days", 0))
        deadline = (env.today() + dt.timedelta(days=days)).isoformat()
        summary = _value(action["summary"], names) if action.get("summary") else None
        schedule_activity(
            env,
            rec._name,
            rec.ids[0],
            action.get("activity_type", "chatter.activity_type_todo"),
            summary,
            deadline,
            user or env.uid,
        )
    elif kind == "notify":
        users = _value(action["users"], names) if action.get("users") else None
        if users is None:
            followers = (
                env["chatter.follower"].elevated().search([("model", "=", rec._name), ("res_id", "=", rec.ids[0])])
            )
            users = [f.user_id.ids[0] for f in followers]
        if isinstance(users, int):
            users = [users]
        title = str(_value(action["title"], names))
        body = str(_value(action["body"], names)) if action.get("body") else ""
        env["core.notification"].notify(
            list(users or []), title, body, kind="system", model=rec._name, res_id=rec.ids[0]
        )
    elif kind == "create_record":
        vals = {f: _value(expr, names) for f, expr in action["values"].items()}
        env[action["model"]].create(vals)
    elif kind == "call_webhook":
        from tessera.jobs import enqueue

        payload = {
            "automation": auto.ids[0],
            "model": rec._name,
            "id": rec.ids[0],
            "data": rec.read(action.get("fields") or None)[0],
        }
        enqueue(
            env,
            "workflow.call_webhook",
            {"url": action["url"], "payload": payload},
            description=f"Automation webhook {auto.name}",
        )
    elif kind == "call_method":
        getattr(rec, action["method"])()


# -- triggers ---------------------------------------------------------------------------------------


def run_transition_automations(record: Model, transition: Model, old_state: str | None) -> None:
    autos = automations_for(record, ("on_transition",))
    for auto in autos or []:
        if auto.transition_id and auto.transition_id.ids[0] != transition.ids[0]:
            continue
        run_automation(auto, record, "transition", extra={"transition": transition.name, "old_state": old_state})


def run_time_automations(env: Any, now: dt.datetime | None = None) -> dict[int, int]:
    """Fire on_time automations whose trigger moment (date field + offset) fell in (last check, now].
    A run key per automation, record and moment makes each fire exactly once, even if checks overlap."""
    now = now or env.now()
    autos = env["workflow.automation"].elevated().search([("trigger", "=", "on_time"), ("active", "=", True)])
    fired: dict[int, int] = {}
    for auto in autos:
        model = env[auto.model_name].elevated()
        field = model._fields[auto.date_field]
        offset = dt.timedelta(days=auto.offset_days or 0)
        since = auto.last_time_check or auto.create_date
        if field.type_name == "date":
            # moment = date + offset, compared as dates: since.date() <= date + offset <= now.date()
            lo, hi = (since.date() - offset).isoformat(), (now.date() - offset).isoformat()
            domain: list[Any] = [(auto.date_field, ">=", lo), (auto.date_field, "<=", hi)]
        else:
            # moment = value + offset in (since, now]
            domain = [
                (auto.date_field, ">", (since - offset).isoformat()),
                (auto.date_field, "<=", (now - offset).isoformat()),
            ]
        if auto.filter_domain:
            domain.append(Domain(auto.filter_domain).node)
        candidates = model.search(domain, limit=5000)
        count = 0
        for rec in candidates:
            moment = rec[auto.date_field]
            key = f"{auto.ids[0]}:{rec.ids[0]}:{moment.isoformat() if moment else ''}"
            count += run_automation(auto, rec.with_env(env), "time", once_key=key)
        auto.elevated().write({"last_time_check": now})
        fired[auto.ids[0]] = count
    return fired
