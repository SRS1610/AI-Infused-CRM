"""Studio operations. Each one records a ``studio.change`` (kind, before/after metadata, the record it
created) so it can be rolled back on its own or as part of a snapshot, and exported as a module.

Adding a field: a ``studio.field`` row → the registry is rebuilt in this process with the field as a
dynamic fragment → additive schema sync (the column) → field metadata → the registry stamp changes so
other processes rebuild on their next transaction (no restart). If the transaction rolls back, the
in-process registry is rebuilt again from the committed state.

Field options (widget, default, required/read-only conditions, groups) are columns of ``studio.field``.
Conditions are domains stored on the field (``required_if`` / ``readonly_if``): the client evaluates
both on forms and the ORM refuses a record that matches ``required_if`` without a value. Editing the
options records their previous values, so the edit rolls back on its own.

Rolling back a field removes it from the registry, its metadata and every Studio view change, option
edit and workflow that uses it, and renames its column (D-007: kept as ``_removed_<stamp>_<name>``);
rolling back twice is a no-op.
"""

from __future__ import annotations

import io
import json
import re
import time
import zipfile
from typing import Any

from modules.studio.hooks import build_fragments, default_of, rows_from_cursor
from tessera.exceptions import AccessError, ValidationError
from tessera.orm import fields as F
from tessera.orm.domain import Domain
from tessera.orm.registry import bump_registry_stamp

