"""Quotations: totals, numbering, sending, customer acceptance by token, expiry, workflow."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from tessera.exceptions import ConflictError, UserError

pytestmark = pytest.mark.db


@pytest.fixture
def quote(env: Any) -> Any:
    customer = env["contacts.partner"].create({"name": "Quote Customer", "email": "buyer@customer.example"})
    arm = env.ref("product.product_robot_arm")
    q = env["sale.quotation"].create(
        {
            "partner_id": customer.ids[0],
            "line_ids": [
                ("create", {"display_type": "section", "name": "Hardware"}),
                ("create", {"product_id": arm.ids[0], "quantity": 2, "discount": 10}),
                ("create", {"name": "Install", "quantity": 3, "price_unit": 100, "tax_percent": 0}),
                ("create", {"name": "Extended warranty", "quantity": 1, "price_unit": 999, "is_optional": True}),
            ],
        }
    )
    return q


def test_totals_numbering_and_product_defaults(quote: Any, env: Any) -> None:
    assert quote.name.startswith(f"QT/{dt.date.today().year}/")
    arm_line = quote.line_ids.filtered(lambda line: line.product_id)
    assert arm_line.price_unit == 18900 and arm_line.tax_percent == 20 and arm_line.uom_id.name == "Unit(s)"
    assert arm_line.price_subtotal == pytest.approx(34020.0)  # 2 x 18900 - 10 %
    assert quote.amount_untaxed == pytest.approx(34320.0)  # + 300 install; optional line excluded
    assert quote.amount_tax == pytest.approx(6804.0)
    assert quote.amount_total == pytest.approx(41124.0)
    assert quote.state == "draft" and quote.validity_date == dt.date.today() + dt.timedelta(days=30)


def test_send_queues_email_with_portal_link_and_pdf(quote: Any, env: Any) -> None:
    out = quote.action_send()
    assert quote.state == "sent" and quote.sent_at
    assert out["portal_url"].endswith(f"/portal/quotes/{quote.ids[0]}?token={quote.elevated().access_token}")
    mail = env["core.mail_queue"].elevated().search([("model", "=", "sale.quotation"), ("res_id", "=", quote.ids[0])])
    assert mail.email_to == "buyer@customer.example" and quote.name in mail.subject
    assert "token=" in mail.body_html and mail.reply_to.startswith("reply+")
    assert mail.attachment_ids.mimetype == "application/pdf" and mail.attachment_ids.raw.startswith(b"%PDF")


def test_send_requires_lines_and_email(env: Any) -> None:
    nobody = env["contacts.partner"].create({"name": "No Email"})
    empty = env["sale.quotation"].create({"partner_id": nobody.ids[0]})
    with pytest.raises(UserError, match="at least one line"):
        empty.action_send()
    empty.write({"line_ids": [("create", {"name": "x", "price_unit": 1})]})
    with pytest.raises(UserError, match="no email"):
        empty.action_send()


def test_state_follows_the_workflow(quote: Any) -> None:
    with pytest.raises(ConflictError):
        quote.write({"state": "accepted"})  # direct writes are refused
    with pytest.raises(UserError):
        quote.accept("Someone")  # not sent yet
    quote.action_send()
    quote.action_cancel()
    assert quote.state == "cancelled"
    quote.action_draft()
    assert quote.state == "draft"


def test_accept_and_reject_rules(quote: Any, env: Any) -> None:
    quote.action_send()
    with pytest.raises(UserError, match="full name"):
        quote.accept("  ")
    quote.accept("Jordan Lee", "jordan@acme.example", "203.0.113.9")
    assert (quote.state, quote.signed_by, quote.signed_ip) == ("accepted", "Jordan Lee", "203.0.113.9")
    with pytest.raises(UserError):
        quote.reject("too late")
    audit = env["core.audit_log"].elevated().search([("model", "=", "sale.quotation"), ("res_id", "=", quote.ids[0])])
    assert any("accepted" in str(a.changes) for a in audit)


def test_expiry_automation(quote: Any, env: Any) -> None:
    from modules.workflow.engine import run_time_automations

    quote.action_send()
    auto = env.ref("sale.auto_expire")
    auto.elevated().write({"last_time_check": env.now() - dt.timedelta(days=60)})
    quote.elevated().write(
        {"date_order": env.now() - dt.timedelta(days=40), "validity_date": env.today() - dt.timedelta(days=3)}
    )
    with pytest.raises(UserError, match="expired"):
        quote.accept("Late Buyer")
    run_time_automations(env.elevated())
    assert quote.state == "expired"


def test_pdf_render(quote: Any, env: Any) -> None:
    html, _, filename = env.ref("sale.report_quotation").render(quote.ids, "html")
    text = html.decode()
    assert filename == f"Quotation {quote.name.replace('/', '')}.html" or filename.startswith("Quotation")
    assert "Hardware" in text and "Extended warranty" in text and "(optional)" in text and "41,124.00" in text


def test_token_link_flow(client, admin) -> None:  # type: ignore[no-untyped-def]
    partner = admin.post(
        "/api/v1/models/contacts.partner", json={"values": {"name": "Token Buyer", "email": "tb@example.test"}}
    ).json()
    q = admin.post(
        "/api/v1/models/sale.quotation",
        json={
            "values": {
                "partner_id": partner["id"],
                "line_ids": [["create", {"name": "Thing", "price_unit": 50, "quantity": 2}]],
            }
        },
    ).json()
    hidden = client.get(f"/api/v1/sale/quotes/{q['id']}", params={"token": "nope"})
    assert hidden.status_code == 404  # drafts are never exposed
    sent = admin.post(f"/api/v1/models/sale.quotation/{q['id']}/call/action_send", json={}).json()["result"]
    token = sent["portal_url"].split("token=")[1]
    assert client.get(f"/api/v1/sale/quotes/{q['id']}", params={"token": "wrong"}).status_code == 401
    assert client.get(f"/api/v1/sale/quotes/{q['id']}").status_code == 401
    view = client.get(f"/api/v1/sale/quotes/{q['id']}", params={"token": token}).json()
    assert view["state"] == "sent" and view["amount_total"] == 100 and view["lines"][0]["name"] == "Thing"
    pdf = client.get(f"/api/v1/sale/quotes/{q['id']}/pdf", params={"token": token})
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    accepted = client.post(f"/api/v1/sale/quotes/{q['id']}/accept", json={"name": "Tia Buyer", "token": token}).json()
    assert accepted["state"] == "accepted" and accepted["signed_by"] == "Tia Buyer"
    again = client.post(f"/api/v1/sale/quotes/{q['id']}/reject", json={"token": token})
    assert again.status_code == 400
