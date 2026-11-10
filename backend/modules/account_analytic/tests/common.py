"""Helpers for analytic tests."""

from __future__ import annotations

from typing import Any

from tessera.orm import Model


def plan(env: Any, name: str = "Departments", **vals: Any) -> Model:
    return env["account.analytic_plan"].create({"name": name, **vals})


def analytic(env: Any, plan_rec: Model, code: str, name: str | None = None, **vals: Any) -> Model:
    return env["account.analytic_account"].create(
        {"code": code, "name": name or code, "plan_id": plan_rec.ids[0], **vals}
    )


def dist(*pairs: tuple[Model, float]) -> dict[str, float]:
    return {str(acc.ids[0]): float(pct) for acc, pct in pairs}


def account(env: Any, code: str, company: Model | None = None) -> Model:
    company = company or env.company
    return env["account.account"].search([("code", "=", code), ("company_id", "=", company.ids[0])], limit=1)


def _field_refs(env: Any, node: Any, model: str, out: list[tuple[str, str]]) -> None:
    """Collect (model, field) pairs a view definition names."""
    if isinstance(node, dict):
        if node.get("kind") == "field" and node.get("name"):
            out.append((model, node["name"]))
            field = env[model]._fields.get(node["name"])
            for col in node.get("columns", []):
                if field is not None and field.comodel_name:
                    out.append((field.comodel_name, col["field"]))
            return
        if isinstance(node.get("field"), str):
            out.append((model, node["field"].split(":")[0]))
        for value in node.values():
            _field_refs(env, value, model, out)
    elif isinstance(node, list):
        for item in node:
            _field_refs(env, item, model, out)


def check_ui(env: Any, module: str) -> int:
    """Resolve every view of ``module`` (extensions through their base view) and check that every field it
    names exists, and that action views and menu actions resolve. Returns the number of views checked."""
    import json
    from pathlib import Path

    from modules.web.views import resolve_view

    root = Path(__file__).resolve().parents[2] / module
    views = json.loads((root / "views.json").read_text())
    for view in views:
        ref = view.get("inherit") or f"{module}.{view['id']}"
        definition = resolve_view(env, view["model"], view["type"], ref)
        found: list[tuple[str, str]] = []
        _field_refs(env, definition.get("root") or definition.get("columns") or [], view["model"], found)
        for key in ("row", "col"):
            if definition.get(key):
                found.append((view["model"], definition[key].split(":")[0]))
        found += [(view["model"], m) for m in definition.get("measures", [])]
        if view["type"] == "search":
            found += [(view["model"], f) for f in definition.get("fields", [])]
            found += [(view["model"], g["field"].split(":")[0]) for g in definition.get("group_bys", [])]
        for model, field in found:
            assert field in env[model]._fields, f"{module}.{view['id']}: {model}.{field} does not exist"
    for action in json.loads((root / "actions.json").read_text()):
        assert action["model"] in env.registry.models
        for ref in (action.get("view_refs") or {}).values():
            assert env.ref(ref, raise_if_not_found=False) is not None, ref
    for menu in json.loads((root / "menus.json").read_text()):
        assert env.ref(f"{module}.{menu['id']}", raise_if_not_found=False) is not None, menu["id"]
        if menu.get("action"):
            assert env.ref(menu["action"], raise_if_not_found=False) is not None, menu["action"]
    return len(views)
