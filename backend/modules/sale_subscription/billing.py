"""The subscription engine: start, recurring invoices and charges, mid-period changes, renewals, pause,
close and reactivation. Every function takes the business date ``day`` explicitly (the daily job passes
today; tests and the ``subscription_date`` context key can pass another day).

Recurring invoices. For a subscription in progress whose ``next_invoice_date`` has come, the period
``next_invoice_date … day before the next grid date`` is invoiced (``periods.py``), dated on its first day,
with the order lines' quantities, prices, discounts and taxes and the period in each description. A fixed
term ending inside a period invoices that last period prorated (covered days / period days). One-off lines
(set-up fees…) are added to the first invoice. Each invoice is posted, then charged on the saved payment
method when there is one; ``next_invoice_date`` moves to the next grid date. A unique key per subscription
and period start makes the invoicing idempotent: a period is never invoiced twice (a cancelled invoice frees
its period).

Payment failures. A declined charge leaves the invoice unpaid, counts a failure and is retried every
``payment_retry_days``; once an invoice is still unpaid ``auto_close_limit`` days after its due date the
subscription is closed with the reason *Payment failure* (churn), without credit (dunning collects the debt).

Mid-period changes (upsell / downgrade). The change order's lines replace the subscription's lines; for the
period already invoiced, each changed line is billed ``(new − old line amount) × remaining days / period
days`` (``periods.remaining_ratio``, the change day included), on one invoice when the total is positive or
one credit note when it is negative. The new lines apply to the next periods.

Closing. *At period end*: the end date becomes the last day of the invoiced period; nothing is credited.
*Now*: service stops today, and today until the period end is credited (current lines × remaining days /
period days) on a credit note matched with the period's invoice when it is still open. Paused subscriptions
are closed without credit.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from modules.sale.models.quotation import INTERNAL
from modules.sale_subscription.periods import (
    ONE_DAY,
    next_start_on_or_after,
    period_of,
    remaining_ratio,
    renewal_term,
)
from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.jobs import job
from tessera.logging import get_logger
from tessera.orm import Model

log = get_logger(__name__)
TODAY_KEY = "subscription_date"
UNPAID = ("not_paid", "partial")
OPEN_RENEWAL_STATES = ("draft", "sent", "accepted")


def today(env: Any) -> dt.date:
    """The business date: the ``subscription_date`` context key when given, else today."""
    value = env.context.get(TODAY_KEY)
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if value:
        return dt.date.fromisoformat(str(value))
    return env.today()


def _internal(order: Model) -> Model:
    return order.with_context(**{INTERNAL: True})


def _post(order: Model, body: str) -> None:
    from modules.chatter.thread import post_message

    post_message(order.env, "sale.quotation", order.ids[0], f"<p>{body}</p>", message_type="notification", notify=False)


def _plan(order: Model) -> Model:
    plan = order.elevated().plan_id
    if not plan:
        raise UserError(_("Subscription {name} has no plan", name=order.name))
    return plan


def _anchor(order: Model) -> dt.date:
    return order.billing_anchor_date or order.start_date or order.next_invoice_date


# -- MRR history ------------------------------------------------------------------------------------------


def logged_mrr(order: Model) -> float:
    """The subscription's MRR according to its history (sum of its movements)."""
    logs = order.env["sale.subscription_log"].elevated().search([("subscription_id", "=", order.ids[0])])
    return float(order.currency_id.round(sum(float(entry.amount_signed or 0.0) for entry in logs)))


def add_log(
    order: Model,
    day: dt.date,
    event_type: str,
    delta: float,
    *,
    reason: Model | None = None,
    note: str | None = None,
) -> Model | None:
    delta = float(order.currency_id.round(delta))
    if not delta and event_type not in ("churn",):
        return None
    after = logged_mrr(order) + delta
    return (
        order.env["sale.subscription_log"]
        .elevated()
        .create(
            {
                "subscription_id": order.ids[0],
                "company_id": order.company_id.ids[0],
                "partner_id": order.partner_id.commercial_partner_id.ids[0],
                "plan_id": order.plan_id.ids[0] if order.plan_id else None,
                "user_id": order.user_id.ids[0] if order.user_id else None,
                "event_date": day,
                "event_type": event_type,
                "currency_id": order.currency_id.ids[0],
                "amount_signed": delta,
                "recurring_monthly": after,
                "amount_company": order._to_company_currency(delta, day),
                "reason_id": reason.ids[0] if reason else None,
                "note": note,
            }
        )
    )


