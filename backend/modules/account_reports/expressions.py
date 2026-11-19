"""Report line expressions.

An expression is a sum of signed terms, e.g. ``type(equity) + type(income, expense)@before_fy``:

==================  ===================================================================================
``type(t, …)``      balance of the journal items on accounts of these types (``account.account`` types)
``code(p, …)``      balance of the items on accounts whose code starts with one of these prefixes
``tag(name, …)``    balance of the items on accounts carrying one of these account tags
``grid(NN, …)``     tax-report grid: items tagged ``+NN`` / ``-NN``, signed so that invoices count positively
                    and credit notes negatively (see ``docs/admin/reports.md``)
``cash(t, …)``      cash flow (direct method): the counterpart of bank and cash movements, i.e. minus the
                    balance of the non-cash items of entries that touch a bank / cash account, on accounts
                    of these types; ``cash(exchange)`` takes the company's exchange gain / loss accounts
                    (which the other ``cash`` terms leave out)
``line(CODE)``      the value of another line of the report (after its sign)
``children``        the sum of the line's children
==================  ===================================================================================

A term may carry ``@scope`` to override the line's date scope: ``period`` (the column's dates),
``as_of`` (everything up to the column's end), ``opening`` (before the column's start), ``fy_to_date``
(from the start of the fiscal year of the column's end) and ``before_fy`` (before that start).
An empty expression is zero.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from tessera.exceptions import ValidationError
from tessera.i18n import _

FUNCTIONS = ("type", "code", "tag", "grid", "cash", "line")
ACCOUNT_FUNCTIONS = ("type", "code", "tag", "cash")
SCOPES = ("period", "as_of", "opening", "fy_to_date", "before_fy")
_TOKEN = re.compile(
    r"\s*(?:(?P<op>[+-])|(?P<children>children\b)|(?P<func>[a-z_]+)\s*\((?P<args>[^()]*)\)(?:\s*@\s*(?P<scope>[a-z_]+))?)"
)


@dataclass(frozen=True)
class Term:
    sign: int
    func: str
    args: tuple[str, ...] = ()
    scope: str | None = None


@dataclass
class Expression:
    terms: list[Term] = field(default_factory=list)

    @property
    def line_refs(self) -> set[str]:
        return {t.args[0] for t in self.terms if t.func == "line"}

    @property
    def uses_children(self) -> bool:
        return any(t.func == "children" for t in self.terms)

    @property
    def account_based(self) -> bool:
        return any(t.func in ACCOUNT_FUNCTIONS for t in self.terms)


def parse(text: str | None) -> Expression:
    """Parse an expression; raises ``ValidationError`` with the offending position."""
    expr = Expression()
    source = (text or "").strip()
    pos = 0
    sign = 1
    expect_term = True
    while pos < len(source):
        match = _TOKEN.match(source, pos)
        if not match or match.end() == pos:
            raise ValidationError(
                _("Cannot read the expression '{expression}' at position {pos}", expression=source, pos=pos + 1),
                field="expression",
            )
        pos = match.end()
        if match.group("op"):
            if not expect_term and match.group("op") in "+-":
                sign = -1 if match.group("op") == "-" else 1
                expect_term = True
            elif expect_term:
                sign = -sign if match.group("op") == "-" else sign
            continue
        if not expect_term:
            raise ValidationError(
                _("Missing + or - before position {pos} in '{expression}'", pos=match.start() + 1, expression=source),
                field="expression",
            )
        if match.group("children"):
            expr.terms.append(Term(sign, "children"))
        else:
            func = match.group("func")
            if func not in FUNCTIONS:
                raise ValidationError(_("Unknown function '{name}'", name=func), field="expression")
            args = tuple(a.strip() for a in match.group("args").split(",") if a.strip())
            if not args:
                raise ValidationError(_("'{name}' needs at least one argument", name=func), field="expression")
            if func == "line" and len(args) != 1:
                raise ValidationError(_("'line' takes exactly one line code"), field="expression")
            scope = match.group("scope")
            if scope and scope not in SCOPES:
                raise ValidationError(_("Unknown date scope '{scope}'", scope=scope), field="expression")
            expr.terms.append(Term(sign, func, args, scope))
        sign = 1
        expect_term = False
    if expect_term and expr.terms:
        raise ValidationError(_("The expression '{expression}' ends with an operator", expression=source))
    return expr
