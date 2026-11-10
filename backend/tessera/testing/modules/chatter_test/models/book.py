from __future__ import annotations

from tessera.orm import Model, fields


class Book(Model):
    _extend = "ktest.book"
    _inherit = ["chatter.thread"]

    status = fields.Selection(tracking=True)
    author_id = fields.Many2one("ktest.author", tracking=True)
    user_id = fields.Many2one("core.user", "Responsible", tracking=True, ondelete="set_null")