# -- documents ------------------------------------------------------------------------------------------


def _create_move(
    order: Model,
    move_type: str,
    kind: str,
    lines: list[dict[str, Any]],
    *,
    invoice_date: dt.date,
    period: tuple[dt.date, dt.date, int],
    key: str | None = None,
) -> Model:
    from modules.sale.invoicing import _move_values

    env = order.env
    vals = {
        **_move_values(order, move_type),
        "invoice_date": invoice_date,
        "date": invoice_date,
        "subscription_id": order.ids[0],
        "subscription_kind": kind,
        "subscription_period_start": period[0],
        "subscription_period_end": period[1],
        "subscription_period_days": period[2],
        "subscription_period_key": key,
        "invoice_line_ids": [("create", {**vals, "sequence": 10 + i}) for i, vals in enumerate(lines)],
    }
    move = env["account.move"].with_company(order.company_id).create(vals)
    move.action_post()
    return move


def _period_label(start: dt.date, end: dt.date) -> str:
    return str(_("{start} to {end}", start=start.isoformat(), end=end.isoformat()))


def invoiced_period(order: Model, day: dt.date) -> Model:
    """The posted period invoice whose period contains ``day`` (empty when that period is not invoiced)."""
    return (
        order.env["account.move"]
        .elevated()
        .search(
            [
                ("subscription_id", "=", order.ids[0]),
                ("subscription_kind", "=", "period"),
                ("move_type", "=", "out_invoice"),
                ("state", "=", "posted"),
                ("subscription_period_start", "<=", day),
                ("subscription_period_end", ">=", day),
            ],
            order="subscription_period_start desc, id desc",
            limit=1,
        )
    )


def invoice_period(order: Model, start: dt.date) -> Model | None:
    """Create and post the invoice of the period starting on ``start`` (None when it already exists)."""
    from modules.sale.invoicing import _line_values

    plan = _plan(order)
    key = f"{order.ids[0]}:{start.isoformat()}"
    moves = order.env["account.move"].elevated()
    if moves.search_count([("subscription_period_key", "=", key)]):
        return None
    period_start, full_end, days = period_of(_anchor(order), plan.billing_period_unit, plan.billing_period_value, start)
    end = min(full_end, order.end_date) if order.end_date else full_end
    covered = (end - period_start).days + 1
    ratio = covered / days
    currency = order.currency_id
    values: list[dict[str, Any]] = []
    for line in order._recurring_lines():
        label = _period_label(period_start, end)
        if covered < days:
            label = str(_("{period}, {days}/{total} days", period=label, days=covered, total=days))
        vals = {
            "display_type": "product",
            "name": f"{line.name} — {label}",
            "product_id": line.product_id.ids[0],
            "uom_id": line.uom_id.ids[0] if line.uom_id else None,
            "quantity": line.quantity,
            "price_unit": float(line.price_unit or 0.0)
            if covered == days
            else float(currency.round(float(line.price_unit or 0.0) * ratio)),
            "discount": line.discount or 0.0,
            "tax_ids": [("set", line.tax_ids.ids)],
            "sale_line_ids": [("set", line.ids)],
        }
        values.append(vals)
    if not values:
        return None
    one_off = order.line_ids.filtered(
        lambda r: (
            not r.is_recurring
            and not r.display_type
            and not r.is_optional
            and not r.is_downpayment
            and (r.qty_to_invoice or 0.0) > 1e-6
        )
    )
    values += [_line_values(line, line.qty_to_invoice, 0) for line in one_off]
    try:
        with order.env.cr.savepoint():
            move = _create_move(
                order,
                "out_invoice",
                "period",
                values,
                invoice_date=period_start,
                period=(period_start, end, days),
                key=key,
            )
    except Exception:
        if moves.search_count([("subscription_period_key", "=", key)]):
            return None  # a concurrent run invoiced this period first
        raise
    _post(order, str(_("Invoice {invoice} for {period}", invoice=move.name, period=_period_label(period_start, end))))
    return move


