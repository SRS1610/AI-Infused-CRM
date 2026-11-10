"""Studio operations. Each one records a ``studio.change`` (kind, before/after metadata, the record it
created) so it can be rolled back on its own or as part of a snapshot, and exported as a module.

Adding a field: a ``studio.field`` row → the registry is rebuilt in this process with the field as a
dynamic fragment → additive schema sync (the column) → field metadata → the registry stamp changes so
other processes rebuild on their next transaction (no restart). If the transaction rolls back, the
in-process registry is rebuilt again from the committed state.

Rolling back a field removes it from the registry, its metadata and every Studio view change that uses
it, and renames its column (D-007: kept as ``_removed_<stamp>_<name>``); rolling back twice is a no-op.
"""

from __future__ import annotations

import io
import json
import re
import time
import zipfile
from typing import Any

from modules.studio.hooks import build_fragments, rows_from_cursor
from tessera.exceptions import AccessError, ValidationError
from tessera.orm import fields as F
from tessera.orm.registry import bump_registry_stamp

DESIGNER = "studio.group_designer"
NAME = re.compile(r"^x_[a-z][a-z0-9_]{0,50}$")
VIEW_TYPES = ("form", "list", "kanban", "search", "calendar", "pivot", "graph")


def check_designer(env: Any) -> None:
    if not env.is_system and not env.registry.security.has_groups(env, [DESIGNER]):
        raise AccessError("Studio is reserved to designers")


def _sys(env: Any) -> Any:
    return env.elevated("studio")


def editable_models(env: Any) -> list[dict[str, Any]]:
    out = []
    for name, cls in sorted(env.registry.models.items()):
        if cls._studio is False or not cls._auto or cls._abstract or name.startswith(("studio.", "ai_core.")):
            continue
        out.append({"model": name, "description": cls._description or name, "module": cls._module})
    return out


def _model_cls(env: Any, model_name: str) -> Any:
    cls = env.registry.models.get(model_name)
    if cls is None or cls._studio is False or not cls._auto:
        raise ValidationError(f"'{model_name}' cannot be customised in Studio")
    return cls


def _record_change(env: Any, kind: str, model_name: str | None, summary: str, **vals: Any) -> Any:
    return _sys(env)["studio.change"].create({"kind": kind, "model_name": model_name, "summary": summary, **vals})


def _rebuild(env: Any, sync_models: list[str]) -> None:
    """Rebuild the in-process registry from this transaction's Studio fields, sync the schema of
    ``sync_models``, refresh field metadata and tell other processes."""
    from tessera.orm.schema import SchemaSync

    registry = env.registry
    env.flush()
    registry.build(dynamic=build_fragments(rows_from_cursor(env.cr)))
    env.cr.on_rollback(registry.build)  # back to the committed state if this transaction fails
    SchemaSync(env.cr, registry).sync([registry.models[m] for m in sync_models if m in registry.models])
    registry.run_hooks("sync_metadata", _sys(env))
    registry.stamp = bump_registry_stamp(env.cr)


# ----------------------------------------------------------------------------- fields


