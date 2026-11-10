"""Numbering sequences (``INV/2026/00001``), per company and per code."""

from __future__ import annotations

import datetime as dt
from typing import Any

from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.orm import Model, api, fields


class Sequence(Model):
    _name = "core.sequence"
    _description = "Sequence"
    _order = "code, company_id"
    _audit = True

    name = fields.Char(required=True)
    code = fields.Char(required=True, index=True, help="Looked up by `next_by_code`")
    company_id = fields.Many2one("core.company", ondelete="cascade", index=True, help="Empty = shared by every company")
    prefix = fields.Char(help="Supports %(year)s %(y)s %(month)s %(day)s %(doy)s %(woy)s %(h24)s %(min)s %(sec)s")
    suffix = fields.Char()
    padding = fields.Integer(default=5)
    number_next = fields.Integer(default=1, required=True)
    step = fields.Integer(default=1, required=True)
    implementation = fields.Selection(
        [
            ("standard", "Standard (fast, may leave gaps on rollback)"),
            ("gapless", "Gapless (locks the sequence row)"),
        ],
        default="standard",
        required=True,
    )
    active = fields.Boolean(default=True)

    def _interpolate(self, text: str | None, when: dt.datetime) -> str:
        if not text:
            return ""
        values = {
            "year": when.strftime("%Y"),
            "y": when.strftime("%y"),
            "month": when.strftime("%m"),
            "day": when.strftime("%d"),
            "doy": when.strftime("%j"),
            "woy": when.strftime("%W"),
            "h24": when.strftime("%H"),
            "min": when.strftime("%M"),
            "sec": when.strftime("%S"),
        }
        try:
            return text % values
        except (KeyError, ValueError) as exc:
            raise UserError(_("Invalid sequence pattern '{text}': {exc}", text=text, exc=exc)) from exc

    def _pg_sequence_name(self) -> str:
        return f"core_sequence_{self.ids[0]}_seq"

    def _next_number(self) -> int:
        self.ensure_one()
        cr = self.env.cr
        if self.implementation == "gapless":
            row = cr.fetchone(
                "SELECT number_next, step FROM core_sequence WHERE id = :id FOR UPDATE", {"id": self.ids[0]}
            )
            number, step = int(row[0]), int(row[1])
            cr.execute(
                "UPDATE core_sequence SET number_next = :n WHERE id = :id",
                {"n": number + step, "id": self.ids[0]},
            )
            self.env.cache.set(self._fields["number_next"], self.ids[0], number + step)
            return number
        name = self._pg_sequence_name()
        cr.execute(
            f"CREATE SEQUENCE IF NOT EXISTS {name} START WITH {int(self.number_next)} INCREMENT BY {int(self.step)}"
        )
        return int(cr.scalar(f"SELECT nextval('{name}')"))

    def next_by_id(self) -> str:
        self.ensure_one()
        now = dt.datetime.now(self.env.tz)
        number = str(self._next_number()).zfill(int(self.padding or 0))
        return f"{self._interpolate(self.prefix, now)}{number}{self._interpolate(self.suffix, now)}"

    @api.model
    def next_by_code(self, code: str, company: Model | None = None) -> str:
        company = company if company is not None else self.env.company
        sequences = self.elevated("sequence allocation")
        seq = None
        if company:
            seq = sequences.search([("code", "=", code), ("company_id", "=", company.ids[0])], limit=1)
        if not seq:
            seq = sequences.search([("code", "=", code), ("company_id", "is_not_set")], limit=1)
        if not seq:
            raise UserError(_("No sequence defined for code '{code}'", code=code), code=code)
        return str(seq.next_by_id())

    def write(self, vals: dict[str, Any]) -> bool:
        result = super().write(vals)
        if "number_next" in vals or "step" in vals:
            for seq in self:
                if seq.implementation == "standard":
                    self.env.cr.execute(f"DROP SEQUENCE IF EXISTS {seq._pg_sequence_name()}")
        return result