def invoice_due_periods(order: Model, day: dt.date) -> Model:
    """Invoice every period of a running subscription that has started on or before ``day``, then charge."""
    plan = _plan(order)
    created = order.env["account.move"]
    guard = 0
    while (
        order.subscription_state == "progress"
        and order.next_invoice_date
        and order.next_invoice_date <= day
        and (not order.end_date or order.next_invoice_date <= order.end_date)
        and guard < 400
    ):
        guard += 1
        start = order.next_invoice_date
        move = invoice_period(order, start)
        if move is not None:
            created |= move
        _start, full_end, _days = period_of(_anchor(order), plan.billing_period_unit, plan.billing_period_value, start)
        _internal(order).write({"next_invoice_date": full_end + ONE_DAY})
    if created and order.payment_token_id:
        charge_invoices(order, created, day)
    return created


# -- payments -------------------------------------------------------------------------------------------


def unpaid_invoices(order: Model) -> Model:
    return (
        order.env["account.move"]
        .elevated()
        .search(
            [
                ("subscription_id", "=", order.ids[0]),
                ("move_type", "=", "out_invoice"),
                ("state", "=", "posted"),
                ("payment_state", "in", list(UNPAID)),
                ("amount_residual", ">", 0),
            ],
            order="invoice_date, id",
        )
    )


def charge_invoices(order: Model, invoices: Model, day: dt.date) -> bool:
    """Charge the saved payment method for each open invoice; True when everything was paid."""
    from modules.payment.service import charge_token, payable_invoices

    token = order.payment_token_id.elevated()
    if not token:
        return False
    ok = True
    for invoice in payable_invoices(invoices.elevated()):
        message = None
        try:
            tx = charge_token(token, float(invoice.amount_residual), reference=invoice.name, invoices=invoice)
            if tx.state != "done":
                message = tx.state_message or str(_("The payment failed"))
        except UserError as exc:
            message = str(exc)
        if message is None:
            _post(order, str(_("Invoice {invoice} paid with {method}", invoice=invoice.name, method=token.display)))
            continue
        ok = False
        _internal(order).write({"payment_failure_count": (order.payment_failure_count or 0) + 1})
        _post(order, str(_("Payment of {invoice} failed: {message}", invoice=invoice.name, message=message)))
    _internal(order).write({"last_charge_date": day})
    return ok


def collect(order: Model, day: dt.date, reasons: Any) -> str | None:
    """Retry declined charges and close subscriptions unpaid for too long; returns what happened."""
    plan = _plan(order)
    unpaid = unpaid_invoices(order)
    if not unpaid:
        return None
    result = None
    retry = int(plan.payment_retry_days or 0)
    last = order.last_charge_date
    if order.payment_token_id and retry and (last is None or day >= last + dt.timedelta(days=retry)):
        result = "paid" if charge_invoices(order, unpaid, day) else "declined"
        unpaid = unpaid_invoices(order)
    limit = int(plan.auto_close_limit or 0)
    if unpaid and limit:
        due = min((m.invoice_date_due or m.invoice_date) for m in unpaid)
        closing = due + dt.timedelta(days=limit)
        if day >= closing:
            terminate(order, closing, reasons("payment_failure"), credit=False)
            result = "closed"
    return result


# -- life cycle ------------------------------------------------------------------------------------------


def confirm_subscription(order: Model, day: dt.date) -> None:
    """The order was confirmed: fix its dates and start it when its start date has come."""
    _plan(order)
    start = order.start_date or day
    _internal(order).write(
        {
            "start_date": start,
            "billing_anchor_date": order.billing_anchor_date or start,
            "next_invoice_date": order.next_invoice_date or start,
        }
    )
    if order.renewal_of_id:
        _post(order.renewal_of_id, str(_("Renewal {name} accepted", name=order.name)))
    if start <= day:
        start_subscription(order, start)
    else:
        _post(order, str(_("The subscription starts on {date}", date=start.isoformat())))


