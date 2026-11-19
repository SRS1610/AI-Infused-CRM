"""Payment reminder runs.

``prepare_run(company)`` finds, per commercial partner, the overdue receivable items of posted invoices that
are not disputed (*No follow-up*), not *In payment* and not excluded, takes the highest dunning level reached
by the oldest one, and creates a reminder line with its rendered message, unless the partner already received
that level within the level's delay. ``send_run(run)`` creates the fee invoices, queues the emails (with the
list of overdue invoices and their pay links) through ``core.mail_queue`` and prints the letters.

Message rendering goes through ``render_reminder`` (replaceable through ``set_renderer``): the AI feature of
sub-phase 2.6 proposes a personalised message for a prepared line, which the user accepts before sending.
"""

from __future__ import annotations

import datetime as dt
import html
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.jobs import job
from tessera.logging import get_logger
from tessera.orm import Model

log = get_logger(__name__)


@dataclass
class OverdueItem:
    move: Model
    residual: float
    days: int


@dataclass
class ReminderContext:
    """Everything a message renderer may use (amounts are computed, never generated)."""

    partner: Model
    company: Model
    level: Model
    items: list[OverdueItem]
    currency: Model

    @property
    def amount_due(self) -> float:
        return float(self.currency.round(sum(i.residual for i in self.items)))

    @property
    def max_days(self) -> int:
        return max((i.days for i in self.items), default=0)


class _SafeDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _public_link(move: Model) -> str:
    from tessera.config import get_settings

    return f"{get_settings().public_url.rstrip('/')}{move.portal_url()}"


def invoice_table(ctx: ReminderContext) -> str:
    """The overdue invoices with amounts due and pay links (always computed from the ledger)."""
    rows = []
    for item in sorted(ctx.items, key=lambda i: -i.days):
        move = item.move
        rows.append(
            "<tr>"
            f"<td>{html.escape(move.name or '')}</td>"
            f"<td>{move.invoice_date or ''}</td>"
            f"<td>{move.invoice_date_due or ''}</td>"
            f"<td style='text-align:right'>{item.days}</td>"
            f"<td style='text-align:right'>{html.escape(ctx.currency.format(item.residual))}</td>"
            f'<td><a href="{html.escape(_public_link(move))}">{html.escape(str(_("View and pay")))}</a></td>'
            "</tr>"
        )
    head = "".join(
        f"<th>{html.escape(str(label))}</th>"
        for label in (_("Invoice"), _("Date"), _("Due date"), _("Days late"), _("Amount due"), "")
    )
    total = html.escape(ctx.currency.format(ctx.amount_due))
    return (
        f"<table class='dunning'><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody>"
        f"<tfoot><tr><td colspan='4'><strong>{html.escape(str(_('Total due')))}</strong></td>"
        f"<td style='text-align:right'><strong>{total}</strong></td><td></td></tr></tfoot></table>"
    )


def render_reminder(ctx: ReminderContext) -> tuple[str, str]:
    """(subject, HTML body) of a reminder from the level's templates."""
    lang = ctx.partner.lang or None
    level = ctx.level.with_context(lang=lang) if lang else ctx.level
    values = _SafeDict(
        partner=ctx.partner.name or "",
        company=ctx.company.name or "",
        amount_due=ctx.currency.format(ctx.amount_due),
        level=level.name or "",
        max_days=str(ctx.max_days),
        count=str(len(ctx.items)),
    )
    subject = (level.subject or "").format_map(values)
    text = (level.message or "").format_map(_SafeDict({k: html.escape(v) for k, v in values.items()}))
    paragraphs = "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in text.split("\n\n") if p.strip())
    return subject, paragraphs + invoice_table(ctx)


_renderer: Callable[[ReminderContext], tuple[str, str]] = render_reminder


def set_renderer(renderer: Callable[[ReminderContext], tuple[str, str]] | None) -> None:
    """Replace the message renderer (``None`` restores the template renderer)."""
    global _renderer
    _renderer = renderer or render_reminder


