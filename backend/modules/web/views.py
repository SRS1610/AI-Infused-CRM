"""View resolution: base view selection, inheritance operations, default views, field filtering.

Inheritance operations (applied in ``sequence, id`` order of the extension views)::

    {"op": "insert_after",  "anchor": {"field": "partner_id"}, "node": {...}}
    {"op": "insert_before", "anchor": {"name": "page_notes"}, "node": {...}}
    {"op": "append",        "anchor": {"name": "main_group"}, "node": {...}}   # as last child
    {"op": "prepend",       "anchor": {"path": "/root"}, "node": {...}}
    {"op": "replace",       "anchor": {"field": "name"}, "node": {...}}
    {"op": "remove",        "anchor": {"field": "secret_margin"}}
    {"op": "set_attr",      "anchor": {"field": "name"}, "attrs": {"readonly": true}}

Anchors match the first node (depth-first) whose ``field`` / ``name`` equals the value, or the
node at a JSON-pointer-like ``path``.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, cast

from tessera.exceptions import ValidationError
from tessera.orm import fields as F

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

OPS = {"insert_after", "insert_before", "append", "prepend", "replace", "remove", "set_attr"}
CHILD_KEYS = ("children", "columns", "pages", "fields", "filters", "group_bys", "measures", "buttons", "card")


# -- validation ---------------------------------------------------------------------------------


def validate_definition(view_type: str, definition: Any) -> None:
    if not isinstance(definition, dict):
        raise ValidationError("View definition must be an object")
    if view_type in ("form", "kanban") and "root" not in definition:
        raise ValidationError(f"A {view_type} view needs a 'root' node")
    if view_type == "list" and not isinstance(definition.get("columns"), list):
        raise ValidationError("A list view needs a 'columns' list")
    if view_type == "search":
        for key in ("fields", "filters", "group_bys"):
            if key in definition and not isinstance(definition[key], list):
                raise ValidationError(f"search view '{key}' must be a list")
    if view_type == "calendar" and not definition.get("date_start"):
        raise ValidationError("A calendar view needs 'date_start'")
    if view_type in ("pivot", "graph") and not isinstance(definition.get("measures", []), list):
        raise ValidationError(f"A {view_type} view needs a 'measures' list")
    _validate_nodes(definition)


NODE_KINDS = frozenset(
    {
        "sheet",
        "header",
        "footer",
        "group",
        "row",
        "column",
        "field",
        "label",
        "separator",
        "notebook",
        "page",
        "buttons",
        "button",
        "status_bar",
        "smart_buttons",
        "card",
        "badge",
        "avatar",
        "image",
        "alert",
        "html",
        "spacer",
        "chatter",
        "component",  # "component": client extension registered under node["component"]
    }
)


def _validate_nodes(node: Any, pointer: str = "") -> None:
    """Validate node kinds recursively; errors name the JSON pointer of the offending node."""
    if isinstance(node, dict):
        if "kind" in node:
            kind = node["kind"]
            if not isinstance(kind, str):
                raise ValidationError(f"node 'kind' must be a string at {pointer or '/'}", pointer=pointer or "/")
            if kind not in NODE_KINDS:
                raise ValidationError(f"Unknown view node kind '{kind}' at {pointer or '/'}", pointer=pointer or "/")
            if kind == "component" and not node.get("component"):
                raise ValidationError(f"component node needs 'component' at {pointer or '/'}", pointer=pointer or "/")
        for key, value in node.items():
            _validate_nodes(value, f"{pointer}/{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _validate_nodes(item, f"{pointer}/{index}")


def validate_operations(operations: Any) -> None:
    if not isinstance(operations, list):
        raise ValidationError("operations must be a list")
    for op in operations:
        if not isinstance(op, dict) or op.get("op") not in OPS:
            raise ValidationError(f"Invalid view operation {op!r}")
        anchor = op.get("anchor")
        if not isinstance(anchor, dict) or not ({"field", "name", "path"} & set(anchor)):
            raise ValidationError(f"Operation {op.get('op')} needs an anchor with field/name/path")
        if op["op"] in ("insert_after", "insert_before", "append", "prepend", "replace") and not isinstance(
            op.get("node"), dict
        ):
            raise ValidationError(f"Operation {op['op']} needs a node")
        if op["op"] == "set_attr" and not isinstance(op.get("attrs"), dict):
            raise ValidationError("set_attr needs attrs")
        if "node" in op:
            _validate_nodes(op["node"], f"/operations/{operations.index(op)}/node")


# -- anchors ---------------------------------------------------------------------------------------


def _matches(node: Any, anchor: dict[str, Any]) -> bool:
    if not isinstance(node, dict):
        return False
    if "field" in anchor:
        return node.get("field") == anchor["field"] or (
            node.get("kind") == "field" and node.get("name") == anchor["field"]
        )
    if "name" in anchor:
        return node.get("name") == anchor["name"] and node.get("kind") != "field"
    return False


def _find(container: Any, anchor: dict[str, Any]) -> tuple[list[Any] | None, int | None, Any]:
    """Return (parent_list, index, node) for the first match; parent_list is None for root-level dict."""
    if "path" in anchor:
        parts = [p for p in str(anchor["path"]).split("/") if p]
        parent: Any = None
        index: Any = None
        current: Any = container
        for part in parts:
            parent = current
            if isinstance(current, list):
                index = int(part)
                current = current[index]
            elif isinstance(current, dict):
                index = part
                current = current[part]
            else:
                raise ValidationError(f"Invalid path '{anchor['path']}'")
        if isinstance(parent, list):
            return parent, int(index), current
        return None, None, current
    stack: list[tuple[list[Any] | None, int | None, Any]] = [(None, None, container)]
    while stack:
        parent_list, idx, node = stack.pop(0)
        if _matches(node, anchor):
            return parent_list, idx, node
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, list):
                    for i, child in enumerate(value):
                        stack.append((value, i, child))
                elif isinstance(value, dict) and key in ("root", "card", "template"):
                    stack.append((None, None, value))
        elif isinstance(node, list):
            for i, child in enumerate(node):
                stack.append((node, i, child))
    raise ValidationError(f"View anchor {anchor} not found")


def _children_list(node: dict[str, Any]) -> list[Any]:
    for key in CHILD_KEYS:
        if isinstance(node.get(key), list):
            return cast("list[Any]", node[key])
    node["children"] = []
    return cast("list[Any]", node["children"])


def apply_operations(definition: dict[str, Any], operations: list[dict[str, Any]]) -> dict[str, Any]:
    result = copy.deepcopy(definition)
    for op in operations:
        kind = op["op"]
        parent, idx, node = _find(result, op["anchor"])
        if kind in ("insert_after", "insert_before", "remove", "replace"):
            if parent is None or idx is None:
                raise ValidationError(f"Operation {kind} needs an anchor inside a list")
            if kind == "insert_after":
                parent.insert(idx + 1, copy.deepcopy(op["node"]))
            elif kind == "insert_before":
                parent.insert(idx, copy.deepcopy(op["node"]))
            elif kind == "remove":
                del parent[idx]
            else:
                parent[idx] = copy.deepcopy(op["node"])
        elif kind == "append":
            _children_list(node).append(copy.deepcopy(op["node"]))
        elif kind == "prepend":
            _children_list(node).insert(0, copy.deepcopy(op["node"]))
        elif kind == "set_attr":
            node.update(copy.deepcopy(op["attrs"]))
    return result


# -- resolution --------------------------------------------------------------------------------------


def resolve_view(env: Environment, model_name: str, view_type: str, view_ref: str | None = None) -> dict[str, Any]:
    views = env["web.view"].elevated()
    base: Model | None = None
    if view_ref:
        ref = env.ref(view_ref, raise_if_not_found=False)
        if ref is not None and ref._name == "web.view":
            base = views.browse(ref.ids)
    if base is None or not base:
        base = views.search(
            [("model_name", "=", model_name), ("type", "=", view_type), ("inherit_id", "is_not_set")], limit=1
        )
    if not base:
        return default_view(env, model_name, view_type)
    definition = copy.deepcopy(base.definition or {})
    definition = _apply_extensions(views, base, definition)
    definition.setdefault("type", view_type)
    definition.setdefault("model", model_name)
    definition["view_id"] = base.ids[0]
    return definition


def _apply_extensions(views: Model, base: Model, definition: dict[str, Any]) -> dict[str, Any]:
    extensions = views.search([("inherit_id", "=", base.ids[0])], order="sequence, id")
    for ext in extensions:
        definition = apply_operations(definition, list(ext.operations or []))
        definition = _apply_extensions(views, ext, definition)
    return definition


def strip_inaccessible_fields(env: Environment, model_name: str, definition: Any) -> Any:
    """Remove nodes that reference fields the user may not read."""
    model = env[model_name]
    allowed = {f.name for f in model._fields.values() if model._check_field_access(f, "read")}

    def visit(node: Any) -> Any:
        if isinstance(node, list):
            out = []
            for item in node:
                if isinstance(item, dict):
                    fname = (
                        item.get("field")
                        if "field" in item
                        else (item.get("name") if item.get("kind") == "field" else None)
                    )
                    if fname and fname in model._fields and fname not in allowed:
                        continue
                out.append(visit(item))
            return out
        if isinstance(node, dict):
            return {k: visit(v) for k, v in node.items()}
        return node

    return visit(definition)


# -- defaults -----------------------------------------------------------------------------------------

SYSTEM_FIELDS = {
    "id",
    "create_date",
    "write_date",
    "create_uid",
    "write_uid",
    "display_name",
    "deleted_at",
    "deleted_by",
}


def _visible_fields(model: Model) -> list[F.Field]:
    out = []
    for f in model._fields.values():
        if f.name in SYSTEM_FIELDS or isinstance(f, F.Binary) or not model._check_field_access(f, "read"):
            continue
        out.append(f)
    return out


def default_view(env: Environment, model_name: str, view_type: str) -> dict[str, Any]:
    model = env[model_name]
    fields_ = _visible_fields(model)
    rec = model._rec_name if model._rec_name in model._fields else None
    simple = [f for f in fields_ if not isinstance(f, F.One2many | F.Many2many | F.Html | F.Text)]
    if view_type == "form":
        head = [{"kind": "field", "name": rec, "widget": "title"}] if rec else []
        body = [{"kind": "field", "name": f.name} for f in simple if f.name != rec]
        texts = [{"kind": "field", "name": f.name} for f in fields_ if isinstance(f, F.Html | F.Text)]
        x2m = [{"kind": "field", "name": f.name} for f in fields_ if isinstance(f, F.One2many | F.Many2many)]
        return {
            "type": "form",
            "model": model_name,
            "generated": True,
            "root": {
                "kind": "sheet",
                "children": [*head, {"kind": "group", "columns": 2, "children": body}, *texts, *x2m],
            },
            "chatter": "chatter.thread" in model._mixin_names,
        }
    if view_type == "list":
        cols = ([rec] if rec else []) + [f.name for f in simple if f.name != rec][:7]
        return {"type": "list", "model": model_name, "generated": True, "columns": [{"field": c} for c in cols]}
    if view_type == "kanban":
        return {
            "type": "kanban",
            "model": model_name,
            "generated": True,
            "root": {"kind": "card", "children": [{"kind": "field", "name": "display_name", "widget": "title"}]},
        }
    if view_type == "search":
        filters = (
            [{"name": "archived", "label": "Archived", "domain": [["active", "=", False]]}]
            if "active" in model._fields
            else []
        )
        groupable = [f.name for f in simple if isinstance(f, F.Many2one | F.Selection)][:5]
        return {
            "type": "search",
            "model": model_name,
            "generated": True,
            "fields": [rec] if rec else ["display_name"],
            "filters": filters,
            "group_bys": [{"field": g} for g in groupable],
        }
    if view_type == "calendar":
        date_field = next((f.name for f in simple if isinstance(f, F.Datetime | F.Date)), None)
        return {
            "type": "calendar",
            "model": model_name,
            "generated": True,
            "date_start": date_field,
            "title": rec or "display_name",
        }
    if view_type in ("pivot", "graph"):
        measures = [f.name for f in simple if isinstance(f, F.Integer | F.Float | F.Monetary)][:3]
        row = next((f.name for f in simple if isinstance(f, F.Many2one | F.Selection)), None)
        return {
            "type": view_type,
            "model": model_name,
            "generated": True,
            "measures": measures,
            "row_groupby": [row] if row else [],
            "col_groupby": [],
        }
    if view_type == "activity":
        return {"type": "activity", "model": model_name, "generated": True}
    if view_type in ("gantt", "map"):
        return {"type": view_type, "model": model_name, "generated": True, "unsupported_until": "Phase 4"}
    raise ValidationError(f"Unknown view type '{view_type}'")
