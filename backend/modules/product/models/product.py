"""Products, categories and units of measure.

Variants (D-071): a product with attribute lines is a *template*. ``action_generate_variants`` creates one
child product per combination of its attribute values (``template_id``, ``attribute_value_ids``). The
fields returned by ``_variant_shared_fields`` are copied from the template and kept in sync when the
template is written; written directly on a variant they are refused.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from tessera.exceptions import UserError, ValidationError
from tessera.i18n import _, translatable
from tessera.orm import Model, api, fields
from tessera.orm import fields as F

UOM_TYPES = [
    ("reference", "Reference unit of its category"),
    ("bigger", "Bigger than the reference unit"),
    ("smaller", "Smaller than the reference unit"),
]

# Fields a variant takes from its template. ``list_price`` is the template's price plus the variant's
# ``price_extra``. Modules adding product fields that must be identical on every variant (taxes, accounts…)
# extend ``Product._variant_shared_fields``.
VARIANT_SHARED_FIELDS = (
    "name",
    "type",
    "category_id",
    "uom_id",
    "description_sale",
    "list_price",
    "standard_price",
    "currency_id",
    "tax_percent",
    "sale_ok",
    "company_id",
    "image",
)
MAX_VARIANTS = 1000
_SYNC_KEY = "_variant_sync"


def round_to_step(value: float, step: float | None) -> float:
    """``value`` rounded half-up to a multiple of ``step`` (unchanged when ``step`` is empty)."""
    if not step:
        return float(value)
    unit = Decimal(str(step))
    return float((Decimal(str(value)) / unit).quantize(Decimal(1), rounding=ROUND_HALF_UP) * unit)


class Uom(Model):
    _name = "product.uom"
    _description = "Unit of measure"
    _order = "category, name"

    name = fields.Char(required=True, translate=True)
    category = fields.Selection(
        [("unit", "Unit"), ("time", "Working time"), ("weight", "Weight"), ("length", "Length"), ("volume", "Volume")],
        required=True,
        default="unit",
    )
    uom_type = fields.Selection(
        UOM_TYPES,
        "Type",
        required=True,
        default="reference",
        help="Each category has one reference unit (ratio 1); the others are bigger or smaller than it",
    )
    factor = fields.Float(default=1.0, help="How many of this unit make the category's reference unit")
    rounding = fields.Float(
        "Rounding precision",
        digits=(16, 6),
        default=0.01,
        help="Quantities converted into this unit are rounded to a multiple of this value",
    )
    active = fields.Boolean(default=True)

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        for vals in batch:  # the type follows the ratio unless given
            if "uom_type" not in vals and vals.get("factor") is not None:
                factor = float(vals["factor"])
                vals["uom_type"] = "reference" if factor == 1 else ("bigger" if factor > 1 else "smaller")
        return super().create(batch)

    @api.constrains("uom_type", "factor", "rounding", "category", "active")
    def _check_units(self) -> None:
        categories = dict(self._fields["category"].get_selection(self))  # type: ignore[attr-defined]
        for uom in self:
            factor = uom.factor or 0.0
            if factor <= 0:
                raise ValidationError(_("The ratio of a unit must be positive"), field="factor")
            if uom.rounding is not None and uom.rounding <= 0:
                raise ValidationError(_("The rounding precision of a unit must be positive"), field="rounding")
            if uom.uom_type == "reference" and factor != 1:
                raise ValidationError(_("A reference unit has a ratio of 1"), field="factor")
            if uom.uom_type == "bigger" and factor <= 1:
                raise ValidationError(_("A unit bigger than the reference has a ratio above 1"), field="factor")
            if uom.uom_type == "smaller" and factor >= 1:
                raise ValidationError(_("A unit smaller than the reference has a ratio below 1"), field="factor")
            if uom.uom_type == "reference" and uom.active:
                others = self.elevated().search_count(
                    [("category", "=", uom.category), ("uom_type", "=", "reference"), ("id", "!=", uom.ids[0])]
                )
                if others:
                    raise ValidationError(
                        _(
                            "The category '{category}' already has a reference unit",
                            category=translatable(categories.get(uom.category, uom.category)),
                        ),
                        field="uom_type",
                    )

    def _check_same_category(self, to_uom: Model) -> None:
        if self.category != to_uom.category:
            raise UserError(
                _(
                    "Cannot convert '{from_unit}' into '{to_unit}': they measure different things",
                    from_unit=self.name,
                    to_unit=to_uom.name,
                )
            )

    def _compute_quantity(self, qty: float, to_uom: Model, round: bool = True) -> float:
        """``qty`` of this unit expressed in ``to_uom`` (same category), rounded to ``to_uom.rounding``."""
        self.ensure_one()
        to_uom.ensure_one()
        self._check_same_category(to_uom)
        amount = float(qty or 0.0)
        if to_uom.ids != self.ids:
            amount = amount * (self.factor or 1.0) / (to_uom.factor or 1.0)
        return round_to_step(amount, to_uom.rounding) if round else amount

    def _compute_price(self, price: float, to_uom: Model) -> float:
        """A price per unit of this unit expressed per ``to_uom`` (same category); not rounded."""
        self.ensure_one()
        to_uom.ensure_one()
        self._check_same_category(to_uom)
        if to_uom.ids == self.ids:
            return float(price or 0.0)
        return float(price or 0.0) * (to_uom.factor or 1.0) / (self.factor or 1.0)


class Category(Model):
    _name = "product.category"
    _description = "Product category"
    _order = "complete_name"
    _rec_name = "complete_name"

    name = fields.Char(required=True, translate=True)
    parent_id = fields.Many2one("product.category", ondelete="cascade")
    complete_name = fields.Char(compute="_compute_complete_name", store=True)

    @api.depends("name", "parent_id.complete_name")
    def _compute_complete_name(self) -> None:
        for cat in self:
            cat.complete_name = f"{cat.parent_id.complete_name} / {cat.name}" if cat.parent_id else cat.name

    @api.constrains("parent_id")
    def _check_parent(self) -> None:
        for cat in self:
            seen: set[int] = set()
            current = cat
            while current:
                if current.ids[0] in seen:
                    raise ValidationError(_("A category cannot be its own parent"), field="parent_id")
                seen.add(current.ids[0])
                current = current.parent_id

    def _ancestor_ids(self) -> list[int]:
        """This category then its parents, deepest first."""
        self.ensure_one()
        out: list[int] = []
        current = self
        while current and current.ids[0] not in out:
            out.append(current.ids[0])
            current = current.parent_id
        return out


class Product(Model):
    _name = "product.product"
    _description = "Product"
    _order = "name, id"
    _audit = True
    _global_search = ["name", "default_code", "barcode"]
    _sql_constraints = [("barcode_uniq", "UNIQUE (barcode)", "A barcode can only be assigned to one product")]

    name = fields.Char(required=True, translate=True, index="trigram")
    default_code = fields.Char("Internal reference", index=True)
    barcode = fields.Char(copy=False)
    type = fields.Selection(
        [("service", "Service"), ("consumable", "Consumable"), ("goods", "Goods")], default="goods", required=True
    )
    category_id = fields.Many2one("product.category", "Category", ondelete="set_null")
    description_sale = fields.Text("Sales description", translate=True)
    uom_id = fields.Many2one(
        "product.uom",
        "Unit",
        ondelete="restrict",
        default=lambda self: self.env.ref("product.uom_unit", raise_if_not_found=False),
    )
    currency_id = fields.Many2one(
        "core.currency",
        default=lambda self: (self.env.company or self.env.ref("core.main_company")).elevated().currency_id.ids[0],
        ondelete="restrict",
    )
    list_price = fields.Monetary("Sales price", currency_field="currency_id", default=0.0)
    standard_price = fields.Monetary(
        "Cost", currency_field="currency_id", default=0.0, groups=("product.group_product_manager", "core.group_system")
    )
    tax_percent = fields.Float("Tax %", default=0.0, help="Placeholder until accounting taxes (Phase 2)")
    image = fields.Image(attachment=True, max_size=(1024, 1024), copy=False)
    sale_ok = fields.Boolean("Can be sold", default=True)
    company_id = fields.Many2one("core.company", ondelete="set_null", help="Empty = shared by every company")
    active = fields.Boolean(default=True)

    # -- variants (D-071) ------------------------------------------------------------------------------
    attribute_line_ids = fields.One2many(
        "product.attribute_line",
        "product_id",
        "Attributes",
        copy=True,
        help="Attributes and values this product comes in; a product with attributes is a template",
    )
    is_template = fields.Boolean(
        "Has variants",
        compute="_compute_is_template",
        store=True,
        help="Set when the product has attributes: it is not sold itself, its variants are",
    )
    template_id = fields.Many2one(
        "product.product",
        "Template",
        ondelete="restrict",
        copy=False,
        domain=[["is_template", "=", True]],
        help="The product this variant was generated from",
    )
    variant_ids = fields.One2many("product.product", "template_id", "Variants", copy=False)
    attribute_value_ids = fields.Many2many(
        "product.attribute_value", "Attribute values", copy=False, help="The combination this variant stands for"
    )
    price_extra = fields.Float(
        "Extra price",
        digits=(16, 4),
        compute="_compute_price_extra",
        store=True,
        help="Sum of the extra prices of the variant's attribute values, added to the template's sales price",
    )
    variant_count = fields.Integer("Number of variants", compute="_compute_variant_count")
    is_sellable = fields.Boolean(
        "Sellable on a line",
        compute="_compute_is_sellable",
        store=True,
        index=True,
        help="Can be sold and is not a template: use [('is_sellable', '=', True)] to choose products for lines",
    )

    @api.depends("attribute_line_ids")
    def _compute_is_template(self) -> None:
        for p in self:
            p.is_template = bool(p.attribute_line_ids)

    @api.depends("attribute_value_ids.price_extra")
    def _compute_price_extra(self) -> None:
        for p in self:
            p.price_extra = sum(v.price_extra or 0.0 for v in p.attribute_value_ids.elevated())

    @api.depends("variant_ids")
    def _compute_variant_count(self) -> None:
        for p in self:
            p.variant_count = len(p.variant_ids)

    @api.depends("sale_ok", "is_template")
    def _compute_is_sellable(self) -> None:
        for p in self:
            p.is_sellable = bool(p.sale_ok) and not p.is_template

    @api.constrains("list_price", "tax_percent")
    def _check_prices(self) -> None:
        for p in self:
            if (p.list_price or 0) < 0:
                raise ValidationError(_("The sales price cannot be negative"), field="list_price")
            if not 0 <= (p.tax_percent or 0) <= 100:
                raise ValidationError(_("Tax must be between 0 and 100 %"), field="tax_percent")

    @api.constrains("template_id", "attribute_line_ids")
    def _check_variant_structure(self) -> None:
        for p in self.elevated():
            if not p.template_id:
                continue
            if p.template_id.ids == p.ids or p.template_id.template_id:
                raise ValidationError(_("A variant cannot be the template of other variants"), field="template_id")
            if p.attribute_line_ids:
                raise ValidationError(
                    _("A variant cannot have attributes of its own: add them to its template"),
                    field="attribute_line_ids",
                )

    @api.depends("name", "default_code", "attribute_value_ids.name")
    def _compute_display_name(self) -> None:
        for p in self:
            name = f"[{p.default_code}] {p.name}" if p.default_code else (p.name or "")
            values = p.attribute_value_ids.elevated()
            if values:
                ordered = values.sorted(
                    lambda v: (v.attribute_id.sequence or 0, v.attribute_id.ids[0], v.sequence or 0, v.ids[0])
                )
                name = f"{name} ({', '.join(v.name for v in ordered)})"
            p.display_name = name

    # -- shared fields -------------------------------------------------------------------------------
    def _variant_shared_fields(self) -> list[str]:
        """Fields every variant takes from its template (extend to add more)."""
        return list(VARIANT_SHARED_FIELDS)

    def _variant_copied_fields(self) -> list[str]:
        """The shared fields that are copied: those stored on the record (an attachment-backed image has no
        column; it is refused on variants like every shared field but not copied)."""
        return [n for n in self._variant_shared_fields() if self._fields[n].store]

    def _template_values(self, template: Model, names: Iterable[str]) -> dict[str, Any]:
        """Values of ``names`` on ``template`` in write format (current language for translated fields)."""
        tmpl = template.elevated("variant sync")
        out: dict[str, Any] = {}
        for name in names:
            field = self._fields[name]
            value = tmpl[name]
            if isinstance(field, F.Many2one):
                value = value.ids[0] if value else None
            out[name] = value
        return out

    def _sync_from_template(self, field_names: Iterable[str] | None = None) -> None:
        """Copy the template's shared fields onto these variants (all of them, or ``field_names``)."""
        wanted = set(field_names) if field_names is not None else None
        names = [n for n in self._variant_copied_fields() if wanted is None or n in wanted]
        variants = self.elevated("variant sync").with_context(active_test=False, **{_SYNC_KEY: True})
        variants = variants.filtered(lambda v: v.template_id)
        if not names or not variants:
            return
        by_template: dict[int, list[int]] = defaultdict(list)
        for v in variants:
            by_template[v.template_id.ids[0]].append(v.ids[0])
        translated = [n for n in names if self._fields[n].translate]
        plain = [n for n in names if not self._fields[n].translate and n != "list_price"]
        for template_id, variant_ids in by_template.items():
            template = variants.browse([template_id])
            group = variants.browse(variant_ids)
            if plain:
                group.write(self._template_values(template, plain))
            if "list_price" in names:
                base = template.list_price or 0.0
                by_extra: dict[float, list[int]] = defaultdict(list)
                for v in group:
                    by_extra[v.price_extra or 0.0].append(v.ids[0])
                for extra, ids in by_extra.items():
                    group.browse(ids).write({"list_price": base + extra})
        if translated:
            # every language of the template's text, not only the current one
            assignments = ", ".join(f'"{n}" = t."{n}"' for n in translated)
            table = self._table
            self.env.cr.execute(
                f'UPDATE "{table}" v SET {assignments} FROM "{table}" t '  # noqa: S608 - names from the registry
                "WHERE v.template_id = t.id AND v.id = ANY(:ids)",
                {"ids": variants.ids},
            )
            self.env.cache.invalidate(self._name, translated, variants.ids)
            variants.modified(translated)

    def _check_shared_writes(self, vals: dict[str, Any]) -> None:
        shared = [n for n in self._variant_shared_fields() if n in vals]
        if not shared:
            return
        for p in self.elevated().with_context(active_test=False):
            if not p.template_id:
                continue
            for name in shared:
                field = self._fields[name]
                new = field.convert_to_cache(vals[name], p)
                old = p[name]
                if isinstance(old, Model):
                    old = old.ids[0] if old else None
                if isinstance(field, F.Float | F.Monetary) and new is not None and old is not None:
                    changed = abs(float(new) - float(old)) > 1e-6
                else:
                    changed = (new or None) != (old or None)
                if changed:
                    raise UserError(
                        _(
                            "'{field}' is shared by every variant of '{template}': change it on the template",
                            field=translatable(field.label),
                            template=p.template_id.display_name,
                        ),
                        field=name,
                    )

    # -- CRUD ------------------------------------------------------------------------------------------
    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        shared = self._variant_copied_fields()
        has_variants = False
        for vals in batch:
            template_id = self._fields["template_id"].convert_to_cache(vals.get("template_id"), self)
            if template_id:  # a variant takes the template's shared values, whatever was given
                has_variants = True
                vals.update(self._template_values(self.browse([template_id]), shared))
        records = super().create(batch)
        if has_variants:
            records._sync_from_template()
        return records

    def write(self, vals: dict[str, Any]) -> bool:
        syncing = bool(self.env.context.get(_SYNC_KEY))
        if not syncing:
            self._check_shared_writes(vals)
        result = super().write(vals)
        if syncing:
            return result
        shared = [n for n in self._variant_shared_fields() if n in vals]
        products = self.elevated().with_context(active_test=False)
        if shared:
            variants = products.search([("template_id", "in", self.ids)])
            if variants:
                variants._sync_from_template(shared)
        if "template_id" in vals:
            products.filtered(lambda p: p.template_id)._sync_from_template()
        elif "attribute_value_ids" in vals:  # the extra price follows the combination
            products.filtered(lambda p: p.template_id)._sync_from_template(["list_price"])
        if vals.get("active") is False:  # archiving a template archives its variants
            products.search([("template_id", "in", self.ids), ("active", "=", True)]).with_context(
                **{_SYNC_KEY: True}
            ).write({"active": False})
        return result

    # -- variant generation --------------------------------------------------------------------------
    def _variant_code(self, values: Model) -> str | None:
        self.ensure_one()
        if not self.default_code:
            return None
        parts = [str(v.code or v.name or "").strip().upper().replace(" ", "") for v in values]
        return "-".join([self.default_code, *[p for p in parts if p]])

    def _combinations(self) -> list[list[int]]:
        """Value id combinations of the template's attribute lines, in line then value order."""
        self.ensure_one()
        lines = self.attribute_line_ids.elevated().sorted(lambda line: (line.sequence or 0, line.ids[0]))
        axes: list[list[int]] = []
        for line in lines:
            values = line.value_ids.filtered(lambda v, line=line: v.attribute_id == line.attribute_id)
            ordered = values.sorted(lambda v: (v.sequence or 0, v.ids[0]))
            if ordered:
                axes.append(ordered.ids)
        if not axes:
            return []
        count = 1
        for axis in axes:
            count *= len(axis)
        if count > MAX_VARIANTS:
            raise UserError(
                _(
                    "These attributes make {count} variants; the limit is {limit}",
                    count=count,
                    limit=MAX_VARIANTS,
                )
            )
        return [list(combo) for combo in itertools.product(*axes)]

    @api.expose
    def action_generate_variants(self) -> dict[str, int]:
        """Create the missing variants of these templates, archive the ones no longer possible and
        reactivate archived ones that are possible again. Variants are never deleted."""
        created = archived = reactivated = 0
        products = self.env["product.product"].with_context(active_test=False)
        values_model = self.env["product.attribute_value"]
        for template in self:
            if template.template_id:
                raise UserError(_("'{name}' is a variant: generate variants from its template", name=template.name))
            if not template.attribute_line_ids:
                raise UserError(_("Add attributes to '{name}' before generating variants", name=template.name))
            existing = products.search([("template_id", "=", template.ids[0])])
            by_key = {frozenset(v.attribute_value_ids.ids): v for v in existing}
            valid: set[frozenset[int]] = set()
            to_create: list[dict[str, Any]] = []
            for combo in template._combinations():
                key = frozenset(combo)
                valid.add(key)
                variant = by_key.get(key)
                if variant is None:
                    to_create.append(
                        {
                            "template_id": template.ids[0],
                            "attribute_value_ids": [("set", combo)],
                            "default_code": template._variant_code(values_model.browse(combo).elevated()),
                        }
                    )
                elif not variant.active:
                    variant.with_context(**{_SYNC_KEY: True}).write({"active": True})
                    variant._sync_from_template()
                    reactivated += 1
            if to_create:
                products.create(to_create)
                created += len(to_create)
            stale = [v for key, v in by_key.items() if key not in valid and v.active]
            for variant in stale:
                variant.with_context(**{_SYNC_KEY: True}).write({"active": False})
                archived += 1
        return {"created": created, "archived": archived, "reactivated": reactivated}