def add_field(
    env: Any,
    model_name: str,
    name: str,
    ttype: str,
    label: str,
    *,
    placements: list[dict[str, Any]] | None = None,
    **options: Any,
) -> dict[str, Any]:
    """Add a field; ``placements``: ``[{"view_type": "form", "after": "partner_id"}, {"view_type": "list"}]``."""
    check_designer(env)
    cls = _model_cls(env, model_name)
    name = name if name.startswith("x_") else f"x_{name}"
    name = re.sub(r"[^a-z0-9_]", "_", name.lower())
    if not NAME.match(name):
        raise ValidationError("Field names start with x_ and use lowercase letters, digits and underscores")
    if name in cls._fields:
        raise ValidationError(f"'{model_name}' already has a field '{name}'")
    if ttype in ("many2one", "many2many"):
        comodel = options.get("comodel")
        if comodel not in env.registry.models:
            raise ValidationError("Link fields need an existing target model", field="comodel")
    if ttype == "monetary":
        currency = options.get("currency_field") or "currency_id"
        if currency not in cls._fields:
            raise ValidationError(f"Amount fields need a currency field on '{model_name}'", field="currency_field")
        options["currency_field"] = currency
    if ttype == "selection" and not options.get("selection"):
        raise ValidationError("Choice fields need their options", field="selection")
    allowed = {
        "help",
        "required",
        "readonly",
        "readonly_if",
        "required_if",
        "default",
        "selection",
        "comodel",
        "currency_field",
        "groups",
        "widget",
        "tracking",
    }
    unknown = set(options) - allowed
    if unknown:
        raise ValidationError(f"Unknown field options: {', '.join(sorted(unknown))}")
    field_rec = (
        _sys(env)["studio.field"]
        .with_context(studio_internal=True)
        .create({"model_name": model_name, "name": name, "ttype": ttype, "label": label, **options})
    )
    _rebuild(env, [model_name])
    _sys(env)["core.model_field"].search([("model_name", "=", model_name), ("name", "=", name)]).write({"studio": True})
    change = _record_change(
        env,
        "add_field",
        model_name,
        f"Added field “{label}” ({name}) to {cls._description or model_name}",
        before=None,
        after={"field": {"model_name": model_name, "name": name, "ttype": ttype, "label": label, **options}},
        res_model="studio.field",
        res_id=field_rec.ids[0],
    )
    view_changes = []
    for place in placements or []:
        view_type = place.get("view_type", "form")
        node: dict[str, Any] = {"kind": "field", "name": name}
        if options.get("widget"):
            node["widget"] = options["widget"]
        if view_type == "list":
            node = {"field": name}
            op = (
                {"op": "insert_after", "anchor": {"field": place["after"]}, "node": node}
                if place.get("after")
                else None
            )
            op = op or {"op": "append_column", "node": node}
        elif place.get("after"):
            op = {"op": "insert_after", "anchor": {"field": place["after"]}, "node": node}
        else:
            op = {"op": "append", "anchor": {"path": place.get("path", "/root")}, "node": node}
        view_changes.append(change_view(env, model_name, view_type, [op], f"Placed {label} on the {view_type} view"))
    return {"change_id": change.ids[0], "field": name, "view_change_ids": [c.ids[0] for c in view_changes]}


# ----------------------------------------------------------------------------- views


def _base_view(env: Any, model_name: str, view_type: str) -> Any:
    from modules.web.views import default_view

    views = _sys(env)["web.view"]
    base = views.search(
        [("model_name", "=", model_name), ("type", "=", view_type), ("inherit_id", "=", False)],
        order="priority, id",
        limit=1,
    )
    if base:
        return base
    # no base view yet: materialise the generated default so Studio has something to extend
    definition = default_view(_sys(env), model_name, view_type)
    for key in ("type", "model", "view_id"):
        definition.pop(key, None)
    return views.create(
        {
            "name": f"{model_name}.{view_type} (studio)",
            "model_name": model_name,
            "type": view_type,
            "definition": definition,
            "module": "studio",
            "studio": True,
            "priority": 99,
        }
    )