def start_subscription(order: Model, start: dt.date) -> None:
    previous = order.renewal_of_id
    event = "new"
    if previous:
        event = "reactivation" if previous.subscription_state == "churned" else "renewal"
    _internal(order).write(
        {
            "subscription_state": "progress",
            "start_date": order.start_date or start,
            "billing_anchor_date": order.billing_anchor_date or start,
            "next_invoice_date": order.next_invoice_date or start,
        }
    )
    add_log(order, start, event, float(order.recurring_monthly or 0.0) - logged_mrr(order))
    _post(order, str(_("Subscription started on {date}", date=start.isoformat())))


def pause(order: Model, day: dt.date) -> None:
    _internal(order).write({"subscription_state": "paused", "paused_on": day, "pause_count": (order.pause_count or 0) + 1})
    _post(order, str(_("Subscription paused on {date}", date=day.isoformat())))


def resume(order: Model, day: dt.date) -> None:
    plan = _plan(order)
    vals: dict[str, Any] = {"subscription_state": "progress", "paused_on": None}
    if order.next_invoice_date and order.next_invoice_date < day:
        # the periods that started while paused are skipped: billing goes on with the grid
        vals["next_invoice_date"] = next_start_on_or_after(
            _anchor(order), plan.billing_period_unit, plan.billing_period_value, day
        )
    _internal(order).write(vals)
    _post(
        order,
        str(_("Subscription resumed on {date}; next invoice on {next}", date=day.isoformat(), next=str(order.next_invoice_date))),
    )


def credit_unused(order: Model, day: dt.date) -> Model | None:
    """Credit the current lines for ``day`` until the end of the invoiced period containing it."""
    invoice = invoiced_period(order, day)
    if not invoice:
        return None
    days = int(invoice.subscription_period_days or 0)
    end = invoice.subscription_period_end
    remaining, ratio = remaining_ratio(day, end, days)
    if remaining <= 0:
        return None
    currency = order.currency_id
    lines = []
    for line in order._recurring_lines():
        price = float(currency.round(line._amount() * ratio))
        if not price:
            continue
        lines.append(
            {
                "display_type": "product",
                "name": str(
                    _(
                        "Unused days: {name} — {period}, {days}/{total} days",
                        name=line.name,
                        period=_period_label(day, end),
                        days=remaining,
                        total=days,
                    )
                ),
                "product_id": line.product_id.ids[0],
                "quantity": 1.0,
                "price_unit": price,
                "tax_ids": [("set", line.tax_ids.ids)],
                "sale_line_ids": [("set", line.ids)],
            }
        )
    if not lines:
        return None
    credit = _create_move(order, "out_refund", "credit", lines, invoice_date=day, period=(day, end, days))
    if invoice.payment_state in UNPAID:
        credit._reconcile_with(invoice)
    _post(order, str(_("Credit note {name} for the unused days", name=credit.name)))
    return credit


def terminate(
    order: Model, effective: dt.date, reason: Model | None, *, credit: bool = False, note: str | None = None
) -> Model | None:
    """Churn the subscription from ``effective`` (its first day without service)."""
    move = credit_unused(order, effective) if credit and order.subscription_state == "progress" else None
    end = effective - ONE_DAY
    if order.start_date and end < order.start_date:
        end = order.start_date
    vals: dict[str, Any] = {
        "subscription_state": "churned",
        "closed_on": effective,
        "close_reason_id": reason.ids[0] if reason else None,
        "end_date": min(order.end_date, end) if order.end_date else end,
        "next_invoice_date": None,
        "paused_on": None,
    }
    if note:
        vals["close_note"] = note
    _internal(order).write(vals)
    add_log(order, effective, "churn", -logged_mrr(order), reason=reason, note=note)
    _post(
        order,
        str(_("Subscription closed on {date}: {reason}", date=effective.isoformat(), reason=reason.name if reason else "")),
    )
    return move


def close(order: Model, day: dt.date, reason: Model, *, when: str = "period_end", note: str | None = None) -> Model | None:
    """Close at the end of the invoiced period (nothing credited) or now (unused days credited)."""
    if when == "period_end":
        invoice = invoiced_period(order, day)
        if invoice:
            end = invoice.subscription_period_end
            if order.end_date and order.end_date < end:
                end = order.end_date
            vals: dict[str, Any] = {"end_date": end, "close_reason_id": reason.ids[0]}
            if note:
                vals["close_note"] = note
            _internal(order).write(vals)
            _post(order, str(_("The subscription will close on {date}: {reason}", date=end.isoformat(), reason=reason.name)))
            return None
    return terminate(order, day, reason, credit=when == "now", note=note)


