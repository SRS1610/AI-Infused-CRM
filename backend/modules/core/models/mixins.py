"""Abstract mixins offered to every module."""

from __future__ import annotations

from tessera.orm import Model, fields


class MultiCompany(Model):
    """Adds ``company_id``; the security provider scopes such models to the user's companies."""

    _name = "core.multi_company"
    _abstract = True
    _description = "Multi-company mixin"

    company_id = fields.Many2one(
        "core.company",
        "Company",
        index=True,
        default=lambda self: self.env.company.ids[0] if self.env.company else None,
    )


class Archivable(Model):
    _name = "core.archivable"
    _abstract = True
    _description = "Archivable mixin"

    active = fields.Boolean(default=True)


class Audited(Model):
    """Every create/write/unlink is written to ``core.audit_log``."""

    _name = "core.audited"
    _abstract = True
    _description = "Audited mixin"
    _audit = True
