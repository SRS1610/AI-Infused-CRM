"""Subscription demo scenario (``tessera seed demo --phase 2``): eight subscriptions of the main company with
a few months of history — monthly, quarterly and yearly plans, an upsell, a downgrade, one churned, one
paused, a fixed term waiting for its renewal, one paid by saved card — so the subscription analysis shows
MRR movements. Idempotent: skipped when the demo subscriptions exist."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

from tessera.logging import get_logger
from tessera.orm.hooks import hook

log = get_logger(__name__)
DEMO_REF = "DEMO-SUB"


def _token(env: Any, company: Any, partner: Any) -> Any:
    provider = env.ref(f"payment.{company.ids[0]}_provider_test", raise_if_not_found=False)
    if provider is None or not provider.journal_id:
        return None
    if provider.state == "disabled":
        provider.elevated().write({"state": "test", "allow_tokenization": True})
    return (
        env["payment.token"]
        .elevated()
        .create(
            {
                "provider_id": provider.ids[0],
                "partner_id": partner.commercial_partner_id.ids[0],
                "provider_ref": "demo-saved-card",
                "display": "Test card •••• 4242",
            }
        )
    )


def seed_subscriptions(env: Any) -> int:
    from modules.sale_subscription import billing

    company = env.ref("core.main_company")
    env = env.with_company(company)
    orders = env["sale.quotation"]
    if orders.search_count([("client_reference", "like", f"{DEMO_REF}-")]):
        return 0
    seat = env.ref("sale_subscription.product_cloud_seat", raise_if_not_found=False)
    support = env.ref("sale_subscription.product_support_plan", raise_if_not_found=False)
    monitoring = env.ref("sale_subscription.product_robot_monitoring", raise_if_not_found=False)
    setup = env.ref("sale_subscription.product_setup_fee", raise_if_not_found=False)
    monthly = env.ref("sale_subscription.plan_monthly")
    quarterly = env.ref("sale_subscription.plan_quarterly")
    yearly = env.ref("sale_subscription.plan_yearly")
    if not all((seat, support, monitoring, setup)):
        return 0
    # customers the company's users can see: shared contacts or the company's own (never another company's
    # records, such as the contact of the other demo company)
    customers = list(
        env["contacts.partner"]
        .elevated()
        .search(
            [
                ("is_company", "=", True),
                {"any": [["company_id", "=", None], ["company_id", "=", company.ids[0]]]},
            ],
            order="id",
            limit=8,
        )
    )
    if not customers:
        return 0
    today = env.today()

    def day(offset: int) -> dt.date:
        return today + dt.timedelta(days=offset)

    def at(when: dt.date) -> Any:
        return env.with_context(**{billing.TODAY_KEY: when.isoformat()})

    created = 0

    def bill(sub: Any, when: dt.date) -> None:
        """Invoice what is due, and record the customer's bank transfers for it (a few days later)."""
        from modules.account.payments import register_payments

        billing.invoice_due_periods(sub.elevated(), when)
        for invoice in billing.unpaid_invoices(sub):
            register_payments(invoice, date=min(invoice.invoice_date + dt.timedelta(days=5), today))

    def upsell(qty: float) -> Callable[[Any, dt.date], None]:
        def run(sub: Any, when: dt.date) -> None:
            info = sub.with_context(**{billing.TODAY_KEY: when.isoformat()}).action_upsell()
            change = at(when)["sale.quotation"].browse([info["upsell_id"]])
            change.line_ids[:1].write({"quantity": qty})
            change.action_confirm()

        return run

    def churn(sub: Any, when: dt.date) -> None:
        sub.with_context(**{billing.TODAY_KEY: when.isoformat()}).action_close(
            reason="competitor", when="now", note="Demo: moved away"
        )

    def pause(sub: Any, when: dt.date) -> None:
        sub.with_context(**{billing.TODAY_KEY: when.isoformat()}).action_pause()

    specs: list[tuple[str, Any, list[tuple[Any, float]], int, int | None, list[tuple[int, Any]]]] = [
        ("01", monthly, [(seat, 10), (setup, 1)], -180, None, [(-60, upsell(15))]),
        ("02", monthly, [(support, 1)], -150, None, [(-40, churn)]),
        ("03", yearly, [(monitoring, 2)], -200, None, []),
        ("04", monthly, [(seat, 3)], -120, None, [(-30, pause)]),
        ("05", monthly, [(support, 1)], -90, 20, []),
        ("06", monthly, [(seat, 8)], -100, None, [(-20, upsell(5))]),
        ("07", monthly, [(seat, 2)], -10, None, []),
        ("08", quarterly, [(support, 2)], -75, None, []),
    ]
    for index, (ref, plan, lines, start, end, events) in enumerate(specs):
        partner = customers[index % len(customers)]
        start_day = day(start)
        try:
            with env.cr.savepoint():
                e = at(start_day)
                sub = e["sale.quotation"].create(
                    {
                        "partner_id": partner.ids[0],
                        "client_reference": f"{DEMO_REF}-{ref}",
                        "plan_id": plan.ids[0],
                        "start_date": start_day,
                        "end_date": day(end) if end is not None else None,
                        "date_order": dt.datetime.combine(start_day, dt.time(9, 0)),
                        "validity_date": start_day + dt.timedelta(days=30),
                        "line_ids": [("create", {"product_id": p.ids[0], "quantity": q}) for p, q in lines],
                    }
                )
                if ref == "01":
                    token = _token(env, company, partner)
                    if token is not None:
                        sub.write({"payment_token_id": token.ids[0]})
                sub.action_confirm()
                for offset, action in sorted(events, key=lambda ev: ev[0]):
                    when = day(offset)
                    bill(sub, when)
                    action(sub, when)
                if sub.subscription_state == "progress":
                    bill(sub, today)
                created += 1
        except Exception as exc:  # the demo must not stop on one scenario
            log.warning("subscription demo skipped", ref=ref, error=str(exc))
    billing.run_recurring(env.elevated(), today, company)
    return created


@hook("seed_demo")
def seed_subscription_demo(env: Any) -> None:
    count = seed_subscriptions(env)
    log.info("subscription demo", subscriptions=count)
