"""``account.anomaly``: the detector, flags with explanations and notifications on bills, never blocking
posting; jobs and hooks; decisions; access and company isolation; the explanation with its numbers guard."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from gateway.adapters.mock import MockAdapter

from modules.account import ai_anomaly as A
from modules.account.chart import chart_ref
from tessera.ai import client as C
from tessera.ai.runtime import get_feature, run_feature
from tessera.exceptions import AccessError, UserError

pytestmark = pytest.mark.db
D = dt.date


@pytest.fixture(autouse=True)
def _fresh() -> Any:
    MockAdapter.reset()
    C.reset_inprocess_state()
    yield
    C.set_client_factory(None)


def _bill(i: int, amount: float, day: D, number: str = "", partner: str = "v", text: str = "") -> A.Bill:
    return A.Bill(i, partner, number or f"INV-{1000 + i}", day, amount, text, f"BILL/{i}")


def test_detector_rules() -> None:
    hist = [_bill(i, 1000 + 10 * (i % 3), D(2026, 1, 1) + dt.timedelta(days=30 * i)) for i in range(1, 8)]
    v = A.Vendor(has_older_bills=True)
    assert A.detect(_bill(20, 1010, D(2026, 9, 1)), hist, v, []) == []
    # same number written differently, OCR confusions
    found = A.detect(_bill(20, 1010, D(2026, 9, 1), number="inv 1003"), hist, v, [])
    assert [f.kind for f in found] == ["duplicate_number"] and found[0].related_id == 3
    found = A.detect(_bill(20, 1020, D(2026, 9, 1), number="INV-1OO2"), hist, v, [])
    assert [f.kind for f in found] == ["duplicate_number"] and found[0].severity == "medium"
    # same amount a few days later; the same amount a month later is a recurring bill
    found = A.detect(_bill(20, hist[-1].amount, hist[-1].date + dt.timedelta(days=3)), hist, v, [])
    assert [f.kind for f in found] == ["duplicate_amount"]
    assert A.detect(_bill(20, hist[-1].amount, hist[-1].date + dt.timedelta(days=30)), hist, v, []) == []
    # unusual amounts, both ways
    assert [f.kind for f in A.detect(_bill(20, 6000, D(2026, 9, 1)), hist, v, [])] == ["unusual_amount"]
    assert [f.kind for f in A.detect(_bill(20, 90, D(2026, 9, 1)), hist, v, [])] == ["unusual_amount"]
    # new vendor: large against the company's usual amounts, small is fine
    amounts = [float(x) for x in range(100, 1100, 50)]
    assert [f.kind for f in A.detect(_bill(1, 5000, D(2026, 9, 1)), [], A.Vendor(), amounts)] == ["new_vendor_large"]
    assert A.detect(_bill(1, 200, D(2026, 9, 1)), [], A.Vendor(), amounts) == []
    # bank details
    changed = A.Vendor(bank_changed_days=3, has_older_bills=True)
    assert [f.kind for f in A.detect(_bill(20, 1010, D(2026, 9, 1)), hist, changed, [])] == ["bank_account_changed"]
    other = A.Vendor(
        known_ibans=["DE89370400440532013000"], has_older_bills=True, document_iban="GB29 NWBK 6016 1331 9268 19"
    )
    found = A.detect(_bill(20, 1010, D(2026, 9, 1)), hist, other, [])
    assert found[0].kind == "bank_account_changed" and "6819" in found[0].explanation


def test_near_duplicate_text() -> None:
    body = " ".join(f"word{i}" for i in range(60))
    hist = [_bill(1, 500, D(2026, 3, 1), number="A-77", text=f"Invoice A-77 {body}")]
    found = A.detect(
        _bill(2, 500, D(2026, 3, 4), number="A-78", text=f"Invoice A-78 {body}"),
        hist,
        A.Vendor(has_older_bills=True),
        [],
    )
    assert {f.kind for f in found} == {"duplicate_amount"}  # the cheaper signal wins
    found = A.detect(
        _bill(2, 500, D(2026, 3, 10), number="A-79", text=f"Invoice A-79 {body}"),
        hist,
        A.Vendor(has_older_bills=True),
        [],
    )
    assert [f.kind for f in found] == ["near_duplicate_text"] and found[0].score > 0.85


def _setup(env: Any) -> tuple[Any, Any, Any]:
    eu = env.ref("core.demo_company_eu")
    e = env.with_company(eu.ids[0]).with_context(skip_ai_anomaly=True)
    env.user.elevated().write({"company_ids": [("add", eu.ids[0])]})
    vendor = e["contacts.partner"].create({"name": "Fournitures Ouest SARL", "is_company": True})
    acc = chart_ref(eu, "expense")
    for i, amount in enumerate((410.0, 395.5, 420.0, 401.2, 388.0)):
        bill = e["account.move"].create(
            {
                "move_type": "in_invoice",
                "partner_id": vendor.ids[0],
                "invoice_date": D(2026, 1, 10) + dt.timedelta(days=30 * i),
                "ref": f"FO-{100 + i}",
                "invoice_line_ids": [
                    ("create", {"name": "Supplies", "account_id": acc.ids[0], "quantity": 1, "price_unit": amount})
                ],
            }
        )
        bill.action_post()
    return e, vendor, acc


def _new_bill(e: Any, vendor: Any, acc: Any, ref: str, amount: float, day: D) -> Any:
    return e["account.move"].create(
        {
            "move_type": "in_invoice",
            "partner_id": vendor.ids[0],
            "invoice_date": day,
            "ref": ref,
            "invoice_line_ids": [
                ("create", {"name": "Supplies", "account_id": acc.ids[0], "quantity": 1, "price_unit": amount})
            ],
        }
    )


def test_flags_are_stored_explained_notified_and_never_block_posting(env: Any) -> None:
    e, vendor, acc = _setup(env)
    dup = _new_bill(e, vendor, acc, "FO-102", 420.0, D(2026, 6, 1))
    new = A.check_move(e, dup)
    assert [f.kind for f in new] == ["duplicate_number"]
    flag = new[0]
    assert "FO-102" in flag.explanation and flag.related_move_id.ref == "FO-102" and flag.state == "open"
    assert flag.company_id == dup.company_id and flag.ai_generated
    note = env["core.notification"].elevated().search([("model", "=", "account.move"), ("res_id", "=", dup.ids[0])])
    assert note and "FO-102" in note.body
    assert dup.ai_anomaly_count == 1
    dup.action_post()  # flags never block posting
    assert dup.state == "posted"
    # same check again: nothing new; a dismissed flag is not raised again
    assert A.check_move(e, dup) == []
    flag.action_dismiss("same number reused by the vendor")
    assert flag.state == "dismissed" and flag.decided_by.ids == env.user.ids
    assert A.check_move(e, dup) == []
    e.cache.invalidate()
    assert dup.ai_anomaly_count == 0
    with pytest.raises(UserError):
        flag.action_confirm()
    # an unusual amount, then corrected: the stale open flag goes away
    big = _new_bill(e, vendor, acc, "FO-200", 9000.0, D(2026, 7, 1))
    assert [f.kind for f in A.check_move(e, big)] == ["unusual_amount"]
    big.write({"invoice_line_ids": [("update", big.invoice_line_ids.ids[0], {"price_unit": 405.0})]})
    A.check_move(e, big)
    e.cache.invalidate()
    assert big.ai_anomaly_count == 0


def test_bank_account_changed_after_the_first_bills(env: Any) -> None:
    e, vendor, acc = _setup(env)
    e["account.bank_account"].create({"partner_id": vendor.ids[0], "acc_number": "FR7630006000011234567890189"})
    bill = _new_bill(e, vendor, acc, "FO-300", 400.0, e.today())
    assert "bank_account_changed" in [f.kind for f in A.check_move(e, bill)]


def test_job_and_hooks_queue_a_check(env: Any) -> None:
    e, vendor, acc = _setup(env)
    e = e.with_context(skip_ai_anomaly=False)
    before = env["core.job"].elevated().search_count([("name", "=", "account.check_anomalies")])
    bill = _new_bill(e, vendor, acc, "FO-103", 401.2, D(2026, 6, 2))
    jobs = env["core.job"].elevated().search([("name", "=", "account.check_anomalies")])
    assert len(jobs) == before + 1
    assert A.check_anomalies_job(env, bill.ids[0]) == 1  # FO-103 was already billed
    get_feature(env, A.FEATURE).set_enabled(bill.company_id.ids[0], False)
    assert A.check_anomalies_job(env, bill.ids[0]) == 0
    assert A.check_anomalies_job(env, 999999999) == 0


def test_flag_access_and_company_isolation(env: Any, env_for: Any) -> None:
    e, vendor, acc = _setup(env)
    dup = _new_bill(e, vendor, acc, "FO-101", 395.5, D(2026, 6, 1))
    flag = A.check_move(e, dup)[0]
    eu = env.ref("core.demo_company_eu").ids[0]
    reader = (
        env["core.user"]
        .elevated()
        .create(
            {
                "login": "acc_reader_eu",
                "name": "Auditor",
                "company_id": eu,
                "company_ids": [("set", [eu])],
                "group_ids": [("set", [env.ref("account.group_account_readonly").ids[0]])],
            }
        )
    )
    auditor = env.with_user(reader.ids[0]).with_company(eu)
    assert auditor["account.move_anomaly"].search([("id", "=", flag.ids[0])]).ids == flag.ids
    with pytest.raises(AccessError):
        auditor["account.move_anomaly"].browse(flag.ids).action_dismiss()
    us_only = env.with_company(env.ref("core.main_company").ids[0])
    us_user = (
        env["core.user"]
        .elevated()
        .create(
            {
                "login": "acc_clerk_us",
                "name": "US clerk",
                "company_id": env.ref("core.main_company").ids[0],
                "company_ids": [("set", [env.ref("core.main_company").ids[0]])],
                "group_ids": [("set", [env.ref("account.group_account_invoice").ids[0]])],
            }
        )
    )
    assert not us_only.with_user(us_user.ids[0])["account.move_anomaly"].search([("id", "=", flag.ids[0])])


def test_explanation_keeps_only_numbers_of_the_flags(env: Any) -> None:
    e, vendor, acc = _setup(env)
    dup = _new_bill(e, vendor, acc, "FO-101", 395.5, D(2026, 6, 1))
    A.check_move(e, dup)
    p = env.ref("ai_core.provider_local")
    good = f"Check FO-101: the same invoice number was billed before for 395.50. [source:account.move:{dup.ids[0]}]"
    p.write(
        {
            "options": {
                **(p.options or {}),
                "script": [{"match": {"feature": A.FEATURE}, "respond": {"json": {"narrative": good}}}],
            }
        }
    )
    res = run_feature(e, A.FEATURE, {}, record=dup, **A.explain_context(e, dup, {}))
    assert res.data["narrative_source"] == "model" and res.data["narrative"] == good
    bad = "Pay 12,345.67 right away."
    p.write(
        {
            "options": {
                **(p.options or {}),
                "script": [{"match": {"feature": A.FEATURE}, "respond": {"json": {"narrative": bad}}}],
            }
        }
    )
    res = run_feature(e, A.FEATURE, {}, record=dup, **A.explain_context(e, dup, {}))
    assert res.data["narrative_source"] == "facts" and res.data["foreign_numbers"] == ["12,345.67"]
    assert "FO-101" in res.data["narrative"]


def test_api_flags_check_and_decide(admin: Any) -> None:
    from tessera.orm.environment import environment_for

    with environment_for() as e:
        company = e.ref("core.main_company").ids[0]
        partner = e.ref("contacts.partner_acme").ids[0]
    values = {"move_type": "in_invoice", "partner_id": partner, "ref": "API-ANOMALY-1",
              "invoice_line_ids": [["create", {"name": "Paper", "quantity": 1, "price_unit": 12.5}]]}  # fmt: skip
    created = admin.post("/api/v1/models/account.move", json={"values": values, "fields": ["id"]})
    assert created.status_code in (200, 201), created.text
    move_id = created.json()["id"]
    got = admin.get(f"/api/v1/account/ai/moves/{move_id}/anomalies")
    assert got.status_code == 200 and got.json()["ai_generated"]
    checked = admin.post(f"/api/v1/account/ai/moves/{move_id}/anomalies/check")
    assert checked.status_code == 200 and "flags" in checked.json()
    for f in checked.json()["flags"]:
        if f["state"] == "open":
            assert (
                admin.post(f"/api/v1/account/ai/anomalies/{f['id']}/dismiss", json={"note": "ok"}).json()["state"]
                == "dismissed"
            )
    assert admin.post("/api/v1/account/ai/anomalies/999999999/dismiss", json={}).status_code == 404
    flag = f"/api/v1/ai/admin/features/{A.FEATURE}/flag"
    admin.client.put(flag, json={"company_id": company, "enabled": False}, headers=admin.headers)
    try:
        assert admin.get(f"/api/v1/account/ai/moves/{move_id}/anomalies").status_code == 404
        assert admin.post(f"/api/v1/account/ai/moves/{move_id}/anomalies/check").status_code == 404
    finally:
        admin.client.put(flag, json={"company_id": company, "enabled": True}, headers=admin.headers)
        admin.delete(f"/api/v1/models/account.move/{move_id}")


def test_demo_step_flags_a_bill_entered_twice(env: Any) -> None:
    from modules.account import ai_demo, demo

    eu = env.ref("core.demo_company_eu")
    env.user.elevated().write({"company_ids": [("add", eu.ids[0])]})
    demo.seed_company(env, eu)
    assert ai_demo.seed_company(env, eu) >= 1
    assert ai_demo.seed_company(env, eu) == 0  # idempotent
    ai_demo.seed_ai_demo(env)
