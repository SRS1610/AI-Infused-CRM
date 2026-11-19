"""Sales AI features (2.6): upsell, discount bands and suggestions, lines from pasted text, pricing flags.

Covers the platform gates for each: results computed as the requesting user (record rules, companies),
proposal-only writes (zero line / order mutations and zero mail before ``accept``), kill switch → 404
``feature_disabled`` and no gateway call, ``ai_core.run`` audit rows, planted instructions, and the HTTP API.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from gateway.adapters.mock import MockAdapter

from modules.sale import ai_common as AC
from modules.sale import ai_discount as D
from modules.sale import ai_pricing as PR
from modules.sale import ai_quote_text as Q
from modules.sale import ai_upsell as U
from modules.sale.tests.common import customer, no_promotions
from tessera.ai import client as C
from tessera.ai import proposals as P
from tessera.ai.errors import FeatureDisabled, InvalidOutput
from tessera.ai.runtime import HANDLERS, run_feature
from tessera.api.tests.conftest import Session
from tessera.exceptions import AccessError, UserError, ValidationError

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _fresh(env: Any) -> Any:
    MockAdapter.reset()
    C.reset_inprocess_state()
    no_promotions(env)
    yield
    C.set_client_factory(None)


def _counts(env: Any) -> dict[str, int]:
    env.flush()
    return {
        t: int(env.cr.scalar(f"SELECT count(*) FROM {t}") or 0)
        for t in ("sale_quotation", "sale_quotation_line", "core_mail_queue", "chatter_message")
    } | {"discounts": int(env.cr.scalar("SELECT count(*) FROM sale_quotation_line WHERE discount > 0") or 0)}


def _off(env: Any, name: str, company: str = "core.main_company") -> None:
    env["ai_core.feature"].search([("name", "=", name)]).set_enabled(env.ref(company).ids[0], False)


def _products(env: Any, *names: str, **vals: Any) -> list[Any]:
    sys = env.elevated()
    return [
        sys["product.product"].create({"name": n, "list_price": 100.0 + 10 * i, **vals}) for i, n in enumerate(names)
    ]


def _order(env: Any, partner: Any, products: list[Any], state: str = "draft", **vals: Any) -> Any:
    return (
        env["sale.quotation"]
        .elevated()
        .create(
            {
                "partner_id": partner.ids[0],
                "state": state,
                "line_ids": [("create", {"product_id": p.ids[0], "quantity": 1}) for p in products],
                **vals,
            }
        )
    )


def _demo_quote(env: Any, env_for: Any, partner: Any, products: list[Any], **vals: Any) -> Any:
    demo = env_for("demo")
    q = _order(env, partner, products, user_id=demo.uid, **vals)
    return q.with_env(demo)


# ----------------------------------------------------------------------------- upsell


def test_co_purchase_rules_rank_with_counts() -> None:
    model = U.CoPurchase.fit([["a", "b"], ["a", "b"], ["a", "c"], ["a", "c"], ["d", "e"], ["d", "e"], ["a"]])
    ranked = model.rank(["a"], limit=3)
    assert [r.key for r in ranked][:2] == ["b", "c"]
    assert ranked[0].evidence[0].both == 2 and ranked[0].evidence[0].antecedent_orders == 5
    assert model.rank(["a"], prior={"c": "Accessory"}, limit=1)[0].key == "c"  # 1 - 0.6 x 0.7 beats 2/5
    assert all(r.popular for r in model.rank(["zz"], limit=3))  # nothing known: popular products fill
    assert model.rank(["a"], exclude=["b"], allowed={"c"}, limit=5)[0].key == "c"


def test_upsell_learns_from_orders_and_proposes_a_line(env: Any, env_for: Any) -> None:
    alpha, bravo, charlie, secret = _products(env, "Upsell Alpha", "Upsell Bravo", "Upsell Charlie", "Upsell Secret")
    eu = env.ref("core.demo_company_eu").ids[0]
    secret.write({"company_id": env.ref("core.main_company").ids[0]})
    partner = customer(env, "Upsell buyer")
    for _ in range(3):
        _order(env, partner, [alpha, bravo], state="accepted")
    for _ in range(2):
        _order(env, partner, [alpha, secret], state="accepted")
    alpha.elevated().write({"accessory_product_ids": [("set", [charlie.ids[0], secret.ids[0]])]})
    q = _demo_quote(env, env_for, partner, [alpha])
    found = U.suggest_products(q)
    names = [s["display_name"] for s in found["suggestions"]]
    assert names[:3] == ["Upsell Bravo", "Upsell Secret", "Upsell Charlie"]
    assert found["suggestions"][0]["reasons"][0] == "Bought with Upsell Alpha in 3 of 5 orders"
    assert "Accessory of Upsell Alpha" in found["suggestions"][2]["reasons"]
    run = env["ai_core.run"].browse([found["run_id"]])
    assert run.feature == U.FEATURE and run.user_id.ids == [env_for("demo").uid] and run.res_id == q.ids[0]
    # a user who cannot read a product never gets it: Marie (Europe only) on a European quotation
    marie = env_for("marie").with_company(eu)
    q_eu = _order(env, partner, [alpha], user_id=marie.uid, company_id=eu)
    eu_names = [s["display_name"] for s in U.suggest_products(q_eu.with_env(marie))["suggestions"]]
    assert "Upsell Secret" not in eu_names
    # adding is a proposal: nothing changes until accepted
    before = _counts(env)
    proposal = U.propose_line(q, found["suggestions"][0]["product_id"], 2, found["run_id"])
    assert proposal.kind == "write" and proposal.run_id.ids == [found["run_id"]]
    assert _counts(env) == before and len(q.line_ids) == 1
    with pytest.raises(UserError):
        U.propose_line(q, alpha.ids[0])  # already on the quotation
    P.accept(env_for("demo"), proposal)
    added = q.line_ids.filtered(lambda line: line.product_id == bravo)
    assert added.quantity == 2 and added.price_unit == q.elevated()._price_for(bravo, 2)  # pricelist, not model


def test_upsell_respects_record_rules_and_kill_switch(env: Any, env_for: Any) -> None:
    (alpha,) = _products(env, "Rule Alpha")
    theirs = _order(env, customer(env, "Rule buyer"), [alpha])  # the administrator's quotation
    demo = env_for("demo")
    with pytest.raises(AccessError):
        U.suggest_products(theirs.with_env(demo))
    q = _demo_quote(env, env_for, customer(env, "Rule buyer 2"), [alpha])
    _off(env, U.FEATURE)
    with pytest.raises(FeatureDisabled):
        U.suggest_products(q)
    with pytest.raises(FeatureDisabled):
        U.propose_line(q, alpha.ids[0])
    assert not env["ai_core.run"].search([("feature", "=", U.FEATURE), ("res_id", "=", q.ids[0])])


def test_upsell_explanation_never_ranks(env: Any, env_for: Any) -> None:
    alpha, bravo = _products(env, "Explain Alpha", "Explain Bravo")
    partner = customer(env, "Explain buyer")
    for _ in range(2):
        _order(env, partner, [alpha, bravo], state="accepted")
    q = _demo_quote(env, env_for, partner, [alpha])
    kwargs = HANDLERS[U.FEATURE](q.env, q, {})
    assert "Explain Bravo: Bought with Explain Alpha in 2 of 2 orders" in kwargs["sources"][0].text
    result = run_feature(q.env, U.FEATURE, {}, record=q, **kwargs)
    assert result.run.status == "ok" and not result.proposals


# ----------------------------------------------------------------------------- discount bands


def _band(env: Any, **vals: Any) -> Any:
    return env["sale.discount_band"].create({"name": "Band", "max_discount": 10, **vals})


def test_band_validation_and_most_specific_band(env: Any) -> None:
    with pytest.raises(ValidationError):
        _band(env, min_discount=12, max_discount=10)
    with pytest.raises(ValidationError):
        _band(env, max_discount=120)
    cat = env["product.category"].create({"name": "Band parent"})
    sub = env["product.category"].create({"name": "Band child", "parent_id": cat.ids[0]})
    tag = env["contacts.tag"].create({"name": "Band key account"})
    general = _band(env, name="General", category_id=cat.ids[0], max_discount=5)
    volume = _band(env, name="Volume", category_id=cat.ids[0], min_quantity=10, max_discount=8)
    child = _band(env, name="Child", category_id=sub.ids[0], max_discount=6)
    key = _band(env, name="Key", category_id=cat.ids[0], partner_tag_ids=[("set", tag.ids)], max_discount=12)
    (p,) = _products(env, "Band product", category_id=cat.ids[0])
    (ps,) = _products(env, "Band sub product", category_id=sub.ids[0])
    plain, vip = customer(env, "Band plain"), customer(env, "Band VIP", tag_ids=[("set", tag.ids)])
    company = env.ref("core.main_company")
    bands = env["sale.discount_band"]
    assert bands._for_line(company, p, plain, 1) == general
    assert bands._for_line(company, p, plain, 10) == volume
    assert bands._for_line(company, p, vip, 50) == key
    assert bands._for_line(company, ps, vip, 1) == child
    eu = env.ref("core.demo_company_eu")
    mine = {general.ids[0], volume.ids[0], child.ids[0], key.ids[0]}
    assert not set(bands._for_line(eu, p, plain, 1).ids) & mine  # bands belong to a company


def test_bands_are_isolated_per_company(env: Any, env_for: Any) -> None:
    main_band = _band(env, name="Main only band")
    marie = env_for("marie").with_company(env.ref("core.demo_company_eu").ids[0])
    assert main_band.ids[0] not in marie["sale.discount_band"].search([]).ids
    with pytest.raises(AccessError):
        marie["sale.discount_band"].create({"name": "Mine", "max_discount": 50})  # sales users read only


def test_discount_suggestions_stay_in_band_and_are_proposals(env: Any, env_for: Any) -> None:
    cat = env["product.category"].create({"name": "Discount test category"})
    band = _band(env, name="Hardware test", category_id=cat.ids[0], min_discount=2, max_discount=12)
    p, other = _products(env, "Discount widget", "Discount gadget", standard_price=40.0)
    p.write({"category_id": cat.ids[0]})
    partner = customer(env, "Discount buyer")
    # greedy history: every win at 40 % — the suggestion must still stay inside the band
    for i in range(10):
        q = _order(env, partner, [p], state="accepted" if i % 2 == 0 else "rejected")
        q.line_ids.elevated().with_context(_sale_internal=True).write({"discount": 40.0 if i % 2 == 0 else 0.0})
    quote = _demo_quote(env, env_for, partner, [p, other])
    quote.line_ids.elevated().write({"quantity": 5})
    before = _counts(env)
    out = D.suggest(quote)
    rows = {r["product"]: r for r in out["lines"]}
    widget = rows["Discount widget"]
    assert widget["status"] == "suggested" and widget["method"] == "history"
    assert 2 <= widget["suggested_discount"] <= 12 and widget["band"]["id"] == band.ids[0]
    assert rows["Discount gadget"]["status"] == "no_band"
    assert _counts(env) == before  # proposals only
    run = env["ai_core.run"].browse([out["run_id"]])
    assert run.feature == D.FEATURE and run.decision == "pending"
    proposal = env["ai_core.proposal"].browse([widget["proposal"]["id"]])
    assert proposal.res_model == "sale.quotation_line" and proposal.payload == {
        "discount": widget["suggested_discount"]
    }
    P.accept(env_for("demo"), proposal)
    line = quote.line_ids.filtered(lambda r: r.product_id == p)
    assert line.discount == widget["suggested_discount"]
    stats = D.acceptance(env)
    assert stats["counts"]["accepted"] >= 1 and stats["acceptance_rate"] is not None
    with pytest.raises(AccessError):
        D.acceptance(env_for("demo"))  # managers only


def test_discount_engine_helpers() -> None:
    assert (
        D.clamp_to_band(7.3, 2, 12) == 7.5 and D.clamp_to_band(40, 2, 12) == 12 and D.clamp_to_band(0.2, 2.3, 5) == 2.3
    )
    assert D.choose_discount(5, 5, 10, 100, 50, None) == (5.0, "band")
    assert D.choose_discount(0, 10, 1, 100, 50, None) == (0.0, "quantity")
    assert D.choose_discount(0, 10, 100, 100, 50, None) == (10.0, "quantity")
    rows = [(10.0, 5.0, 1)] * 6 + [(0.0, 5.0, 0)] * 6
    model = D.fit_win_model(rows)
    assert model is not None and model.p(10, 5) > model.p(0, 5)
    assert D.fit_win_model(rows[:5]) is None and D.fit_win_model([(1.0, 1.0, 1)] * 10) is None


def test_discount_kill_switch_and_locked_orders(env: Any, env_for: Any) -> None:
    (p,) = _products(env, "Locked widget")
    q = _demo_quote(env, env_for, customer(env, "Locked buyer"), [p])
    q.elevated().action_confirm()
    out = D.suggest(q)  # a sales order still gets suggestions, but no proposal
    assert all("proposal" not in r for r in out["lines"])
    with pytest.raises(UserError):
        D.check_editable(q)
    _off(env, D.FEATURE)
    with pytest.raises(FeatureDisabled):
        D.suggest(q)


# ----------------------------------------------------------------------------- pricing flags


def test_pricing_rules_flag_and_notify_managers(env: Any, env_for: Any) -> None:
    cat = env["product.category"].create({"name": "Pricing test category"})
    _band(env, name="Pricing test", category_id=cat.ids[0], max_discount=10, min_margin=20)
    p, cheap = _products(env, "Margin widget", "Cheap widget", standard_price=60.0, category_id=cat.ids[0])
    partner = customer(env, "Pricing buyer")
    quote = _demo_quote(env, env_for, partner, [p, cheap])
    lines = quote.line_ids.elevated()
    lines.filtered(lambda r: r.product_id == p).write({"discount": 30})  # below cost + 20 %, above the band
    lines.filtered(lambda r: r.product_id == cheap).write({"price_unit": 95.0})  # 13.6 % below the pricelist
    found = PR.check(quote)
    codes = sorted((f["product"], f["code"]) for f in found)
    assert codes == [
        ("Cheap widget", "below_pricelist"),
        ("Margin widget", "below_margin"),
        ("Margin widget", "discount_above_band"),
    ]
    assert "60" not in json.dumps([f["message"] for f in found])  # the cost is never shown
    admin_uid = env.uid
    notified_before = env["core.notification"].elevated().search_count([("user_id", "=", admin_uid)])
    out = PR.sync_flags(quote)
    assert out["new"] == 3 and len(out["flags"]) == 3
    assert env["core.notification"].elevated().search_count([("user_id", "=", admin_uid)]) == notified_before + 1
    run = env["ai_core.run"].browse([out["run_id"]])
    assert run.feature == PR.FEATURE and run.provider == AC.PROVIDER
    # unchanged lines keep their flags; a fixed line resolves its flag
    assert PR.sync_flags(quote)["new"] == 0
    lines.filtered(lambda r: r.product_id == cheap).write({"price_unit": None})
    PR.sync_flags(quote)
    states = {f.code: f.state for f in env["sale.pricing_flag"].search([("quotation_id", "=", quote.ids[0])])}
    assert states["below_pricelist"] == "resolved"
    # approval: approvers of the band only
    flag = env["sale.pricing_flag"].search([("quotation_id", "=", quote.ids[0]), ("code", "=", "below_margin")])
    demo = env_for("demo")
    assert flag.with_env(demo).read(["code"])  # the salesperson sees the flags of their quotation
    with pytest.raises(AccessError):
        flag.with_env(demo).action_approve()
    flag.action_approve()
    assert flag.state == "approved" and flag.approved_by.ids == [admin_uid]
    with pytest.raises(UserError):
        flag.action_approve()
    assert PR.sync_flags(quote)["new"] == 0 and flag.state == "approved"


def test_pricing_check_runs_when_a_quotation_is_confirmed(env: Any, env_for: Any) -> None:
    (p,) = _products(env, "Confirm widget", standard_price=150.0)  # sold below cost
    quote = _demo_quote(env, env_for, customer(env, "Confirm buyer"), [p])
    quote.elevated().action_confirm()
    flags = env["sale.pricing_flag"].search([("quotation_id", "=", quote.ids[0])])
    assert flags.mapped("code") == ["below_margin"] and flags.state == "open"
    other = _demo_quote(env, env_for, customer(env, "Confirm buyer 2"), [p])
    _off(env, PR.FEATURE)
    other.elevated().action_confirm()  # switched off: the sale goes on, nothing is checked
    assert not env["sale.pricing_flag"].search([("quotation_id", "=", other.ids[0])])
    with pytest.raises(FeatureDisabled):
        PR.check(other)


def test_pricing_flags_follow_quotation_access(env: Any, env_for: Any) -> None:
    (p,) = _products(env, "Hidden widget", standard_price=500.0)
    theirs = _order(env, customer(env, "Hidden buyer"), [p])
    PR.sync_flags(theirs)
    demo = env_for("demo")
    assert not demo["sale.pricing_flag"].search([("quotation_id", "=", theirs.ids[0])])
    with pytest.raises(AccessError):
        PR.check(theirs.with_env(demo))
    summary = HANDLERS[PR.FEATURE](env, theirs, {})["sources"][0].text
    assert "Net price below cost" in summary and "500" not in summary


# ----------------------------------------------------------------------------- lines from text


def _script(env: Any, items: list[dict[str, Any]]) -> None:
    p = env.ref("ai_core.provider_local")
    rule = {"match": {"feature": Q.FEATURE}, "respond": {"json": {"items": items}}}
    p.write({"options": {**(p.options or {}), "script": [rule]}})


def _item(desc: str, qty: float = 1, **kw: Any) -> dict[str, Any]:
    return {"description": desc, "quantity": qty, "unit": None, "code": None, "constraints": None, **kw}


def test_quote_lines_from_text_are_matched_flagged_and_proposed(env: Any, env_for: Any) -> None:
    sys = env.elevated()
    arm = sys["product.product"].create({"name": "Zephyr robot arm", "default_code": "TXT-ARM", "list_price": 900})
    grip = sys["product.product"].create({"name": "Zephyr suction gripper", "list_price": 90})
    sys["product.product"].create(
        {
            "name": "Text secret prototype",
            "default_code": "TXT-SEC",
            "company_id": env.ref("core.demo_company_eu").ids[0],
        }
    )
    day = sys["product.product"].create(
        {
            "name": "Zephyr consulting",
            "type": "service",
            "uom_id": env.ref("product.uom_day").ids[0],
            "list_price": 1000,
        }
    )
    q = _demo_quote(env, env_for, customer(env, "Text buyer"), [])
    demo = q.env
    text = "Please quote 2 TXT-ARM, 3 Zephyr suction grippers, 1 TXT-SEC, 16 hours of Zephyr consulting, a teleporter."
    _script(
        env,
        [
            _item("TXT-ARM", 2, code="TXT-ARM"),
            _item("Zephyr suction grippers", 3, constraints="before March"),
            _item("secret prototype", 1, code="TXT-SEC"),
            _item("Zephyr consulting", 16, unit="hours"),
            _item("teleporter", 1),
            _item("Zephyr arm", 7),  # 7 is not in the text
        ],
    )
    before = _counts(env)
    kwargs = HANDLERS[Q.FEATURE](demo, q, {"text": text})
    result = run_feature(demo, Q.FEATURE, {"text": text}, record=q, **kwargs)
    data = result.data
    assert [(line["display_name"], line["quantity"]) for line in data["lines"]] == [
        ("[TXT-ARM] Zephyr robot arm", 2.0),
        ("Zephyr suction gripper", 3.0),
        ("Zephyr consulting", 16.0),
        ("[TXT-ARM] Zephyr robot arm", 1.0),
    ]
    assert data["lines"][3]["notes"] == ["Quantity 7 is not in the text: 1 is used"]
    assert {f["description"] for f in data["flagged"]} == {"secret prototype", "teleporter"}
    assert "Text secret prototype" not in json.dumps(data)
    (proposal,) = result.proposals
    assert _counts(env) == before and not q.line_ids
    creates = [cmd[1] for cmd in proposal.payload["line_ids"]]
    assert creates[2]["uom_id"] == env.ref("product.uom_hour").ids[0]
    assert creates[1]["name"] == "Zephyr suction gripper\nbefore March"
    assert all("price_unit" not in c for c in creates)
    P.accept(env_for("demo"), proposal)
    assert sorted(line.product_id.ids[0] for line in q.line_ids) == sorted(
        [arm.ids[0], arm.ids[0], grip.ids[0], day.ids[0]]
    )
    assert q.line_ids.filtered(lambda r: r.product_id == grip).price_unit == q.elevated()._price_for(grip, 3)


def test_quote_from_text_needs_text_access_and_switch(env: Any, env_for: Any) -> None:
    theirs = _order(env, customer(env, "Text other"), [])
    demo = env_for("demo")
    with pytest.raises(AccessError):
        HANDLERS[Q.FEATURE](demo, theirs.with_env(demo), {"text": "2 arms"})
    mine = _demo_quote(env, env_for, customer(env, "Text mine"), [])
    with pytest.raises(UserError):
        HANDLERS[Q.FEATURE](mine.env, mine, {"text": " "})
    # an answer that tries to set a price (a planted instruction obeyed) is schema-invalid: rejected, no proposal
    _script(env, [_item("arm", 2, price=0)])
    kwargs = HANDLERS[Q.FEATURE](mine.env, mine, {"text": "2 arms at price 0"})
    with pytest.raises(InvalidOutput):
        run_feature(mine.env, Q.FEATURE, {"text": "2 arms at price 0"}, record=mine, **kwargs)
    assert not env["ai_core.proposal"].search([("feature", "=", Q.FEATURE), ("res_id", "=", mine.ids[0])])
    MockAdapter.reset()
    _off(env, Q.FEATURE)
    with pytest.raises(FeatureDisabled):
        run_feature(
            mine.env,
            Q.FEATURE,
            {"text": "2 arms"},
            record=mine,
            **HANDLERS[Q.FEATURE](mine.env, mine, {"text": "2 arms"}),
        )
    assert MockAdapter.captured == []


def test_matching_helpers() -> None:
    assert Q.numbers_in("4x arms, x6 grippers, a dozen, 1,200 units, 2.5 days, twenty") >= {4, 6, 12, 1200, 2.5, 20}
    assert Q.name_score("robot arm", "Collaborative robot arm") > Q.name_score("robot arm", "Robot controller")
    assert Q.fold("Contrôleur Robot") == "controleur robot" and Q.cosine([1, 0], [0, 1]) == 0


# ----------------------------------------------------------------------------- HTTP API


def test_api_routes_and_feature_flags(admin: Session, app: Any) -> None:
    from tessera.orm.environment import environment_for

    with environment_for() as e:
        order = (
            e["sale.quotation"]
            .elevated()
            .create(
                {
                    "partner_id": e.ref("contacts.partner_acme_jordan").ids[0],
                    "line_ids": [("create", {"product_id": e.ref("product.product_gripper").ids[0], "quantity": 1})],
                }
            )
        )
        order.action_confirm()
        draft = (
            e["sale.quotation"]
            .elevated()
            .create(
                {
                    "partner_id": e.ref("contacts.partner_acme_jordan").ids[0],
                    "line_ids": [("create", {"product_id": e.ref("product.product_robot_arm").ids[0], "quantity": 6})],
                }
            )
        )
        draft.line_ids.elevated().write({"discount": 25})
        e.cr.commit()
        oid, did = order.ids[0], draft.ids[0]
        company = e.ref("core.main_company").ids[0]
        sensor = e.ref("product.product_gripper").ids[0]  # accessory of the robot arm
    up = admin.get(f"/api/v1/sale/ai/quotations/{did}/upsell")
    assert up.status_code == 200 and up.json()["ai_generated"] and up.json()["run_id"]
    assert sensor in [s["product_id"] for s in up.json()["suggestions"]]
    prop = admin.post(f"/api/v1/sale/ai/quotations/{did}/upsell/propose", json={"product_id": sensor, "quantity": 1})
    assert prop.status_code == 201 and prop.json()["state"] == "pending"
    disc = admin.post(f"/api/v1/sale/ai/quotations/{did}/discounts")
    assert disc.status_code == 200, disc.text
    line = disc.json()["lines"][0]
    assert line["band"]["name"] == "Hardware, 5 units or more" and 3 <= line["suggested_discount"] <= 15
    assert admin.post(f"/api/v1/sale/ai/quotations/{oid}/discounts").status_code == 400  # a sales order
    assert admin.get("/api/v1/sale/ai/discounts/acceptance").json()["counts"]["pending"] >= 1
    pricing = admin.get(f"/api/v1/sale/ai/quotations/{did}/pricing").json()
    assert {f["code"] for f in pricing["findings"]} >= {"discount_above_band"}
    checked = admin.post(f"/api/v1/sale/ai/quotations/{did}/pricing/check").json()
    flag_id = next(f["id"] for f in checked["flags"] if f["code"] == "discount_above_band")
    approved = admin.post(f"/api/v1/sale/ai/pricing-flags/{flag_id}/approve")
    assert approved.status_code == 200 and approved.json()["state"] == "approved"
    assert admin.get("/api/v1/sale/ai/quotations/999999999/upsell").status_code == 404
    demo = Session(TestClient(app, base_url="http://localhost"), "demo", "demo1234")
    assert demo.get(f"/api/v1/sale/ai/quotations/{did}/pricing").status_code in (403, 404)  # not their quotation
    routes = [
        ("sale.upsell", "get", f"/api/v1/sale/ai/quotations/{did}/upsell"),
        ("sale.discount_suggest", "post", f"/api/v1/sale/ai/quotations/{did}/discounts"),
        ("sale.pricing_breach", "get", f"/api/v1/sale/ai/quotations/{did}/pricing"),
        ("sale.quote_from_text", "post", "/api/v1/ai/features/sale.quote_from_text/run"),
    ]
    for feature, method, url in routes:
        flag = f"/api/v1/ai/admin/features/{feature}/flag"
        admin.client.put(flag, json={"company_id": company, "enabled": False}, headers=admin.headers)
        try:
            body = {"res_model": "sale.quotation", "res_id": did, "inputs": {"text": "2 arms"}}
            off = admin.get(url) if method == "get" else admin.post(url, json=body)
            assert off.status_code == 404 and off.json()["code"] == "feature_disabled", (feature, off.text)
        finally:
            admin.client.put(flag, json={"company_id": company, "enabled": True}, headers=admin.headers)


# ----------------------------------------------------------------------------- demo, synthetic history, explanations


def test_demo_step_scripts_the_test_provider_and_checks_demo_orders(env: Any) -> None:
    from modules.sale import ai_demo, ai_synth

    ai_demo.seed_sale_ai(env.elevated())
    script = env.ref("ai_core.provider_local").options["script"]
    assert {r["match"]["feature"] for r in script} >= {Q.FEATURE, "product.description_draft"}
    ai_demo.seed_sale_ai(env.elevated())  # idempotent: the rules are replaced, not added again
    assert len(env.ref("ai_core.provider_local").options["script"]) == len(script)
    cases = ai_synth.test_cases(10)
    assert len(cases) == 10 and all(c["hidden"] not in c["basket"] and c["date"] >= "2026-07-01" for c in cases)
    assert len(ai_synth.training_baskets()) > 900


def test_discount_explanation_uses_only_computed_figures(env: Any, env_for: Any) -> None:
    cat = env["product.category"].create({"name": "Explain discount category"})
    _band(env, name="Explain band", category_id=cat.ids[0], max_discount=6)
    p, other = _products(env, "Explain discount widget", "Explain plain widget")
    p.write({"category_id": cat.ids[0]})
    q = _demo_quote(env, env_for, customer(env, "Explain discount buyer"), [p, other])
    text = HANDLERS[D.FEATURE](q.env, q, {})["sources"][0].text
    assert "Explain discount widget x 1: suggested 0 %" in text and "Explain plain widget x 1: no approved band" in text
    assert not env["ai_core.proposal"].search([("feature", "=", D.FEATURE), ("res_id", "in", q.line_ids.ids)])
    with pytest.raises(UserError):
        HANDLERS[D.FEATURE](env, None, {})