def expire(order: Model, day: dt.date, reasons: Any) -> str | None:
    """End a subscription whose end date has passed: replaced by its accepted renewal, else churned."""
    if not order.end_date or order.end_date >= day:
        return None
    effective = order.end_date + ONE_DAY
    renewal = order.renewal_ids.filtered(lambda r: r.state == "accepted" and r.is_subscription)
    if renewal:
        _internal(order).write(
            {"subscription_state": "closed", "closed_on": effective, "paused_on": None, "next_invoice_date": None}
        )
        add_log(order, effective, "renewal", -logged_mrr(order), note=renewal[0].name)
        _post(order, str(_("Subscription renewed by {name}", name=renewal[0].name)))
        return "renewed"
    terminate(order, effective, order.close_reason_id or reasons("not_renewed"))
    return "churned"


def reactivate(order: Model, day: dt.date) -> None:
    _internal(order).write(
        {
            "subscription_state": "progress",
            "billing_anchor_date": day,
            "next_invoice_date": day,
            "end_date": None,
            "close_reason_id": None,
            "close_note": None,
            "closed_on": None,
        }
    )
    add_log(order, day, "reactivation", float(order.recurring_monthly or 0.0) - logged_mrr(order))
    _post(order, str(_("Subscription reactivated on {date}", date=day.isoformat())))


# -- upsells and renewals --------------------------------------------------------------------------------


def _order_values(order: Model, day: dt.date) -> dict[str, Any]:
    return {
        "partner_id": order.partner_id.ids[0],
        "company_id": order.company_id.ids[0],
        "pricelist_id": order.pricelist_id.ids[0] if order.pricelist_id else None,
        "currency_id": order.currency_id.ids[0],
        "fiscal_position_id": order.fiscal_position_id.ids[0] if order.fiscal_position_id else None,
        "payment_term_id": order.payment_term_id.ids[0] if order.payment_term_id else None,
        "user_id": order.user_id.ids[0] if order.user_id else None,
        "client_reference": order.client_reference,
        "date_order": dt.datetime.combine(day, dt.time(12, 0)),
    }


def _line_copies(order: Model) -> list[Any]:
    return [
        (
            "create",
            {
                "product_id": line.product_id.ids[0],
                "name": line.name,
                "quantity": line.quantity,
                "uom_id": line.uom_id.ids[0] if line.uom_id else None,
                "price_unit": line.price_unit,
                "discount": line.discount or 0.0,
                "tax_ids": [("set", line.tax_ids.ids)],
                "subscription_line_id": line.ids[0],
                "sequence": line.sequence,
            },
        )
        for line in order._recurring_lines()
    ]


def create_upsell(order: Model) -> Model:
    day = today(order.env)
    values = {
        **_order_values(order, day),
        "upsell_of_id": order.ids[0],
        "validity_date": day + dt.timedelta(days=30),
        "line_ids": _line_copies(order),
    }
    upsell = order.env["sale.quotation"].with_context(sale_no_suggestions=True).create(values)
    _post(order, str(_("Change order {name} prepared", name=upsell.name)))
    return upsell


