"""Models exercising every kernel feature."""

from __future__ import annotations

from tessera.exceptions import ValidationError
from tessera.orm import Model, api, fields


class Author(Model):
    _name = "ktest.author"
    _description = "Author"
    _order = "name"
    _inherit = ["core.multi_company"]

    name = fields.Char(required=True, index=True, tracking=True)
    bio = fields.Text(translate=True)
    country_id = fields.Many2one("core.country", ondelete="set_null")
    book_ids = fields.One2many("ktest.book", "author_id", "Books")
    book_count = fields.Integer(compute="_compute_book_count", store=True)
    active = fields.Boolean(default=True)
    nickname = fields.Char(company_dependent=True)
    secret_notes = fields.Text(groups="core.group_system")

    @api.depends("book_ids")
    def _compute_book_count(self) -> None:
        for author in self:
            author.book_count = len(author.with_context(active_test=False).book_ids)


class Tag(Model):
    _name = "ktest.tag"
    _description = "Tag"
    _sql_constraints = [("name_uniq", "UNIQUE (name)", "Tag names must be unique")]

    name = fields.Char(required=True)
    color = fields.Integer(default=0)
    book_ids = fields.Many2many(
        "ktest.book", relation="ktest_book__tag_ids", column1="ktest_tag_id", column2="ktest_book_id"
    )


class Book(Model):
    _name = "ktest.book"
    _description = "Book"
    _order = "published desc, name"
    _soft_delete = True
    _audit = True
    _inherit = ["core.multi_company"]
    _global_search = ["name"]

    name = fields.Char("Title", required=True, index="trigram", tracking=True)
    description = fields.Text(translate=True)
    author_id = fields.Many2one("ktest.author", ondelete="restrict", index=True)
    author_name = fields.Related("author_id.name")
    author_country_code = fields.Char(related="author_id.country_id.code")
    tag_ids = fields.Many2many("ktest.tag")
    chapter_ids = fields.One2many("ktest.chapter", "book_id")
    page_count = fields.Integer(default=0)
    currency_id = fields.Many2one(
        "core.currency",
        default=lambda self: self.env.company.currency_id.ids[0] if self.env.company else None,
    )
    price = fields.Monetary(currency_field="currency_id")
    published = fields.Date()
    status = fields.Selection(
        [("draft", "Draft"), ("review", "In review"), ("published", "Published")],
        default="draft",
        required=True,
    )
    active = fields.Boolean(default=True)
    title_upper = fields.Char(compute="_compute_title_upper")
    total_words = fields.Integer(compute="_compute_total_words", store=True)
    reference = fields.Char(readonly=True, copy=False)

    @api.depends("name")
    def _compute_title_upper(self) -> None:
        for book in self:
            book.title_upper = (book.name or "").upper()

    @api.depends("chapter_ids.words")
    def _compute_total_words(self) -> None:
        for book in self:
            book.total_words = sum(book.chapter_ids.mapped("words"))

    @api.constrains("page_count")
    def _check_pages(self) -> None:
        for book in self:
            if book.page_count < 0:
                raise ValidationError("A book cannot have a negative page count", field="page_count")

    def create(self, vals_list):  # type: ignore[override]
        batch = [vals_list] if isinstance(vals_list, dict) else list(vals_list)
        for vals in batch:
            if not vals.get("reference"):
                vals["reference"] = self.env["core.sequence"].next_by_code("ktest.book")
        return super().create(batch)

    @api.expose
    def action_publish(self) -> bool:
        self.write({"status": "published"})
        return True


class Chapter(Model):
    _name = "ktest.chapter"
    _description = "Chapter"
    _order = "book_id, sequence, id"

    book_id = fields.Many2one("ktest.book", required=True, ondelete="cascade", index=True)
    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    words = fields.Integer(default=0)
    book_title = fields.Related("book_id.name")
