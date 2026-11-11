"""Subscription behaviours around the fixture: periods arithmetic, starting, idempotent invoicing, one-off
lines, guards on actions, upsell edge cases, renewals, payment failures, the job and its entry points."""

from __future__ import annotations

from typing import Any

import pytest

from modules.sale_subscription import billing
from modules.sale_subscription.periods import (
    grid_date,
    monthly_factor,
    next_start_on_or_after,
    period_of,
    remaining_ratio,
    renewal_term,
    shift,
)
from modules.sale_subscription.tests.common import (
    D,
    at,
    customer,
    docs,
    history,
    plan,
    recurring,
    run,
    subscription,
    token,
)
from tessera.exceptions import UserError, ValidationError

pytestmark = pytest.mark.db


def _sub(env: Any, start: Any = D(2026, 3, 1), price: float = 100.0, **vals: Any) -> Any:
    the_plan = vals.pop("the_plan", None) or plan(env)
    lines = vals.pop("lines", None) or [{"product_id": recurring(env, price=price).ids[0], "quantity": 1}]
    return subscription(env, customer(env), the_plan, lines, start, **vals)


def test_periods_arithmetic() -> None:
    assert shift(D(2026, 1, 31), "month", 1) == D(2026, 2, 28)
    assert grid_date(D(2026, 1, 31), "month", 1, 2) == D(2026, 3, 31)
    assert period_of(D(2026, 1, 1), "month", 3, D(2026, 4, 1)) == (D(2026, 4, 1), D(2026, 6, 30), 91)
    assert period_of(D(2026, 1, 1), "month", 1, D(2026, 1, 15))[1] == D(2026, 2, 14)  # off the grid
    assert next_start_on_or_after(D(2026, 1, 1), "week", 2, D(2026, 1, 20)) == D(2026, 1, 29)
    assert next_start_on_or_after(D(2026, 1, 1), "month", 1, D(2025, 12, 1)) == D(2026, 1, 1)
    assert next_start_on_or_after(D(2020, 1, 31), "month", 1, D(2026, 3, 1)) == D(2026, 3, 31)
    assert monthly_factor("week", 1) == pytest.approx(52 / 12) and monthly_factor("year", 2) == 1 / 24
    assert monthly_factor("month", 3) == pytest.approx(1 / 3)
    assert remaining_ratio(D(2026, 3, 16), D(2026, 3, 31), 31) == (16, 16 / 31)
    assert remaining_ratio(D(2026, 4, 2), D(2026, 3, 31), 31) == (0, 0.0)
    assert renewal_term(D(2026, 1, 1), D(2026, 12, 31)) == (D(2027, 1, 1), D(2027, 12, 31))
    assert renewal_term(D(2026, 1, 10), D(2026, 2, 3)) == (D(2026, 2, 4), D(2026, 2, 28))
    with pytest.raises(ValueError):
        shift(D(2026, 1, 1), "day", 1)


def test_start_later_and_manual_start(env: Any) -> None:
    sub = _sub(env, D(2026, 3, 10), confirm=False)
    assert sub.subscription_state == "draft" and sub.recurring_monthly == 100
    early = at(env, D(2026, 3, 1))["sale.quotation"].browse(sub.ids)
    early.action_start()  # confirms the quotation; the subscription waits for its start date
    with pytest.raises(UserError, match="starts on"):
        early.action_start()
    assert sub.state == "accepted" and sub.subscription_state == "draft"
    assert run(env, D(2026, 3, 9))["started"] == 0
    summary = run(env, D(2026, 3, 10))
    assert summary["started"] == 1 and summary["invoices"] == 1 and sub.subscription_state == "progress"
    with pytest.raises(UserError, match="already started"):
        sub.action_start()
    plain = env["sale.quotation"].create({"partner_id": customer(env).ids[0], "plan_id": plan(env).ids[0]})
    with pytest.raises(UserError, match="Choose a plan"):
        plain.action_start()


def test_manual_start_on_the_day(env: Any) -> None:
    sub = _sub(env, D(2026, 3, 10), confirm=False)
    sub.with_env(at(env, D(2026, 3, 1))).action_confirm()
    assert sub.subscription_state == "draft"
    sub.with_env(at(env, D(2026, 3, 12))).action_start()
    assert sub.subscription_state == "progress" and history(sub) == [("2026-03-10", "new", 100.0)]


