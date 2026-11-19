"""Dunning levels, reminder runs and their lines (ERD_PHASE_2 §5). The run logic is in ``dunning.py``."""

from __future__ import annotations

from typing import Any

from tessera.exceptions import UserError, ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields

DEFAULT_SUBJECT = "Payment reminder: {company}"
DEFAULT_MESSAGE = (
    "Hello {partner},\n\nOur records show that the invoices below are overdue, for a total of {amount_due}. "
    "If you have already paid, please ignore this message.\n\nKind regards,\n{company}"
)


class DunningLevel(Model):
    _name = "account.dunning_level"
    _description = "Dunning level"
    _order = "company_id, delay_days, id"
    _sql_constraints = [
        ("delay_company_uniq", "UNIQUE (delay_days, company_id)", "Two levels cannot have the same delay"),
    ]

    name = fields.Char(required=True, translate=True)
    delay_days = fields.Integer("Days overdue", required=True, help="Reached when an invoice is this many days late")
    send_email = fields.Boolean("Send an email", default=True)
    print_letter = fields.Boolean("Print a letter")
    fee_amount = fields.Monetary("Fee", help="Charged on a fee invoice when the level is reached")
    fee_product_id = fields.Many2one("product.product", "Fee product", ondelete="set_null")
    currency_id = fields.Many2one("core.currency", related="company_id.currency_id")
    subject = fields.Char("Email subject", translate=True, default=DEFAULT_SUBJECT)
    message = fields.Text(
        "Message",
        translate=True,
        default=DEFAULT_MESSAGE,
        help="Placeholders: {partner}, {company}, {amount_due}, {level}, {max_days}, {count}. "
        "The list of overdue invoices and their pay links is added below the message.",
    )
    company_id = fields.Many2one(
        "core.company",
        required=True,
        ondelete="cascade",
        default=lambda self: self.env.company.ids[0] if self.env.company else None,
    )
    active = fields.Boolean(default=True)

    @api.constrains("delay_days", "fee_amount")
    def _check_values(self) -> None:
        for level in self:
            if (level.delay_days or 0) < 0:
                raise ValidationError(_("The delay cannot be negative"), field="delay_days")
            if (level.fee_amount or 0) < 0:
                raise ValidationError(_("The fee cannot be negative"), field="fee_amount")


class CompanyDunning(Model):
    _extend = "core.company"

    dunning_min_amount = fields.Float(
        "Minimum overdue amount for reminders", digits=(16, 2), default=0.0, help="Smaller balances are not reminded"
    )


class PartnerDunning(Model):
    _extend = "contacts.partner"

    dunning_excluded = fields.Boolean("No payment reminders", help="Never send payment reminders to this customer")


class DunningRun(Model):
    _name = "account.dunning_run"
    _description = "Payment reminder run"
    _order = "date desc, id desc"
    _inherit = ["chatter.thread"]

    name = fields.Char(required=True, default="/", readonly=True)
    date = fields.Date(required=True, default=lambda self: self.env.today(), readonly=True)
    company_id = fields.Many2one(
        "core.company",
        required=True,
        ondelete="cascade",
        readonly=True,
        default=lambda self: self.env.company.ids[0] if self.env.company else None,
    )
    state = fields.Selection([("draft", "To send"), ("done", "Sent")], default="draft", required=True, readonly=True)
    line_ids = fields.One2many("account.dunning_run_line", "run_id", "Reminders")
    letter_id = fields.Many2one("core.attachment", "Letters (PDF)", readonly=True, ondelete="set_null")
    line_count = fields.Integer("Customers", compute="_compute_counts")
    sent_count = fields.Integer("Emails sent", compute="_compute_counts")

    @api.depends("line_ids.state")
    def _compute_counts(self) -> None:
        for run in self:
            run.line_count = len(run.line_ids)
            run.sent_count = len(run.line_ids.filtered(lambda line: line.state == "sent"))

    @api.expose
    def action_prepare(self) -> int:
        """Find the customers to remind (for this run's company and date)."""
        from modules.account_dunning.dunning import prepare_run

        self.ensure_one()
        self.check_access("write")
        if self.state != "draft" or self.line_ids:
            raise UserError(_("This run has already been prepared"))
        prepare_run(self.company_id, self.date, run=self)
        if not self.line_ids:
            raise UserError(_("No customer needs a reminder today"))
        return len(self.line_ids)

    @api.expose
    def action_send(self) -> dict[str, Any]:
        from modules.account_dunning.dunning import send_run

        self.ensure_one()
        self.check_access("write")
        return send_run(self)

    def unlink(self) -> bool:
        if self.filtered(lambda run: run.state == "done"):
            raise UserError(_("Sent reminder runs are kept as a record"))
        return super().unlink()


class DunningRunLine(Model):
    _name = "account.dunning_run_line"
    _description = "Payment reminder"
    _order = "run_id desc, overdue_amount desc, id"

    run_id = fields.Many2one("account.dunning_run", "Run", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one("core.company", related="run_id.company_id", store=True, index=True)
    date = fields.Date(related="run_id.date", store=True)
    partner_id = fields.Many2one("contacts.partner", "Customer", required=True, ondelete="cascade", index=True)
    level_id = fields.Many2one("account.dunning_level", "Level", required=True, ondelete="restrict", index=True)
    currency_id = fields.Many2one("core.currency", ondelete="restrict")
    overdue_amount = fields.Monetary("Overdue")
    max_days_overdue = fields.Integer("Days overdue")
    invoice_ids = fields.Many2many(
        "account.move",
        "Overdue invoices",
        relation="account_dunning_line_move_rel",
        column1="line_id",
        column2="move_id",
    )
    email_to = fields.Char("Email")
    subject = fields.Char()
    message = fields.Html("Message", help="Rendered reminder; it can be edited before the run is sent")
    state = fields.Selection(
        [("draft", "To send"), ("sent", "Sent"), ("letter", "Letter only"), ("no_email", "No email address")],
        default="draft",
        required=True,
        index=True,
    )
    mail_id = fields.Many2one("core.mail_queue", "Email", readonly=True, ondelete="set_null")
    fee_move_id = fields.Many2one("account.move", "Fee invoice", readonly=True, ondelete="set_null")