def apply_upsell(upsell: Model, day: dt.date) -> Model | None:
    """Apply a confirmed change order to its subscription: prorate the invoiced period, update the lines."""
    order = upsell.upsell_of_id.elevated()
    if order.subscription_state != "progress":
        raise UserError(_("Only subscriptions in progress can be changed ({name})", name=order.name))
    if upsell.currency_id.ids != order.currency_id.ids:
        raise UserError(_("A change order uses the currency of its subscription"))
    currency = order.currency_id
    changes: list[tuple[Model, Model | None, float, float]] = []
    for line in upsell.line_ids.filtered(
        lambda r: r.product_id and not r.display_type and not r.is_optional and not r.is_downpayment and r.is_recurring
    ):
        target = line.subscription_line_id
        if target and target.quotation_id.ids != order.ids:
            raise UserError(_("Line '{name}' belongs to another subscription", name=line.name))
        old = target._amount() if target else 0.0
        new = line._amount()
        same_taxes = bool(target) and set(target.tax_ids.ids) == set(line.tax_ids.ids)
        if target and abs(new - old) < 1e-9 and same_taxes and abs((target.quantity or 0) - (line.quantity or 0)) < 1e-9:
            continue
        if not target and not line.quantity:
            continue
        changes.append((line, target or None, old, new))
    if not changes:
        return None
    before = float(order.recurring_monthly or 0.0)
    invoice = invoiced_period(order, day)
    doc_lines: list[dict[str, Any]] = []
    period: tuple[dt.date, dt.date, int] | None = None
    if invoice:
        days = int(invoice.subscription_period_days or 0)
        end = invoice.subscription_period_end
        remaining, ratio = remaining_ratio(day, end, days)
        if remaining > 0:
            period = (day, end, days)
            for line, target, old, new in changes:
                price = float(currency.round((new - old) * ratio))
                if not price:
                    continue
                if target:
                    what = _(
                        "{name}: {old} → {new}",
                        name=line.name,
                        old=f"{target.quantity:g} × {target._price_reduced():,.2f}",
                        new=f"{line.quantity:g} × {line._price_reduced():,.2f}",
                    )
                else:
                    what = _("{name}: added", name=line.name)
                doc_lines.append(
                    {
                        "display_type": "product",
                        "name": str(
                            _(
                                "{change} — {period}, {days}/{total} days",
                                change=what,
                                period=_period_label(day, end),
                                days=remaining,
                                total=days,
                            )
                        ),
                        "product_id": line.product_id.ids[0],
                        "quantity": 1.0,
                        "price_unit": price,
                        "tax_ids": [("set", (line.tax_ids or (target.tax_ids if target else line.tax_ids)).ids)],
                        "sale_line_ids": [("set", line.ids)],
                    }
                )
    # the subscription's lines become the change order's
    lines = order.env["sale.quotation_line"].with_context(**{INTERNAL: True, "sale_no_suggestions": True})
    for line, target, _old, _new in changes:
        vals = {
            "quantity": line.quantity,
            "price_unit": line.price_unit,
            "price_manual": True,
            "discount": line.discount or 0.0,
            "tax_ids": [("set", line.tax_ids.ids)],
            "name": line.name,
        }
        if target:
            lines.browse(target.ids).write(vals)
        else:
            created = lines.create(
                {
                    **vals,
                    "quotation_id": order.ids[0],
                    "product_id": line.product_id.ids[0],
                    "uom_id": line.uom_id.ids[0] if line.uom_id else None,
                    "sequence": line.sequence,
                }
            )
            lines.browse(line.ids).write({"subscription_line_id": created.ids[0]})
    after = float(order.recurring_monthly or 0.0)
    if abs(after - before) > 0.005:
        add_log(order, day, "upsell" if after > before else "downsell", after - before, note=upsell.name)
    move = None
    if doc_lines and period is not None:
        total = sum(v["price_unit"] for v in doc_lines)
        if total < 0:
            for v in doc_lines:
                v["price_unit"] = -v["price_unit"]
            move = _create_move(order, "out_refund", "proration", doc_lines, invoice_date=day, period=period)
            if invoice.payment_state in UNPAID:
                move._reconcile_with(invoice)
        elif total > 0:
            move = _create_move(order, "out_invoice", "proration", doc_lines, invoice_date=day, period=period)
            if order.payment_token_id:
                charge_invoices(order, move, day)
    _post(
        order,
        str(_("Change order {name} applied on {date}", name=upsell.name, date=day.isoformat()))
        + (f" — {move.name}" if move else ""),
    )
    return move


