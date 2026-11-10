"""Extends ktest.book and ktest.author from module A."""

from __future__ import annotations

from tessera.exceptions import ValidationError
from tessera.orm import Model, api, fields


class BookExtension(Model):
    _extend = "ktest.book"

    isbn = fields.Char(index="unique", size=17)
    name = fields.Char(help="Overridden help from module B")  # merges attributes into the existing field
    is_long = fields.Boolean(compute="_compute_is_long", store=True)

    @api.depends("page_count")
    def _compute_is_long(self) -> None:
        for book in self:
            book.is_long = (book.page_count or 0) > 500

    @api.constrains("isbn")
    def _check_isbn(self) -> None:
        for book in self:
            if book.isbn and len(book.isbn.replace("-", "")) not in (10, 13):
                raise ValidationError("ISBN must have 10 or 13 digits", field="isbn")

    def action_publish(self) -> bool:  # type: ignore[override]
        result = super().action_publish()
        self.write({"page_count": self.page_count or 1})
        return result

    def _compute_title_upper(self) -> None:  # type: ignore[override]
        super()._compute_title_upper()
        for book in self:
            if book.isbn:
                book.title_upper = f"{book.title_upper} [{book.isbn}]"


class AuthorExtension(Model):
    _extend = "ktest.author"

    pen_name = fields.Char()


class Review(Model):
    _name = "ktest.review"
    _description = "Review"
    _inherit = ["core.multi_company"]

    book_id = fields.Many2one("ktest.book", required=True, ondelete="cascade")
    rating = fields.Integer(required=True)
    comment = fields.Text()
    book_isbn = fields.Related("book_id.isbn")
