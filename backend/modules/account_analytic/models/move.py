"""Analytic distributions on journal items and the analytic items posting creates.

* ``account.move_line.analytic_distribution`` is ``{"<analytic account id>": percent}``; percentages of one
  plan add up to at most 100. It is part of the ledger's posted-writable fields (D-073 invariant 3), so a
  posted item's distribution can be corrected: its analytic items are then regenerated.
* Posting creates one ``account.analytic_line`` per (item, analytic account) with ``amount = -balance x
  percent / 100`` (income positive, costs negative); reset to draft and cancel delete them.
* A new product line without a distribution gets the one its distribution models propose.
* A plan that is mandatory for an item (by account type, account prefix or journal) must cover 100 % of it
  before the entry can be posted.
"""

from __future__ import annotations

from typing import Any

from modules.account.models.move import INTERNAL, POSTED_LINE_WRITABLE, POSTED_WRITABLE
from modules.account_analytic.models.analytic import _validate, normalise_distribution, parse_distribution
from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.orm import Model, api, fields

LINE_COMMANDS = {"line_ids", "invoice_line_ids"}


class MoveLineAnalytic(Model):
    _extend = "account.move_line"

    analytic_distribution = fields.Json(
        "Analytic distribution", copy=True, help='{"<analytic account id>": percent}; at most 100 % per plan'
    )
    analytic_line_ids = fields.One2many("account.analytic_line", "move_line_id", "Analytic items")

    # -- create / write ------------------------------------------------------------------------------

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        explicit: list[bool] = []
        for vals in batch:
            explicit.append("analytic_distribution" in vals)
            if "analytic_distribution" in vals:
                vals["analytic_distribution"] = normalise_distribution(vals["analytic_distribution"])
        lines = super().create(batch)
        if not self.env.context.get(INTERNAL):
            for line, given in zip(lines, explicit, strict=False):
                if not given and line.display_type == "product" and not line.analytic_distribution:
                    line._apply_distribution_models()
        posted = lines.filtered(lambda line: line.parent_state == "posted" and line.analytic_distribution)
        if posted:
            posted._create_analytic_lines()
        return lines

    def write(self, vals: dict[str, Any]) -> bool:
        if "analytic_distribution" in vals:
            vals = {**vals, "analytic_distribution": normalise_distribution(vals["analytic_distribution"])}
        result = super().write(vals)
        if "analytic_distribution" in vals:
            posted = self.filtered(lambda line: line.parent_state == "posted")
            if posted:
                if not self.env.context.get(INTERNAL):
                    posted._check_mandatory_plans()
                posted._remove_analytic_lines()
                posted._create_analytic_lines()
        return result

    @api.constrains("analytic_distribution")
    def _check_analytic_distribution(self) -> None:
        for line in self:
            if line.analytic_distribution:
                _validate(self.env, line.analytic_distribution, line.move_id.company_id)

    # -- distribution models -------------------------------------------------------------------------

    def _distribution_context(self) -> dict[str, Any]:
        """What the distribution models match on for this line."""
        self.ensure_one()
        move = self.move_id
        account = self.account_id
        if not account and move.is_invoice():
            account = move._product_line_account(self)
        partner = move.partner_id or self.partner_id
        return {
            "partner_id": partner.ids[0] if partner else None,
            "product_id": self.product_id.ids[0] if self.product_id else None,
            "account_id": account.ids[0] if account else None,
            "company_id": move.company_id.ids[0],
        }

    def _apply_distribution_models(self) -> None:
        models = self.env["account.analytic_distribution_model"]
        for line in self:
            dist = models.distribution_for(line._distribution_context())
            if dist:
                line.with_context(**{INTERNAL: True}).write({"analytic_distribution": dist})

    # -- mandatory plans -----------------------------------------------------------------------------

    def _check_mandatory_plans(self) -> None:
        """Refuse items whose account or journal makes a plan mandatory and that do not cover it 100 %."""
        lines = self.filtered(lambda line: line.display_type == "product" and line.account_id)
        if not lines:
            return
        plans = self.env["account.analytic_plan"].elevated().search([])
        if not plans:
            return
        accounts = self.env["account.analytic_account"].elevated().with_context(active_test=False)
        for line in lines:
            company = line.move_id.company_id
            dist = parse_distribution(line.analytic_distribution)
            per_plan: dict[int, float] = {}
            for acc in accounts.browse(list(dist)).exists():
                per_plan[acc.plan_id.ids[0]] = per_plan.get(acc.plan_id.ids[0], 0.0) + dist[acc.ids[0]]
            for plan in plans:
                if plan.company_id and plan.company_id.ids != company.ids:
                    continue
                if plan._applicability_for(line.account_id, line.move_id.journal_id, company) != "mandatory":
                    continue
                if per_plan.get(plan.ids[0], 0.0) < 99.9999:
                    raise UserError(
                        _(
                            "Journal item '{label}' ({account}) needs a 100 % analytic distribution on plan {plan}",
                            label=line.name or "",
                            account=line.account_id.elevated().code,
                            plan=plan.name,
                        ),
                        plan_id=plan.ids[0],
                    )

    # -- analytic items ------------------------------------------------------------------------------

    def _analytic_line_vals(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for line in self:
            dist = parse_distribution(line.analytic_distribution)
            if not dist or line.display_type in ("line_section", "line_note"):
                continue
            move = line.move_id
            ccur = move.company_id.elevated().currency_id
            for account_id, pct in dist.items():
                out.append(
                    {
                        "name": line.name or move.ref or move.name or "/",
                        "date": line.date or move.date,
                        "amount": float(ccur.round(-float(line.balance or 0) * pct / 100.0)),
                        "unit_amount": float(line.quantity or 0) * pct / 100.0,
                        "account_id": account_id,
                        "general_account_id": line.account_id.ids[0] if line.account_id else None,
                        "partner_id": line.partner_id.ids[0] if line.partner_id else None,
                        "product_id": line.product_id.ids[0] if line.product_id else None,
                        "move_line_id": line.ids[0],
                        "percentage": pct,
                        "ref": move.name,
                        "company_id": move.company_id.ids[0],
                    }
                )
        return out

    def _create_analytic_lines(self) -> Model:
        vals = self._analytic_line_vals()
        model = self.env["account.analytic_line"].elevated("analytic items of posted journal items")
        return model.create(vals) if vals else model

    def _remove_analytic_lines(self) -> None:
        if not self._ids:
            return
        found = (
            self.env["account.analytic_line"]
            .elevated("analytic items of journal items")
            .search([("move_line_id", "in", list(self._ids))])
        )
        if found:
            found.unlink()


class MoveAnalytic(Model):
    _extend = "account.move"

    analytic_line_ids = fields.One2many("account.analytic_line", "move_id", "Analytic items")

    def write(self, vals: dict[str, Any]) -> bool:
        """Let posted entries receive distribution corrections through their line one2many fields."""
        if not self.env.context.get(INTERNAL) and LINE_COMMANDS & set(vals):
            posted = self.filtered(lambda m: m.state == "posted")
            if posted and len(posted) == len(self) and self._only_posted_line_updates(vals):
                rest = {k: v for k, v in vals.items() if k not in LINE_COMMANDS}
                line_model = self.env["account.move_line"]
                for key in LINE_COMMANDS & set(vals):
                    for cmd in vals[key]:
                        line = line_model.browse([int(cmd[1])])
                        if line.move_id.ids[0] not in self.ids:
                            raise UserError(_("This journal item belongs to another entry"))
                        line.write(dict(cmd[2]))
                return super().write(rest) if rest else True
        return super().write(vals)

    @staticmethod
    def _only_posted_line_updates(vals: dict[str, Any]) -> bool:
        if set(vals) - LINE_COMMANDS - POSTED_WRITABLE:
            return False
        for key in LINE_COMMANDS & set(vals):
            for cmd in vals[key] or []:
                if not (isinstance(cmd, list | tuple) and len(cmd) == 3 and cmd[0] == "update"):
                    return False
                if set(cmd[2]) - POSTED_LINE_WRITABLE:
                    return False
        return True

    @api.expose
    def action_post(self) -> bool:
        for move in self:
            if move.state == "draft":
                move.line_ids._check_mandatory_plans()
        result = super().action_post()
        posted = self.filtered(lambda m: m.state == "posted")
        lines = posted.mapped("line_ids").filtered(lambda line: line.analytic_distribution)
        if lines:
            lines._remove_analytic_lines()
            lines._create_analytic_lines()
        return result

    @api.expose
    def button_draft(self) -> bool:
        result = super().button_draft()
        self.mapped("line_ids")._remove_analytic_lines()
        return result

    @api.expose
    def button_cancel(self) -> bool:
        result = super().button_cancel()
        self.mapped("line_ids")._remove_analytic_lines()
        return result

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        """Credit notes and reversals keep the distribution of the lines they reverse."""
        moves = super().create(vals_list)
        for reverse in moves.filtered(lambda m: m.reversed_entry_id and m.state == "draft"):
            original = reverse.reversed_entry_id
            for src, dst in zip(_reversible_lines(original), _reversible_lines(reverse), strict=False):
                if src.analytic_distribution != dst.analytic_distribution:
                    dst.with_context(**{INTERNAL: True}).write({"analytic_distribution": src.analytic_distribution})
        return moves


def _reversible_lines(move: Model) -> list[Model]:
    if move.is_invoice():
        lines = move.line_ids.filtered(lambda line: line.display_type == "product")
    else:
        lines = move.line_ids.filtered(lambda line: line.display_type not in ("line_section", "line_note"))
    return list(lines.sorted(lambda line: line.ids[0]))