def create_renewal(order: Model, *, send: bool) -> Model:
    """The renewal quotation of a fixed-term subscription: same lines and prices, the next term's dates."""
    if not order.end_date:
        raise UserError(_("Only fixed-term subscriptions are renewed (set an end date)"))
    if order.close_reason_id:
        raise UserError(_("Subscription {name} is closing: it is not renewed", name=order.name))
    if order.renewal_ids.filtered(lambda r: r.state in OPEN_RENEWAL_STATES):
        raise UserError(_("Subscription {name} already has a renewal quotation", name=order.name))
    day = today(order.env)
    start, end = renewal_term(order.billing_anchor_date or order.start_date or order.end_date, order.end_date)
    values = {
        **_order_values(order, min(day, order.end_date)),
        "plan_id": order.plan_id.ids[0],
        "renewal_of_id": order.ids[0],
        "origin_order_id": order.ids[0],
        "payment_token_id": order.payment_token_id.ids[0] if order.payment_token_id else None,
        "start_date": start,
        "end_date": end,
        "validity_date": order.end_date,
        "line_ids": _line_copies(order),
    }
    renewal = order.env["sale.quotation"].with_context(sale_no_suggestions=True).create(values)
    _post(order, str(_("Renewal quotation {name} for {period}", name=renewal.name, period=_period_label(start, end))))
    if send and (order.partner_id.email or order.partner_id.commercial_partner_id.email):
        try:
            with order.env.cr.savepoint():
                renewal.action_send()
        except Exception as exc:  # the renewal exists; sending can be done by hand
            log.warning("renewal reminder not sent", order=order.name, error=str(exc))
    return renewal


# -- the daily job ---------------------------------------------------------------------------------------


def _reasons(env: Any) -> Any:
    def get(name: str) -> Model:
        return env.ref(f"sale_subscription.close_reason_{name}")

    return get


def run_recurring(env: Any, day: dt.date, company: Model | None = None) -> dict[str, Any]:
    """Everything due on ``day``: starts, invoices and charges, retries and auto-close, ends, renewals."""
    env = env.with_context(**{TODAY_KEY: day.isoformat()})
    orders = env["sale.quotation"].elevated("subscription job")
    base: list[Any] = [("is_subscription", "=", True), ("state", "=", "accepted")]
    if company is not None:
        base.append(("company_id", "=", company.ids[0]))
    reasons = _reasons(env)
    summary: dict[str, Any] = {"date": day.isoformat(), "started": 0, "invoices": 0, "closed": 0, "renewed": 0}
    summary.update({"renewal_quotes": 0, "paid": 0, "declined": 0, "errors": 0})

    def each(domain: list[Any], step: Any) -> None:
        for order in orders.search(base + domain, order="id"):
            sub = order.with_company(order.company_id)
            try:
                with env.cr.savepoint():
                    step(sub)
            except Exception as exc:  # one broken subscription must not block the others
                summary["errors"] += 1
                log.warning("subscription job step failed", order=order.name, error=str(exc))

    def do_start(sub: Model) -> None:
        start_subscription(sub, sub.start_date)
        summary["started"] += 1

    def do_invoice(sub: Model) -> None:
        summary["invoices"] += len(invoice_due_periods(sub, day))

    def do_collect(sub: Model) -> None:
        result = collect(sub, day, reasons)
        if result in ("paid", "declined"):
            summary[result] += 1
        elif result == "closed":
            summary["closed"] += 1

    def do_expire(sub: Model) -> None:
        result = expire(sub, day, reasons)
        if result == "renewed":
            summary["renewed"] += 1
        elif result == "churned":
            summary["closed"] += 1

    def do_remind(sub: Model) -> None:
        days = int(sub.plan_id.renewal_reminder_days or 0)
        if not days or sub.close_reason_id or day < sub.end_date - dt.timedelta(days=days):
            return
        if sub.renewal_ids.filtered(lambda r: r.state != "cancelled"):
            return  # one automatic renewal quotation per term (a declined one is not sent again)
        create_renewal(sub, send=True)
        summary["renewal_quotes"] += 1

    each([("subscription_state", "=", "draft"), ("start_date", "<=", day)], do_start)
    each([("subscription_state", "=", "progress"), ("next_invoice_date", "<=", day)], do_invoice)
    each([("subscription_state", "=", "progress"), ("subscription_move_ids.payment_state", "in", list(UNPAID))], do_collect)
    each([("subscription_state", "in", ["progress", "paused"]), ("end_date", "<", day)], do_expire)
    each([("subscription_state", "in", ["progress", "paused"]), ("end_date", ">=", day)], do_remind)
    return summary


@job("sale_subscription.run", queue="default", max_attempts=1, as_system=True)
def run_recurring_job(env: Any, date: str | None = None) -> dict[str, Any]:
    day = dt.date.fromisoformat(date) if date else env.today()
    summary = run_recurring(env, day)
    log.info("subscription job", **summary)
    return summary