def test_one_off_lines_on_first_invoice_and_regular_invoicing_skips_recurring(env: Any) -> None:
    seat = recurring(env, "Seat", 40)
    setup = env["product.product"].create(
        {"name": "Setup", "list_price": 150, "type": "service", "taxes_id": [("set", [])]}
    )
    sub = _sub(
        env,
        lines=[{"product_id": seat.ids[0], "quantity": 2}, {"product_id": setup.ids[0], "quantity": 1}],
    )
    assert sub.recurring_total == 80 and sub.recurring_monthly == 80
    seat_line = sub.line_ids.filtered(lambda r: r.product_id == seat)
    assert seat_line.is_recurring and seat_line.qty_to_invoice == 0 and seat_line.invoice_status == "no"
    run(env, D(2026, 3, 1))
    run(env, D(2026, 4, 1))
    first, second = docs(sub)
    assert (first.amount_total, second.amount_total) == (230.0, 80.0)
    assert "2026-03-01 to 2026-03-31" in first.invoice_line_ids[0].name
    assert sub.invoice_count == 2 and sub.subscription_invoice_count == 2 and not sub.locked
    with pytest.raises(UserError, match="nothing to invoice"):
        sub.action_create_invoice()


def test_invoicing_is_idempotent_and_cancel_frees_the_period(env: Any) -> None:
    sub = _sub(env)
    first = billing.invoice_period(sub.elevated(), D(2026, 3, 1))
    assert first is not None and billing.invoice_period(sub.elevated(), D(2026, 3, 1)) is None
    first.button_cancel()
    again = billing.invoice_period(sub.elevated(), D(2026, 3, 1))
    assert again is not None and again != first


def test_manual_invoicing_action_and_manual_next_date(env: Any) -> None:
    sub = _sub(env)
    result = sub.with_env(at(env, D(2026, 4, 2))).action_create_recurring_invoices()
    assert len(result["invoice_ids"]) == 2 and sub.next_invoice_date == D(2026, 5, 1)
    sub.write({"next_invoice_date": "2026-05-15"})
    assert sub.billing_anchor_date == D(2026, 5, 15)
    sub.with_env(at(env, D(2026, 5, 15))).action_create_recurring_invoices()
    assert docs(sub)[-1].subscription_period_end == D(2026, 6, 14)


def test_guards(env: Any) -> None:
    sub = _sub(env)
    with pytest.raises(UserError, match="close it instead"):
        sub.action_cancel()
    with pytest.raises(UserError, match="plan of a started"):
        sub.write({"plan_id": plan(env, "year").ids[0]})
    with pytest.raises(UserError, match="this action is not possible"):
        sub.action_resume()
    with pytest.raises(UserError, match="Choose why"):
        sub.action_close()
    with pytest.raises(UserError, match="Choose why"):
        sub.action_close(reason="nonsense")
    with pytest.raises(UserError, match="now or at the end"):
        sub.action_close(reason="price", when="tomorrow")
    with pytest.raises(UserError, match="fixed-term"):
        sub.action_renew()
    other = token(env, customer(env, "Someone else"))
    with pytest.raises(ValidationError, match="another customer"):
        sub.write({"payment_token_id": other.ids[0]})
    plain = env["sale.quotation"].create(
        {"partner_id": customer(env).ids[0], "line_ids": [("create", {"name": "x", "price_unit": 1})]}
    )
    with pytest.raises(UserError, match="not a confirmed subscription"):
        plain.action_pause()
    reason = env.ref("sale_subscription.close_reason_price")
    assert sub.action_close(reason_id=reason.ids[0], when="period_end") is True
    assert sub.close_reason_id == reason and sub.subscription_state == "churned"  # nothing invoiced: closed now
    fixed = _sub(env, end_date=D(2026, 12, 31))
    run(env, D(2026, 3, 1))
    fixed.with_env(at(env, D(2026, 3, 5))).action_close(reason="price")
    assert fixed.end_date == D(2026, 3, 31) and fixed.subscription_state == "progress"
    with pytest.raises(UserError, match="is closing"):
        fixed.action_renew()
    with pytest.raises(ValidationError, match="end date is before"):
        fixed.write({"end_date": "2026-01-01"})


