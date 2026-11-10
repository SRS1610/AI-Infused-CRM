from __future__ import annotations

from tessera.orm import Model, fields


class Book(Model):
    _extend = "ktest.book"

    publisher_id = fields.Many2one("contacts.partner", "Publisher", ondelete="set_null")
    reviewer_ids = fields.Many2many(
        "contacts.partner", relation="ktest_book__reviewer_ids", column1="book_id", column2="partner_id"
    )
