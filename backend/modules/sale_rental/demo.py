"""Rental demo scenario (``tessera seed demo --phase 2``): projector and robot arm rentals in every stage —
returned late with a fee, out with the customer, overdue, reserved for next week — so the availability calendar,
the pickups and returns list and the late job have something to show. Idempotent."""

from __future__ import annotations

import datetime as dt
from typing import Any

from tessera.logging import get_logger
from tessera.orm.hooks import hook

log = get_logger(__name__)
DEMO_REF = "DEMO-RENT"


def seed_rentals(env: Any, now: dt.datetime | None = None) -> int:
    company = env.ref("core.main_company")
    env = env.with_company(company)
    orders = env["sale.quotation"]
    if orders.search_count([("client_reference", "like", f"{DEMO_REF}-")]):
        return 0
    projector = env.ref("sale_rental.product_projector", raise_if_not_found=False)
    robot = env.ref("sale_rental.product_demo_robot", raise_if_not_found=False)
    acme = env.ref("contacts.partner_acme_jordan", raise_if_not_found=False)
    initech = env.ref("contacts.partner_initech_priya", raise_if_not_found=False)
    if not all((projector, robot, acme, initech)):
        return 0
    now = (now or env.now()).replace(minute=0, second=0, microsecond=0)
    day = dt.timedelta(days=1)

    def rental(partner: Any, ref: str, product: Any, qty: int, start: dt.datetime, end: dt.datetime) -> Any:
        order = orders.create(
            {
                "partner_id": partner.ids[0],
                "client_reference": ref,
                "line_ids": [
                    (
                        "create",
                        {
                            "product_id": product.ids[0],
                            "quantity": qty,
                            "is_rental": True,
                            "start_date": start,
                            "return_date": end,
                        },
                    )
                ],
            }
        )
        order.action_confirm()
        return order

    def line(order: Any) -> Any:
        return order.line_ids.filtered(lambda r: r.is_rental)

    # 1. two projectors for a conference, returned 5 hours late: a late fee on the order, invoiced
    done = rental(acme, f"{DEMO_REF}-01", projector, 2, now - 12 * day, now - 9 * day)
    line(done).action_pickup(when=(now - 12 * day).isoformat())
    line(done).action_return(when=(now - 9 * day + dt.timedelta(hours=5)).isoformat())
    env["account.move"].browse(done.action_create_invoice()["invoice_ids"]).action_post()
    # 2. the robot arm for a two-week trial, out with the customer
    out = rental(initech, f"{DEMO_REF}-02", robot, 1, now - 3 * day, now + 11 * day)
    line(out).action_pickup(when=(now - 3 * day).isoformat())
    # 3. one projector that should have come back yesterday: the late job flags it
    overdue = rental(initech, f"{DEMO_REF}-03", projector, 1, now - 4 * day, now - day)
    line(overdue).action_pickup(when=(now - 4 * day).isoformat())
    from modules.sale_rental.rental import mark_late

    mark_late(env, now)
    # 4. two projectors reserved for next week (9 days: one week and two days)
    rental(acme, f"{DEMO_REF}-04", projector, 2, now + 7 * day, now + 16 * day)
    return 4


@hook("seed_demo")
def seed_rental_orders(env: Any) -> None:
    log.info("rental demo", orders=seed_rentals(env))
