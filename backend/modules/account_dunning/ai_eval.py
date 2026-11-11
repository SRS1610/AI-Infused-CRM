"""Evaluation scenarios of ``account_dunning.message_draft``: the ledger of one customer (overdue, disputed
and earlier paid invoices, earlier reminders), the company's dunning levels, and the run prepared on the
scenario's day; returns the customer's reminder."""

from __future__ import annotations

import datetime as dt
from typing import Any

from modules.account_dunning.demo import install_levels
from modules.account_dunning.dunning import prepare_run
from tessera.exceptions import UserError


def _invoice(env: Any, partner: Any, company: Any, amount: float, due: dt.date, label: str) -> Any:
    move = env["account.move"].create(
        {
            "move_type": "out_invoice",
            "partner_id": partner.ids[0],
            "company_id": company.ids[0],
            "invoice_date": due,
            "invoice_date_due": due,
            "payment_term_id": None,
            "invoice_line_ids": [
                ("create", {"name": label, "quantity": 1, "price_unit": amount, "tax_ids": [("set", [])]})
            ],
        }
    )
    move.action_post()
    return move


def build(env: Any, spec: dict[str, Any]) -> Any:
    from modules.account.payments import register_payments

    sys_env = env.elevated("ai eval scenario")
    company = sys_env["core.company"].browse(env.company.ids)
    day = dt.date.fromisoformat(spec["day"])
    partner = sys_env["contacts.partner"].browse([int(spec["partner"])])
    levels = install_levels(company).sorted(lambda lv: lv.delay_days)
    for i, inv in enumerate(spec.get("invoices") or []):
        move = _invoice(sys_env, partner, company, inv["amount"], day - dt.timedelta(days=inv["days_late"]), f"Order {i + 1}")
        if inv.get("blocked"):
            move.line_ids.filtered(lambda r: r.account_id.account_type == "asset_receivable").write({"blocked": True})
    for i, paid in enumerate(spec.get("paid") or []):
        due = day - dt.timedelta(days=paid["days_ago"])
        move = _invoice(sys_env, partner, company, paid["amount"], due, f"Earlier order {i + 1}")
        register_payments(move, date=due + dt.timedelta(days=paid["days_late"]))
    for past in spec.get("earlier_reminders") or []:
        run = sys_env["account.dunning_run"].create(
            {"company_id": company.ids[0], "date": day - dt.timedelta(days=past["days_ago"])}
        )
        sys_env["account.dunning_run_line"].create(
            {
                "run_id": run.ids[0],
                "partner_id": partner.ids[0],
                "level_id": levels[past["level"]].ids[0],
                "currency_id": company.currency_id.ids[0],
                "overdue_amount": past.get("amount", 100.0),
                "state": "sent",
            }
        )
        run.write({"state": "done"})
    run = prepare_run(company, day)
    if run is None:
        raise UserError("the scenario produced no reminder")
    line = run.line_ids.filtered(lambda r: r.partner_id.ids == partner.ids)
    if not line:
        raise UserError("the scenario's customer got no reminder")
    return env["account.dunning_run_line"].browse(line.ids)
