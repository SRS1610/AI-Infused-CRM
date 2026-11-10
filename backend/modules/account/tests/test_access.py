"""Access rights, company isolation (U-4), customer access to invoices, CLI and demo scenario."""

from __future__ import annotations

from typing import Any

import pytest

from modules.account.tests.common import D, eu_env, invoice, partner
from tessera.exceptions import AccessError, UserError

pytestmark = pytest.mark.db


def _user(env: Any, login: str, groups: list[str], company: Any | None = None) -> Any:
    company = company or env.company
    return env["core.user"].create(
        {
            "name": login,
            "login": login,
            "company_id": company.ids[0],
            "company_ids": [("set", company.ids)],
            "group_ids": [("set", [env.ref(g).ids[0] for g in groups])],
        }
    )


def test_billing_user_invoices_but_cannot_configure(env):
    clerk = env.with_user(_user(env, "billing@test", ["account.group_account_invoice"]).ids[0])
    move = invoice(clerk, partner(clerk), [{"price_unit": 10.0}])
    assert move.state == "posted"
    with pytest.raises(AccessError):
        clerk["account.account"].create({"code": "9999", "name": "Nope", "account_type": "expense"})
    with pytest.raises(AccessError):
        clerk["account.tax"].search([], limit=1).write({"amount": 99})


def test_readonly_user_sees_but_cannot_create(env):
    move = invoice(env, partner(env), [{"price_unit": 10.0}])
    auditor = env.with_user(_user(env, "auditor@test", ["account.group_account_readonly"]).ids[0])
    assert auditor["account.move"].browse(move.ids).name == move.name
    with pytest.raises(AccessError):
        auditor["account.move"].create({"move_type": "out_invoice"})


def test_plain_users_cannot_read_invoices(env):
    invoice(env, partner(env), [{"price_unit": 10.0}])
    user = env.with_user(_user(env, "plain@test", ["core.group_user"]).ids[0])
    with pytest.raises(AccessError):
        user["account.move"].search([])
    assert user["account.tax"].search_count([]) > 0  # configuration is readable


def test_company_isolation(env):
    """U-4: a user of the US company sees no record of the EU company."""
    e = eu_env(env)
    eu_invoice = invoice(e, partner(e, "Lyon SA"), [{"price_unit": 10.0}])
    us_user = env.with_user(
        _user(env, "us-accountant@test", ["account.group_account_user"], env.ref("core.main_company")).ids[0]
    )
    assert not us_user["account.move"].search([("id", "=", eu_invoice.ids[0])])
    assert not us_user["account.move_line"].search([("move_id", "=", eu_invoice.ids[0])])
    assert not us_user["account.account"].search([("company_id", "=", e.company.ids[0])])
    assert not us_user["account.journal"].search([("company_id", "=", e.company.ids[0])])
    with pytest.raises(AccessError):
        us_user["account.move"].browse(eu_invoice.ids).read(["name"])


def test_portal_user_sees_only_own_posted_invoices(env):
    customer = partner(env, "Portal Customer Inc")
    other = partner(env, "Someone Else")
    mine = invoice(env, customer, [{"price_unit": 10.0}])
    draft = invoice(env, customer, [{"price_unit": 20.0}], post=False)
    theirs = invoice(env, other, [{"price_unit": 30.0}])
    contact = env["contacts.partner"].create(
        {"name": "Pat Portal", "parent_id": customer.ids[0], "email": "pat@portal.test"}
    )
    portal = env["core.user"].create(
        {
            "name": "Pat Portal",
            "login": "pat@portal.test",
            "partner_id": contact.ids[0],
            "group_ids": [("set", [env.ref("core.group_portal").ids[0]])],
        }
    )
    penv = env.with_user(portal.ids[0])
    visible = penv["account.move"].search([])
    assert mine.ids[0] in visible.ids
    assert draft.ids[0] not in visible.ids and theirs.ids[0] not in visible.ids
    with pytest.raises(AccessError):
        penv["account.move"].browse(mine.ids).write({"ref": "x"})


def test_new_company_gets_a_chart(env):
    de = env["core.country"].search([("code", "=", "DE")])
    eur = env["core.currency"].search([("name", "=", "EUR")])
    company = env["core.company"].create({"name": "Berlin Branch", "country_id": de.ids[0], "currency_id": eur.ids[0]})
    assert company.chart_template == "eu"
    assert env.ref(f"account.{company.ids[0]}_sale_vat_20").elevated().company_id == company
    from modules.account.chart import install_chart

    assert install_chart(company) == {"accounts": 0, "taxes": 0, "journals": 0, "fiscal_positions": 0}
    with pytest.raises(UserError):
        install_chart(company, "us")


