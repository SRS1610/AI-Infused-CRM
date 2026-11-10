"""Taxes, tax groups, repartition and fiscal positions (ERD_PHASE_2 §3.4).

``account.tax.compute_all`` is the single tax engine used by invoices, sales lines and reports:

* price-included taxes are removed from the price first (consecutive included taxes that do not affect
  each other's base are removed together, so 121 incl. 10 % + 11 % gives 100, not 99.10);
* each tax is then computed on the excluded base, or on the base increased by earlier taxes that
  ``include_base_amount`` when the tax ``is_base_affected``;
* amounts are rounded per line to the currency, and when prices include taxes the excluded total is
  adjusted so that excluded + taxes equals the price exactly;
* each tax amount is split over its repartition lines (invoice or refund), which carry the account
  and the tax-report tags. Reverse charge is a +100 % and a -100 % repartition line.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from tessera.exceptions import ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields


def _round(value: float, places: int) -> float:
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


class TaxGroup(Model):
    _name = "account.tax_group"
    _description = "Tax group"
    _order = "sequence, name"

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    country_id = fields.Many2one("core.country", ondelete="set_null")
    company_id = fields.Many2one(
        "core.company",
        ondelete="cascade",
        default=lambda self: self.env.company.ids[0] if self.env.company else None,
    )
    tax_payable_account_id = fields.Many2one("account.account", "Tax payable account (closing)", ondelete="set_null")
    tax_receivable_account_id = fields.Many2one(
        "account.account", "Tax receivable account (closing)", ondelete="set_null"
    )


class Tax(Model):
    _name = "account.tax"
    _description = "Tax"
    _order = "sequence, id"
    _audit = True
    _sql_constraints = [
        (
            "name_company_use_uniq",
            "UNIQUE (name, company_id, type_tax_use)",
            "Tax names must be unique per company and use",
        )
    ]

    name = fields.Char(required=True, translate=True)
    description = fields.Char("Label on documents", translate=True)
    type_tax_use = fields.Selection(
        [("sale", "Sales"), ("purchase", "Purchases"), ("none", "None")], "Use", default="sale", required=True
    )
    amount_type = fields.Selection(
        [
            ("percent", "Percentage of price"),
            ("fixed", "Fixed amount per unit"),
            ("division", "Percentage of price tax included"),
            ("group", "Group of taxes"),
        ],
        "Computation",
        default="percent",
        required=True,
    )
    amount = fields.Float(digits=(16, 4), default=0.0)
    price_include = fields.Boolean("Included in price")
    include_base_amount = fields.Boolean("Affects the base of later taxes")
    is_base_affected = fields.Boolean("Base affected by earlier taxes", default=True)
    children_tax_ids = fields.Many2many(
        "account.tax",
        "Children taxes",
        relation="account_tax_children_rel",
        column1="parent_tax_id",
        column2="child_tax_id",
    )
    sequence = fields.Integer(default=1)
    tax_group_id = fields.Many2one("account.tax_group", "Tax group", ondelete="restrict")
    country_id = fields.Many2one("core.country", ondelete="set_null")
    tax_scope = fields.Selection([("service", "Services"), ("consu", "Goods")], help="Restrict to a kind of product")
    company_id = fields.Many2one(
        "core.company",
        required=True,
        ondelete="cascade",
        default=lambda self: self.env.company.ids[0] if self.env.company else None,
    )
    repartition_line_ids = fields.One2many("account.tax_repartition_line", "tax_id", "Distribution", copy=True)
    active = fields.Boolean(default=True)

    @api.depends("name", "description")
    def _compute_display_name(self) -> None:
        for tax in self:
            tax.display_name = tax.name

    @api.constrains("amount_type", "amount", "children_tax_ids")
    def _check_amount(self) -> None:
        for tax in self:
            if tax.amount_type == "division" and not 0 <= (tax.amount or 0) < 100:
                raise ValidationError(_("A tax-included percentage must be between 0 and 100"), field="amount")
            if tax.amount_type == "group":
                if tax.ids[0] in tax.children_tax_ids.ids:
                    raise ValidationError(_("A tax group cannot contain itself"), field="children_tax_ids")
                if tax.children_tax_ids.filtered(lambda c: c.amount_type == "group"):
                    raise ValidationError(_("Tax groups cannot be nested"), field="children_tax_ids")

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        for vals in batch:
            if vals.get("amount_type") != "group" and not vals.get("repartition_line_ids"):
                # a plain tax books 100 % of its amount on one line (account set by the chart or later)
                vals["repartition_line_ids"] = [
                    ("create", {"document_type": doc, "repartition_type": kind, "factor_percent": 100.0})
                    for doc in ("invoice", "refund")
                    for kind in ("base", "tax")
                ]
        return super().create(batch)

    # -- engine --------------------------------------------------------------------------------------

    def _flatten(self) -> list[Model]:
        out: list[Model] = []
        for tax in self.sorted(lambda t: (t.sequence or 0, t.ids[0])):
            if tax.amount_type == "group":
                out.extend(tax.children_tax_ids.sorted(lambda t: (t.sequence or 0, t.ids[0])))
            else:
                out.append(tax)
        return out

    @staticmethod
    def _remove_included(base: float, percent: float, fixed: float, division: float) -> float:
        return (base - fixed) / (1 + percent / 100.0) * (1 - division / 100.0)

    def _amount_on(self, base: float, quantity: float, *, included: bool) -> float:
        """Unrounded amount of one (non-group) tax for a base excluding it."""
        rate = float(self.amount or 0)
        if self.amount_type == "fixed":
            return rate * quantity
        if self.amount_type == "percent":
            return base * rate / 100.0
        if self.amount_type == "division":
            return base * rate / (100.0 - rate) if rate != 100 else 0.0
        return 0.0

    def compute_all(
        self,
        price_unit: float,
        currency: Model | None = None,
        quantity: float = 1.0,
        product: Model | None = None,
        partner: Model | None = None,
        *,
        is_refund: bool = False,
        include_caba_tags: bool = False,
    ) -> dict[str, Any]:
        """Taxes of ``quantity`` times ``price_unit`` (see the module docstring for the rules)."""
        places = int(currency.decimal_places) if currency else 2
        taxes = self._flatten()
        raw_total = price_unit * quantity
        total = _round(raw_total, places)
        # 1. remove included taxes (walking backwards, resolving at every tax that affects later bases)
        base = raw_total
        acc = {"percent": 0.0, "fixed": 0.0, "division": 0.0}
        for tax in reversed(taxes):
            if not tax.price_include:
                continue
            if tax.include_base_amount:
                base = self._remove_included(base, acc["percent"], acc["fixed"], acc["division"])
                acc = {"percent": 0.0, "fixed": 0.0, "division": 0.0}
            rate = float(tax.amount or 0)
            if tax.amount_type == "fixed":
                acc["fixed"] += rate * quantity
            elif tax.amount_type == "percent":
                acc["percent"] += rate
            elif tax.amount_type == "division":
                acc["division"] += rate
        total_excluded_raw = self._remove_included(base, acc["percent"], acc["fixed"], acc["division"])
        # 2. compute every tax forward
        results: list[dict[str, Any]] = []
        cumulated = total_excluded_raw
        included_sum = 0.0
        any_included = False
        for tax in taxes:
            tax_base_raw = cumulated if tax.is_base_affected else total_excluded_raw
            amount = _round(tax._amount_on(tax_base_raw, quantity, included=bool(tax.price_include)), places)
            if tax.price_include:
                any_included = True
                included_sum += amount
            doc = "refund" if is_refund else "invoice"
            reps = tax.repartition_line_ids.filtered(lambda r, d=doc: r.document_type == d)
            base_rep = reps.filtered(lambda r: r.repartition_type == "base")
            tax_reps = reps.filtered(lambda r: r.repartition_type == "tax").sorted(
                lambda r: (r.sequence or 0, r.ids[0])
            )
            distributed = 0.0
            for i, rep in enumerate(tax_reps):
                share = _round(amount * float(rep.factor_percent or 0) / 100.0, places)
                if i == len(tax_reps) - 1 and abs(sum(float(r.factor_percent or 0) for r in tax_reps) - 100.0) < 1e-9:
                    share = _round(amount - distributed, places)  # the last share takes the rounding
                distributed += share
                results.append(
                    {
                        "id": tax.ids[0],
                        "name": tax.description or tax.name,
                        "amount": share,
                        "base": _round(tax_base_raw, places),
                        "sequence": tax.sequence,
                        "account_id": rep.account_id.ids[0] if rep.account_id else None,
                        "tax_repartition_line_id": rep.ids[0],
                        "tag_ids": rep.tag_ids.ids,
                        "price_include": bool(tax.price_include),
                        "tax_group_id": tax.tax_group_id.ids[0] if tax.tax_group_id else None,
                        "base_tag_ids": base_rep.tag_ids.ids if base_rep else [],
                    }
                )
            if tax.include_base_amount:
                cumulated += amount
        taxes_total = sum(r["amount"] for r in results)
        total_excluded = _round(total - included_sum if any_included else total_excluded_raw, places)
        base_tags: list[int] = sorted({t for r in results for t in r["base_tag_ids"]})
        return {
            "total_excluded": total_excluded,
            "total_included": _round(total_excluded + taxes_total, places),
            "total_void": _round(sum(r["amount"] for r in results if not r["account_id"]), places),
            "taxes": results,
            "base_tags": base_tags,
        }

    @api.expose
    def compute_preview(
        self, price_unit: float, quantity: float = 1.0, currency_id: int | None = None
    ) -> dict[str, Any]:
        """Tax breakdown for the client (live totals on documents)."""
        currency = self.env["core.currency"].browse([currency_id]) if currency_id else None
        result = self.compute_all(price_unit, currency, quantity)
        return {k: result[k] for k in ("total_excluded", "total_included")} | {
            "taxes": [{"id": t["id"], "name": t["name"], "amount": t["amount"]} for t in result["taxes"]]
        }


class TaxRepartitionLine(Model):
    _name = "account.tax_repartition_line"
    _description = "Tax distribution line"
    _order = "document_type, repartition_type, sequence, id"

    tax_id = fields.Many2one("account.tax", required=True, ondelete="cascade", index=True)
    document_type = fields.Selection([("invoice", "Invoice"), ("refund", "Credit note")], required=True)
    repartition_type = fields.Selection([("base", "Base"), ("tax", "Tax")], required=True, default="tax")
    factor_percent = fields.Float("%", digits=(16, 4), default=100.0)
    account_id = fields.Many2one("account.account", ondelete="restrict")
    tag_ids = fields.Many2many(
        "account.account_tag",
        "Tax grids",
        relation="account_tax_repartition_tag_rel",
        column1="line_id",
        column2="tag_id",
    )
    use_in_tax_closing = fields.Boolean(default=True)
    sequence = fields.Integer(default=1)
    company_id = fields.Many2one("core.company", related="tax_id.company_id", store=True)


class FiscalPosition(Model):
    _name = "account.fiscal_position"
    _description = "Fiscal position"
    _order = "sequence, id"

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    auto_apply = fields.Boolean("Detect automatically")
    country_id = fields.Many2one("core.country", ondelete="set_null")
    country_ids = fields.Many2many("core.country", "Countries", relation="account_fiscal_position_country_rel")
    exclude_country_ids = fields.Many2many(
        "core.country",
        "Except countries",
        relation="account_fiscal_position_excl_country_rel",
        column1="position_id",
        column2="country_id",
    )
    vat_required = fields.Boolean("Tax ID required")
    note = fields.Html("Legal notes on documents")
    tax_ids = fields.One2many("account.fiscal_position_tax", "position_id", "Tax mapping", copy=True)
    account_ids = fields.One2many("account.fiscal_position_account", "position_id", "Account mapping", copy=True)
    company_id = fields.Many2one(
        "core.company",
        required=True,
        ondelete="cascade",
        default=lambda self: self.env.company.ids[0] if self.env.company else None,
    )
    active = fields.Boolean(default=True)

    def map_tax(self, taxes: Model) -> Model:
        """Taxes after mapping (an empty destination removes the tax)."""
        if not self:
            return taxes
        self.ensure_one()
        out: list[int] = []
        rows = self.tax_ids
        for tax in taxes:
            matches = rows.filtered(lambda r, t=tax: r.tax_src_id.ids == t.ids)
            if not matches:
                out.append(tax.ids[0])
                continue
            out.extend(i for m in matches for i in m.tax_dest_id.ids)
        return taxes.browse(list(dict.fromkeys(out)))

    def map_account(self, account: Model) -> Model:
        if not self or not account:
            return account
        self.ensure_one()
        for row in self.account_ids:
            if row.account_src_id.ids == account.ids:
                return row.account_dest_id
        return account

    @api.model
    def _get_for_partner(self, partner: Model, company: Model) -> Model:
        """The partner's explicit position (D-072), else the first automatic one matching country and tax ID."""
        if not partner:
            return self.browse([])
        explicit = partner._accounting(company, "property_account_position_id")
        if explicit:
            return explicit
        commercial = partner.elevated().commercial_partner_id or partner.elevated()
        country = commercial.country_id
        candidates = self.elevated().search(
            [("auto_apply", "=", True), ("company_id", "=", company.ids[0])], order="sequence, id"
        )
        has_vat = bool(getattr(commercial, "vat", None))
        for fp in candidates:
            if fp.vat_required and not has_vat:
                continue
            countries = set(fp.country_ids.ids) | set(fp.country_id.ids)
            if countries and (not country or country.ids[0] not in countries):
                continue
            if country and country.ids[0] in set(fp.exclude_country_ids.ids):
                continue
            if fp.exclude_country_ids and not country:
                continue
            return self.browse(fp.ids)
        return self.browse([])


class FiscalPositionTax(Model):
    _name = "account.fiscal_position_tax"
    _description = "Fiscal position tax mapping"

    position_id = fields.Many2one("account.fiscal_position", required=True, ondelete="cascade", index=True)
    tax_src_id = fields.Many2one("account.tax", "Tax on product", required=True, ondelete="cascade")
    tax_dest_id = fields.Many2one("account.tax", "Tax to apply", ondelete="cascade")


class FiscalPositionAccount(Model):
    _name = "account.fiscal_position_account"
    _description = "Fiscal position account mapping"

    position_id = fields.Many2one("account.fiscal_position", required=True, ondelete="cascade", index=True)
    account_src_id = fields.Many2one("account.account", "Account on product", required=True, ondelete="cascade")
    account_dest_id = fields.Many2one("account.account", "Account to use", required=True, ondelete="cascade")
