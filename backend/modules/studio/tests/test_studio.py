"""Studio v1 acceptance: 1.7-a (add a field live), 1.7-b (rollback), 1.7-c (automation), 1.7-d (export →
install reproduces), 1.7-e (designers only)."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from pathlib import Path
from typing import Any

import pytest

from modules.studio import service as S
from tessera.exceptions import AccessError, ValidationError
from tessera.orm.registry import read_registry_stamp
from tessera.orm.schema import SchemaSync

pytestmark = pytest.mark.db


def _column_exists(env: Any, table: str, column: str) -> bool:
    return bool(
        env.cr.scalar(
            "SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = :c",
            {"t": table, "c": column},
        )
    )


def _drift(env: Any) -> list[Any]:
    return SchemaSync(env.cr, env.registry).drift()


def _form_fields(env: Any, model: str, view_type: str = "form") -> str:
    from modules.web.views import resolve_view

    return json.dumps(resolve_view(env.elevated(), model, view_type))


def test_add_monetary_field_live(env: Any) -> None:
    """1.7-a: column, metadata, form/list placement and API exposure without restart; no drift."""
    stamp_before = read_registry_stamp(env.cr)
    out = S.add_field(
        env,
        "crm.lead",
        "competitor_offer",
        "monetary",
        "Competitor offer",
        placements=[{"view_type": "form", "after": "expected_revenue"}, {"view_type": "list"}],
        help="What the competitor quoted",
    )
    assert out["field"] == "x_competitor_offer" and len(out["view_change_ids"]) == 2
    assert _column_exists(env, "crm_lead", "x_competitor_offer")
    meta = (
        env["core.model_field"]
        .elevated()
        .search([("model_name", "=", "crm.lead"), ("name", "=", "x_competitor_offer")])
    )
    assert meta.type == "monetary" and meta.studio and meta.string == "Competitor offer"
    assert "x_competitor_offer" in _form_fields(env, "crm.lead") and "x_competitor_offer" in _form_fields(
        env, "crm.lead", "list"
    )
    lead = env["crm.lead"].create({"name": "Studio lead", "x_competitor_offer": 1250.5})
    assert lead.read(["x_competitor_offer"])[0]["x_competitor_offer"] == 1250.5
    assert "x_competitor_offer" in env.registry.describe_model("crm.lead")["fields"]
    assert [d for d in _drift(env) if "crm_lead" in d.object] == []
    assert read_registry_stamp(env.cr) != stamp_before
    change = env["studio.change"].browse([out["change_id"]])
    assert change.kind == "add_field" and change.after["field"]["ttype"] == "monetary" and change.before is None


def test_rollback_field_and_second_rollback_is_noop(env: Any) -> None:
    """1.7-b: removed from views and metadata, column renamed (D-007); rolling back twice does nothing."""
    out = S.add_field(env, "crm.lead", "x_budget_note", "char", "Budget note", placements=[{"view_type": "form"}])
    change = env["studio.change"].browse([out["change_id"]])
    assert S.rollback(env, change) is True
    assert "x_budget_note" not in env.registry.models["crm.lead"]._fields
    assert "x_budget_note" not in _form_fields(env, "crm.lead")
    assert (
        not env["core.model_field"]
        .elevated()
        .with_context(active_test=False)
        .search([("model_name", "=", "crm.lead"), ("name", "=", "x_budget_note")])
    )
    assert not _column_exists(env, "crm_lead", "x_budget_note")
    renamed = env.cr.scalar(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'crm_lead' AND column_name LIKE '\\_removed\\_%x_budget_note'"
    )
    assert renamed
    assert all(c.state == "rolled_back" for c in env["studio.change"].browse(out["view_change_ids"]))
    assert S.rollback(env, change) is False
    assert [d for d in _drift(env) if "crm_lead" in d.object] == []


def test_transaction_rollback_restores_the_registry(registry: Any, database_url: str) -> None:
    from tessera.db.engine import Cursor, get_engine
    from tessera.orm.environment import SYSTEM_UID, Environment

    cr = Cursor(get_engine(database_url))
    env = Environment.new(registry, cr, SYSTEM_UID, su=True)
    S.add_field(env, "crm.lead", "x_temporary", "integer", "Temporary")
    assert "x_temporary" in registry.models["crm.lead"]._fields
    cr.rollback()
    cr.close()
    assert "x_temporary" not in registry.models["crm.lead"]._fields


def test_field_types_and_validation(env: Any) -> None:
    S.add_field(
        env, "crm.lead", "x_channel", "selection", "Channel", selection=[["web", "Web"], ["fair", "Trade fair"]]
    )
    S.add_field(env, "crm.lead", "x_rivals", "many2many", "Rivals", comodel="contacts.partner")
    S.add_field(env, "crm.lead", "x_referrer", "many2one", "Referrer", comodel="contacts.partner", tracking=True)
    S.add_field(env, "crm.lead", "x_met_on", "date", "Met on", groups=["crm.group_crm_manager"])
    partner = env["contacts.partner"].create({"name": "Rival Inc"})
    lead = env["crm.lead"].create(
        {"name": "Typed", "x_channel": "fair", "x_rivals": [partner.ids[0]], "x_referrer": partner.ids[0]}
    )
    assert lead.x_channel == "fair" and lead.x_rivals.ids == partner.ids and lead.x_referrer.ids == partner.ids
    assert env.cr.scalar("SELECT to_regclass('x_crm_lead_x_rivals_rel') IS NOT NULL")
    assert env.registry.models["crm.lead"]._fields["x_met_on"].groups == ("crm.group_crm_manager",)
    for bad in (
        lambda: S.add_field(env, "crm.lead", "x_channel", "char", "Dup"),
        lambda: S.add_field(env, "crm.lead", "9 bad name", "char", "Bad"),
        lambda: S.add_field(env, "crm.lead", "x_sel", "selection", "No options"),
        lambda: S.add_field(env, "crm.lead", "x_link", "many2one", "No target", comodel="no.model"),
        lambda: S.add_field(env, "core.security_stamp", "x_nope", "char", "Not studio"),
        lambda: S.add_field(env, "crm.lead", "x_opt", "char", "Bad option", formula="1+1"),
    ):
        with pytest.raises(ValidationError):
            bad()
    with pytest.raises(ValidationError):
        S.add_field(env, "contacts.partner", "x_amount", "monetary", "Amount", currency_field="nope")


def test_views_automation_workflow_snapshot(env: Any) -> None:
    """1.7-c (backend): a Studio automation on stage change schedules an activity."""
    snap = S.snapshot(env, "before")
    S.change_view(
        env,
        "crm.lead",
        "form",
        [{"op": "set_attr", "anchor": {"field": "name"}, "attrs": {"readonly": True}}],
        "Lock the name",
    )
    S.create_view(
        env,
        "crm.lead",
        "kanban",
        {"root": {"kind": "card", "children": [{"kind": "field", "name": "name"}]}},
        "Compact cards",
    )
    won = env.ref("crm.stage_won")
    S.add_automation(
        env,
        {
            "name": "Call when won",
            "model_name": "crm.lead",
            "trigger": "on_write",
            "watched_fields": ["stage_id"],
            "filter_domain": [["stage_id", "=", won.ids[0]]],
            "actions": [{"type": "schedule_activity", "summary": "'Thank-you call'", "days": 1}],
        },
    )
    S.create_workflow(
        env,
        "crm.lead",
        "priority",
        [{"key": "0", "name": "Normal"}, {"key": "1", "name": "High"}],
        [{"name": "Escalate", "from": ["0"], "to": "1"}],
        "Escalation",
    )
    lead = env["crm.lead"].create({"name": "Automation target"})
    lead.write({"stage_id": won.ids[0]})
    acts = env["chatter.activity"].elevated().search([("model", "=", "crm.lead"), ("res_id", "=", lead.ids[0])])
    assert "Thank-you call" in acts.mapped("summary")
    assert S.rollback_to(env, snap) == 4
    assert not env["workflow.automation"].search([("name", "=", "Call when won")])
    assert not env["workflow.definition"].search([("name", "=", "Escalation")])
    assert S.rollback_to(env, snap) == 0


def test_bad_view_operation_is_refused(env: Any) -> None:
    with pytest.raises(ValidationError):
        S.change_view(env, "crm.lead", "form", [{"op": "explode"}], "x")
    with pytest.raises(Exception):  # noqa: B017 - anchor that does not exist
        S.change_view(env, "crm.lead", "form", [{"op": "remove", "anchor": {"field": "no_such_field"}}], "x")


def test_designers_only(env: Any, env_for: Any) -> None:
    demo = env_for("demo")
    for call in (
        lambda: S.add_field(demo, "crm.lead", "x_a", "char", "A"),
        lambda: S.change_view(demo, "crm.lead", "form", [], "x"),
        lambda: S.snapshot(demo, "s"),
        lambda: S.export_module(demo),
    ):
        with pytest.raises(AccessError):
            call()


def test_api_403_for_non_designers(app: Any) -> None:
    """1.7-e: every Studio endpoint answers 403 to a non-designer."""
    from fastapi.testclient import TestClient

    from tessera.api.tests.conftest import Session

    demo = Session(TestClient(app, base_url="http://localhost"), "demo", "demo1234")
    app.openapi_schema = None
    paths = {p: ops for p, ops in app.openapi()["paths"].items() if p.startswith("/api/v1/studio")}
    assert sum(len(ops) for ops in paths.values()) >= 16
    for full_path, ops in paths.items():
        path = full_path.replace("{model}", "crm.lead").replace("{view_type}", "form").replace("{name}", "x_a")
        path = path.replace("{change_id}", "1").replace("{snapshot_id}", "1")
        for method in ops:
            resp = demo.client.request(method, path, headers=demo.headers, json={})
            assert resp.status_code == 403, (method, path, resp.status_code)


def test_export_installs_on_a_fresh_database(env: Any, tmp_path: Path) -> None:
    """1.7-d: the exported customisation installs on a fresh database and reproduces fields and views."""
    import psycopg

    from tessera.config import get_settings
    from tessera.modules.installer import check_schema, drop_everything, install_modules
    from tessera.modules.loader import clear_discovery_cache
    from tessera.orm.environment import environment_for
    from tessera.orm.registry import Registry

    S.add_field(env, "crm.lead", "x_rival", "char", "Rival", placements=[{"view_type": "form", "after": "partner_id"}])
    S.add_field(env, "crm.lead", "x_rival_offer", "monetary", "Rival offer", placements=[{"view_type": "list"}])
    S.change_view(
        env,
        "crm.lead",
        "form",
        [{"op": "set_attr", "anchor": {"field": "x_rival"}, "attrs": {"readonly": True}}],
        "Lock",
    )
    S.create_workflow(
        env,
        "crm.lead",
        "priority",
        [{"key": "0"}, {"key": "1"}],
        [{"name": "Up", "from": ["0"], "to": "1"}],
        "Studio prio",
    )
    golden_form = _form_fields(env, "crm.lead")
    golden_list = _form_fields(env, "crm.lead", "list")
    name, payload = S.export_module(env, "Acme Corp")
    assert name == "studio_customizations_acme_corp"
    zipfile.ZipFile(io.BytesIO(payload)).extractall(tmp_path)

    base = get_settings().test_database_url.rsplit("/", 1)[0]
    fresh = f"studio_export_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(base + "/postgres", autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{fresh}"')
    url = f"{base}/{fresh}"
    settings = get_settings()
    settings.module_paths.append(tmp_path)
    clear_discovery_cache()
    try:
        install_modules(["crm", "studio", name], database_url=url)
        assert check_schema(database_url=url) == []
        with environment_for(url, su=True) as e2:
            from modules.web.views import resolve_view

            form = resolve_view(e2, "crm.lead", "form")
            lst = resolve_view(e2, "crm.lead", "list")
            strip = lambda d: json.dumps({k: v for k, v in d.items() if k != "view_id"}, sort_keys=True)  # noqa: E731
            assert strip(form) == strip(json.loads(golden_form))
            assert strip(lst) == strip(json.loads(golden_list))
            assert {"x_rival", "x_rival_offer"} <= set(e2.registry.models["crm.lead"]._fields)
            assert e2["workflow.definition"].search([("name", "=", "Studio prio")])
    finally:
        settings.module_paths.remove(tmp_path)
        clear_discovery_cache()
        Registry.forget(url)
        drop_everything(database_url=url)
        with psycopg.connect(base + "/postgres", autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{fresh}" WITH (FORCE)')
