"""Evaluation corpus isolation: the sandbox hides the target database's business records from every search,
and an evaluation run gives the same result with unrelated data around, writing nothing."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from typing import Any

import pytest
from gateway.adapters.mock import MockAdapter

from tessera.ai import client as C
from tessera.ai import evals as E
from tessera.ai import retrieval as R
from tessera.orm.sandbox import sandbox, scope_for, sql_filter

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _fresh() -> Any:
    MockAdapter.reset()
    C.reset_inprocess_state()
    yield
    C.set_client_factory(None)


def test_sandbox_hides_existing_records_from_every_search(env: Any, env_for: Any) -> None:
    leads = env["crm.lead"].elevated()
    old = leads.create({"name": "Sandbox: existing deal", "type": "opportunity"})
    demo_opp = env.ref("crm.opp_1")
    with sandbox(env, ["crm.lead"], {"crm.lead": demo_opp.ids}) as scopes:
        new = leads.create({"name": "Sandbox: corpus deal", "type": "opportunity"})
        found = leads.with_context(active_test=False).search([("name", "ilike", "Sandbox:")])
        assert found.ids == new.ids  # elevated searches are scoped too
        assert env_for("demo")["crm.lead"].search_count([("id", "=", old.ids[0])]) == 0
        assert leads.search([("id", "in", [old.ids[0], demo_opp.ids[0]])]).ids == demo_opp.ids  # kept: named
        groups = leads.read_group([("name", "ilike", "Sandbox:")], ["expected_revenue:sum"], ["type"])
        assert sum(g["__count"] for g in groups) == 1
        assert leads.browse(old.ids).name == "Sandbox: existing deal"  # reading by id is not filtered
        assert scopes["crm.lead"].visible(new.ids[0]) and not scopes["crm.lead"].visible(old.ids[0])
        cond = sql_filter(env.cr, "crm.lead", "l.id")
        rows = env.cr.fetchall(f"SELECT l.id FROM crm_lead l WHERE l.name LIKE 'Sandbox:%'{cond}")
        assert [r[0] for r in rows] == new.ids
        with sandbox(env, ["contacts.partner"]):  # nested: both scopes apply, the outer one comes back
            assert scope_for(env.cr, "crm.lead") is not None and scope_for(env.cr, "contacts.partner") is not None
        assert scope_for(env.cr, "contacts.partner") is None
    assert env.cr.sandbox is None and sql_filter(env.cr, "crm.lead") == ""
    assert old.ids[0] in leads.search([("name", "ilike", "Sandbox:")]).ids


def test_retrieval_inside_the_sandbox_sees_only_the_corpus(env: Any) -> None:
    leads = env["crm.lead"].elevated()
    story = "<p>Price was too high for the budget; the payback study won it.</p>"
    old = leads.create({"name": "Old: price objection", "type": "opportunity", "description": story})
    R.index_records(old)
    with sandbox(env, ["crm.lead"]):
        new = leads.create({"name": "New: price objection", "type": "opportunity", "description": story})
        R.index_records(new)
        assert {h.res_id for h in R.search(env, "price too high for the budget", models=["crm.lead"])} == set(new.ids)
    assert old.ids[0] in {h.res_id for h in R.search(env, "price too high for the budget", models=["crm.lead"])}


def _noise(env: Any, spec: E.FeatureSpec, copies: int = 2) -> int:
    """Closed deals telling the same stories as the corpus, for other customers (unrelated business data)."""
    leads = env["crm.lead"].elevated().with_context(active_test=False, tracking_disable=True)
    made = 0
    for n, case in enumerate(spec.cases()):
        for item in case["setup"]:
            if item["model"] != "crm.lead" or item["ref"] not in ("w0", "w1", "l0"):
                continue
            values = {k: v for k, v in item["values"].items() if not (isinstance(v, str) and v.startswith("$id:"))}
            values = E.resolve_xmlids(env, values)
            for c in range(copies):
                leads.create({**values, "name": f"Archive {n}-{c}", "partner_name": f"Archive customer {n}-{c}"})
                made += 1
    return made


def _table_counts(env: Any) -> dict[str, int]:
    env.flush()
    tables = [r[0] for r in env.cr.fetchall("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")]
    return {t: int(env.cr.scalar(f'SELECT count(*) FROM "{t}"') or 0) for t in tables}


@contextlib.contextmanager
def _without_sandbox(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    @contextlib.contextmanager
    def nothing(*args: Any, **kwargs: Any) -> Iterator[dict[str, Any]]:
        yield {}

    with monkeypatch.context() as m:
        m.setattr(E, "sandbox", nothing)
        yield


def test_objection_eval_ignores_unrelated_deals_and_writes_nothing(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    (spec,) = E.feature_specs(["crm.objections"])
    assert _noise(env, spec) >= 150
    R.rebuild(env.elevated(), ["crm.lead"])  # the target database's index knows these deals
    before = _table_counts(env)
    report = E.evaluate(env, spec)
    assert report.passed, (report.failures, report.metrics)
    assert report.metrics["citation_validity"] == 1.0 and report.metrics["citations_resolvable"] == 1.0
    assert _table_counts(env) == before  # the whole run is rolled back
    # the control: the same run without the sandbox lets the archive crowd out the cited deals
    with _without_sandbox(monkeypatch):
        control = E.evaluate(env, spec)
    assert control.metrics["citation_validity"] < 1.0, json.dumps(control.metrics)


def test_referenced_records_are_the_ones_named_by_external_id(env: Any) -> None:
    cases = [{"setup": [{"values": {"partner_id": "$xml:contacts.partner_acme", "x": "$xml:core.main_company"}}]}]
    refs = E.referenced_records(env, cases)
    assert refs["contacts.partner"] == set(env.ref("contacts.partner_acme").ids)
    assert refs["core.company"] == set(env.ref("core.main_company").ids)
    assert E.referenced_records(env, [{"setup": []}]) == {}
    (spec,) = E.feature_specs(["crm.objections"])
    models = E.sandbox_models(env, spec)
    assert {"crm.lead", "contacts.partner", "chatter.message", "ai_test.ticket"} <= set(models)
