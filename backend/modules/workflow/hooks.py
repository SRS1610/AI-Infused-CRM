"""Write guard for strict workflows, and the on_create / on_write / on_delete automation triggers."""

from __future__ import annotations

from typing import Any

from modules.workflow.engine import automations_for, run_automation
from modules.workflow.models.workflow import workflow_for
from tessera.exceptions import ConflictError
from tessera.i18n import _, translatable
from tessera.orm import Model
from tessera.orm.hooks import hook

OLD_KEY = "__workflow_old__"
SYSTEM_UID = 1


def _skip(records: Model) -> bool:
    return (
        not records._ids
        or records._name.startswith(("workflow.", "core.", "chatter.", "web."))
        or "workflow.automation" not in records.env.registry.models
        or records.env.context.get("automation_disable")
    )


@hook("before_write")
def guard_and_snapshot(records: Model, vals: dict[str, Any], old_values: dict[Any, Any]) -> None:
    if _skip(records):
        return
    wf = workflow_for(records.env, records._name)
    guarded = wf and wf.strict and wf.state_field in vals and not records.env.context.get("workflow_transition")
    if guarded and records.env.uid != SYSTEM_UID:  # data files and platform code run as the system user
        changing = [r for r in records if wf.state_key(r) != _as_key(vals[wf.state_field])]
        if changing:
            raise ConflictError(
                _(
                    "'{label}' follows the {name} workflow: use its buttons",
                    label=translatable(records._fields[wf.state_field].label),
                    name=wf.name,
                ),
                field=wf.state_field,
            )
    autos = automations_for(records, ("on_write", "on_create_or_write"))
    if not autos:
        return
    names: set[str] = set()
    for auto in autos:
        names.update(auto.watched_fields or [])
    names &= set(records._fields)
    snapshot = {}
    for rec in records.elevated():
        snapshot[rec.ids[0]] = {n: rec[n] for n in names}
    pre: dict[int, set[int]] = {}
    for auto in autos:
        if auto.pre_filter_domain:
            from modules.workflow.engine import _matching

            pre[auto.ids[0]] = set(_matching(auto, records.elevated(), auto.pre_filter_domain).ids)
    old_values[OLD_KEY] = {"snapshot": snapshot, "pre": pre, "autos": autos.ids}


def _as_key(value: Any) -> str | None:
    if hasattr(value, "_ids"):
        return str(value.ids[0]) if value.ids else None
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return value or None


@hook("after_write")
def trigger_on_write(records: Model, vals: dict[str, Any], old_values: dict[Any, Any]) -> None:
    info = old_values.get(OLD_KEY)
    if not info or _skip(records):
        return
    autos = records.env["workflow.automation"].elevated().browse(info["autos"])
    for auto in autos:
        watched = set(auto.watched_fields or [])
        targets = records
        if watched:
            if not watched & set(vals):
                continue
            targets = records.filtered(
                lambda r, w=watched: any(info["snapshot"].get(r.ids[0], {}).get(n) != r[n] for n in w)
            )
        if auto.pre_filter_domain:
            allowed = info["pre"].get(auto.ids[0], set())
            targets = targets.filtered(lambda r, a=allowed: r.ids[0] in a)
        run_automation(auto, targets, "write", old=info["snapshot"])


@hook("after_create")
def trigger_on_create(records: Model, vals_list: list[dict[str, Any]]) -> None:
    if _skip(records):
        return
    for auto in automations_for(records, ("on_create", "on_create_or_write")) or []:
        run_automation(auto, records, "create")


@hook("before_unlink")
def trigger_on_delete(records: Model) -> None:
    if _skip(records):
        return
    for auto in automations_for(records, ("on_delete",)) or []:
        run_automation(auto, records, "delete")