def test_upsell_edge_cases(env: Any) -> None:
    sub = _sub(env)
    run(env, D(2026, 3, 1))
    e = at(env, D(2026, 3, 10))
    unchanged = e["sale.quotation"].browse([sub.with_env(e).action_upsell()["upsell_id"]])
    assert unchanged.upsell_of_id == sub and not unchanged.is_subscription
    assert unchanged.line_ids.subscription_line_id == sub.line_ids and unchanged.line_ids.qty_to_invoice == 0
    unchanged.action_confirm()  # nothing changes: no document, no history
    assert len(docs(sub)) == 1 and len(history(sub)) == 1
    stop = e["sale.quotation"].browse([sub.with_env(e).action_upsell()["upsell_id"]])
    stop.line_ids.write({"quantity": 0})
    stop.action_confirm()
    credit = docs(sub)[-1]
    assert credit.move_type == "out_refund" and credit.amount_total == 70.97  # 100 x 22/31
    assert sub.recurring_monthly == 0 and history(sub)[-1] == ("2026-03-10", "downsell", -100.0)
    sub.with_env(e).action_pause()
    paused = (
        e["sale.quotation"]
        .with_context(sale_no_suggestions=True)
        .create({"partner_id": sub.partner_id.ids[0], "upsell_of_id": sub.ids[0], "line_ids": []})
    )
    paused.write({"line_ids": [("create", {"product_id": sub.line_ids.product_id.ids[0], "quantity": 1})]})
    with pytest.raises(UserError, match="in progress"):
        paused.action_confirm()


def test_upsell_before_first_invoice_has_no_proration(env: Any) -> None:
    sub = _sub(env)
    e = at(env, D(2026, 3, 1))
    change = e["sale.quotation"].browse([sub.with_env(e).action_upsell()["upsell_id"]])
    change.line_ids.write({"quantity": 3})
    change.action_confirm()
    assert docs(sub) == [] and sub.recurring_total == 300
    run(env, D(2026, 3, 1))
    assert docs(sub)[0].amount_total == 300


def test_upsell_charges_the_saved_card(env: Any) -> None:
    partner = customer(env)
    sub = subscription(
        env,
        partner,
        plan(env),
        [{"product_id": recurring(env).ids[0], "quantity": 1}],
        D(2026, 3, 1),
        payment_token_id=token(env, partner).ids[0],
        payment_term_id=env.ref("account.term_immediate").ids[0],
    )
    run(env, D(2026, 3, 1))
    e = at(env, D(2026, 3, 16))
    change = e["sale.quotation"].browse([sub.with_env(e).action_upsell()["upsell_id"]])
    change.line_ids.write({"quantity": 2})
    change.action_confirm()
    pro = docs(sub)[-1]
    assert pro.subscription_kind == "proration" and pro.payment_state in ("paid", "in_payment")


def test_manual_renewal_and_late_acceptance(env: Any) -> None:
    sub = _sub(env, D(2026, 1, 1), end_date=D(2026, 3, 31))
    info = sub.with_env(at(env, D(2026, 3, 1))).action_renew()
    renewal = env["sale.quotation"].browse([info["renewal_id"]])
    assert renewal.is_subscription and renewal.state == "draft" and renewal.origin_order_id == sub
    assert (renewal.start_date, renewal.end_date) == (D(2026, 4, 1), D(2026, 6, 30))
    assert renewal.line_ids.price_unit == 100 and renewal.line_ids.subscription_line_id == sub.line_ids
    with pytest.raises(UserError, match="already has a renewal"):
        sub.action_renew()
    run(env, D(2026, 4, 1))  # not accepted in time: churned
    assert sub.subscription_state == "churned"
    renewal.with_env(at(env, D(2026, 4, 5))).action_confirm()  # confirmed late: a reactivation
    assert renewal.subscription_state == "progress"
    assert history(renewal) == [("2026-04-01", "reactivation", 100.0)]