def overdue_by_partner(company: Model, day: dt.date) -> dict[int, list[OverdueItem]]:
    """Overdue, undisputed receivable items of posted customer invoices, per commercial partner."""
    env = company.env
    lines = (
        env["account.move_line"]
        .elevated("dunning")
        .search(
            [
                ("company_id", "=", company.ids[0]),
                ("parent_state", "=", "posted"),
                ("account_id.account_type", "=", "asset_receivable"),
                ("reconciled", "=", False),
                ("amount_residual", ">", 0),
                ("date_maturity", "<", day),
                ("blocked", "=", False),
                ("move_id.move_type", "=", "out_invoice"),
                ("move_id.payment_state", "in", ["not_paid", "partial"]),
            ],
            order="date_maturity, id",
        )
    )
    by_move: dict[int, OverdueItem] = {}
    partner_of: dict[int, int] = {}
    for line in lines:
        move = line.move_id
        partner = move.commercial_partner_id or line.partner_id
        if not partner or partner.dunning_excluded:
            continue
        days = (day - line.date_maturity).days
        item = by_move.get(move.ids[0])
        if item is None:
            by_move[move.ids[0]] = OverdueItem(move=move, residual=float(line.amount_residual), days=days)
            partner_of[move.ids[0]] = partner.ids[0]
        else:
            item.residual += float(line.amount_residual)
            item.days = max(item.days, days)
    out: dict[int, list[OverdueItem]] = defaultdict(list)
    for move_id, item in by_move.items():
        out[partner_of[move_id]].append(item)
    return dict(out)


def _level_for(levels: Model, days: int) -> Model | None:
    reached = [lv for lv in levels if (lv.delay_days or 0) <= days]
    return max(reached, key=lambda lv: lv.delay_days or 0) if reached else None


def _recently_reminded(env: Any, partner_id: int, level: Model, day: dt.date) -> bool:
    since = day - dt.timedelta(days=max(int(level.delay_days or 0), 1))
    return bool(
        env["account.dunning_run_line"]
        .elevated()
        .search_count(
            [
                ("partner_id", "=", partner_id),
                ("level_id.delay_days", ">=", int(level.delay_days or 0)),
                ("company_id", "=", level.company_id.ids[0]),
                ("state", "in", ["sent", "letter", "draft"]),
                ("date", ">", since),
            ]
        )
    )


def prepare_run(company: Model, day: dt.date | None = None, run: Model | None = None) -> Model | None:
    """Create (or fill ``run``) the reminders of ``company`` (None when nobody needs a reminder)."""
    env = company.env
    company = company.elevated()
    day = day or env.today()
    levels = env["account.dunning_level"].elevated().search([("company_id", "=", company.ids[0])], order="delay_days")
    if not levels:
        return None
    currency = company.currency_id
    minimum = float(company.dunning_min_amount or 0)
    lines: list[dict[str, Any]] = []
    partners = env["contacts.partner"].elevated()
    for partner_id, items in sorted(overdue_by_partner(company, day).items()):
        partner = partners.browse([partner_id])
        level = _level_for(levels, max(i.days for i in items))
        if level is None:
            continue
        context = ReminderContext(partner=partner, company=company, level=level, items=items, currency=currency)
        if context.amount_due <= minimum or _recently_reminded(env, partner_id, level, day):
            continue
        subject, body = _renderer(context)
        email = partner.email or ""
        lines.append(
            {
                "partner_id": partner_id,
                "level_id": level.ids[0],
                "currency_id": currency.ids[0],
                "overdue_amount": context.amount_due,
                "max_days_overdue": context.max_days,
                "invoice_ids": [("set", [i.move.ids[0] for i in items])],
                "email_to": email,
                "subject": subject,
                "message": body,
                "state": "draft" if (email or not level.send_email) else "no_email",
            }
        )
    if not lines:
        return None
    commands = [("create", vals) for vals in lines]
    if run is not None:
        run.elevated().write({"line_ids": commands, "name": f"{company.name} {day.isoformat()}"})
        return run
    run = (
        env["account.dunning_run"].elevated().create({"company_id": company.ids[0], "date": day, "line_ids": commands})
    )
    run.write({"name": f"{company.name} {day.isoformat()}"})
    return run


