"""Global search index: one row per searchable record (models declaring ``_global_search``)."""

from __future__ import annotations

from tessera.orm import Model, fields


class SearchIndex(Model):
    _name = "web.search_index"
    _description = "Global search index"
    _rec_name = "title"
    _studio = False
    _log_access = False
    _sql_constraints = [("record_uniq", "UNIQUE (model, res_id)", "One index row per record")]

    model = fields.Char(required=True, index=True)
    res_id = fields.Integer(required=True)
    title = fields.Char()
    content = fields.Text(index="trigram")
    company_id = fields.Integer(index=True)
    indexed_at = fields.Datetime()