DESIGNER = "studio.group_designer"
NAME = re.compile(r"^x_[a-z][a-z0-9_]{0,50}$")
WIDGET = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
VIEW_TYPES = ("form", "list", "kanban", "search", "calendar", "pivot", "graph")
FIELD_OPTIONS = (
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
EDITABLE_OPTIONS = (
    "label",
    "help",
    "required",
    "readonly",
    "readonly_if",
    "required_if",
    "default",
    "selection",
    "groups",
    "widget",
    "tracking",
)
# list of a view -> key identifying one of its items (the anchor of inheritance operations)
LIST_KEYS = {"columns": "field", "filters": "name", "group_bys": "field"}


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


def list_groups(env: Any) -> list[dict[str, Any]]:
    """Security groups a field or a transition can be restricted to (those with an external id)."""
    groups = _sys(env)["core.group"].search([])
    xmlids = groups.get_external_id()
    return [{"xmlid": xmlids[g.ids[0]], "name": g.full_name} for g in groups if xmlids.get(g.ids[0])]


def _model_cls(env: Any, model_name: str) -> Any:
    cls = env.registry.models.get(model_name)
    if cls is None or cls._studio is False or not cls._auto:
        raise ValidationError(f"'{model_name}' cannot be customised in Studio")
    return cls


def _record_change(env: Any, kind: str, model_name: str | None, summary: str, **vals: Any) -> Any:
    return _sys(env)["studio.change"].create({"kind": kind, "model_name": model_name, "summary": summary, **vals})


def _rebuild(env: Any, sync_models: list[str], meta_models: list[str] | None = None) -> None:
    """Rebuild the in-process registry from this transaction's Studio fields, sync the schema of
    ``sync_models``, refresh field metadata and tell other processes."""
    from tessera.orm.schema import SchemaSync

    registry = env.registry
    env.flush()
    registry.build(dynamic=build_fragments(rows_from_cursor(env.cr)))
    env.cr.on_rollback(registry.build)  # back to the committed state if this transaction fails
    SchemaSync(env.cr, registry).sync([registry.models[m] for m in sync_models if m in registry.models])
    from modules.core.hooks import sync_model_metadata

    sync_model_metadata(_sys(env), only=list(meta_models if meta_models is not None else sync_models))
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
    """Add a field; ``placements``: ``[{"view_type": "form", "after": "partner_id"}, {"view_type": "list"}]``
    (``kanban`` adds it at the end of the card)."""
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
    unknown = set(options) - set(FIELD_OPTIONS)
    if unknown:
        raise ValidationError(f"Unknown field options: {', '.join(sorted(unknown))}")
    _check_options(env, cls, name, ttype, options, options.get("selection"))
    field_rec = (
        _sys(env)["studio.field"]
        .with_context(studio_internal=True)
        .create(_stored({"model_name": model_name, "name": name, "ttype": ttype, "label": label, **options}))
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
            node = {"field": name, **({"widget": options["widget"]} if options.get("widget") else {})}
            op = (
                {"op": "insert_after", "anchor": {"field": place["after"]}, "node": node}
                if place.get("after")
                else {"op": "append_to", "list": "columns", "node": node}
            )
        elif place.get("after"):
            op = {"op": "insert_after", "anchor": {"field": place["after"]}, "node": node}
        else:
            op = {"op": "append", "anchor": {"path": place.get("path", "/root")}, "node": node}
        view_changes.append(change_view(env, model_name, view_type, [op], f"Placed {label} on the {view_type} view"))
    return {"change_id": change.ids[0], "field": name, "view_change_ids": [c.ids[0] for c in view_changes]}


def _check_options(env: Any, cls: Any, name: str, ttype: str, options: dict[str, Any], selection: Any) -> None:
    """Validate (and normalise in place) the conditions, groups, widget and default of a field."""
    for key in ("required_if", "readonly_if"):
        if key not in options:
            continue
        if not options[key]:
            options[key] = None
            continue
        try:
            used = {path.split(".")[0] for path in Domain(options[key]).fields()}
        except Exception as exc:
            raise ValidationError(f"Invalid condition: {exc}", field=key) from exc
        unknown = used - set(cls._fields) - {name}
        if unknown:
            raise ValidationError(f"Unknown field in the condition: {', '.join(sorted(unknown))}", field=key)
    if "groups" in options:
        groups = options["groups"] or []
        if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
            raise ValidationError("Groups are a list of group references", field="groups")
        for ref in groups:
            group = env.ref(ref, raise_if_not_found=False)
            if group is None or group._name != "core.group":
                raise ValidationError(f"Unknown group '{ref}'", field="groups")
        options["groups"] = groups
    if "widget" in options:
        widget = options["widget"] or None
        if widget is not None and (not isinstance(widget, str) or not WIDGET.match(widget)):
            raise ValidationError("Invalid widget name", field="widget")
        options["widget"] = widget
    if "default" in options:
        options["default"] = _check_default(ttype, options["default"], selection)


def _check_default(ttype: str, value: Any, selection: Any) -> Any:
    if value is None or value == "":
        return None
    if ttype == "selection":
        ok = value in {str(opt[0]) for opt in selection or []}
    elif ttype in ("many2one", "integer"):
        ok = isinstance(value, int) and not isinstance(value, bool) and (ttype == "integer" or value > 0)
    elif ttype in ("float", "monetary"):
        ok = isinstance(value, int | float) and not isinstance(value, bool)
    elif ttype == "boolean":
        ok = isinstance(value, bool)
    elif ttype == "date":
        ok = isinstance(value, str) and bool(re.match(r"^\d{4}-\d{2}-\d{2}$", value))
    elif ttype == "datetime":
        ok = isinstance(value, str) and bool(re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", value))
    elif ttype == "many2many":
        ok = False
    else:
        ok = isinstance(value, str)
    if not ok:
        raise ValidationError("The default value does not suit the field's type", field="default")
    return value


def _stored(vals: dict[str, Any]) -> dict[str, Any]:
    """Values for ``studio.field``: a Json column takes a bare string for JSON text, so the default is
    stored wrapped as ``{"value": …}`` (``hooks.default_of`` unwraps it)."""
    if "default" in vals:
        return {**vals, "default": None if vals["default"] is None else {"value": vals["default"]}}
    return vals


def _studio_field(env: Any, model_name: str, name: str) -> Any:
    rec = _sys(env)["studio.field"].search(
        [("model_name", "=", model_name), ("name", "=", name), ("active", "=", True)], limit=1
    )
    if not rec:
        raise ValidationError(f"'{name}' is not a Studio field of '{model_name}'")
    return rec


def field_options(rec: Any) -> dict[str, Any]:
    """Current label and options of a Studio field (what the Edit field form shows)."""
    values = {k: rec[k] for k in EDITABLE_OPTIONS} | {"ttype": rec.ttype, "comodel": rec.comodel}
    return values | {"default": default_of(values["default"])}


def _refresh_field_meta(env: Any, model_name: str, name: str) -> None:
    """The metadata sync leaves ``core.model_field`` rows of Studio fields alone: refresh this one."""
    field = env.registry.models[model_name]._fields[name]
    desc = field.describe()
    vals = {k: desc[k] for k in ("string", "required", "readonly", "tracking", "groups")}
    vals["help"] = desc.get("help") or None
    if isinstance(field, F.Selection) and isinstance(field.selection, list | tuple):
        vals["selection"] = [list(opt) for opt in field.selection]
    _sys(env)["core.model_field"].search([("model_name", "=", model_name), ("name", "=", name)]).write(vals)


def edit_field(env: Any, model_name: str, name: str, **changes: Any) -> Any:
    """Change the label or options (widget, default, conditions, groups…) of a Studio field."""
    check_designer(env)
    cls = _model_cls(env, model_name)
    rec = _studio_field(env, model_name, name)
    unknown = set(changes) - set(EDITABLE_OPTIONS)
    if unknown:
        raise ValidationError(f"These options cannot be changed: {', '.join(sorted(unknown))}")
    if "label" in changes and not str(changes["label"] or "").strip():
        raise ValidationError("The label cannot be empty", field="label")
    if "selection" in changes:
        if rec.ttype != "selection" or not changes["selection"]:
            raise ValidationError("Only choice fields have options, and they need at least one", field="selection")
        changes["selection"] = [[str(k), str(v)] for k, v in changes["selection"]]
        if not {str(opt[0]) for opt in rec.selection or []} <= {k for k, _ in changes["selection"]}:
            raise ValidationError("Options can be added or renamed, not removed", field="selection")
    _check_options(env, cls, name, rec.ttype, changes, changes.get("selection") or rec.selection)
    before = field_options(rec)
    changed = {k: v for k, v in changes.items() if before.get(k) != v}
    if not changed:
        raise ValidationError("Nothing to change")
    rec.with_context(studio_internal=True).write(_stored(changed))
    if set(changed) - {"widget"}:
        _rebuild(env, [model_name])
        _refresh_field_meta(env, model_name, name)
    view_changes = []
    if "widget" in changed:
        for view_type in ("form", "list", "kanban"):
            if _view_has_field(env, model_name, view_type, name):
                op = {"op": "set_attr", "anchor": {"field": name}, "attrs": {"widget": changed["widget"]}}
                summary = f"Widget of {rec.label} on the {view_type} view"
                view_changes.append(change_view(env, model_name, view_type, [op], summary))
    return _record_change(
        env,
        "edit_field",
        model_name,
        f"Changed field “{rec.label}” ({name}): {', '.join(sorted(changed))}",
        before={"field": {k: before[k] for k in changed}},
        after={"field": changed, "view_change_ids": [c.ids[0] for c in view_changes]},
        res_model="studio.field",
        res_id=rec.ids[0],
    )


def _view_has_field(env: Any, model_name: str, view_type: str, name: str) -> bool:
    from modules.web.views import _find, resolve_view

    try:
        _find(resolve_view(_sys(env), model_name, view_type), {"field": name})
    except ValidationError:
        return False
    return True


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


def _studio_ops(cls: Any, view_type: str, current: dict[str, Any], operations: Any) -> list[dict[str, Any]]:
    """Translate Studio-only operations into inheritance operations, checking each one against the view
    as it will be (a missing anchor is reported before anything is stored).

    ``{"op": "append_to", "list": "columns" | "filters" | "group_bys", "node": {...}}`` adds an item at
    the end of a list of the view (``append_column`` is the older spelling for columns): it becomes an
    ``insert_after`` the current last item, or sets the list on the root while it is empty."""
    from modules.web.views import apply_operations, validate_operations

    if not isinstance(operations, list):
        raise ValidationError("operations must be a list")
    out = []
    for op in operations:
        if isinstance(op, dict) and op.get("op") == "append_column":
            op = {"op": "append_to", "list": "columns", "node": op.get("node")}
        if isinstance(op, dict) and op.get("op") == "append_to":
            key = op.get("list")
            if key not in LIST_KEYS or not isinstance(op.get("node"), dict):
                raise ValidationError(f"append_to needs a list ({', '.join(LIST_KEYS)}) and a node")
            ident = LIST_KEYS[key]
            items = [i for i in current.get(key) or [] if isinstance(i, dict) and i.get(ident)]
            if items:
                op = {"op": "insert_after", "anchor": {ident: items[-1][ident]}, "node": op["node"]}
            else:
                attrs = {key: [*(current.get(key) or []), op["node"]]}
                op = {"op": "set_attr", "anchor": {"path": "/"}, "attrs": attrs}
        validate_operations([op])
        _check_nodes(cls, view_type, op)
        current = apply_operations(current, [op])
        out.append(op)
    return out


def _check_nodes(cls: Any, view_type: str, op: dict[str, Any]) -> None:
    """Fields named by added nodes must exist; search filters need a name, a label and a valid domain."""
    nodes = [op["node"]] if isinstance(op.get("node"), dict) else []
    attrs = op.get("attrs") or {}
    for key in LIST_KEYS:
        if isinstance(attrs.get(key), list):
            nodes += [n for n in attrs[key] if isinstance(n, dict)]
    if view_type == "search" and op["op"] == "set_attr" and "domain" in attrs:
        nodes.append({"name": "domain", "label": "domain", "domain": attrs["domain"]})
    for node in nodes:
        fname = node.get("field") or (node.get("name") if node.get("kind") == "field" else None)
        if fname and str(fname).split(":")[0] not in cls._fields:
            raise ValidationError(f"'{cls._name}' has no field '{fname}'")
        if view_type == "search" and "field" not in node:
            if not node.get("name") or not node.get("label"):
                raise ValidationError("A filter needs a name and a label")
            try:
                Domain(node.get("domain") or [])
            except Exception as exc:
                raise ValidationError(f"Invalid filter condition: {exc}", field="domain") from exc


def change_view(env: Any, model_name: str, view_type: str, operations: list[dict[str, Any]], summary: str) -> Any:
    """Store a layout change as an extension view of the model's base view (view inheritance)."""
    from modules.web.views import resolve_view

    check_designer(env)
    cls = _model_cls(env, model_name)
    if view_type not in VIEW_TYPES:
        raise ValidationError(f"Unknown view type '{view_type}'")
    base = _base_view(env, model_name, view_type)
    before = resolve_view(_sys(env), model_name, view_type)
    operations = _studio_ops(cls, view_type, before, operations)
    if not operations:
        raise ValidationError("Nothing to change")
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
    after = resolve_view(_sys(env), model_name, view_type)
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


def create_view(
    env: Any,
    model_name: str,
    view_type: str,
    definition: dict[str, Any] | None = None,
    name: str | None = None,
    *,
    make_default: bool = False,
) -> Any:
    """A new base view; without ``definition`` it starts from the generated default. ``make_default``
    gives it a priority below the current base view, so users (and Studio's editors) get it."""
    from modules.web.views import default_view, validate_definition

    check_designer(env)
    cls = _model_cls(env, model_name)
    if view_type not in VIEW_TYPES:
        raise ValidationError(f"Unknown view type '{view_type}'")
    if definition is None:
        definition = default_view(_sys(env), model_name, view_type)
        for key in ("type", "model", "view_id", "generated"):
            definition.pop(key, None)
    validate_definition(view_type, definition)
    name = (name or "").strip() or f"{cls._description or model_name} {view_type} (Studio)"
    views = _sys(env)["web.view"]
    priority = 20
    if make_default:
        current = views.search(
            [("model_name", "=", model_name), ("type", "=", view_type), ("inherit_id", "=", False)],
            order="priority, id",
            limit=1,
        )
        priority = min(current.priority - 1, 15) if current else 16
    view = views.create(
        {
            "name": name,
            "model_name": model_name,
            "type": view_type,
            "definition": definition,
            "module": "studio",
            "studio": True,
            "priority": priority,
        }
    )
    return _record_change(
        env,
        "new_view",
        model_name,
        f"Created the {view_type} view “{name}”" + (" as the default" if make_default else ""),
        before=None,
        after={"definition": definition, "priority": priority, "make_default": make_default},
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


def _check_workflow(states: list[dict[str, Any]], transitions: list[dict[str, Any]]) -> None:
    if not states:
        raise ValidationError("A workflow needs at least one state", field="states")
    keys = [str(s.get("key") or "").strip() for s in states]
    if not all(keys) or len(set(keys)) != len(keys):
        raise ValidationError("Every state needs its own key", field="states")
    for t in transitions:
        if not str(t.get("name") or "").strip():
            raise ValidationError("Every transition needs a label", field="transitions")
        if str(t.get("to")) not in keys or not {str(k) for k in t.get("from", [])} <= set(keys):
            raise ValidationError(f"Transition “{t.get('name')}” uses an unknown state", field="transitions")
        if t.get("condition"):
            try:
                Domain(t["condition"])
            except Exception as exc:
                raise ValidationError(f"Invalid condition: {exc}", field="transitions") from exc


def create_workflow(
    env: Any,
    model_name: str,
    state_field: str,
    states: list[dict[str, Any]],
    transitions: list[dict[str, Any]],
    name: str,
    *,
    strict: bool = True,
    new_field: dict[str, Any] | None = None,
    replace: bool = False,
) -> Any:
    """A simple state machine on a selection/many2one field (no code).

    ``states``: ``[{"key", "name", "fold", "done"}]`` in order; ``transitions``: ``[{"name", "from": [keys]
    (empty = from any state), "to", "condition" (domain), "groups" (external ids), "highlight"}]``.
    ``new_field`` (``{"label", "name"}``) first adds a Studio choice field whose options are the states
    (default: the first one). ``replace`` archives the model's current workflow (restored on rollback).
    For a many2one state field each state also records the external id of its record (``ref``), so an
    export installs on a database where the ids differ."""
    from modules.workflow.models.workflow import workflow_for

    check_designer(env)
    cls = _model_cls(env, model_name)
    states = [{**s, "key": str(s.get("key") or "").strip()} for s in states]
    _check_workflow(states, transitions)
    groups: list[list[int]] = []
    for t in transitions:
        groups.append([])
        for ref in t.get("groups") or []:
            group = env.ref(ref, raise_if_not_found=False)
            if group is None or group._name != "core.group":
                raise ValidationError(f"Unknown group '{ref}'", field="transitions")
            groups[-1].append(group.ids[0])
    existing = workflow_for(env, model_name)
    if existing and not replace:
        raise ValidationError(f"'{model_name}' already has the workflow “{existing.name}”", field="model")
    if new_field is None and state_field not in cls._fields:
        raise ValidationError(f"'{model_name}' has no field '{state_field}'", field="state_field")
    sys_env = _sys(env)
    if new_field is not None:
        label = str(new_field.get("label") or name).strip()
        out = add_field(
            env,
            model_name,
            str(new_field.get("name") or label),
            "selection",
            label,
            placements=new_field.get("placements", [{"view_type": "list"}]),
            selection=[[s["key"], s.get("name") or s["key"]] for s in states],
            default=states[0]["key"],
        )
        state_field = out["field"]
        cls = env.registry.models[model_name]
    field = cls._fields[state_field]
    keymap = {s["key"]: s["key"] for s in states}
    if isinstance(field, F.Many2one):
        comodel = sys_env[field.comodel_name]
        for s in states:
            if s.get("ref"):  # from an export: the record's external id gives its id here
                rec = env.ref(s["ref"], raise_if_not_found=False)
                if rec is not None:
                    keymap[s["key"]] = str(rec.ids[0])
            elif s["key"].isdigit():
                xmlid = comodel.browse([int(s["key"])]).get_external_id().get(int(s["key"]))
                if xmlid:
                    s["ref"] = xmlid
    if existing:
        existing.write({"active": False})
    wf = sys_env["workflow.definition"].create(
        {"name": name, "model_name": model_name, "state_field": state_field, "strict": strict}
    )
    by_key = {}
    for i, s in enumerate(states):
        by_key[s["key"]] = sys_env["workflow.state"].create(
            {
                "definition_id": wf.ids[0],
                "key": keymap[s["key"]],
                "name": s.get("name") or s["key"],
                "sequence": (i + 1) * 10,
                "fold": bool(s.get("fold")),
                "is_final": bool(s.get("done")),
            }
        )
    for i, (t, group_ids) in enumerate(zip(transitions, groups, strict=True)):
        sys_env["workflow.transition"].create(
            {
                "definition_id": wf.ids[0],
                "name": t["name"],
                "from_state_ids": [by_key[str(k)].ids[0] for k in t.get("from", [])],
                "to_state_id": by_key[str(t["to"])].ids[0],
                "guard_domain": t.get("condition") or None,
                "group_ids": group_ids,
                "highlight": bool(t.get("highlight")),
                "sequence": (i + 1) * 10,
            }
        )
    return _record_change(
        env,
        "workflow",
        model_name,
        f"Workflow “{name}” on {cls._description or model_name}",
        before={"replaced_id": existing.ids[0]} if existing else None,
        after={
            "state_field": state_field,
            "states": states,
            "transitions": transitions,
            "name": name,
            "strict": strict,
        },
        res_model="workflow.definition",
        res_id=wf.ids[0],
    )


def workflow_info(env: Any, model_name: str) -> dict[str, Any] | None:
    """The model's active workflow as the builder shows it (states in order, transitions by state key)."""
    from modules.workflow.models.workflow import workflow_for

    check_designer(env)
    _model_cls(env, model_name)
    wf = workflow_for(env, model_name)
    if not wf:
        return None
    studio = _sys(env)["studio.change"].search(
        [("kind", "=", "workflow"), ("res_id", "=", wf.ids[0]), ("state", "=", "applied")], limit=1
    )
    return {
        "id": wf.ids[0],
        "name": wf.name,
        "state_field": wf.state_field,
        "strict": wf.strict,
        "studio_change_id": studio.ids[0] if studio else None,
        "states": [
            {"key": s.key, "name": s.name, "fold": s.fold, "done": s.is_final, "sequence": s.sequence}
            for s in wf.state_ids
        ],
        "transitions": [
            {
                "id": t.ids[0],
                "name": t.name,
                "from": t.from_state_ids.mapped("key"),
                "to": t.to_state_id.key,
                "condition": t.guard_domain,
                "groups": [x for x in t.group_ids.get_external_id().values() if x],
                "highlight": t.highlight,
            }
            for t in wf.transition_ids
        ],
    }


# ----------------------------------------------------------------------------- rollback


def _field_used_by(change: Any, field_name: str) -> bool:
    return f'"{field_name}"' in json.dumps((change.after or {}).get("operations") or [])


def _rollback_field(env: Any, change: Any, target: Any) -> None:
    sys_env = _sys(env)
    name, model_name = target.name, target.model_name
    dependants = sys_env["studio.change"].search(
        [("model_name", "=", model_name), ("state", "=", "applied"), ("id", ">", change.ids[0])], order="id desc"
    )
    for dep in dependants:
        uses_field = (dep.kind == "view_change" and _field_used_by(dep, name)) or (
            dep.kind == "workflow" and (dep.after or {}).get("state_field") == name
        )
        if uses_field:
            rollback(env, dep)
        elif dep.kind == "edit_field" and dep.res_id == target.ids[0]:  # moot once the field is gone
            dep.write({"state": "rolled_back", "rolled_back_at": env.now(), "rolled_back_by": env.uid})
    fld = env.registry.models[model_name]._fields.get(name)
    target.write({"active": False})
    _rebuild(env, [], meta_models=[model_name])
    stamp = time.strftime("%Y%m%d%H%M%S")
    table = env.registry.models[model_name]._table
    if isinstance(fld, F.Many2many):
        env.cr.execute(f'ALTER TABLE IF EXISTS "{fld.relation}" RENAME TO "_removed_{stamp}_{fld.relation}"'[:200])
    else:
        env.cr.execute(f'ALTER TABLE "{table}" RENAME COLUMN "{name}" TO "_removed_{stamp}_{name}"')
    sys_env["core.model_field"].with_context(active_test=False).search(
        [("model_name", "=", model_name), ("name", "=", name)]
    ).unlink()


def _unlink_view(env: Any, view: Any) -> None:
    """Remove a Studio view, refusing when a later change relies on it (the view would no longer resolve,
    e.g. a field moved after a column this change added)."""
    from modules.web.views import resolve_view

    view.write({"active": False})
    try:
        resolve_view(_sys(env), view.model_name, view.type)
    except ValidationError as exc:
        view.write({"active": True})
        raise ValidationError(f"A later change relies on this one: roll it back first ({exc.message})") from exc
    view.unlink()


def _rollback_edit(env: Any, change: Any, target: Any) -> None:
    sys_env = _sys(env)
    later = sys_env["studio.change"].search(
        [
            ("kind", "=", "edit_field"),
            ("res_id", "=", change.res_id),
            ("state", "=", "applied"),
            ("id", ">", change.ids[0]),
        ],
        order="id desc",
    )
    for dep in later:  # newer edits of the same field build on this one
        rollback(env, dep)
    for dep in sys_env["studio.change"].browse((change.after or {}).get("view_change_ids") or []).exists():
        rollback(env, dep)
    previous = dict((change.before or {}).get("field") or {})
    if target and target.active and previous:
        target.with_context(studio_internal=True).write(_stored(previous))
        if set(previous) - {"widget"}:
            _rebuild(env, [target.model_name])
            _refresh_field_meta(env, target.model_name, target.name)


def rollback(env: Any, change: Any) -> bool:
    """Undo one change. Returns False when it was already rolled back (no-op)."""
    check_designer(env)
    sys_env = _sys(env)
    change = sys_env["studio.change"].browse(change.ids)
    if change.state == "rolled_back":
        return False
    target = (
        sys_env[change.res_model].with_context(active_test=False).browse([change.res_id]).exists()
        if change.res_model in env.registry.models
        else None
    )
    if change.kind == "add_field" and target and target.active:
        _rollback_field(env, change, target)
    elif change.kind == "edit_field":
        _rollback_edit(env, change, target)
    elif change.kind == "new_view" and target:
        extensions = sys_env["web.view"].search([("inherit_id", "=", target.ids[0])])
        for dep in sys_env["studio.change"].search(
            [("res_model", "=", "web.view"), ("res_id", "in", extensions.ids), ("state", "=", "applied")],
            order="id desc",
        ):
            rollback(env, dep)
        _unlink_view(env, target)
    elif change.kind == "view_change" and target:
        _unlink_view(env, target)
    elif change.kind == "workflow" and target:
        target.unlink()
        replaced = (change.before or {}).get("replaced_id")
        if replaced:
            previous = sys_env["workflow.definition"].with_context(active_test=False).browse([replaced]).exists()
            if previous:
                previous.write({"active": True})
    elif change.kind == "automation" and target:
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

FIELD_EXPORT = ("model_name", "name", "ttype", "label", *FIELD_OPTIONS)

HOOKS_PY = '''"""Re-creates the Studio workflows of the export."""

from modules.studio.service import create_workflow

WORKFLOWS = {workflows}


def post_init(env):  # type: ignore[no-untyped-def]
    for wf in WORKFLOWS:
        create_workflow(
            env,
            wf["model_name"],
            wf["state_field"],
            wf["states"],
            wf["transitions"],
            wf["name"],
            strict=wf.get("strict", True),
            replace=True,
        )
'''


def export_module(env: Any, company_slug: str = "default") -> tuple[str, bytes]:
    """Every applied Studio change as an installable module ``studio_customizations_<company>``:
    fields (with their current options), view extensions, views, automations and workflows as data
    files (1.7-d)."""
    check_designer(env)
    sys_env = _sys(env)
    module = f"studio_customizations_{re.sub(r'[^a-z0-9_]', '_', company_slug.lower())}"
    changes = sys_env["studio.change"].search([("state", "=", "applied")], order="id")
    models = sorted({c.model_name for c in changes if c.model_name})
    depends = sorted({"studio", *(env.registry.models[m]._module for m in models if m in env.registry.models)})
    fields_data: list[dict[str, Any]] = []
    views_data: list[dict[str, Any]] = []
    autos: list[dict[str, Any]] = []
    workflows: list[dict[str, Any]] = []
    view_xids: dict[int, str] = {}  # views created by this export → their external id in the module

    def base_ref(base: Any) -> str:
        if base.ids[0] in view_xids:
            return view_xids[base.ids[0]]
        found = next(iter(base.get_external_id().values()), None)
        if found:
            return str(found)
        base_id = f"base_{base.ids[0]}"  # a Studio-materialised base view: export it first
        values = {
            "name": base.name,
            "model_name": base.model_name,
            "type": base.type,
            "definition": base.definition,
            "priority": base.priority,
            "module": module,
            "studio": True,
        }
        views_data.append({"id": base_id, "model": "web.view", "values": values})
        view_xids[base.ids[0]] = f"{module}.{base_id}"
        return view_xids[base.ids[0]]

    for c in changes:
        target = (
            sys_env[c.res_model].with_context(active_test=False).browse([c.res_id]).exists() if c.res_model else None
        )
        if not target:
            continue
        xid = f"{c.kind}_{c.ids[0]}"
        if c.kind == "add_field":
            vals = {k: target[k] for k in FIELD_EXPORT}
            fields_data.append(
                {
                    "id": xid,
                    "model": "studio.field",
                    "values": {k: v for k, v in vals.items() if v not in (None, False, "")},  # default stays wrapped
                }
            )
        elif c.kind in ("view_change", "new_view"):
            vals = {
                "name": target.name,
                "model_name": target.model_name,
                "type": target.type,
                "module": module,
                "studio": True,
            }
            if target.inherit_id:
                vals.update(
                    operations=target.operations, sequence=target.sequence, inherit_id=base_ref(target.inherit_id)
                )
            else:
                vals.update(definition=target.definition, priority=target.priority)
                view_xids[target.ids[0]] = f"{module}.{xid}"
            views_data.append({"id": xid, "model": "web.view", "values": vals})
        elif c.kind == "automation":
            autos.append({"id": xid, "model": "workflow.automation", "values": (c.after or {}).get("automation", {})})
        elif c.kind == "workflow" and target.active:
            after = dict(c.after or {})
            workflows.append(
                {key: after.get(key) for key in ("state_field", "states", "transitions", "name", "strict")}
                | {"model_name": c.model_name}
            )
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
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{module}/manifest.toml", "\n".join(lines))
        z.writestr(f"{module}/__init__.py", f'"""{module}: exported Studio customisations."""\n')
        z.writestr(f"{module}/data/fields.json", json.dumps(fields_data, indent=2))
        z.writestr(f"{module}/data/views.json", json.dumps(views_data, indent=2))
        z.writestr(f"{module}/data/automations.json", json.dumps(autos, indent=2))
        if workflows:
            z.writestr(f"{module}/hooks.py", HOOKS_PY.format(workflows=repr(workflows)))
    return module, buf.getvalue()
