"""Studio v1, remaining scope of 1.7: field options and Edit field, list/kanban/search editors, new views
and the workflow builder; each one rolls back, exports and installs on a fresh database."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from pathlib import Path
from typing import Any

import pytest

from modules.studio import service as S
from tessera.exceptions import AccessError, ConflictError, ValidationError

pytestmark = pytest.mark.db


def _view(env: Any, view_type: str, model: str = "crm.lead") -> dict[str, Any]:
    from modules.web.views import resolve_view

    return resolve_view(env.elevated(), model, view_type)


def _columns(env: Any) -> list[str]:
    return [c["field"] for c in _view(env, "list")["columns"]]


def _card(env: Any) -> list[str]:
    return [n["name"] for n in _view(env, "kanban")["root"]["children"] if n.get("kind") == "field"]


def _change(env: Any, change_id: int) -> Any:
    return env["studio.change"].elevated().browse([change_id])


def _won(env: Any) -> int:
    return int(env.ref("crm.stage_won").ids[0])


# ----------------------------------------------------------------------------- field options


def test_field_options_widget_default_conditions_groups(env: Any) -> None:
    won = _won(env)
    out = S.add_field(
        env,
        "crm.lead",
        "x_win_reason",
        "selection",
        "Win reason",
        placements=[{"view_type": "form", "after": "partner_id"}, {"view_type": "list"}, {"view_type": "kanban"}],
        selection=[["price", "Price"], ["fit", "Fit"]],
        widget="badge",
        default="fit",
        required_if=[["stage_id", "=", won]],
        readonly_if=[["is_won", "=", True]],
        groups=["crm.group_crm_user"],
    )
    assert len(out["view_change_ids"]) == 3
    field = env.registry.models["crm.lead"]._fields["x_win_reason"]
    assert field.groups == ("crm.group_crm_user",) and field.required_if == [["stage_id", "=", won]]
    meta = env["crm.lead"].fields_get(["x_win_reason"])["x_win_reason"]
    assert meta["required_if"] == [["stage_id", "=", won]] and meta["readonly_if"] == [["is_won", "=", True]]
    form = json.dumps(_view(env, "form"))
    assert '"name": "x_win_reason", "widget": "badge"' in form
    assert {"field": "x_win_reason", "widget": "badge"} in _view(env, "list")["columns"]
    assert _card(env)[-1] == "x_win_reason"
    lead = env["crm.lead"].create({"name": "Defaulted"})
    assert lead.x_win_reason == "fit"
    lead.write({"x_win_reason": False})
    with pytest.raises(ValidationError, match="Win reason"):
        lead.write({"stage_id": won})
    lead.write({"stage_id": won, "x_win_reason": "price"})  # set together: fine
    with pytest.raises(ValidationError, match="Win reason"):
        env["crm.lead"].create({"name": "Won without reason", "stage_id": won, "x_win_reason": False})


@pytest.mark.parametrize(
    "options",
    [
        {"required_if": [["no_such_field", "=", 1]]},
        {"readonly_if": {"bogus": 1}},
        {"groups": ["nope.group_missing"]},
        {"groups": "crm.group_crm_user"},
        {"widget": "Bad Widget!"},
        {"default": 12},
    ],
)
def test_field_options_are_checked(env: Any, options: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        S.add_field(env, "crm.lead", "x_checked", "char", "Checked", **options)


@pytest.mark.parametrize(
    ("ttype", "default", "ok"),
    [
        ("integer", 3, True),
        ("integer", "3", False),
        ("float", 2.5, True),
        ("boolean", True, True),
        ("boolean", "yes", False),
        ("date", "2026-11-10", True),
        ("date", "10/11/2026", False),
        ("datetime", "2026-11-10T09:30", True),
        ("many2one", 0, False),
        ("many2many", [1], False),
        ("char", "", True),
    ],
)
def test_defaults_by_type(ttype: str, default: Any, ok: bool) -> None:
    if ok:
        assert S._check_default(ttype, default, None) in (default, None)
    else:
        with pytest.raises(ValidationError):
            S._check_default(ttype, default, None)


def test_edit_field_and_rollback(env: Any) -> None:
    won = _won(env)
    S.add_field(
        env,
        "crm.lead",
        "x_channel",
        "selection",
        "Channel",
        placements=[{"view_type": "form"}, {"view_type": "list"}],
        selection=[["web", "Web"], ["fair", "Fair"]],
    )
    edit = S.edit_field(
        env,
        "crm.lead",
        "x_channel",
        label="Sales channel",
        default="web",
        required_if=[["stage_id", "=", won]],
        widget="badge",
        selection=[["web", "Website"], ["fair", "Fair"], ["call", "Cold call"]],
        groups=["crm.group_crm_user"],
    )
    assert edit.kind == "edit_field" and len(edit.after["view_change_ids"]) == 2
    assert edit.before["field"]["label"] == "Channel" and edit.before["field"]["default"] is None
    field = env.registry.models["crm.lead"]._fields["x_channel"]
    assert field.label == "Sales channel" and field.required_if and ("call", "Cold call") in field.selection
    meta = env["core.model_field"].elevated().search([("model_name", "=", "crm.lead"), ("name", "=", "x_channel")])
    assert meta.string == "Sales channel" and meta.studio
    assert {"field": "x_channel", "widget": "badge"} in _view(env, "list")["columns"]
    assert env["crm.lead"].create({"name": "Edited"}).x_channel == "web"
    later = S.edit_field(env, "crm.lead", "x_channel", help="Where the lead came from")

    assert S.rollback(env, edit) is True  # also undoes the later edit and the widget view changes
    assert _change(env, later.ids[0]).state == "rolled_back"
    assert all(c.state == "rolled_back" for c in env["studio.change"].browse(edit.after["view_change_ids"]))
    field = env.registry.models["crm.lead"]._fields["x_channel"]
    assert field.label == "Channel" and not field.required_if and not field.help
    assert {"field": "x_channel"} in _view(env, "list")["columns"]
    assert S.rollback(env, edit) is False


def test_edit_field_refusals(env: Any) -> None:
    S.add_field(env, "crm.lead", "x_level", "selection", "Level", selection=[["a", "A"], ["b", "B"]])
    for bad in (
        lambda: S.edit_field(env, "crm.lead", "name", label="Not Studio"),
        lambda: S.edit_field(env, "crm.lead", "x_level", ttype="char"),
        lambda: S.edit_field(env, "crm.lead", "x_level", label="  "),
        lambda: S.edit_field(env, "crm.lead", "x_level", selection=[["a", "A"]]),
        lambda: S.edit_field(env, "crm.lead", "x_level", default="zzz"),
        lambda: S.edit_field(env, "crm.lead", "x_level", label="Level"),  # nothing changes
    ):
        with pytest.raises(ValidationError):
            bad()


def test_rolling_back_a_field_takes_its_edits_and_workflow(env: Any) -> None:
    added = S.add_field(env, "crm.lead", "x_tier", "selection", "Tier", selection=[["s", "Small"], ["l", "Large"]])
    edit = S.edit_field(env, "crm.lead", "x_tier", help="Account size")
    wf = S.create_workflow(
        env, "crm.lead", "x_tier", [{"key": "s"}, {"key": "l"}], [{"name": "Grow", "to": "l"}], "Tiers"
    )
    assert S.rollback(env, _change(env, added["change_id"])) is True
    assert _change(env, edit.ids[0]).state == "rolled_back" and _change(env, wf.ids[0]).state == "rolled_back"
    assert not env["workflow.definition"].search([("name", "=", "Tiers")])
    assert S.rollback(env, edit) is False


# ----------------------------------------------------------------------------- list and kanban editors


def test_list_editor_operations(env: Any) -> None:
    before = _columns(env)
    assert before[:3] == ["name", "partner_id", "email_from"]
    ops = [
        {"op": "remove", "anchor": {"field": "email_from"}},  # hide
        {"op": "remove", "anchor": {"field": "stage_id"}},  # move stage first
        {"op": "insert_before", "anchor": {"field": "name"}, "node": {"field": "stage_id"}},
        {"op": "set_attr", "anchor": {"field": "team_id"}, "attrs": {"optional": "show"}},
        {"op": "set_attr", "anchor": {"field": "partner_id"}, "attrs": {"optional": "hide"}},
        {"op": "append_to", "list": "columns", "node": {"field": "phone", "optional": "hide"}},
    ]
    change = S.change_view(env, "crm.lead", "list", ops, "List columns changed in Studio")
    cols = _view(env, "list")["columns"]
    assert [c["field"] for c in cols][:3] == ["stage_id", "name", "partner_id"]
    assert "email_from" not in [c["field"] for c in cols]
    assert cols[-1] == {"field": "phone", "optional": "hide"}
    by_field = {c["field"]: c for c in cols}
    assert by_field["team_id"]["optional"] == "show" and by_field["partner_id"]["optional"] == "hide"
    assert change.before["view"]["columns"][0]["field"] == "name"
    with pytest.raises(ValidationError):  # columns must name fields of the model
        S.change_view(env, "crm.lead", "list", [{"op": "append_to", "list": "columns", "node": {"field": "nope"}}], "x")
    with pytest.raises(ValidationError):
        S.change_view(env, "crm.lead", "list", [{"op": "append_to", "list": "rows", "node": {}}], "x")
    with pytest.raises(ValidationError):
        S.change_view(env, "crm.lead", "list", [], "Nothing")
    assert S.rollback(env, change) is True
    assert _columns(env) == before


def test_kanban_editor_operations(env: Any) -> None:
    before = _card(env)
    assert before[0] == "name"
    ops = [
        {"op": "remove", "anchor": {"field": "user_id"}},
        {"op": "remove", "anchor": {"field": "priority"}},
        {"op": "insert_after", "anchor": {"field": "name"}, "node": {"kind": "field", "name": "priority"}},
        {"op": "append", "anchor": {"path": "/root"}, "node": {"kind": "field", "name": "date_deadline"}},
    ]
    change = S.change_view(env, "crm.lead", "kanban", ops, "Kanban card changed in Studio")
    card = _card(env)
    assert card[:2] == ["name", "priority"] and card[-1] == "date_deadline" and "user_id" not in card
    S.rollback(env, change)
    assert _card(env) == before


# ----------------------------------------------------------------------------- new views


def test_create_view_from_default_as_default(env: Any) -> None:
    module_list = _view(env, "list")
    change = S.create_view(env, "crm.lead", "list", None, "Compact leads", make_default=True)
    assert change.kind == "new_view" and "as the default" in change.summary
    view = env["web.view"].elevated().browse([change.res_id])
    assert view.priority < 16 and "generated" not in view.definition
    resolved = _view(env, "list")
    assert resolved["view_id"] == view.ids[0] and resolved["columns"][0]["field"] == "name"
    # Studio's editors now extend the new view
    edit = S.change_view(env, "crm.lead", "list", [{"op": "remove", "anchor": {"field": "name"}}], "Hide name")
    assert env["web.view"].elevated().browse([edit.res_id]).inherit_id.ids == view.ids
    assert "name" not in _columns(env)
    assert S.rollback(env, change) is True
    assert _change(env, edit.ids[0]).state == "rolled_back"
    assert _view(env, "list")["view_id"] == module_list["view_id"]
    # not the default: kept aside with a lower rank
    aside = S.create_view(env, "crm.lead", "kanban", None, "")
    assert env["web.view"].elevated().browse([aside.res_id]).priority == 20
    assert _view(env, "kanban")["view_id"] != aside.res_id
    with pytest.raises(ValidationError):
        S.create_view(env, "crm.lead", "nonsense")


# ----------------------------------------------------------------------------- search filters


def test_search_filters_and_group_bys(env: Any) -> None:
    snap = S.snapshot(env, "before the search changes")
    before = _view(env, "search")
    names = [f["name"] for f in before["filters"]]
    assert names[0] == "mine"
    ops = [
        {"op": "remove", "anchor": {"name": "unassigned"}},
        {"op": "remove", "anchor": {"name": "won"}},
        {
            "op": "insert_before",
            "anchor": {"name": "mine"},
            "node": next(f for f in before["filters"] if f["name"] == "won"),
        },
        {"op": "set_attr", "anchor": {"name": "hot"}, "attrs": {"label": "Hot leads", "domain": [["score", ">=", 70]]}},
        {
            "op": "append_to",
            "list": "filters",
            "node": {"name": "x_big", "label": "Big deals", "domain": [["expected_revenue", ">", 10000]]},
        },
        {"op": "remove", "anchor": {"field": "country_id"}},
        {"op": "append_to", "list": "group_bys", "node": {"field": "priority"}},
    ]
    change = S.change_view(env, "crm.lead", "search", ops, "Search filters changed in Studio")
    after = _view(env, "search")
    filters = {f["name"]: f for f in after["filters"]}
    assert next(f["name"] for f in after["filters"]) == "won" and "unassigned" not in filters
    assert filters["hot"]["label"] == "Hot leads" and filters["hot"]["domain"] == [["score", ">=", 70]]
    assert after["filters"][-1]["name"] == "x_big"
    assert [g["field"] for g in after["group_bys"]][-1] == "priority" and {"field": "country_id"} not in after[
        "group_bys"
    ]
    # an empty list is set on the root, then extended
    clear = [{"op": "remove", "anchor": {"name": f["name"]}} for f in after["filters"]]
    S.change_view(env, "crm.lead", "search", clear, "No filters")
    first = {"name": "x_mine", "label": "Mine", "domain": [["user_id", "=", "$uid"]]}
    added = S.change_view(env, "crm.lead", "search", [{"op": "append_to", "list": "filters", "node": first}], "One")
    assert added.after["operations"][0]["op"] == "set_attr" and _view(env, "search")["filters"] == [first]
    for bad in (
        [{"op": "append_to", "list": "filters", "node": {"name": "x", "domain": []}}],  # no label
        [{"op": "append_to", "list": "filters", "node": {"name": "x", "label": "X", "domain": {"nope": 1}}}],
        [{"op": "append_to", "list": "group_bys", "node": {"field": "no_such_field"}}],
        [{"op": "set_attr", "anchor": {"name": "x_mine"}, "attrs": {"domain": ["bad"]}}],
    ):
        with pytest.raises(ValidationError):
            S.change_view(env, "crm.lead", "search", bad, "Bad")
    with pytest.raises(ValidationError, match="later change relies on this one"):
        S.rollback(env, change)  # "No filters" removes x_big, which this change added
    assert _change(env, change.ids[0]).state == "applied" and _view(env, "search")["filters"] == [first]
    assert S.rollback_to(env, snap) == 3
    assert _view(env, "search") == before


# ----------------------------------------------------------------------------- workflows


STATES = [
    {"key": "draft", "name": "Draft"},
    {"key": "submitted", "name": "Submitted"},
    {"key": "approved", "name": "Approved", "done": True},
    {"key": "refused", "name": "Refused", "fold": True, "done": True},
]


def test_workflow_builder_with_a_new_field(env: Any, env_for: Any) -> None:
    won = _won(env)
    transitions = [
        {"name": "Submit", "from": ["draft"], "to": "submitted", "highlight": True},
        {"name": "Approve", "from": ["submitted"], "to": "approved", "groups": ["crm.group_crm_manager"]},
        {"name": "Approve when won", "from": ["submitted"], "to": "approved", "condition": [["stage_id", "=", won]]},
        {"name": "Refuse", "from": ["submitted"], "to": "refused"},
        {"name": "Reset", "from": [], "to": "draft"},
    ]
    change = S.create_workflow(env, "crm.lead", "", STATES, transitions, "Approval", new_field={"label": "Approval"})
    assert change.after["state_field"] == "x_approval"
    field = env.registry.models["crm.lead"]._fields["x_approval"]
    assert [k for k, _ in field.selection] == ["draft", "submitted", "approved", "refused"]
    assert "x_approval" in _columns(env)
    info = S.workflow_info(env, "crm.lead")
    assert info is not None and info["studio_change_id"] == change.ids[0] and info["strict"]
    assert [s["key"] for s in info["states"]] == ["draft", "submitted", "approved", "refused"]
    assert info["states"][3]["fold"] and info["states"][2]["done"]
    by_name = {t["name"]: t for t in info["transitions"]}
    assert by_name["Approve"]["groups"] == ["crm.group_crm_manager"] and by_name["Reset"]["from"] == []
    assert by_name["Approve when won"]["condition"] == [["stage_id", "=", won]]

    lead = env["crm.lead"].create({"name": "Needs approval"})
    assert lead.x_approval == "draft"
    with pytest.raises(ConflictError):  # strict: only through transitions
        lead.write({"x_approval": "approved"})
    tr = env["workflow.transition"].elevated().browse([by_name["Submit"]["id"]])
    tr.with_env(env).apply(lead)
    assert lead.x_approval == "submitted"
    guarded = env["workflow.transition"].elevated().browse([by_name["Approve when won"]["id"]])
    assert guarded.with_env(env).blocked_reason(lead)
    manager_only = env["workflow.transition"].elevated().browse([by_name["Approve"]["id"]])
    demo = env_for("demo")
    assert manager_only.with_env(demo).blocked_reason(lead.with_env(demo)) == "You are not allowed to do this"
    manager_only.with_env(env).apply(lead)
    assert lead.x_approval == "approved"

    with pytest.raises(ValidationError, match="already has"):
        S.create_workflow(env, "crm.lead", "priority", [{"key": "0"}], [], "Second")
    assert S.rollback(env, change) is True
    assert S.workflow_info(env, "crm.lead") is None
    assert "x_approval" in env.registry.models["crm.lead"]._fields  # the field is its own change


def test_workflow_replace_and_validation(env: Any) -> None:
    first = S.create_workflow(
        env, "crm.lead", "priority", [{"key": "0"}, {"key": "1"}], [{"name": "Up", "to": "1"}], "One"
    )
    second = S.create_workflow(
        env,
        "crm.lead",
        "stage_id",
        [{"key": str(env.ref("crm.stage_new").ids[0]), "name": "New"}, {"key": str(_won(env)), "name": "Won"}],
        [{"name": "Win", "to": str(_won(env))}],
        "Two",
        strict=False,
        replace=True,
    )
    assert second.before == {"replaced_id": first.res_id}
    assert [s["ref"] for s in second.after["states"]] == ["crm.stage_new", "crm.stage_won"]
    assert S.workflow_info(env, "crm.lead")["name"] == "Two"  # type: ignore[index]
    S.rollback(env, second)
    assert S.workflow_info(env, "crm.lead")["name"] == "One"  # type: ignore[index]
    for states, transitions in (
        ([], []),
        ([{"key": "a"}, {"key": "a"}], []),
        ([{"key": "a"}], [{"name": "", "to": "a"}]),
        ([{"key": "a"}], [{"name": "Go", "to": "b"}]),
        ([{"key": "a"}], [{"name": "Go", "to": "a", "condition": {"bad": 1}}]),
        ([{"key": "a"}], [{"name": "Go", "to": "a", "groups": ["nope.nothing"]}]),
    ):
        with pytest.raises(ValidationError):
            S.create_workflow(env, "crm.lead", "priority", states, transitions, "Bad", replace=True)
    with pytest.raises(ValidationError):
        S.create_workflow(env, "crm.lead", "no_field", [{"key": "a"}], [], "Bad", replace=True)


def test_new_editors_are_for_designers_only(env: Any, env_for: Any) -> None:
    demo = env_for("demo")
    for call in (
        lambda: S.edit_field(demo, "crm.lead", "x_any", label="A"),
        lambda: S.create_view(demo, "crm.lead", "list"),
        lambda: S.create_workflow(demo, "crm.lead", "priority", [{"key": "0"}], [], "W"),
        lambda: S.workflow_info(demo, "crm.lead"),
    ):
        with pytest.raises(AccessError):
            call()


def test_api_reads_and_refusals(app: Any) -> None:
    from fastapi.testclient import TestClient

    from tessera.api.tests.conftest import Session

    admin = Session(TestClient(app, base_url="http://localhost"), "admin", "admin1234")  # no lifespan needed
    groups = admin.get("/api/v1/studio/groups").json()
    assert any(g["xmlid"] == "studio.group_designer" and "Designer" in g["name"] for g in groups)
    assert admin.get("/api/v1/studio/models/crm.lead/workflow").json() == {"workflow": None}
    fields = admin.get("/api/v1/studio/models/crm.lead/fields").json()
    assert all("options" not in f for f in fields if not f["studio"])
    resp = admin.patch("/api/v1/studio/models/crm.lead/fields/name", json={"options": {"label": "X"}})
    assert resp.status_code == 422
    resp = admin.post("/api/v1/studio/views", json={"model": "crm.lead", "view_type": "nonsense"})
    assert resp.status_code == 422
    resp = admin.post("/api/v1/studio/workflows", json={"model": "crm.lead", "name": "W", "states": []})
    assert resp.status_code == 422


# ----------------------------------------------------------------------------- export → fresh database


def test_export_reproduces_the_editors_on_a_fresh_database(env: Any, tmp_path: Path) -> None:
    import psycopg

    from modules.web.views import resolve_view
    from tessera.config import get_settings
    from tessera.modules.installer import check_schema, drop_everything, install_modules
    from tessera.modules.loader import clear_discovery_cache
    from tessera.orm.environment import environment_for
    from tessera.orm.registry import Registry

    won = _won(env)
    S.add_field(env, "crm.lead", "x_reason", "char", "Reason", placements=[{"view_type": "form"}])
    S.edit_field(env, "crm.lead", "x_reason", required_if=[["stage_id", "=", won]], default="n/a", widget="badge")
    S.change_view(
        env,
        "crm.lead",
        "list",
        [
            {"op": "remove", "anchor": {"field": "email_from"}},
            {"op": "append_to", "list": "columns", "node": {"field": "x_reason"}},
        ],
        "List",
    )
    S.change_view(env, "crm.lead", "kanban", [{"op": "remove", "anchor": {"field": "user_id"}}], "Card")
    S.change_view(
        env,
        "crm.lead",
        "search",
        [
            {
                "op": "append_to",
                "list": "filters",
                "node": {"name": "x_r", "label": "With reason", "domain": [["x_reason", "!=", False]]},
            }
        ],
        "Filter",
    )
    new_form = S.create_view(env, "crm.lead", "form", None, "Studio form", make_default=True)
    S.change_view(
        env, "crm.lead", "form", [{"op": "set_attr", "anchor": {"field": "name"}, "attrs": {"readonly": True}}], "Lock"
    )
    S.create_workflow(
        env,
        "crm.lead",
        "",
        STATES,
        [{"name": "Submit", "from": ["draft"], "to": "submitted", "groups": ["crm.group_crm_user"]}],
        "Approval",
        new_field={"label": "Approval"},
    )
    golden = {vt: _view(env, vt) for vt in ("form", "list", "kanban", "search")}
    golden_wf = S.workflow_info(env, "crm.lead")
    assert golden["form"]["view_id"] == new_form.res_id
    name, payload = S.export_module(env, "editors")
    files = zipfile.ZipFile(io.BytesIO(payload))
    views = json.loads(files.read(f"{name}/data/views.json"))
    assert sum(1 for v in views if "definition" in v["values"]) == 1  # the new form, exported once
    files.extractall(tmp_path)

    base = get_settings().test_database_url.rsplit("/", 1)[0]
    fresh = f"studio_editors_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(base + "/postgres", autocommit=True) as admin_conn:
        admin_conn.execute(f'CREATE DATABASE "{fresh}"')
    url = f"{base}/{fresh}"
    settings = get_settings()
    settings.module_paths.append(tmp_path)
    clear_discovery_cache()
    try:
        install_modules(["crm", "studio", name], database_url=url)
        assert check_schema(database_url=url) == []
        with environment_for(url, su=True) as e2:
            strip = lambda d: json.dumps({k: v for k, v in d.items() if k != "view_id"}, sort_keys=True)  # noqa: E731
            for vt, view in golden.items():
                assert strip(resolve_view(e2, "crm.lead", vt)) == strip(view), vt
            field = e2.registry.models["crm.lead"]._fields["x_reason"]
            assert field.required_if == [["stage_id", "=", won]] and field.default == "n/a"
            wf = S.workflow_info(e2, "crm.lead")
            assert wf is not None and golden_wf is not None
            assert wf["states"] == golden_wf["states"] and wf["state_field"] == "x_approval"
            assert [(t["name"], t["from"], t["to"], t["groups"]) for t in wf["transitions"]] == [
                (t["name"], t["from"], t["to"], t["groups"]) for t in golden_wf["transitions"]
            ]
    finally:
        settings.module_paths.remove(tmp_path)
        clear_discovery_cache()
        Registry.forget(url)
        drop_everything(database_url=url)
        with psycopg.connect(base + "/postgres", autocommit=True) as admin_conn:
            admin_conn.execute(f'DROP DATABASE IF EXISTS "{fresh}" WITH (FORCE)')