def _fee_invoice(line: Model) -> Model | None:
    level = line.level_id
    amount = float(level.fee_amount or 0)
    if amount <= 0:
        return None
    env = line.env
    product = level.fee_product_id
    move = (
        env["account.move"]
        .elevated("dunning fee")
        .with_company(line.company_id)
        .create(
            {
                "move_type": "out_invoice",
                "partner_id": line.partner_id.ids[0],
                "company_id": line.company_id.ids[0],
                "invoice_date": line.run_id.date,
                "ref": _("Reminder fee ({level})", level=level.name),
                "invoice_line_ids": [
                    (
                        "create",
                        {
                            "name": str(_("Late payment fee: {level}", level=level.name)),
                            "product_id": product.ids[0] if product else None,
                            "quantity": 1,
                            "price_unit": amount,
                            "tax_ids": [("set", [])],
                        },
                    )
                ],
            }
        )
    )
    move.action_post()
    return move


def send_run(run: Model) -> dict[str, Any]:
    """Book fees, queue the emails and print the letters of a prepared run."""
    from tessera.reports.engine import render_pdf

    run = run.elevated("dunning")
    if run.state == "done":
        raise UserError(_("This run has already been sent"))
    env = run.env
    sent = letters = 0
    letter_pages: list[str] = []
    for line in run.line_ids.filtered(lambda r: r.state in ("draft", "no_email")):
        level = line.level_id
        fee = _fee_invoice(line)
        body = line.message or ""
        if fee is not None:
            notice = _(
                "A reminder fee of {amount} has been charged: invoice {number}.",
                amount=line.currency_id.format(float(fee.amount_total)),
                number=fee.name,
            )
            body += f"<p>{html.escape(str(notice))}</p>"
        vals: dict[str, Any] = {"message": body, "fee_move_id": fee.ids[0] if fee else None}
        if level.send_email and line.email_to:
            mail = env["core.mail_queue"].queue(
                {
                    "email_to": line.email_to,
                    "subject": line.subject,
                    "body_html": body,
                    "model": "account.dunning_run",
                    "res_id": run.ids[0],
                }
            )
            vals.update(state="sent", mail_id=mail.ids[0])
            sent += 1
        elif level.print_letter:
            vals["state"] = "letter"
        if level.print_letter:
            letters += 1
            address = "<br>".join(
                html.escape(p)
                for p in (
                    line.partner_id.name,
                    line.partner_id.street,
                    f"{line.partner_id.zip or ''} {line.partner_id.city or ''}".strip(),
                )
                if p
            )
            letter_pages.append(f"<p>{address}</p><h2>{html.escape(line.subject or '')}</h2>{body}")
        line.write(vals)
    letter = None
    if letter_pages:
        doc = "".join(f"<section style='page-break-after: always'>{p}</section>" for p in letter_pages)
        content = render_pdf(f"<!doctype html><html><head><meta charset='utf-8'></head><body>{doc}</body></html>")
        letter = (
            env["core.attachment"]
            .elevated()
            .create(
                {
                    "name": f"reminders-{run.date.isoformat()}.pdf",
                    "mimetype": "application/pdf",
                    "model": "account.dunning_run",
                    "res_id": run.ids[0],
                    "raw": content,
                    "company_id": run.company_id.ids[0],
                }
            )
        )
    run.write({"state": "done", "letter_id": letter.ids[0] if letter else None})
    return {"sent": sent, "letters": letters, "lines": len(run.line_ids)}


@job("account_dunning.run", queue="default", max_attempts=1, as_system=True)
def dunning_job(env: Any, send: bool = True) -> dict[str, Any]:
    """Daily: prepare (and send) the reminders of every company with dunning levels."""
    result: dict[str, Any] = {}
    for company in env["core.company"].elevated().search([]):
        run = prepare_run(company)
        if run is None:
            continue
        result[company.name] = send_run(run) if send else {"prepared": len(run.line_ids)}
    return result
