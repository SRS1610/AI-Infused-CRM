"""Contacts AI features (1.6): duplicates, tag & parent suggestions, enrichment with sources.

Covers behaviour, permissions (nothing from a company the user cannot read reaches a prompt or a
result), proposals (no change and no mail before ``accept``; accept applies with the user's rights),
background jobs with notifications, the API, interactive latency on the mock provider and the
evaluation gate in replay.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from gateway.adapters.mock import MockAdapter

from modules.contacts import ai_features as A
from modules.contacts import duplicates as D
from modules.contacts import enrichment as E
from tessera.ai import client as C
from tessera.ai import evals as EV
from tessera.ai import proposals as P
from tessera.ai.runtime import HANDLERS, run_feature
from tessera.api.tests.conftest import Session
from tessera.exceptions import AccessError

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _fresh() -> Any:
    MockAdapter.reset()
    C.reset_inprocess_state()
    yield
    C.set_client_factory(None)


def _script(env: Any, rules: list[dict[str, Any]]) -> None:
    p = env.ref("ai_core.provider_local")
    p.write({"options": {**(p.options or {}), "script": rules}})


def _ids(env: Any) -> tuple[int, int]:
    return env.ref("core.main_company").ids[0], env.ref("core.demo_company_eu").ids[0]


def _counts(env: Any) -> dict[str, int]:
    env.flush()
    return {
        t: int(env.cr.scalar(f"SELECT count(*) FROM {t}") or 0)  # noqa: S608
        for t in ("contacts_partner", "contacts_partner__tag_ids", "contacts_merge_log", "core_mail_queue")
    } | {"active": int(env.cr.scalar("SELECT count(*) FROM contacts_partner WHERE active") or 0)}


# -- duplicates ---------------------------------------------------------------------------------------


def test_name_normalisation_and_signals() -> None:
    assert D.name_similarity("Globex Logistics SAS", "Globex Logistics") == 1.0
    assert D.name_similarity("Monarch & Finch", "Monarch and Finch LLP") == 1.0
    assert D.name_similarity("Zoë Lefèvre", "zoe lefevre") == 1.0
    assert D.name_similarity("Kestrel Intl. Shipping", "Kestrel International Shipping") == 1.0
    assert D.name_similarity("Chidi Okonkwo", "Okonkwo Chidi") == 1.0
    assert D.name_similarity("J. Castellanos", "Javier Castellanos") > 0.9
    assert D.name_similarity("Acme Robotics", "Zenith Foods") < 0.5
    assert D.given_name_conflict("Anna Sorensen", "Erik Sorensen")
    assert not D.given_name_conflict("Jordon Lee", "Jordan Lee") and not D.given_name_conflict("J. Lee", "Jordan Lee")
    assert D.website_domain("https://www.Example.test/path") == "example.test"


def test_detection_is_ranked_explained_and_never_crosses_companies(env: Any, env_for: Any) -> None:
    main, eu = _ids(env)
    partners = env["contacts.partner"].elevated()
    me = partners.create({"name": "Lucia Ferraro", "email": "lucia@ferraro-design.test", "company_id": eu})
    twin = partners.create({"name": "Lucia Ferraro", "email": "LUCIA@ferraro-design.test", "company_id": eu})
    typo = partners.create({"name": "Lucia Ferarro", "city": "Milano", "company_id": eu})
    hidden = partners.create(
        {"name": "Lucia Ferraro", "email": "lucia@ferraro-design.test", "company_id": main, "comment": "<p>secret</p>"}
    )
    colleague = partners.create({"name": "Marco Bianchi", "email": "marco@ferraro-design.test", "company_id": eu})
    marie = env_for("marie").with_company(eu)
    found = D.detect(me.with_env(marie))
    ids = [c["id"] for c in found["candidates"]]
    assert hidden.ids[0] not in ids and hidden.ids[0] not in found["duplicates"]
    assert found["duplicates"][0] == twin.ids[0] and typo.ids[0] in found["duplicates"]
    assert colleague.ids[0] not in found["duplicates"]
    first = found["candidates"][0]
    assert "Same e-mail address" in first["reasons"] and first["score"] > 0.9
    assert found["embedding"] == "ok"  # the semantic signal went through the gateway
    # a user of both companies does see the other company's twin
    both = env_for("demo").with_company(main)
    assert hidden.ids[0] in D.detect(me.with_env(both))["duplicates"]
    # without the gateway the fuzzy signals still work
    C.set_client_factory(lambda e: C.HttpGatewayClient("http://127.0.0.1:9", "t", timeout=0.3))
    offline = D.detect(me.with_env(marie))
    assert offline["embedding"] == "unavailable" and twin.ids[0] in offline["duplicates"]


def test_merge_is_a_proposal_until_accepted(env: Any, env_for: Any) -> None:
    """1.6-c: zero mutations and zero mail until accept; accept runs the merge as the user."""
    demo = env_for("demo")
    partners = env["contacts.partner"].elevated()
    keep = partners.create({"name": "Brightwater Analytics", "is_company": True, "email": "info@brightwater.test"})
    dup = partners.create({"name": "Brightwater Analytics Ltd", "is_company": True, "website": "brightwater.test"})
    person = partners.create({"name": "Ada Brook", "parent_id": dup.ids[0]})
    before = _counts(env)
    proposal = D.propose_merge(demo, dup.with_env(demo), [keep.ids[0]])
    assert proposal.kind == "action" and proposal.payload["action"] == "contacts.merge"
    assert proposal.res_id == keep.ids[0] and proposal.payload["args"]["merge_ids"] == [dup.ids[0]]
    assert _counts(env) == before and dup.active and person.parent_id == dup
    with pytest.raises(AccessError):
        P.accept(env_for("marie"), proposal)  # only the requester decides
    accepted = P.accept(demo, proposal)
    assert accepted.state == "accepted"
    assert not dup.with_context(active_test=False).active and person.parent_id == keep
    log = env["contacts.merge_log"].search([("kept_partner_id", "=", keep.ids[0])])
    assert log.method == "ai_suggested" and log.merged_partner_ids == [dup.ids[0]]
    # a user who may not write one of the contacts cannot even get the proposal
    main, _eu = _ids(env)
    other = partners.create({"name": "Brightwater Analytics", "is_company": True, "company_id": main})
    marie = env_for("marie").with_company(env.ref("core.demo_company_eu").ids[0])
    mine = partners.create({"name": "Brightwater", "is_company": True, "company_id": env.ref("core.demo_company_eu").ids[0]})
    with pytest.raises(AccessError):
        D.propose_merge(marie, mine.with_env(marie), [other.ids[0]])


def test_duplicate_check_runs_in_background_and_notifies(env: Any, env_for: Any) -> None:
    demo = env_for("demo")
    partners = env["contacts.partner"].elevated()
    partners.create({"name": "Saltmarsh Brewing", "is_company": True, "city": "Whitby", "email": "hi@saltmarsh.test"})
    new = demo["contacts.partner"].create({"name": "The Saltmarsh Brewery", "is_company": True, "email": "x@saltmarsh.test"})
    jobs = env["core.job"].elevated().search([("name", "=", "contacts.check_duplicates")], order="id desc", limit=1)
    assert jobs and jobs.kwargs == {"partner_id": new.ids[0]} and jobs.user_id.ids == [demo.uid]
    assert A.check_duplicates_job(demo, new.ids[0]) == 1
    note = env["core.notification"].elevated().search([("user_id", "=", demo.uid)], limit=1)
    assert note.title.startswith("Possible duplicate") and note.url == f"/m/contacts.partner/{new.ids[0]}"
    # system-created contacts (imports, users) are not checked in the background
    partners.create({"name": "Imported Co", "is_company": True})
    assert env["core.job"].elevated().search_count([("name", "=", "contacts.check_duplicates")]) == 1
    # switched off for the company: no work, no notification
    feature = env["ai_core.feature"].search([("name", "=", D.FEATURE)])
    feature.set_enabled(env.ref("core.main_company").ids[0], False)
    assert A.check_duplicates_job(demo, new.ids[0]) == 0


# -- tags and parent company ------------------------------------------------------------------------


def _tag_answer(tags: list[tuple[str, float]], parent: int | None = None) -> dict[str, Any]:
    return {
        "tags": [{"name": n, "confidence": c, "reason": "fits"} for n, c in tags],
        "parent_id": parent,
        "parent_reason": "same domain" if parent else "",
        "citations": [],
    }


def test_tag_suggestions_are_a_write_proposal(env: Any, env_for: Any) -> None:
    demo = env_for("demo")
    partners = env["contacts.partner"].elevated()
    tags = env["contacts.tag"].elevated()
    supplier = tags.create({"name": "Supplier"})
    company = partners.create({"name": "Kranich Labs", "is_company": True, "email": "info@kranich-labs.test"})
    peer = partners.create({"name": "Jonas Weber", "email": "jonas@kranich-labs.test", "parent_id": company.ids[0]})
    peer.write({"tag_ids": [supplier.ids[0]]})
    lena = partners.create({"name": "Lena Fischer", "email": "lena@kranich-labs.test"})
    _script(env, [{"match": {"feature": A.TAG_FEATURE}, "respond": {"json": _tag_answer(
        [("Supplier", 0.9), ("Imaginary", 0.8), ("VIP", 0.3)], company.ids[0])}}])  # fmt: skip
    kwargs = HANDLERS[A.TAG_FEATURE](demo, lena.with_env(demo), {})
    assert {s.id for s in kwargs["sources"]} >= {f"contacts.partner:{lena.ids[0]}", f"contacts.partner:{company.ids[0]}"}
    before = _counts(env)
    result = run_feature(demo, A.TAG_FEATURE, {}, record=lena.with_env(demo), **kwargs)
    (proposal,) = result.proposals
    assert [s["name"] for s in result.data["suggested"]] == ["Supplier", "VIP"]
    assert {"name": "Imaginary", "why": "not an existing tag"} in result.data["dropped"]
    assert proposal.kind == "write" and proposal.payload == {"tag_ids": [["add", supplier.ids[0]]], "parent_id": company.ids[0]}
    assert _counts(env) == before and not lena.tag_ids and not lena.parent_id
    P.accept(demo, proposal)
    assert lena.tag_ids == supplier and lena.parent_id == company


def test_tag_context_and_parent_never_cross_companies(env: Any, env_for: Any) -> None:
    main, eu = _ids(env)
    partners = env["contacts.partner"].elevated()
    vip = env.ref("contacts.tag_vip")
    hidden = partners.create(
        {"name": "Rosehip Board", "is_company": True, "email": "board@rosehip.test", "company_id": main, "tag_ids": [vip.ids[0]]}
    )
    me = partners.create({"name": "Rosa Petal", "email": "rosa@rosehip.test", "company_id": eu})
    marie = env_for("marie").with_company(eu)
    kwargs = HANDLERS[A.TAG_FEATURE](marie, me.with_env(marie), {})
    assert all(s.res_id != hidden.ids[0] for s in kwargs["sources"])
    assert "Rosehip Board" not in json.dumps([s.text for s in kwargs["sources"]])
    _script(env, [{"match": {"feature": A.TAG_FEATURE}, "respond": {"json": _tag_answer([("VIP", 0.99)], hidden.ids[0])}}])
    result = run_feature(marie, A.TAG_FEATURE, {}, record=me.with_env(marie), **kwargs)
    assert result.data["parent"] is None and result.data["payload"] == {"tag_ids": [["add", vip.ids[0]]]}
    assert "Rosehip Board" not in json.dumps(MockAdapter.captured)


def test_tag_suggestion_latency_p95_on_mock(env: Any, env_for: Any) -> None:
    """1.6-f: interactive feature, p95 ≤ 3 s on the mock provider."""
    demo = env_for("demo")
    partner = env.ref("contacts.partner_globex").with_env(demo)
    _script(env, [{"match": {"feature": A.TAG_FEATURE}, "respond": {"json": _tag_answer([("Customer", 0.8)])}}])
    times = []
    for _ in range(20):
        started = time.monotonic()
        run_feature(demo, A.TAG_FEATURE, {}, record=partner, **HANDLERS[A.TAG_FEATURE](demo, partner, {}))
        times.append(time.monotonic() - started)
    assert sorted(times)[int(0.95 * len(times)) - 1] <= 3.0


# -- enrichment --------------------------------------------------------------------------------------


def test_adapter_registry_and_approval(env: Any, env_for: Any) -> None:
    main, eu = _ids(env)
    assert "fixture" in E.ADAPTERS
    sources = env["contacts.enrichment_source"].elevated()
    demo_source = env.ref("contacts.enrichment_source_fixture")
    assert demo_source.approved and [s.ids[0] for s in E.approved_sources(env_for("demo"))] == demo_source.ids
    off = sources.create({"name": "Not for Europe", "adapter": "fixture", "approved": False, "company_id": eu})
    assert E.approved_sources(env_for("marie").with_company(eu)) == []  # the company's own record wins
    off.write({"approved": True})
    assert [s.ids[0] for s in E.approved_sources(env_for("marie").with_company(eu))] == off.ids
    from tessera.exceptions import ValidationError

    with pytest.raises(ValidationError, match="Unknown enrichment adapter"):
        sources.create({"name": "x", "adapter": "scraper"})
    results = E.FixtureAdapter().lookup({"name": "Blue Harbor Logistics"})
    assert {r.entity for r in results} == {"Blue Harbor Logistics"} and len({r.source_url for r in results}) == 2
    with pytest.raises(AccessError):  # users cannot approve adapters
        env_for("demo")["contacts.enrichment_source"].create({"name": "mine", "adapter": "fixture", "approved": True})


def _enrich_answer(src: int, **values: str) -> dict[str, Any]:
    return {
        "suggestions": [
            {"field": f, "value": v, "source": f"contacts.enrichment_source:{src}", "evidence": "registry"}
            for f, v in values.items()
        ],
        "citations": [f"contacts.enrichment_source:{src}"],
    }


def test_enrichment_suggestions_carry_sources_and_wait_for_accept(env: Any, env_for: Any) -> None:
    demo = env_for("demo")
    src = env.ref("contacts.enrichment_source_fixture").ids[0]
    partner = env["contacts.partner"].elevated().create(
        {"name": "Northwind Traders", "is_company": True, "email": "sales@northwind-traders.test", "city": "Bristol"}
    )
    answer = _enrich_answer(src, website="https://northwind-traders.test", phone="+44 117 496 0321",
                            country="GB", industry="Retail & wholesale", street="99 Invented Street", city="Bristol")  # fmt: skip
    answer["suggestions"].append({"field": "vat", "value": "GB451287700", "source": f"contacts.partner:{partner.ids[0]}", "evidence": "x"})
    _script(env, [{"match": {"feature": A.ENRICH_FEATURE}, "respond": {"json": answer}}])
    before = _counts(env)
    kwargs = HANDLERS[A.ENRICH_FEATURE](demo, partner.with_env(demo), {})
    assert f"contacts.enrichment_source:{src}" in {s.id for s in kwargs["sources"]}
    result = run_feature(demo, A.ENRICH_FEATURE, {}, record=partner.with_env(demo), **kwargs)
    kept = {s["field"]: s for s in result.data["suggestions"]}
    assert set(kept) == {"website", "phone", "country", "industry"}
    assert all(s["source_url"].startswith("https://registry.fixture.test/") for s in kept.values())
    why = {d["field"]: d["why"] for d in result.data["dropped"]}
    assert why == {"street": "value not found in the cited source", "city": "already set", "vat": "no approved source cited"}
    (proposal,) = result.proposals
    gb = env.ref("core.country_gb").ids[0]
    assert proposal.payload == {"website": "https://northwind-traders.test", "phone": "+44 117 496 0321",
                                "country_id": gb, "industry_id": env.ref("contacts.industry_retail").ids[0]}  # fmt: skip
    assert [c["id"] for c in result.citations] == [f"contacts.enrichment_source:{src}"]
    assert _counts(env) == before and not partner.website and not partner.country_id
    P.accept(demo, proposal)
    assert partner.website == "https://northwind-traders.test" and partner.country_id.ids == [gb]


def test_enrichment_without_approved_adapter_sends_nothing_outside(env: Any, env_for: Any) -> None:
    env.ref("contacts.enrichment_source_fixture").write({"approved": False})
    demo = env_for("demo")
    partner = env["contacts.partner"].elevated().create({"name": "Northwind Traders", "is_company": True})
    kwargs = HANDLERS[A.ENRICH_FEATURE](demo, partner.with_env(demo), {})
    assert [s.model for s in kwargs["sources"]] == ["contacts.partner"]


def test_enrichment_job_notifies_with_link(env: Any, env_for: Any) -> None:
    demo = env_for("demo")
    src = env.ref("contacts.enrichment_source_fixture").ids[0]
    partner = env["contacts.partner"].elevated().create(
        {"name": "Woodgrove Bank", "is_company": True, "email": "it@woodgrove-bank.test"}
    )
    _script(env, [{"match": {"feature": A.ENRICH_FEATURE}, "respond": {"json": _enrich_answer(src, city="Zurich", zip="8001")}}])
    out = A.enrich_partner_job(demo, partner.ids[0])
    assert out["suggestions"] == 2
    note = env["core.notification"].elevated().search([("user_id", "=", demo.uid)], limit=1)
    assert note.title == "Enrichment ready: Woodgrove Bank" and note.url == f"/m/contacts.partner/{partner.ids[0]}"
    assert note.kind == "job" and not partner.city  # still only a proposal
    C.set_client_factory(lambda e: C.HttpGatewayClient("http://127.0.0.1:9", "t", timeout=0.3))
    assert A.enrich_partner_job(demo, partner.ids[0]) == {"error": "ai_unavailable"}


# -- API ---------------------------------------------------------------------------------------------


def test_api_duplicates_merge_proposal_enrich_and_flags(admin: Session, app: Any) -> None:
    from tessera.orm.environment import environment_for

    with environment_for() as e:
        acme = e.ref("contacts.partner_acme").ids[0]
        jordan = e.ref("contacts.partner_acme_jordan").ids[0]
        company = e.ref("core.main_company").ids[0]
    found = admin.get(f"/api/v1/contacts/{acme}/ai/duplicates")
    assert found.status_code == 200 and {"candidates", "duplicates", "embedding"} <= set(found.json())
    prop = admin.post(f"/api/v1/contacts/{jordan}/ai/duplicates/propose-merge", json={"ids": [acme]})
    assert prop.status_code == 201 and prop.json()["kind"] == "action" and prop.json()["state"] == "pending"
    assert admin.post(f"/api/v1/ai/proposals/{prop.json()['id']}/reject", json={"reason": "not the same"}).status_code == 200
    queued = admin.post(f"/api/v1/contacts/{acme}/ai/enrich")
    assert queued.status_code == 202 and queued.json()["state"] == "queued"
    adapters = admin.get("/api/v1/contacts/ai/enrichment-adapters").json()
    assert {"key": "fixture", "approved": True}.items() <= next(a for a in adapters if a["key"] == "fixture").items()
    demo = Session(TestClient(app, base_url="http://localhost"), "demo", "demo1234")
    assert demo.get("/api/v1/contacts/999999999/ai/duplicates").status_code == 404
    flag = "/api/v1/ai/admin/features/contacts.duplicates/flag"
    admin.client.put(flag, json={"company_id": company, "enabled": False}, headers=admin.headers)
    try:
        off = admin.get(f"/api/v1/contacts/{acme}/ai/duplicates")
        assert off.status_code == 404 and off.json()["code"] == "feature_disabled"
    finally:
        admin.client.put(flag, json={"company_id": company, "enabled": True}, headers=admin.headers)


# -- evaluation gate ---------------------------------------------------------------------------------


def test_contacts_evaluations_pass_in_replay(env: Any) -> None:
    """1.6-a/b: every contacts feature is complete and meets its thresholds in replay."""
    specs = EV.feature_specs(modules=["contacts"])
    assert {s.name for s in specs} == {"contacts.duplicates", "contacts.tag_suggest", "contacts.enrich"}
    assert EV.completeness(specs) == []
    for spec in specs:
        report = EV.evaluate(env, spec)
        assert report.passed, (spec.name, report.failures, report.metrics)
        assert all(r["unexpected_writes"] == 0 for r in report.case_results), spec.name
    dup = EV.evaluate(env, next(s for s in specs if s.name == "contacts.duplicates"))
    assert dup.metrics["precision"] >= 0.9 and dup.metrics["recall"] >= 0.8
