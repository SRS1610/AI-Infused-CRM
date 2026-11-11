"""``account.bill_ocr``: one proposal per document, nothing written before acceptance (2-i), auto-coding,
totals checks, the vision path through the gateway, audit, kill switch, access and company isolation."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from fastapi.testclient import TestClient
from gateway.adapters.mock import MockAdapter

from modules.account import ai_bill as B
from modules.account import ai_eval as AE
from modules.account.ai.bills import corpus, render
from modules.account.chart import chart_ref
from tessera.ai import client as C
from tessera.ai.errors import FeatureDisabled
from tessera.ai.runtime import get_feature
from tessera.api.tests.conftest import Session
from tessera.exceptions import AccessError, UserError, ValidationError

pytestmark = pytest.mark.db
WRITE = ("account.move", "account.move_line", "contacts.partner", "account.bank_account", "core.mail_queue")


@pytest.fixture(autouse=True)
def _fresh() -> Any:
    MockAdapter.reset()
    C.reset_inprocess_state()
    yield
    C.set_client_factory(None)


@pytest.fixture(scope="module")
def bills() -> list[dict[str, Any]]:
    return corpus.draw()


def _eu(env: Any) -> Any:
    return env.ref("core.demo_company_eu")


@pytest.fixture
def clerk(env: Any) -> Any:
    eu = _eu(env)
    user = (
        env["core.user"]
        .elevated()
        .create(
            {
                "login": "ap_clerk_test",
                "name": "AP Clerk",
                "company_id": eu.ids[0],
                "company_ids": [("set", [eu.ids[0]])],
                "group_ids": [("set", [env.ref("account.group_account_invoice").ids[0]])],
            }
        )
    )
    return env.with_user(user.ids[0]).with_company(eu.ids[0])


def _counts(env: Any) -> dict[str, int]:
    env.flush()
    return {m: int(env.cr.scalar(f'SELECT count(*) FROM "{env.registry.models[m]._table}"')) for m in WRITE}


def _bill(
    bills: list[dict[str, Any]], kind: str, *, known: bool = True, move_type: str = "in_invoice"
) -> dict[str, Any]:
    return next(
        b
        for b in bills
        if b["spec"]["kind"] == kind
        and bool(b["world"]["vendor"].get("known")) == known
        and b["spec"]["move_type"] == move_type
        and not b.get("injection")
        and not b["world"]["vendor"].get("other_company")
        and not b["gold"]["needs_review"]
    )


def _script(env: Any, rules: list[dict[str, Any]]) -> None:
    p = env.ref("ai_core.provider_local")
    p.write({"options": {**(p.options or {}), "script": rules}})


def test_digital_bill_proposal_writes_nothing_until_accepted(env: Any, clerk: Any, bills: list[dict[str, Any]]) -> None:
    """2-i gate: the reading creates one proposal carrying every auto-coded line and no business record."""
    bill = _bill(bills, "digital")
    partner_id = AE.build_world(env, _eu(env), bill["world"])
    content, media, name = render.render(bill["spec"])
    attachment = clerk["core.attachment"].create({"name": name, "mimetype": media, "raw": content})
    before = _counts(env)
    out = B.read_document(clerk, content, media, name, attachment_id=attachment.ids[0])
    assert _counts(env) == before
    assert out["ai_generated"] and out["status"] == "ok"
    data = out["data"]
    assert data["method"] == "text" and data["partner"] == {
        "id": partner_id,
        "display_name": bill["gold"]["partner_name"],
        "method": "vat",
        "confidence": 1.0,
    }
    assert not data["needs_review"] and not data["checks"]["differences"]
    assert len(out["proposals"]) == 1
    proposal = out["proposals"][0]
    assert proposal["kind"] == "action" and proposal["state"] == "pending" and proposal["ai_generated"]
    lines = proposal["payload"]["args"]["values"]["invoice_line_ids"]
    assert len(lines) == len(bill["gold"]["lines"]) == len(data["lines"])
    for cmd, gold in zip(lines, bill["gold"]["lines"], strict=True):
        assert cmd[0] == "create"
        assert cmd[1]["account_id"] == chart_ref(_eu(env), gold["account"]).ids[0]
        assert cmd[1]["tax_ids"] == [["set", chart_ref(_eu(env), gold["tax"]).ids]]
    run = env["ai_core.run"].elevated().browse([out["run_id"]])
    assert run.feature == B.FEATURE and run.user_id.ids == clerk.user.ids and run.company_id == _eu(env)
    assert run.provider == "tessera" and run.status == "ok" and run.decision == "pending" and run.prompt_version
    # acceptance creates the draft with exactly the proposed lines, linked to its document
    accepted = B.accept(clerk, clerk["ai_core.proposal"].browse([proposal["id"]]))
    move = clerk["account.move"].browse([accepted["move_id"]])
    assert accepted["proposal"]["state"] == "accepted" and accepted["proposal"]["ai_generated"] is False
    assert move.state == "draft" and move.move_type == "in_invoice" and move.partner_id.ids == [partner_id]
    assert move.ref == bill["gold"]["invoice_number"] and not move.to_check
    assert round(float(move.amount_total), 2) == bill["gold"]["amount_total"]
    assert move.invoice_line_ids.mapped("name") == [ln["description"] for ln in bill["gold"]["lines"]]
    assert bill["gold"]["invoice_number"] in (move.ai_ocr_text or "")
    assert attachment.elevated().model == "account.move" and attachment.elevated().res_id == move.ids[0]
    assert env["ai_core.proposal"].elevated().browse([proposal["id"]]).result_res_id == move.ids[0]
    assert run.decision == "accepted"


def test_new_vendor_is_created_on_acceptance_with_its_bank_account(
    env: Any, clerk: Any, bills: list[dict[str, Any]]
) -> None:
    bill = _bill(bills, "digital", known=False)
    content, media, name = render.render(bill["spec"])
    out = B.read_document(clerk, content, media, name)
    assert out["data"]["partner"]["method"] == "new"
    args = out["proposals"][0]["payload"]["args"]
    assert args["new_partner"]["name"] == bill["gold"]["partner_name"] and "partner_id" not in args["values"]
    partners = env["contacts.partner"].search_count([("name", "=", bill["gold"]["partner_name"])])
    accepted = B.accept(clerk, clerk["ai_core.proposal"].browse([out["proposals"][0]["id"]]))
    move = clerk["account.move"].browse([accepted["move_id"]])
    assert move.partner_id.name == bill["gold"]["partner_name"]
    assert env["contacts.partner"].search_count([("name", "=", bill["gold"]["partner_name"])]) == partners + 1
    if bill["gold"]["iban"]:
        assert move.partner_id.bank_ids.mapped("sanitized_acc_number") == [bill["gold"]["iban"]]


def test_customer_invoice_and_edited_accounts(env: Any, clerk: Any, bills: list[dict[str, Any]]) -> None:
    bill = _bill(bills, "digital", move_type="out_invoice")
    AE.build_world(env, _eu(env), bill["world"])
    content, media, name = render.render(bill["spec"])
    out = B.read_document(clerk, content, media, name, move_type="out_invoice")
    assert out["data"]["move_type"] == "out_invoice"
    proposal = out["proposals"][0]
    payload = copy.deepcopy(proposal["payload"])
    other = chart_ref(_eu(env), "income_other").ids[0]
    payload["args"]["values"]["invoice_line_ids"][0][1]["account_id"] = other
    accepted = B.accept(clerk, clerk["ai_core.proposal"].browse([proposal["id"]]), payload)
    assert accepted["proposal"]["state"] == "edited"
    move = clerk["account.move"].browse([accepted["move_id"]])
    assert move.move_type == "out_invoice" and move.invoice_line_ids[0].account_id.ids == [other]


def test_edited_payload_cannot_reach_another_company_or_other_commands(
    env: Any, clerk: Any, bills: list[dict[str, Any]]
) -> None:
    bill = _bill(bills, "digital")
    content, media, name = render.render(bill["spec"])
    out = B.read_document(clerk, content, media, name)
    proposal = out["proposals"][0]
    us_account = env["account.account"].search([("company_id", "=", env.ref("core.main_company").ids[0])], limit=1)
    bad = copy.deepcopy(proposal["payload"])
    bad["args"]["values"]["invoice_line_ids"][0][1]["account_id"] = us_account.ids[0]
    failed = B.accept(clerk, clerk["ai_core.proposal"].browse([proposal["id"]]), bad)
    assert failed["proposal"]["state"] == "failed" and failed["move_id"] is None
    with pytest.raises(ValidationError):
        B._clean_values(clerk, {"move_type": "in_invoice", "invoice_line_ids": [["delete", 5]]}, create=True)
    with pytest.raises(ValidationError):
        B._clean_values(clerk, {"move_type": "entry"}, create=True)


def test_totals_mismatch_and_unknown_rate_need_review(env: Any, clerk: Any, bills: list[dict[str, Any]]) -> None:
    bill = _bill(bills, "digital")
    spec = copy.deepcopy(bill["spec"])
    spec["amount_total"] = round(spec["amount_total"] + 10, 2)  # a printed total that does not add up
    content, media, name = render.render(spec)
    out = B.read_document(clerk, content, media, name)
    # the parser cannot reconcile it, so the model is asked (synthetic answer) — the checks still apply
    assert out["data"]["needs_review"]
    foreign = next(b for b in bills if b["spec"]["kind"] == "digital" and b["gold"]["needs_review"])
    content, media, name = render.render(foreign["spec"])
    data = B.read_document(clerk, content, media, name)["data"]
    assert data["needs_review"] and any("not deductible" in (ln["issue"] or "") for ln in data["lines"])
    assert any(d["check"] == "tax" for d in data["checks"]["differences"])


def test_scan_goes_to_the_vision_route_with_the_image(env: Any, clerk: Any, bills: list[dict[str, Any]]) -> None:
    from modules.account.ai.build_evals import model_answer

    bill = _bill(bills, "scan")
    AE.build_world(env, _eu(env), bill["world"])
    _script(env, [{"match": {"feature": B.FEATURE}, "respond": {"json": model_answer(bill, None)}}])
    content, media, name = render.render(bill["spec"])
    before = _counts(env)
    out = B.read_document(clerk, content, media, name)
    assert _counts(env) == before
    assert out["data"]["method"] == "vision" and len(out["proposals"]) == 1
    sent = MockAdapter.captured[-1]
    blocks = sent["messages"][0]["content"]
    assert blocks[0]["type"] == "image" and blocks[0]["media_type"] == "image/jpeg" and blocks[-1]["type"] == "text"
    run = env["ai_core.run"].elevated().browse([out["run_id"]])
    assert run.provider and run.provider != "tessera" and "[attached:" in run.input_text
    assert out["proposals"][0]["run_id"] == run.ids[0]
    header = out["data"]["extraction"]
    assert header["invoice_number"] == bill["gold"]["invoice_number"]


def test_switched_off_feature_answers_feature_disabled_without_gateway_call(
    env: Any, clerk: Any, bills: list[dict[str, Any]]
) -> None:
    get_feature(env, B.FEATURE).set_enabled(_eu(env).ids[0], False)
    content, media, name = render.render(_bill(bills, "scan")["spec"])
    with pytest.raises(FeatureDisabled):
        B.read_document(clerk, content, media, name)
    assert not MockAdapter.captured


def test_access_rights_and_company_isolation(env: Any, env_for: Any, clerk: Any, bills: list[dict[str, Any]]) -> None:
    content, media, name = render.render(_bill(bills, "digital")["spec"])
    marie = env_for("marie").with_company(_eu(env).ids[0])  # internal user without accounting rights
    with pytest.raises(AccessError):
        B.read_document(marie, content, media, name)
    iso = next(b for b in bills if b.get("isolation"))
    assert AE.build_world(env, _eu(env), iso["world"]) is None  # the vendor belongs to the US company
    content, media, name = render.render(iso["spec"])
    out = B.read_document(clerk, content, media, name)
    assert out["data"]["partner"]["method"] == "new"  # never matched to a partner the clerk cannot read
    with pytest.raises(ValidationError):
        B.read_document(clerk, b"<html>not a document</html>", "text/html", "x.html")


def test_fill_an_empty_draft(env: Any, clerk: Any, bills: list[dict[str, Any]]) -> None:
    bill = _bill(bills, "digital")
    draft = clerk["account.move"].create({"move_type": "in_invoice"})
    content, media, name = render.render(bill["spec"])
    out = B.read_document(clerk, content, media, name, target=draft)
    proposal = out["proposals"][0]
    assert proposal["res_id"] == draft.ids[0] and not draft.invoice_line_ids
    accepted = B.accept(clerk, clerk["ai_core.proposal"].browse([proposal["id"]]))
    assert accepted["move_id"] == draft.ids[0] and len(draft.invoice_line_ids) == len(bill["gold"]["lines"])
    with pytest.raises(UserError):
        B.read_document(clerk, content, media, name, target=draft)  # no longer empty


def test_media_type_sniffing() -> None:
    assert B.media_type(b"%PDF-1.7", "image/png") == "application/pdf"
    assert B.media_type(b"\x89PNG\r\n", None) == "image/png"
    assert B.media_type(b"\xff\xd8\xff\xe0", None) == "image/jpeg"
    assert B.media_type(b"RIFF1234WEBP", None) == "image/webp"
    assert B.media_type(b"GIF89a", None) == "image/gif"
    assert B.media_type(b"hello", "text/plain; charset=utf-8") == "text/plain"


def test_api_read_reject_and_flag(admin: Session, app: Any, bills: list[dict[str, Any]]) -> None:
    from tessera.orm.environment import environment_for

    bill = _bill(bills, "digital", known=False)
    content, media, name = render.render(bill["spec"])
    resp = admin.post(
        "/api/v1/account/ai/bills/read", files={"file": (name, content, media)}, data={"move_type": "in_invoice"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ai_generated"] and len(body["proposals"]) == 1 and body["data"]["lines"]
    pid = body["proposals"][0]["id"]
    assert admin.post(f"/api/v1/ai/proposals/{pid}/reject", json={"reason": "test"}).status_code == 200
    again = admin.post("/api/v1/account/ai/bills/read", files={"file": (name, content, media)}).json()
    accepted = admin.post(f"/api/v1/account/ai/bills/proposals/{again['proposals'][0]['id']}/accept", json={})
    assert accepted.status_code == 200 and accepted.json()["move_id"]
    move_id = accepted.json()["move_id"]
    att = admin.get(
        "/api/v1/models/core.attachment",
        params={"domain": f'[["model", "=", "account.move"], ["res_id", "=", {move_id}]]'},
    )
    assert att.status_code == 200 and att.json()["items"]
    assert admin.delete(f"/api/v1/models/account.move/{move_id}").status_code in (200, 204)
    bad = admin.post("/api/v1/account/ai/bills/read", files={"file": ("x.html", b"<html></html>", "text/html")})
    assert bad.status_code in (400, 422)
    with environment_for() as e:
        company = e.ref("core.main_company").ids[0]
    flag = f"/api/v1/ai/admin/features/{B.FEATURE}/flag"
    admin.client.put(flag, json={"company_id": company, "enabled": False}, headers=admin.headers)
    try:
        off = admin.post("/api/v1/account/ai/bills/read", files={"file": (name, content, media)})
        assert off.status_code == 404 and off.json()["code"] == "feature_disabled"
    finally:
        admin.client.put(flag, json={"company_id": company, "enabled": True}, headers=admin.headers)
    demo = Session(TestClient(app, base_url="http://localhost"), "demo", "demo1234")
    denied = demo.post("/api/v1/account/ai/bills/read", files={"file": (name, content, media)})
    assert denied.status_code == 403
