"""Which enrichment adapters administrators have approved (feature ``contacts.enrich``)."""

from __future__ import annotations

from tessera.exceptions import ValidationError
from tessera.orm import Model, api, fields


class EnrichmentSource(Model):
    _name = "contacts.enrichment_source"
    _description = "Enrichment source"
    _order = "name, id"
    _audit = True

    name = fields.Char(required=True)
    adapter = fields.Char(required=True, help="Key of a registered enrichment adapter, e.g. 'fixture'")
    approved = fields.Boolean(
        default=False, help="Only approved adapters are queried; contact data is sent to the adapter"
    )
    company_id = fields.Many2one(
        "core.company", ondelete="cascade", help="Empty = every company; a company's own record overrides it"
    )
    note = fields.Text(help="What the adapter sends and the provider's data-use terms")

    @api.constrains("adapter")
    def _check_adapter(self) -> None:
        from modules.contacts.enrichment import ADAPTERS

        for src in self:
            if src.adapter not in ADAPTERS:
                known = ", ".join(sorted(ADAPTERS)) or "none"
                raise ValidationError(f"Unknown enrichment adapter '{src.adapter}' (known: {known})", field="adapter")
