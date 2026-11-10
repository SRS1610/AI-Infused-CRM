"""Report definitions (ERD_PHASE_2 §6): a report is either a tree of lines with expressions (balance sheet,
profit and loss, cash flow, tax returns…) or one of the built-in ledgers (general ledger, trial balance,
partner ledger, aged balances). Countries add their own reports as data."""

from __future__ import annotations

from modules.account.models.account import ACCOUNT_TYPES
from modules.account_reports.expressions import parse
from tessera.exceptions import ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields

ENGINES = [
    ("lines", "Lines with expressions"),
    ("general_ledger", "General ledger"),
    ("trial_balance", "Trial balance"),
    ("partner_ledger", "Partner ledger"),
    ("aged_receivable", "Aged receivable"),
    ("aged_payable", "Aged payable"),
]
SCOPE_SELECTION = [
    ("period", "Column period"),
    ("as_of", "Up to the column's end"),
    ("opening", "Before the column's start"),
    ("fy_to_date", "Fiscal year to date"),
    ("before_fy", "Before the fiscal year"),
]


class Report(Model):
    _name = "account.report"
    _description = "Financial report"
    _order = "sequence, id"
    _audit = True
    _sql_constraints = [("code_uniq", "UNIQUE (code)", "Report codes must be unique")]

    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True, index=True, help="Used in the API: /api/v1/account/reports/<code>")
    sequence = fields.Integer(default=10)
    engine = fields.Selection(ENGINES, default="lines", required=True)
    date_mode = fields.Selection(
        [("range", "Date range"), ("as_of", "As of a date")],
        "Dates",
        default="range",
        required=True,
        help="Balance-type reports are computed as of a date; flow reports over a range",
    )
    allow_comparison = fields.Boolean("Comparison columns", default=True)
    variant_of = fields.Char(
        "Variant of", index=True, help="Code of the generic report this one specialises for a chart template"
    )
    chart_template = fields.Char(help="Only for companies using this chart template (variants)")
    description = fields.Text(translate=True)
    line_ids = fields.One2many("account.report_line", "report_id", "Lines", copy=True)
    active = fields.Boolean(default=True)

    @api.expose
    def check_definition(self) -> bool:
        """Every ``line(CODE)`` reference resolves inside the report (checked when the report runs, too)."""
        for report in self:
            codes = {line.code for line in report.line_ids if line.code}
            for line in report.line_ids:
                for ref in parse(line.expression).line_refs:
                    if ref not in codes:
                        raise ValidationError(
                            _("Line '{line}' refers to an unknown line code '{code}'", line=line.name, code=ref),
                            field="line_ids",
                        )
        return True


class ReportLine(Model):
    _name = "account.report_line"
    _description = "Financial report line"
    _order = "report_id, sequence, id"
    _sql_constraints = [("code_report_uniq", "UNIQUE (report_id, code)", "Line codes must be unique per report")]

    report_id = fields.Many2one("account.report", required=True, ondelete="cascade", index=True)
    parent_id = fields.Many2one("account.report_line", "Parent line", ondelete="cascade", index=True)
    children_ids = fields.One2many("account.report_line", "parent_id", "Children")
    sequence = fields.Integer(default=10)
    name = fields.Char(required=True, translate=True)
    code = fields.Char(help="Referenced by line(CODE) in other lines")
    expression = fields.Char(help="e.g. type(asset_cash) or line(A) - line(B); see the reports guide")
    date_scope = fields.Selection(SCOPE_SELECTION, default="period", required=True)
    sign = fields.Selection([("1", "As computed"), ("-1", "Reversed")], default="1", required=True)
    groupby_account = fields.Boolean("Unfold by account", help="The line can be unfolded into one line per account")
    hide_if_zero = fields.Boolean()
    consolidation_only = fields.Boolean(
        help="Only computed when the report converts companies into another currency (translation differences)"
    )
    style = fields.Selection(
        [("normal", "Normal"), ("header", "Section"), ("total", "Total")], default="normal", required=True
    )

    @api.constrains("expression")
    def _check_expression(self) -> None:
        types = {t for t, _label in ACCOUNT_TYPES}
        for line in self:
            expr = parse(line.expression)
            for term in expr.terms:
                allowed = types | {"exchange"} if term.func == "cash" else types
                if term.func in ("type", "cash"):
                    unknown = [a for a in term.args if a not in allowed]
                    if unknown:
                        raise ValidationError(_("Unknown account type '{type}'", type=unknown[0]), field="expression")

    @api.constrains("parent_id", "report_id")
    def _check_parent(self) -> None:
        for line in self:
            if line.parent_id and line.parent_id.report_id.ids != line.report_id.ids:
                raise ValidationError(_("A line's parent belongs to the same report"), field="parent_id")