def _list_ops(base: Any, operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """List views keep columns in ``columns``: translate the Studio-only ``append_column`` op."""
    out = []
    for op in operations:
        if op.get("op") == "append_column":
            columns = list((base.definition or {}).get("columns") or [])
            anchor = columns[-1]["field"] if columns else None
            if anchor:
                out.append({"op": "insert_after", "anchor": {"field": anchor}, "node": op["node"]})
                continue
        out.append(op)
    return out


def change_view(env: Any, model_name: str, view_type: str, operations: list[dict[str, Any]], summary: str) -> Any:
    """Store a layout change as an extension view of the model's base view (view inheritance)."""
    from modules.web.views import resolve_view, validate_operations

    check_designer(env)
    _model_cls(env, model_name)
    if view_type not in VIEW_TYPES:
        raise ValidationError(f"Unknown view type '{view_type}'")
    base = _base_view(env, model_name, view_type)
    operations = _list_ops(base, operations)
    validate_operations(operations)
    before = resolve_view(_sys(env), model_name, view_type)
    ext = _sys(env)["web.view"].create(
        {
            "name": f"{model_name}.{view_type} studio #{int(time.time() * 1000) % 10_000_000}",
            "model_name": model_name,
            "type": view_type,
            "inherit_id": base.ids[0],
            "operations": operations,
            "sequence": 1000,
            "module": "studio",
            "studio": True,
        }
    )
    after = resolve_view(_sys(env), model_name, view_type)  # raises if an anchor does not exist
    return _record_change(
        env,
        "view_change",
        model_name,
        summary,
        before={"view": before},
        after={"view": after, "operations": operations},
        res_model="web.view",
        res_id=ext.ids[0],
    )


def create_view(env: Any, model_name: str, view_type: str, definition: dict[str, Any], name: str) -> Any:
    from modules.web.views import validate_definition

    check_designer(env)
    _model_cls(env, model_name)
    validate_definition(view_type, definition)
    view = _sys(env)["web.view"].create(
        {
            "name": name,
            "model_name": model_name,
            "type": view_type,
            "definition": definition,
            "module": "studio",
            "studio": True,
            "priority": 20,
        }
    )
    return _record_change(
        env,
        "new_view",
        model_name,
        f"Created the {view_type} view “{name}”",
        before=None,
        after={"definition": definition},
        res_model="web.view",
        res_id=view.ids[0],
    )


# ----------------------------------------------------------------------------- automations and workflows


def add_automation(env: Any, values: dict[str, Any]) -> Any:
    check_designer(env)
    _model_cls(env, values.get("model_name", ""))
    auto = _sys(env)["workflow.automation"].create(values)
    return _record_change(
        env,
        "automation",
        values["model_name"],
        f"Automated action “{values.get('name')}”",
        before=None,
        after={"automation": values},
        res_model="workflow.automation",
        res_id=auto.ids[0],
    )


def create_workflow(
    env: Any,
    model_name: str,
    state_field: str,
    states: list[dict[str, Any]],
    transitions: list[dict[str, Any]],
    name: str,
) -> Any:
    """A simple state machine on an existing selection/many2one field (no code)."""
    check_designer(env)
    cls = _model_cls(env, model_name)
    if state_field not in cls._fields:
        raise ValidationError(f"'{model_name}' has no field '{state_field}'", field="state_field")
    sys_env = _sys(env)
    wf = sys_env["workflow.definition"].create({"name": name, "model_name": model_name, "state_field": state_field})
    by_key = {}
    for i, s in enumerate(states):
        by_key[s["key"]] = sys_env["workflow.state"].create(
            {"definition_id": wf.ids[0], "key": s["key"], "name": s.get("name", s["key"]), "sequence": i}
        )
    for t in transitions:
        sys_env["workflow.transition"].create(
            {
                "definition_id": wf.ids[0],
                "name": t["name"],
                "from_state_ids": [by_key[k].ids[0] for k in t.get("from", [])],
                "to_state_id": by_key[t["to"]].ids[0],
            }
        )
    return _record_change(
        env,
        "workflow",
        model_name,
        f"Workflow “{name}” on {cls._description or model_name}",
        before=None,
        after={"state_field": state_field, "states": states, "transitions": transitions, "name": name},
        res_model="workflow.definition",
        res_id=wf.ids[0],
    )


# ----------------------------------------------------------------------------- rollback


def _field_used_by(change: Any, field_name: str) -> bool:
    return field_name in json.dumps((change.after or {}).get("operations") or [])


def rollback(env: Any, change: Any) -> bool:
    """Undo one change. Returns False when it was already rolled back (no-op)."""
    check_designer(env)
    sys_env = _sys(env)
    change = sys_env["studio.change"].browse(change.ids)
    if change.state == "rolled_back":
        return False
    target = (
        sys_env[change.res_model].browse([change.res_id]).exists() if change.res_model in env.registry.models else None
    )
    if change.kind == "add_field" and target:
        name, model_name = target.name, target.model_name
        dependants = sys_env["studio.change"].search(
            [("kind", "=", "view_change"), ("model_name", "=", model_name), ("state", "=", "applied")]
        )
        for dep in dependants:
            if _field_used_by(dep, name):
                rollback(env, dep)
        fld = env.registry.models[model_name]._fields.get(name)
        target.write({"active": False})
        _rebuild(env, [])
        stamp = time.strftime("%Y%m%d%H%M%S")
        table = env.registry.models[model_name]._table
        if isinstance(fld, F.Many2many):
            env.cr.execute(f'ALTER TABLE IF EXISTS "{fld.relation}" RENAME TO "_removed_{stamp}_{fld.relation}"'[:200])
        else:
            env.cr.execute(f'ALTER TABLE "{table}" RENAME COLUMN "{name}" TO "_removed_{stamp}_{name}"')
        sys_env["core.model_field"].with_context(active_test=False).search(
            [("model_name", "=", model_name), ("name", "=", name)]
        ).unlink()
    elif change.kind in ("view_change", "new_view", "automation", "workflow") and target:
        target.unlink()
    change.write({"state": "rolled_back", "rolled_back_at": env.now(), "rolled_back_by": env.uid})
    return True


def snapshot(env: Any, name: str) -> Any:
    check_designer(env)
    last = _sys(env)["studio.change"].search([], order="id desc", limit=1)
    return _sys(env)["studio.snapshot"].create({"name": name, "last_change_id": last.ids[0] if last else 0})


def rollback_to(env: Any, snap: Any) -> int:
    """Undo every change made after the snapshot, newest first."""
    check_designer(env)
    later = _sys(env)["studio.change"].search(
        [("id", ">", snap.last_change_id), ("state", "=", "applied")], order="id desc"
    )
    return sum(1 for c in later if rollback(env, c))


# ----------------------------------------------------------------------------- export


def export_module(env: Any, company_slug: str = "default") -> tuple[str, bytes]:
    """Every applied Studio change as an installable module ``studio_customizations_<company>``:
    fields, view extensions, views, automations and workflows as data files (1.7-d)."""
    check_designer(env)
    sys_env = _sys(env)
    module = f"studio_customizations_{re.sub(r'[^a-z0-9_]', '_', company_slug.lower())}"
    changes = sys_env["studio.change"].search([("state", "=", "applied")], order="id")
    models = sorted({c.model_name for c in changes if c.model_name})
    depends = sorted({"studio", *(env.registry.models[m]._module for m in models if m in env.registry.models)})
    fields_data, views_data, autos, workflows = [], [], [], []
    for c in changes:
        target = sys_env[c.res_model].browse([c.res_id]).exists() if c.res_model else None
        if not target:
            continue
        xid = f"{c.kind}_{c.ids[0]}"
        if c.kind == "add_field":
            vals = {
                k: target[k]
                for k in (
                    "model_name",
                    "name",
                    "ttype",
                    "label",
                    "help",
                    "required",
                    "readonly",
                    "readonly_if",
                    "required_if",
                    "default",
                    "selection",
                    "comodel",
                    "currency_field",
                    "groups",
                    "widget",
                    "tracking",
                )
            }
            fields_data.append(
                {
                    "id": xid,
                    "model": "studio.field",
                    "values": {k: v for k, v in vals.items() if v not in (None, False, "")},
                }
            )
        elif c.kind in ("view_change", "new_view"):
            base = target.inherit_id
            vals = {
                "name": target.name,
                "model_name": target.model_name,
                "type": target.type,
                "module": module,
                "studio": True,
            }
            if base:
                base_xid = next(iter(base.get_external_id().values()), None)
                vals.update(operations=target.operations, sequence=target.sequence)
                if base_xid:
                    vals["inherit_id"] = base_xid
                else:  # a Studio-materialised base view: export it first
                    base_id = f"base_{base.ids[0]}"
                    if not any(v["id"] == base_id for v in views_data):
                        views_data.append(
                            {
                                "id": base_id,
                                "model": "web.view",
                                "values": {
                                    "name": base.name,
                                    "model_name": base.model_name,
                                    "type": base.type,
                                    "definition": base.definition,
                                    "priority": base.priority,
                                    "module": module,
                                    "studio": True,
                                },
                            }
                        )
                    vals["inherit_id"] = f"{module}.{base_id}"
            else:
                vals.update(definition=target.definition, priority=target.priority)
            views_data.append({"id": xid, "model": "web.view", "values": vals})
        elif c.kind == "automation":
            autos.append({"id": xid, "model": "workflow.automation", "values": (c.after or {}).get("automation", {})})
        elif c.kind == "workflow":
            workflows.append({"id": xid, "after": c.after})
    lines = [
        f'name = "{module}"',
        'version = "1.0.0"',
        'summary = "Studio customisations exported from Tessera"',
        'category = "Customisations"',
        f"depends = {json.dumps(depends)}",
        "installable = true",
    ]
    if workflows:
        lines.append('post_init = "hooks:post_init"')
    lines += ["", "[load]", 'data = ["data/fields.json", "data/views.json", "data/automations.json"]', ""]
    model_of = {c.ids[0]: c.model_name for c in changes if c.kind == "workflow"}
    exported = [{**w["after"], "model_name": model_of[int(w["id"].rsplit("_", 1)[1])]} for w in workflows]
    hooks_py = (
        '"""Re-creates the Studio workflows of the export."""\n\nfrom modules.studio.service import create_workflow\n\n'
        f"WORKFLOWS = {json.dumps(exported, indent=2)}\n\n\n"
        "def post_init(env):  # type: ignore[no-untyped-def]\n"
        "    for wf in WORKFLOWS:\n"
        "        create_workflow(\n"
        '            env, wf["model_name"], wf["state_field"], wf["states"], wf["transitions"], wf["name"]\n'
        "        )\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{module}/manifest.toml", "\n".join(lines))
        z.writestr(f"{module}/__init__.py", f'"""{module}: exported Studio customisations."""\n')
        z.writestr(f"{module}/data/fields.json", json.dumps(fields_data, indent=2))
        z.writestr(f"{module}/data/views.json", json.dumps(views_data, indent=2))
        z.writestr(f"{module}/data/automations.json", json.dumps(autos, indent=2))
        if workflows:
            z.writestr(f"{module}/hooks.py", hooks_py)
    return module, buf.getvalue()
