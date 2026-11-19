"""Domain → SQL compilation and query building (SQLAlchemy Core expressions).

Relational traversal uses sub-selects (``IN (SELECT …)`` / ``EXISTS``), which PostgreSQL
plans well and which compose with record rules. Ordering on a many2one field joins the
comodel and orders by its ``_order``/``_rec_name``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, Table, and_, cast, exists, func, literal, not_, or_, select, true
from sqlalchemy.sql import ColumnElement
from sqlalchemy.types import Text as SAText

from tessera.exceptions import ValidationError
from tessera.i18n import _
from tessera.orm import fields as F
from tessera.orm.domain import And, Leaf, Node, Not, Or, parse

if TYPE_CHECKING:
    from tessera.orm.model import Model


def _lang_text(col: Any, lang: str, fallback: str) -> ColumnElement[Any]:
    """Expression reading a translatable JSONB column for ``lang`` with fallback chain."""
    if lang == fallback:
        return func.coalesce(col[lang].astext, col[fallback].astext)
    return func.coalesce(col[lang].astext, col[fallback].astext)


def _company_text(col: Any, company_id: int | None, cast_type: Any = None) -> ColumnElement[Any]:
    key = str(company_id or 0)
    expr = func.coalesce(col[key].astext, col["default"].astext)
    return cast(expr, cast_type) if cast_type is not None else expr


class Query:
    """Builds ``SELECT`` statements for one model in one environment."""

    def __init__(self, model: Model) -> None:
        self.model = model
        self.env = model.env
        self.cls = type(model)
        self.registry = model.env.registry
        self.table: Table = self.registry.table_for(model._name)

    # -- public ---------------------------------------------------------------------------
    def where(self, node: Node, table: Table | None = None, cls: type[Model] | None = None) -> ColumnElement[Any]:
        return self._compile(node, table or self.table, cls or self.cls)

    def select_ids(
        self, node: Node, order: str | None = None, limit: int | None = None, offset: int | None = None
    ) -> Select[Any]:
        stmt: Select[Any] = select(self.table.c.id).where(self._compile(node, self.table, self.cls))
        stmt = self._apply_order(stmt, order or self.cls._order)
        if offset:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        return stmt

    def count(self, node: Node) -> Select[Any]:
        return select(func.count()).select_from(self.table).where(self._compile(node, self.table, self.cls))

    def column(self, field: F.Field, table: Table | None = None) -> ColumnElement[Any]:
        """Expression for a stored field's current-language / current-company value."""
        table = table if table is not None else self.table
        col = table.c[field.column_name]
        if field.translate:
            return _lang_text(col, self.env.lang, self.registry.default_lang)
        if field.company_dependent:
            cid = self.env.company.ids[0] if self.env.company else None
            return _company_text(col, cid)
        return col

    # -- order ---------------------------------------------------------------------------
    def _apply_order(self, stmt: Select[Any], order: str | None) -> Select[Any]:
        if not order:
            return stmt.order_by(self.table.c.id)
        clauses = []
        for part in order.split(","):
            part = part.strip()
            if not part:
                continue
            pieces = part.split()
            fname = pieces[0]
            direction = pieces[1].lower() if len(pieces) > 1 else "asc"
            nulls = pieces[2].lower() if len(pieces) > 2 else None
            field = self.cls._fields.get(fname)
            if field is None:
                raise ValidationError(
                    _("Cannot order by unknown field '{fname}' on {name}", fname=fname, name=self.cls._name)
                )
            if isinstance(field, F.Many2one) and field.store:
                co_cls = self.registry.models[field.comodel_name or ""]
                co_table = self.registry.table_for(co_cls._name).alias(f"ord_{fname}")
                stmt = stmt.outerjoin(co_table, co_table.c.id == self.table.c[fname])
                rec = (
                    co_cls._rec_name
                    if co_cls._rec_name in co_cls._fields and co_cls._fields[co_cls._rec_name].store
                    else None
                )
                expr: ColumnElement[Any] = co_table.c[rec] if rec else co_table.c.id
                if rec and co_cls._fields[rec].translate:
                    expr = _lang_text(co_table.c[rec], self.env.lang, self.registry.default_lang)
            elif field.store:
                expr = self.column(field)
            else:
                raise ValidationError(
                    _("Cannot order by non-stored field '{fname}' on {name}", fname=fname, name=self.cls._name)
                )
            clause = expr.desc() if direction == "desc" else expr.asc()
            if nulls == "first" or (nulls is None and direction == "desc"):
                clause = clause.nulls_first()
            elif nulls == "last":
                clause = clause.nulls_last()
            clauses.append(clause)
        clauses.append(self.table.c.id.asc())
        return stmt.order_by(*clauses)

    # -- compilation ---------------------------------------------------------------------
    def _compile(self, node: Node, table: Table, cls: type[Model]) -> ColumnElement[Any]:
        if isinstance(node, And):
            if not node.children:
                return true()
            return and_(*(self._compile(c, table, cls) for c in node.children))
        if isinstance(node, Or):
            if not node.children:
                return literal(False) == true()
            return or_(*(self._compile(c, table, cls) for c in node.children))
        if isinstance(node, Not):
            return not_(self._compile(node.child, table, cls))
        return self._compile_leaf(node, table, cls)

    def _compile_leaf(self, leaf: Leaf, table: Table, cls: type[Model]) -> ColumnElement[Any]:
        segments = leaf.segments
        head = segments[0]
        field = cls._fields.get(head)
        if field is None:
            raise ValidationError(_("Unknown field '{head}' in domain on {name}", head=head, name=cls._name))

        if len(segments) > 1:
            rest = Leaf(".".join(segments[1:]), leaf.operator, leaf.value)
            return self._traverse(field, rest, table, cls, negate=False)

        if head == "id" and leaf.operator in ("child_of", "parent_of"):
            ids = self._hierarchy_ids(cls, self._ids_from_value(leaf.value), leaf.operator)
            return table.c.id.in_(ids) if ids else literal(False) == true()

        if not field.store:
            return self._compile_non_stored(field, leaf, table, cls)

        op, value = leaf.operator, leaf.value
        if isinstance(field, F.Many2one):
            return self._compile_many2one(field, op, value, table, cls)
        if isinstance(field, F.One2many | F.Many2many):
            return self._compile_x2many(field, op, value, table, cls)
        if op in ("child_of", "parent_of"):
            raise ValidationError(_("Operator '{op}' only applies to relational fields", op=op))

        col = self.column(field, table)
        if op in ("any", "not_any"):
            raise ValidationError(_("Operator '{op}' only applies to relational fields", op=op))
        if op == "is_set":
            return col.isnot(None) if not isinstance(field, F.Boolean) else col.is_(True)
        if op == "is_not_set":
            return col.is_(None) if not isinstance(field, F.Boolean) else or_(col.is_(None), col.is_(False))
        if isinstance(field, F.Boolean):
            value = bool(value)
            if op == "=":
                return col.is_(True) if value else or_(col.is_(None), col.is_(False))
            if op == "!=":
                return or_(col.is_(None), col.is_(False)) if value else col.is_(True)
        value = self._coerce(field, value)
        if op == "=":
            return col.is_(None) if value is None else col == value
        if op == "!=":
            return col.isnot(None) if value is None else or_(col.is_(None), col != value)
        if op in (">", ">=", "<", "<="):
            if value is None:
                raise ValidationError(_("Operator '{op}' requires a value", op=op))
            comparisons: dict[str, ColumnElement[Any]] = {
                ">": col > value,
                ">=": col >= value,
                "<": col < value,
                "<=": col <= value,
            }
            return comparisons[op]
        if op in ("in", "not_in"):
            values = self._as_list(value)
            values = [self._coerce(field, v) for v in values]
            has_null = any(v is None for v in values)
            values = [v for v in values if v is not None]
            expr: ColumnElement[Any] = col.in_(values) if values else literal(False) == true()
            if has_null:
                expr = or_(expr, col.is_(None))
            return (
                expr
                if op == "in"
                else not_(expr) & (col.isnot(None) if not has_null else true())
                | (col.is_(None) if not has_null else literal(False) == true())
            )
        text_col = cast(col, SAText) if not isinstance(field, F.Char | F.Text | F.Html | F.Selection) else col
        if op in ("like", "not_like", "ilike", "not_ilike"):
            pattern = str(value if value is not None else "")
            if "%" not in pattern and "_" not in pattern:
                pattern = f"%{pattern}%"
            expr = text_col.ilike(pattern) if op.endswith("ilike") else text_col.like(pattern)
            return expr if not op.startswith("not") else or_(text_col.is_(None), not_(expr))
        if op == "starts_with":
            return text_col.ilike(f"{value}%")
        raise ValidationError(_("Unsupported operator '{op}'", op=op))

    def _coerce(self, field: F.Field, value: Any) -> Any:
        if value is None or (value is False and not isinstance(field, F.Boolean)):
            return (
                None
                if value is None
                or isinstance(field, F.Char | F.Text | F.Html | F.Selection | F.Many2one | F.Date | F.Datetime)
                else value
            )
        if isinstance(field, F.Date) and isinstance(value, str):
            return F.Date.to_date(value)
        if isinstance(field, F.Datetime) and isinstance(value, str | dt.date):
            return F.Datetime.to_datetime(value)
        if isinstance(field, F.Integer) and isinstance(value, str) and value.lstrip("-").isdigit():
            return int(value)
        if isinstance(field, F.Float | F.Monetary) and isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return value
        return value

    @staticmethod
    def _as_list(value: Any) -> list[Any]:
        if hasattr(value, "_ids"):
            return list(value._ids)
        if isinstance(value, list | tuple | set | frozenset):
            return list(value)
        return [value]

    def _ids_from_value(self, value: Any) -> list[int]:
        ids: list[int] = []
        for v in self._as_list(value):
            if v is None or v is False:
                continue
            if isinstance(v, dict) and "id" in v:
                ids.append(int(v["id"]))
            elif isinstance(v, int):
                ids.append(v)
            elif isinstance(v, str) and v.isdigit():
                ids.append(int(v))
            else:
                raise ValidationError(_("'{value}' is not a record id", value=repr(v)))
        return ids

    # -- relational leaves --------------------------------------------------------------
    def _comodel_query(self, comodel_name: str) -> tuple[type[Model], Any]:
        """The comodel class and an aliased table for it (an ``Alias`` behaves like a ``Table`` here)."""
        co_cls = self.registry.models[comodel_name]
        return co_cls, self.registry.table_for(comodel_name).alias()

    def _comodel_filter(self, co_cls: type[Model], co_table: Table, node: Node) -> ColumnElement[Any]:
        cond = self._compile(node, co_table, co_cls)
        if co_cls._soft_delete and "deleted_at" in co_cls._fields:
            cond = and_(cond, co_table.c.deleted_at.is_(None))
        return cond

    def _compile_many2one(
        self, field: F.Many2one, op: str, value: Any, table: Table, cls: type[Model]
    ) -> ColumnElement[Any]:
        col = table.c[field.column_name]
        comodel = field.comodel_name or ""
        if op == "is_set":
            return col.isnot(None)
        if op == "is_not_set":
            return col.is_(None)
        if op in ("any", "not_any"):
            co_cls, co_table = self._comodel_query(comodel)
            sub = select(co_table.c.id).where(self._comodel_filter(co_cls, co_table, parse(value)))
            return col.in_(sub) if op == "any" else or_(col.is_(None), col.notin_(sub))
        if op in ("child_of", "parent_of"):
            co_cls = self.registry.models[comodel]
            ids = self._hierarchy_ids(co_cls, self._ids_from_value(value), op)
            return col.in_(ids) if ids else literal(False) == true()
        if (
            isinstance(value, str)
            and not value.isdigit()
            and op in ("=", "!=", "like", "ilike", "not_like", "not_ilike", "starts_with")
        ):
            co_cls, co_table = self._comodel_query(comodel)
            rec = co_cls._rec_name if co_cls._rec_name in co_cls._fields else "id"
            sub_op = "ilike" if op in ("=", "like", "ilike", "starts_with") else "not_ilike"
            if op == "=":
                sub_op = "ilike"
            sub = select(co_table.c.id).where(
                self._comodel_filter(
                    co_cls, co_table, Leaf(rec, sub_op if op != "starts_with" else "starts_with", value)
                )
            )
            expr: ColumnElement[Any] = col.in_(sub)
            return expr if op in ("=", "like", "ilike", "starts_with") else or_(col.is_(None), not_(expr))
        if op == "=":
            ids = self._ids_from_value(value)
            return col.is_(None) if not ids else col == ids[0]
        if op == "!=":
            ids = self._ids_from_value(value)
            return col.isnot(None) if not ids else or_(col.is_(None), col != ids[0])
        if op in ("in", "not_in"):
            raw = self._as_list(value)
            has_null = any(v in (None, False) for v in raw)
            ids = self._ids_from_value(value)
            expr = col.in_(ids) if ids else literal(False) == true()
            if has_null:
                expr = or_(expr, col.is_(None))
            if op == "in":
                return expr
            return not_(expr) if has_null else or_(col.is_(None), col.notin_(ids) if ids else true())
        if op in (">", ">=", "<", "<="):
            ids = self._ids_from_value(value)
            return {">": col > ids[0], ">=": col >= ids[0], "<": col < ids[0], "<=": col <= ids[0]}[op]
        raise ValidationError(_("Unsupported operator '{op}' for many2one '{name}'", op=op, name=field.name))

    def _x2many_subquery(
        self,
        field: F.One2many | F.Many2many,
        table: Table,
        co_cls: type[Model],
        co_table: Table,
        cond: ColumnElement[Any],
    ) -> ColumnElement[Any]:
        """EXISTS(...) linking ``table`` rows to comodel rows satisfying ``cond``."""
        if isinstance(field, F.One2many):
            inv = co_table.c[field.inverse_name]
            return exists(select(literal(1)).select_from(co_table).where(and_(inv == table.c.id, cond)))
        rel = self.registry.m2m_tables[field.relation].alias()
        return exists(
            select(literal(1))
            .select_from(rel.join(co_table, co_table.c.id == rel.c[field.column2]))
            .where(and_(rel.c[field.column1] == table.c.id, cond))
        )

    def _compile_x2many(
        self, field: F.One2many | F.Many2many, op: str, value: Any, table: Table, cls: type[Model]
    ) -> ColumnElement[Any]:
        co_cls, co_table = self._comodel_query(field.comodel_name or "")
        base_cond: ColumnElement[Any] = true()
        if field.domain:
            base_cond = self._compile(parse(field.domain), co_table, co_cls)
        if op in ("is_set", "is_not_set"):
            e = self._x2many_subquery(field, table, co_cls, co_table, base_cond)
            return e if op == "is_set" else not_(e)
        if op in ("any", "not_any"):
            cond = and_(base_cond, self._comodel_filter(co_cls, co_table, parse(value)))
            e = self._x2many_subquery(field, table, co_cls, co_table, cond)
            return e if op == "any" else not_(e)
        if op in ("child_of", "parent_of"):
            ids = self._hierarchy_ids(co_cls, self._ids_from_value(value), op)
            e = self._x2many_subquery(
                field, table, co_cls, co_table, co_table.c.id.in_(ids) if ids else literal(False) == true()
            )
            return e
        if isinstance(value, str) and not value.isdigit():
            rec = co_cls._rec_name if co_cls._rec_name in co_cls._fields else "id"
            sub_op = (
                "ilike"
                if op in ("=", "like", "ilike", "in")
                else "not_ilike"
                if op in ("not_like", "not_ilike")
                else op
            )
            leaf = Leaf(rec, "starts_with", value) if sub_op == "starts_with" else Leaf(rec, "ilike", value)
            cond = and_(base_cond, self._compile(leaf, co_table, co_cls))
            e = self._x2many_subquery(field, table, co_cls, co_table, cond)
            return e if op in ("=", "like", "ilike", "in", "starts_with") else not_(e)
        ids = self._ids_from_value(value)
        if op in ("=", "in"):
            if not ids:
                return not_(self._x2many_subquery(field, table, co_cls, co_table, base_cond))
            return self._x2many_subquery(field, table, co_cls, co_table, and_(base_cond, co_table.c.id.in_(ids)))
        if op in ("!=", "not_in"):
            if not ids:
                return self._x2many_subquery(field, table, co_cls, co_table, base_cond)
            return not_(self._x2many_subquery(field, table, co_cls, co_table, and_(base_cond, co_table.c.id.in_(ids))))
        raise ValidationError(_("Unsupported operator '{op}' for x2many '{name}'", op=op, name=field.name))

    def _traverse(self, field: F.Field, rest: Leaf, table: Table, cls: type[Model], negate: bool) -> ColumnElement[Any]:
        if not field.relational or not field.comodel_name:
            raise ValidationError(
                _("Cannot traverse non-relational field '{field}' on {model}", field=field.name, model=cls._name)
            )
        co_cls, co_table = self._comodel_query(field.comodel_name)
        if isinstance(field, F.Many2one):
            if not field.store:
                raise ValidationError(_("Cannot traverse non-stored many2one '{name}'", name=field.name))
            sub = select(co_table.c.id).where(self._comodel_filter(co_cls, co_table, rest))
            return table.c[field.column_name].in_(sub)
        cond: ColumnElement[Any] = self._comodel_filter(co_cls, co_table, rest)
        if isinstance(field, F._X2many) and field.domain:
            cond = and_(cond, self._compile(parse(field.domain), co_table, co_cls))
        return self._x2many_subquery(field, table, co_cls, co_table, cond)  # type: ignore[arg-type]

    def _compile_non_stored(self, field: F.Field, leaf: Leaf, table: Table, cls: type[Model]) -> ColumnElement[Any]:
        if field.related:
            return self._compile_leaf(Leaf(field.related, leaf.operator, leaf.value), table, cls)
        if field.search:
            method = getattr(self.env[cls._name], field.search)
            domain = method(leaf.operator, leaf.value)
            return self._compile(parse(domain), table, cls)
        if isinstance(field, F.One2many | F.Many2many):
            return self._compile_x2many(field, leaf.operator, leaf.value, table, cls)
        if field.name == "display_name":
            rec = cls._rec_name if cls._rec_name in cls._fields and cls._fields[cls._rec_name].store else None
            if rec:
                return self._compile_leaf(Leaf(rec, leaf.operator, leaf.value), table, cls)
        raise ValidationError(
            _("Field '{field}' on {model} is not stored and cannot be searched", field=field.name, model=cls._name)
        )

    def _hierarchy_ids(self, co_cls: type[Model], ids: Sequence[int], op: str) -> list[int]:
        parent = co_cls._parent_name
        if parent not in co_cls._fields:
            raise ValidationError(
                _("Model '{name}' has no parent field '{parent}' for {op}", name=co_cls._name, parent=parent, op=op)
            )
        if not ids:
            return []
        table = self.registry.table_for(co_cls._name)
        if op == "child_of":
            base = select(table.c.id).where(table.c.id.in_(list(ids))).cte(recursive=True, name="hier")
            child = table.alias()
            cte = base.union_all(select(child.c.id).where(child.c[parent] == base.c.id))
        else:
            base = (
                select(table.c.id, table.c[parent].label("parent"))
                .where(table.c.id.in_(list(ids)))
                .cte(recursive=True, name="hier")
            )
            par = table.alias()
            cte = base.union_all(select(par.c.id, par.c[parent].label("parent")).where(par.c.id == base.c.parent))
        rows = self.env.cr.fetchall(select(cte.c.id).distinct())
        return [r[0] for r in rows]

    # -- aggregation ---------------------------------------------------------------------
    def read_group(
        self,
        node: Node,
        aggregates: Sequence[str],
        groupby: Sequence[str],
        order: str | None,
        limit: int | None,
        offset: int | None,
    ) -> Select[Any]:
        group_exprs: list[Any] = []
        labels: list[str] = []
        for spec in groupby:
            fname, _sep, granularity = spec.partition(":")
            field = self.cls._fields.get(fname)
            if field is None or not field.store:
                raise ValidationError(_("Cannot group by '{fname}' on {name}", fname=fname, name=self.cls._name))
            expr = self.column(field)
            if granularity:
                if not isinstance(field, F.Date | F.Datetime):
                    raise ValidationError(
                        _("Granularity '{granularity}' only applies to date fields", granularity=granularity)
                    )
                if granularity not in ("day", "week", "month", "quarter", "year"):
                    raise ValidationError(_("Unknown granularity '{granularity}'", granularity=granularity))
                expr = func.date_trunc(granularity, expr)
            label = spec.replace(":", "__")
            group_exprs.append(expr.label(label))
            labels.append(label)
        agg_exprs: list[Any] = [func.count().label("__count")]
        for spec in aggregates:
            fname, _sep, fn = spec.partition(":")
            fn = fn or "sum"
            field = self.cls._fields.get(fname)
            if field is None or not field.store:
                raise ValidationError(_("Cannot aggregate '{fname}' on {name}", fname=fname, name=self.cls._name))
            col = self.column(field)
            if fn not in ("sum", "avg", "min", "max", "count", "count_distinct"):
                raise ValidationError(_("Unknown aggregate '{fn}'", fn=fn))
            agg = func.count(func.distinct(col)) if fn == "count_distinct" else getattr(func, fn)(col)
            agg_exprs.append(agg.label(f"{fname}__{fn}"))
        stmt: Select[Any] = (
            select(*group_exprs, *agg_exprs).select_from(self.table).where(self._compile(node, self.table, self.cls))
        )
        if group_exprs:
            stmt = stmt.group_by(*list(group_exprs))
        order_clauses = []
        if order:
            for part in order.split(","):
                pieces = part.strip().split()
                if not pieces:
                    continue
                key = pieces[0].replace(":", "__")
                direction = pieces[1].lower() if len(pieces) > 1 else "asc"
                target = next((e for e in [*group_exprs, *agg_exprs] if e.name == key), None)
                if target is None:
                    raise ValidationError(_("Cannot order groups by '{order}'", order=pieces[0]))
                order_clauses.append(target.desc() if direction == "desc" else target.asc())
        elif group_exprs:
            order_clauses = [e.asc().nulls_first() for e in group_exprs]
        if order_clauses:
            stmt = stmt.order_by(*order_clauses)
        if offset:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        return stmt