def test_bank_account_iban_check(env):
    holder = partner(env, "IBAN Holder")
    good = env["account.bank_account"].create(
        {"partner_id": holder.ids[0], "acc_number": "FR76 3000 6000 0112 3456 7890 189"}
    )
    assert good.sanitized_acc_number == "FR7630006000011234567890189"
    from tessera.exceptions import ValidationError

    with pytest.raises(ValidationError):
        env["account.bank_account"].create(
            {"partner_id": holder.ids[0], "acc_number": "FR76 3000 6000 0112 3456 7890 188"}
        )


def test_variants_share_accounting_fields(env):
    tshirt = env.ref("product.product_tshirt", raise_if_not_found=False)
    if tshirt is None:
        pytest.skip("no demo variants")
    tax = env.company.default_sale_tax_id
    tshirt.write({"taxes_id": [("set", tax.ids)]})
    variant = tshirt.variant_ids[:1]
    assert variant.taxes_id == tax
    with pytest.raises(UserError):
        variant.write({"taxes_id": [("set", [])]})


def test_invoice_token_link(client, admin):  # type: ignore[no-untyped-def]
    p = admin.post(
        "/api/v1/models/contacts.partner", json={"values": {"name": "Link Buyer", "email": "lb@example.test"}}
    ).json()
    move = admin.post(
        "/api/v1/models/account.move",
        json={
            "values": {
                "move_type": "out_invoice",
                "partner_id": p["id"],
                "invoice_line_ids": [["create", {"name": "Widget", "price_unit": 40, "quantity": 2}]],
            }
        },
    ).json()
    assert client.get(f"/api/v1/account/invoices/{move['id']}", params={"token": "x"}).status_code == 404  # draft
    admin.post(f"/api/v1/models/account.move/{move['id']}/call/action_post", json={})
    sent = admin.post(f"/api/v1/models/account.move/{move['id']}/call/action_send", json={}).json()["result"]
    token = sent["portal_url"].split("token=")[1]
    assert client.get(f"/api/v1/account/invoices/{move['id']}", params={"token": "wrong"}).status_code == 401
    assert client.get(f"/api/v1/account/invoices/{move['id']}").status_code == 401
    view = client.get(f"/api/v1/account/invoices/{move['id']}", params={"token": token}).json()
    assert view["amount_untaxed"] == 80 and view["lines"][0]["name"] == "Widget"
    assert view["payment_state"] == "not_paid"
    pdf = client.get(f"/api/v1/account/invoices/{move['id']}/pdf", params={"token": token})
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    # an internal user with read access opens it without a token
    assert admin.get(f"/api/v1/account/invoices/{move['id']}").status_code == 200


def test_register_payment_through_the_api(admin):  # type: ignore[no-untyped-def]
    p = admin.post("/api/v1/models/contacts.partner", json={"values": {"name": "API Payer"}}).json()
    move = admin.post(
        "/api/v1/models/account.move",
        json={
            "values": {
                "move_type": "out_invoice",
                "partner_id": p["id"],
                "invoice_line_ids": [["create", {"name": "X", "price_unit": 100}]],
            }
        },
    ).json()
    admin.post(f"/api/v1/models/account.move/{move['id']}/call/action_post", json={})
    res = admin.post(
        f"/api/v1/models/account.move/{move['id']}/call/action_register_payment",
        json={"kwargs": {"amount": 30, "date": "2026-11-01"}},
    ).json()["result"]
    assert len(res["payment_ids"]) == 1
    state = admin.get(
        f"/api/v1/models/account.move/{move['id']}", params={"fields": "payment_state,amount_residual"}
    ).json()
    assert state["payment_state"] == "partial" and state["amount_residual"] == 70


def test_demo_scenario_and_integrity_cli(env, system_env):
    from typer.testing import CliRunner

    from modules.account.demo import seed_company
    from modules.account.integrity import check_company

    company = env.ref("core.main_company")
    created = seed_company(system_env, company)
    assert created >= 10
    assert seed_company(system_env, company) == 0  # idempotent
    moves = env["account.move"].search([("ref", "like", "DEMO-"), ("company_id", "=", company.ids[0])])
    states = set(moves.mapped("payment_state"))
    assert {"paid", "partial", "not_paid"} <= states
    assert all(r["ok"] for r in check_company(env, company))
    from tessera.cli.account import app

    result = CliRunner().invoke(app, ["charts"])
    assert result.exit_code == 0 and "generic" in result.output and "eu" in result.output


def test_lock_dates_only_for_advisers(env):
    clerk = env.with_user(_user(env, "lock-clerk@test", ["account.group_account_user"]).ids[0])
    with pytest.raises(UserError):
        clerk.company.write({"fiscalyear_lock_date": D(2026, 1, 31)})
    env.company.write({"fiscalyear_lock_date": D(2026, 1, 31)})
    assert env.company.fiscalyear_lock_date == D(2026, 1, 31)
