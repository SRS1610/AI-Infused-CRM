"""Payment reminders: levels, exclusions, no double sending, emails with pay links, fees, letters, access."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from modules.account.tests.common import eu_env, invoice, partner
from modules.account_dunning import dunning
from modules.account_dunning.demo import install_levels
from tessera.exceptions import AccessError, UserError, ValidationError

pytestmark = pytest.mark.db


def _late(env: Any, customer: Any, amount: float, days: int) -> Any:
    day = env.today() - dt.timedelta(days=days)
    return invoice(env, customer, [{"price_unit": amount}], invoice_date=day)


def _run(env: Any, day: dt.date | None = None) -> Any:
    return dunning.prepare_run(env.company, day)


@pytest.fixture
def levels(env):  # type: ignore[no-untyped-def]
    return install_levels(env.company)


def _line(run: Any, customer: Any) -> Any:
    return run.line_ids.filtered(lambda r: r.partner_id == customer.commercial_partner_id)


def test_levels_by_days_overdue(env, levels):  # type: ignore[no-untyped-def]
    a = partner(env, "Late Seven", email="seven@example.test")
    b = partner(env, "Late Forty", email="forty@example.test")
    c = partner(env, "Late Ninety", email="ninety@example.test")
    _late(env, a, 100.0, 10)
    inv_b1 = _late(env, b, 200.0, 45)
    _late(env, b, 50.0, 0)  # due today: not overdue yet, not listed
    _late(env, c, 300.0, 90)
    run = _run(env)
    assert _line(run, a).level_id.delay_days == 7
    line_b = _line(run, b)
    assert line_b.level_id.delay_days == 30 and line_b.invoice_ids == inv_b1
    assert line_b.overdue_amount == pytest.approx(200.0) and line_b.max_days_overdue == 45
    assert _line(run, c).level_id.delay_days == 60
    assert inv_b1.name in line_b.message and "/portal/invoices/" in line_b.message and "Late Forty" in line_b.message
    assert "Second reminder" in line_b.subject or "$" in line_b.subject or "overdue" in line_b.subject


def test_exclusions_blocked_items_in_payment_and_minimum(env, levels):  # type: ignore[no-untyped-def]
    excluded = partner(env, "Never Remind", email="n@example.test", dunning_excluded=True)
    _late(env, excluded, 100.0, 20)
    disputed = partner(env, "Disputed", email="d@example.test")
    inv = _late(env, disputed, 100.0, 20)
    inv.line_ids.filtered(lambda r: r.display_type == "payment_term").write({"blocked": True})
    paying = partner(env, "In Payment", email="p@example.test")
    inv_p = _late(env, paying, 100.0, 20)
    bank = env["account.journal"].search([("type", "=", "bank"), ("company_id", "=", env.company.ids[0])], limit=1)
    bank.write({"payment_through_outstanding": True})
    inv_p.action_register_payment(journal_id=bank.ids[0])
    assert inv_p.payment_state == "in_payment"
    small = partner(env, "Small Balance", email="s@example.test")
    _late(env, small, 4.0, 20)
    env.company.write({"dunning_min_amount": 5.0})
    run = _run(env)
    reminded = set(run.line_ids.mapped("partner_id").ids) if run else set()
    for p in (excluded, disputed, paying, small):
        assert p.ids[0] not in reminded


def test_send_once_per_level_and_escalate(env, levels):  # type: ignore[no-untyped-def]
    customer = partner(env, "Escalating", email="esc@example.test")
    _late(env, customer, 120.0, 35)
    run = _run(env)
    result = run.action_send()
    line = _line(run, customer)
    assert line.state == "sent" and line.mail_id and result["sent"] >= 1 and run.state == "done"
    assert line.mail_id.email_to == "esc@example.test" and "/portal/invoices/" in line.mail_id.body_html
    with pytest.raises(UserError):
        run.action_send()
    with pytest.raises(UserError):
        run.unlink()
    # the same level is not sent again within its delay
    again = _run(env)
    assert not again or not _line(again, customer)
    later = env.today() + dt.timedelta(days=10)
    assert not (r := dunning.prepare_run(env.company, later)) or not _line(r, customer)
    # 30 days later the 60-day level is reached and sent
    much_later = env.today() + dt.timedelta(days=30)
    escalated = dunning.prepare_run(env.company, much_later)
    assert _line(escalated, customer).level_id.delay_days == 60


def test_fee_invoice_and_letter(env, levels):  # type: ignore[no-untyped-def]
    customer = partner(env, "Fee Payer", email=None, street="1 Main St", city="Springfield")
    _late(env, customer, 500.0, 70)
    run = _run(env)
    line = _line(run, customer)
    assert line.state == "no_email"
    run.action_send()
    fee = line.fee_move_id
    assert fee.state == "posted" and fee.amount_total == pytest.approx(15.0) and fee.partner_id == customer
    assert line.state == "letter" and run.letter_id and run.letter_id.mimetype == "application/pdf"
    assert bytes(run.letter_id.raw).startswith(b"%PDF")


def test_renderer_can_be_replaced(env, levels):  # type: ignore[no-untyped-def]
    customer = partner(env, "Custom Text", email="ct@example.test")
    _late(env, customer, 80.0, 12)
    seen: list[dunning.ReminderContext] = []

    def proposal(ctx: dunning.ReminderContext) -> tuple[str, str]:
        seen.append(ctx)
        return "Personal note", "<p>Dear friend</p>" + dunning.invoice_table(ctx)

    dunning.set_renderer(proposal)
    try:
        run = _run(env)
    finally:
        dunning.set_renderer(None)
    line = _line(run, customer)
    assert line.subject == "Personal note" and "Dear friend" in line.message
    ctx = next(c for c in seen if c.partner == customer.commercial_partner_id)
    assert ctx.amount_due == pytest.approx(80.0) and ctx.max_days == 12


def test_run_from_the_form_and_job(env, levels):  # type: ignore[no-untyped-def]
    customer = partner(env, "Form Run", email="fr@example.test")
    _late(env, customer, 64.0, 9)
    run = env["account.dunning_run"].create({})
    assert run.action_prepare() >= 1 and _line(run, customer)
    with pytest.raises(UserError):
        run.action_prepare()
    empty_day = env.today() - dt.timedelta(days=3650)
    empty = env["account.dunning_run"].create({"date": empty_day})
    with pytest.raises(UserError):
        empty.action_prepare()
    other = partner(env, "Job Run", email="jr@example.test")
    _late(env, other, 33.0, 8)
    result = dunning.dunning_job(env, send=False)
    assert result[env.company.name]["prepared"] >= 1


def test_level_constraints_and_access(env, levels):  # type: ignore[no-untyped-def]
    with pytest.raises(ValidationError):
        env["account.dunning_level"].create({"name": "Bad", "delay_days": -1})
    user = env["core.user"].create(
        {
            "name": "dunning clerk",
            "login": "dunning-clerk@test",
            "group_ids": [("set", [env.ref("account.group_account_invoice").ids[0]])],
        }
    )
    clerk = env.with_user(user.ids[0])
    assert clerk["account.dunning_level"].search_count([]) >= 3
    with pytest.raises(AccessError):
        clerk["account.dunning_level"].create({"name": "Nope", "delay_days": 99})
    plain = env["core.user"].create({"name": "plain", "login": "dunning-plain@test"})
    with pytest.raises(AccessError):
        env.with_user(plain.ids[0])["account.dunning_run"].search([])


def test_company_isolation(env, levels):  # type: ignore[no-untyped-def]
    e = eu_env(env)
    install_levels(e.company)
    eu_customer = partner(e, "EU Late", email="eu@example.test")
    _late(e, eu_customer, 90.0, 20)
    eu_run = dunning.prepare_run(e.company)
    assert _line(eu_run, eu_customer)
    us_run = dunning.prepare_run(env.ref("core.main_company"))
    assert not us_run or not _line(us_run, eu_customer)
    user = env["core.user"].create(
        {
            "name": "us dunning",
            "login": "dunning-us@test",
            "company_id": env.ref("core.main_company").ids[0],
            "company_ids": [("set", env.ref("core.main_company").ids)],
            "group_ids": [("set", [env.ref("account.group_account_user").ids[0]])],
        }
    )
    us = env.with_user(user.ids[0])
    assert not us["account.dunning_run"].search([("id", "=", eu_run.ids[0])])
    assert not us["account.dunning_level"].search([("company_id", "=", e.company.ids[0])])


def test_demo(env, system_env):  # type: ignore[no-untyped-def]
    from modules.account_dunning.demo import seed_company

    run = seed_company(system_env, env.ref("core.main_company"))
    assert run is not None and run.state == "draft" and run.line_ids
    assert {lv.delay_days for lv in env["account.dunning_level"].search([("company_id", "=", env.company.ids[0])])} >= {
        7,
        30,
        60,
    }
    assert seed_company(system_env, env.ref("core.main_company")) is None