def test_automatic_renewal_sends_the_quotation(env: Any) -> None:
    sub = _sub(env, D(2026, 1, 1), end_date=D(2026, 3, 31), the_plan=plan(env, renewal_reminder_days=20))
    summary = run(env, D(2026, 3, 11))
    renewal = sub.renewal_ids
    assert summary["renewal_quotes"] == 1 and renewal.state == "sent" and renewal.validity_date == D(2026, 3, 31)
    mail = env["core.mail_queue"].search([("model", "=", "sale.quotation"), ("res_id", "=", renewal.ids[0])])
    assert mail and "portal/quotes" in mail[0].body_html


def test_paused_close_has_no_credit_and_period_end_without_invoice(env: Any) -> None:
    sub = _sub(env)
    run(env, D(2026, 3, 1))
    e = at(env, D(2026, 3, 10))
    sub.with_env(e).action_pause()
    assert sub.pause_count == 1 and sub.paused_on == D(2026, 3, 10)
    sub.with_env(e).action_close(reason="price", when="now")
    assert sub.subscription_state == "churned" and len(docs(sub)) == 1
    later = _sub(env, D(2026, 3, 1))
    later.with_env(at(env, D(2026, 3, 5))).action_close(reason="quality", when="period_end")
    assert later.subscription_state == "churned" and later.closed_on == D(2026, 3, 5)  # nothing invoiced: now
    assert later.end_date == D(2026, 3, 4)


def test_payment_failures_with_a_disabled_provider(env: Any) -> None:
    partner = customer(env)
    card = token(env, partner)
    sub = subscription(
        env,
        partner,
        plan(env, payment_retry_days=1),
        [{"product_id": recurring(env).ids[0], "quantity": 1}],
        D(2026, 3, 1),
        payment_token_id=card.ids[0],
    )
    card.provider_id.write({"state": "disabled"})
    summary = run(env, D(2026, 3, 1))
    assert summary["invoices"] == 1 and sub.payment_failure_count == 1
    assert run(env, D(2026, 3, 2))["declined"] == 1 and sub.payment_failure_count == 2
    card.provider_id.write({"state": "test"})
    assert run(env, D(2026, 3, 3))["paid"] == 1
    assert run(env, D(2026, 3, 4))["paid"] == 0


def test_job_entry_points(env: Any, system_env: Any) -> None:
    from modules.sale_subscription.billing import run_recurring_job, today

    sub = _sub(env)
    result = run_recurring_job(system_env, date="2026-03-01")
    assert result["date"] == "2026-03-01" and result["invoices"] >= 1 and len(docs(sub)) == 1
    assert today(env.with_context(subscription_date=D(2026, 1, 2))) == D(2026, 1, 2)
    assert today(env) == env.today()
    out = env["sale.quotation"].with_context(subscription_date="2026-04-01").action_run_recurring()
    assert out["date"] == "2026-04-01" and len(docs(sub)) == 2
    job = env.ref("sale_subscription.sched_recurring")
    assert job.job_name == "sale_subscription.run"


def test_a_broken_subscription_does_not_stop_the_job(env: Any, monkeypatch: Any) -> None:
    good, bad = _sub(env), _sub(env)
    real = billing.invoice_period

    def flaky(order: Any, start: Any) -> Any:
        if order.ids == bad.ids:
            raise RuntimeError("boom")
        return real(order, start)

    monkeypatch.setattr(billing, "invoice_period", flaky)
    summary = run(env, D(2026, 3, 1))
    assert summary["errors"] >= 1 and len(docs(good)) == 1 and not docs(bad)
    assert bad.next_invoice_date == D(2026, 3, 1)


def test_quarterly_mrr_and_plan_count(env: Any) -> None:
    quarterly = plan(env, "month", 3)
    sub = _sub(env, the_plan=quarterly, price=300)
    assert sub.recurring_monthly == 100 and quarterly.subscription_count == 1
    run(env, D(2026, 3, 1))
    assert docs(sub)[0].subscription_period_end == D(2026, 5, 31)
    with pytest.raises(ValidationError):
        plan(env, "month", 0)
    with pytest.raises(ValidationError):
        plan(env, auto_close_limit=-1)
