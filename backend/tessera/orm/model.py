"""The :class:`Model` base class: metadata declaration and the recordset API.

A model class instance is a *recordset*: an ordered tuple of ids bound to an
:class:`~tessera.orm.environment.Environment`. All CRUD goes through here, so access
rights, record rules, field-level access, audit hooks, tracking and recomputation apply
to every caller — controllers, imports, jobs and AI tools alike.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from sqlalchemy import bindparam, delete, func, insert, literal, select, update
from sqlalchemy import cast as sa_cast
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from tessera.exceptions import AccessError, MissingError, UserError, ValidationError
from tessera.orm import fields as F
from tessera.orm.domain import And, Domain, Leaf, Node, RawDomain, combine_and, parse, substitute
from tessera.orm.registry import current_loading_module, register_fragment

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.registry import Registry

Vals = dict[str, Any]
_SCALAR_BULK_FETCH_EXCLUDE = (F.Binary,)


def _module_of(cls: type) -> str:
    current = current_loading_module()
    if current:
        return current
    parts = cls.__module__.split(".")
    if "modules" in parts:
        idx = parts.index("modules")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return "kernel"


class Model:
    """Base class for every model (fragments, mixins and composed classes)."""

    # declaration attributes (class-level, overridable per fragment)
    _name: ClassVar[str] = ""
    _extend: ClassVar[str | None] = None
    _inherit: ClassVar[tuple[str, ...] | list[str]] = ()
    _abstract: ClassVar[bool] = False
    _description: ClassVar[str] = ""
    _order: ClassVar[str] = "id"
    _rec_name: ClassVar[str] = "name"
    _parent_name: ClassVar[str] = "parent_id"
    _table: ClassVar[str] = ""
    _auto: ClassVar[bool] = True
    _log_access: ClassVar[bool] = True
    _soft_delete: ClassVar[bool] = False
    _studio: ClassVar[bool] = True
    _transient: ClassVar[bool] = False
    _audit: ClassVar[bool] = False
    _indexes: ClassVar[list[tuple[str, list[str]]]] = []
    _sql_constraints: ClassVar[list[tuple[str, str, str]]] = []
    _global_search: ClassVar[list[str]] = []
    _ai_index: ClassVar[list[str]] = []
    _register: ClassVar[bool] = True

    # populated by the registry on composed classes
    _fields: ClassVar[dict[str, F.Field]] = {}
    _fragments: ClassVar[tuple[type[Model], ...]] = ()
    _mixin_names: ClassVar[tuple[str, ...]] = ()
    _registry: ClassVar[Registry | None] = None
    _module: ClassVar[str] = "kernel"
    _constraint_methods: ClassVar[list[tuple[tuple[str, ...], str]]] = []
    _onchange_methods: ClassVar[dict[str, list[str]]] = {}
    _exposed_methods: ClassVar[set[str]] = set()

    __slots__ = ("_ids", "_prefetch_ids", "env")
    env: Environment
    _ids: tuple[int, ...]
    _prefetch_ids: tuple[int, ...]

    def __getattr__(self, name: str) -> Any:
        """Fields are descriptors installed on the composed class; this only runs for unknown
        names and exists so static type checkers accept ``record.<field>`` access."""
        raise AttributeError(f"{self._name} has no field or attribute '{name}'")

    def __setattr__(self, name: str, value: Any) -> None:
        """Plain attribute set (descriptors handle fields); declared so ``record.field = value``
        type-checks."""
        object.__setattr__(self, name, value)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.__dict__.get("_register", True):
            if "_inherit" in cls.__dict__ and isinstance(cls.__dict__["_inherit"], str):
                cls._inherit = (cls.__dict__["_inherit"],)
            register_fragment(cls, _module_of(cls))

    # ------------------------------------------------------------------ construction
    @classmethod
    def _browse(cls, env: Environment, ids: Iterable[int], prefetch_ids: Iterable[int] | None = None) -> Model:
        rec = object.__new__(cls)
        rec.env = env
        rec._ids = tuple(ids)
        rec._prefetch_ids = tuple(prefetch_ids) if prefetch_ids is not None else rec._ids
        return rec

    def browse(self, ids: int | Iterable[int] | None = None) -> Model:
        if ids is None:
            ids = ()
        elif isinstance(ids, int):
            ids = (ids,)
        return self._browse(self.env, [int(i) for i in ids])

    # ------------------------------------------------------------------ recordset basics
    @property
    def ids(self) -> list[int]:
        return list(self._ids)

    def _single_id(self) -> int:
        self.ensure_one()
        return self._ids[0]

    def ensure_one(self) -> Model:
        if len(self._ids) != 1:
            raise ValidationError(f"Expected a single {self._name} record, got {len(self._ids)}")
        return self

    def exists(self) -> Model:
        if not self._ids:
            return self
        table = self.env.registry.table_for(self._name)
        rows = self.env.cr.fetchall(select(table.c.id).where(table.c.id.in_(list(set(self._ids)))))
        found = {r[0] for r in rows}
        return self.browse([i for i in self._ids if i in found])

    def __len__(self) -> int:
        return len(self._ids)

    def __bool__(self) -> bool:
        return bool(self._ids)

    def __iter__(self) -> Iterator[Model]:
        for i in self._ids:
            yield self._browse(self.env, (i,), self._ids)

    def __contains__(self, item: object) -> bool:
        if isinstance(item, Model) and item._name == self._name:
            return all(i in self._ids for i in item._ids)
        if isinstance(item, str):
            return item in self._fields
        return False

    def __getitem__(self, key: str | int | slice) -> Any:
        if isinstance(key, str):
            return getattr(self, key)
        if isinstance(key, slice):
            return self.browse(self._ids[key])
        return self.browse((self._ids[key],))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Model) and other._name == self._name and set(other._ids) == set(self._ids)

    def __hash__(self) -> int:
        return hash((self._name, frozenset(self._ids)))

    def __or__(self, other: Model) -> Model:
        self._same_model(other)
        return self.browse(list(dict.fromkeys([*self._ids, *other._ids])))

    def __add__(self, other: Model) -> Model:
        return self | other

    def __sub__(self, other: Model) -> Model:
        self._same_model(other)
        return self.browse([i for i in self._ids if i not in other._ids])

    def __and__(self, other: Model) -> Model:
        self._same_model(other)
        return self.browse([i for i in self._ids if i in other._ids])

    def _same_model(self, other: Model) -> None:
        if not isinstance(other, Model) or other._name != self._name:
            raise TypeError(f"Cannot combine {self._name} with {getattr(other, '_name', type(other))}")

    def __repr__(self) -> str:
        return f"{self._name}{list(self._ids)}"

    def filtered(self, func: Callable[[Model], bool] | str) -> Model:
        if isinstance(func, str):
            name = func
            return self.browse([rec._ids[0] for rec in self if rec[name]])
        return self.browse([rec._ids[0] for rec in self if func(rec)])

    def mapped(self, func: Callable[[Model], Any] | str) -> Any:
        if isinstance(func, str):
            path = func.split(".")
            values: Any = self
            for seg in path:
                if isinstance(values, Model):
                    field = values._fields.get(seg)
                    if field is not None and field.relational and field.comodel_name:
                        ids: list[int] = []
                        for rec in values:
                            ids.extend(rec[seg]._ids)
                        values = self.env[field.comodel_name].browse(list(dict.fromkeys(ids)))
                    else:
                        values = [rec[seg] for rec in values]
                else:
                    values = [getattr(v, seg) for v in values]
            return values
        results = [func(rec) for rec in self]
        if results and all(isinstance(r, Model) for r in results):
            out = results[0].browse([])
            for r in results:
                out = out | r
            return out
        return results

    def sorted(self, key: Callable[[Model], Any] | str | None = None, reverse: bool = False) -> Model:
        if key is None:
            return self.search([("id", "in", self.ids)], order=self._order) if self._ids else self
        keyfn = (lambda r: r[key]) if isinstance(key, str) else key
        return self.browse([r._ids[0] for r in sorted(self, key=keyfn, reverse=reverse)])

    # ------------------------------------------------------------------ environment switches
    def with_env(self, env: Environment) -> Model:
        return self._browse(env, self._ids, self._prefetch_ids)

    def with_context(self, **values: Any) -> Model:
        return self.with_env(self.env.with_context(**values))

    def with_user(self, user: int | Model) -> Model:
        return self.with_env(self.env.with_user(user))

    def with_company(self, company: int | Model) -> Model:
        return self.with_env(self.env.with_company(company))

    def elevated(self, reason: str = "") -> Model:
        return self.with_env(self.env.elevated(reason))

    # ------------------------------------------------------------------ access control
    def check_access_rights(self, operation: str, raise_exception: bool = True) -> bool:
        if self.env.is_system:
            return True
        allowed = self.env.registry.security.check_model_access(self.env, self._name, operation)
        if not allowed and raise_exception:
            raise AccessError(
                f"You are not allowed to {operation} '{self._description}' ({self._name}) records",
                model=self._name,
                operation=operation,
            )
        return allowed

    def _rule_domain(self, operation: str) -> Node | None:
        if self.env.is_system:
            return None
        return self.env.registry.security.rule_domain(self.env, self._name, operation)

    def check_access_rule(self, operation: str) -> None:
        """Verify every record of ``self`` satisfies the record rules for ``operation``."""
        if self.env.is_system or not self._ids:
            return
        rule = self._rule_domain(operation)
        if rule is None:
            return
        from tessera.orm.sql import Query

        node = combine_and(Leaf("id", "in", list(set(self._ids))), rule)
        count = self.env.cr.scalar(Query(self).count(node))
        if count != len(set(self._ids)):
            raise AccessError(
                f"You are not allowed to {operation} some of these '{self._description}' records",
                model=self._name,
                operation=operation,
            )

    def check_access(self, operation: str) -> None:
        self.check_access_rights(operation)
        self.check_access_rule(operation)

    def _check_field_access(self, field: F.Field, operation: str) -> bool:
        if not field.groups or self.env.is_system:
            return True
        return bool(self.env.registry.security.has_groups(self.env, field.groups))

    def _readable_fields(self) -> list[F.Field]:
        return [f for f in self._fields.values() if self._check_field_access(f, "read")]

    # ------------------------------------------------------------------ domains
    def _domain_variables(self) -> Callable[[str], Any]:
        env = self.env

        def resolve(name: str) -> Any:
            if name == "uid":
                return env.uid
            if name == "company":
                return env.company.ids[0] if env.company else None
            if name == "companies":
                return env.companies.ids
            if name == "now":
                return env.now()
            if name == "today":
                return env.today()
            if name == "user" or name.startswith("user."):
                value: Any = env.user.elevated()
                for part in name.split(".")[1:]:
                    value = value[part]
                if isinstance(value, Model):
                    return value.ids if len(value.ids) != 1 else value.ids[0]
                return value
            raise ValidationError(f"Unknown domain variable '${name}'")

        return resolve

    def _search_domain(self, domain: RawDomain, operation: str = "read", *, apply_rules: bool = True) -> Node:
        node = substitute(parse(domain), self._domain_variables())
        extra: list[Node] = []
        if self.env.active_test and "active" in self._fields and "active" not in Domain(node).fields():
            extra.append(Leaf("active", "=", True))
        if self._soft_delete and "deleted_at" in self._fields and not self.env.context.get("include_deleted"):
            extra.append(Leaf("deleted_at", "is_not_set"))
        if apply_rules:
            rule = self._rule_domain(operation)
            if rule is not None:
                extra.append(rule)
        return combine_and(node, *extra) if extra else node

    # ------------------------------------------------------------------ search
    def search(
        self, domain: RawDomain = None, offset: int = 0, limit: int | None = None, order: str | None = None
    ) -> Model:
        from tessera.orm.sql import Query

        self.check_access_rights("read")
        node = self._search_domain(domain)
        stmt = Query(self).select_ids(node, order, limit, offset)
        rows = self.env.cr.fetchall(stmt)
        return self.browse([r[0] for r in rows])

    def search_count(self, domain: RawDomain = None) -> int:
        from tessera.orm.sql import Query

        self.check_access_rights("read")
        node = self._search_domain(domain)
        return int(self.env.cr.scalar(Query(self).count(node)) or 0)

    def search_read(
        self,
        domain: RawDomain = None,
        fields: Sequence[str] | None = None,
        offset: int = 0,
        limit: int | None = None,
        order: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.search(domain, offset=offset, limit=limit, order=order).read(fields)

    def read_group(
        self,
        domain: RawDomain,
        aggregates: Sequence[str],
        groupby: Sequence[str],
        offset: int = 0,
        limit: int | None = None,
        orderby: str | None = None,
    ) -> list[dict[str, Any]]:
        from tessera.orm.sql import Query

        self.check_access_rights("read")
        node = self._search_domain(domain)
        groupby = [groupby] if isinstance(groupby, str) else list(groupby)
        stmt = Query(self).read_group(node, aggregates, groupby, orderby, limit, offset)
        rows = self.env.cr.execute(stmt).mappings().all()
        out: list[dict[str, Any]] = []
        for row in rows:
            item: dict[str, Any] = {}
            group_domain: list[Node] = []
            for spec in groupby:
                fname, _, granularity = spec.partition(":")
                label = spec.replace(":", "__")
                value = row[label]
                field = self._fields[fname]
                if isinstance(field, F.Many2one) and value is not None:
                    target = self.env[field.comodel_name or ""].browse([value])
                    item[spec] = {"id": value, "display_name": target.elevated().display_name}
                    group_domain.append(Leaf(fname, "=", value))
                elif granularity and value is not None:
                    start = value
                    item[spec] = start.isoformat() if hasattr(start, "isoformat") else start
                    end = _granularity_end(start, granularity)
                    group_domain.append(Leaf(fname, ">=", start))
                    group_domain.append(Leaf(fname, "<", end))
                else:
                    item[spec] = field.convert_to_read(value, self) if value is not None else None
                    group_domain.append(Leaf(fname, "=", value))
            item["__count"] = int(row["__count"])
            for spec in aggregates:
                fname, _, fn = spec.partition(":")
                fn = fn or "sum"
                key = f"{fname}__{fn}"
                val = row[key]
                item[f"{fname}:{fn}"] = (
                    float(val)
                    if val is not None and fn not in {"count", "count_distinct"} and not isinstance(val, int)
                    else val
                )
            item["__domain"] = (
                combine_and(parse(domain), *group_domain).to_json() if group_domain else parse(domain).to_json()
            )
            out.append(item)
        return out

    def name_search(
        self, name: str = "", domain: RawDomain = None, operator: str = "ilike", limit: int | None = 100
    ) -> list[tuple[int, str]]:
        search_domain = parse(domain) if domain is not None else And(())
        if name:
            search_domain = combine_and(search_domain, Leaf("display_name", operator, name))
        records = self.search(search_domain, limit=limit)
        return [(rec._ids[0], rec.display_name) for rec in records]

    # ------------------------------------------------------------------ reading
    def _cache_qualifier(self, field: F.Field) -> Any:
        """Cache bucket: values are scoped per user (system users share one scope) so that a
        rule-restricted user never reads a value another environment fetched; translatable and
        company-dependent fields are further scoped by language / company."""
        scope: Any = "sys" if self.env.is_system else self.env.uid
        if field.translate:
            return (scope, self.env.lang)
        if field.company_dependent:
            return (scope, self.env.company.ids[0] if self.env.company else 0)
        return scope

    def _get_field_value(self, field: F.Field) -> Any:
        self.ensure_one()
        if not self._check_field_access(field, "read"):
            raise AccessError(
                f"You are not allowed to read field '{field.name}' of {self._name}",
                model=self._name,
                field=field.name,
            )
        rid = self._ids[0]
        cache = self.env.cache
        qualifier = self._cache_qualifier(field)
        if not cache.contains(field, rid, qualifier):
            if field.store:
                self._fetch([field.name])
            elif field.compute:
                self._compute_field(field)
            elif isinstance(field, F._X2many):
                self._fetch_x2many(field)
            else:
                cache.set(field, rid, None, qualifier)
        value = cache.get(field, rid, qualifier, default=None)
        return field.convert_to_record(value, self)

    def _set_field_value(self, field: F.Field, value: Any) -> None:
        self.ensure_one()
        key = (self._name, field.name)
        if key in self.env.cache.computing or (field.compute and not field.store and not field.inverse):
            cache_value = field.convert_to_cache(value, self)
            self.env.cache.set(field, self._ids[0], cache_value, self._cache_qualifier(field))
            return
        self.write({field.name: value})

    def _fetch(self, field_names: Iterable[str]) -> None:
        """Load stored columns for the prefetch set into the cache (applies read rules)."""
        from tessera.orm.sql import Query

        if not self._ids:
            return
        requested = [self._fields[n] for n in field_names if n != "id"]
        wanted: list[F.Field] = []
        for fld in requested:
            if fld.store and not isinstance(fld, _SCALAR_BULK_FETCH_EXCLUDE):
                # fetch every cheap stored field alongside, once
                wanted = [
                    f
                    for f in self._fields.values()
                    if f.store
                    and f.name != "id"
                    and not isinstance(f, _SCALAR_BULK_FETCH_EXCLUDE)
                    and self._check_field_access(f, "read")
                ]
                break
        for fld in requested:
            if fld.store and fld not in wanted:
                wanted.append(fld)
        if not wanted:
            return
        qualifiers = {f: self._cache_qualifier(f) for f in wanted}
        ids = [
            i
            for i in self._prefetch_ids
            if any(not self.env.cache.contains(f, i, qualifiers[f]) for f in requested if f.store)
        ]
        if self._ids[0] not in ids:
            ids.append(self._ids[0])
        ids = list(dict.fromkeys(ids))
        query = Query(self)
        table = query.table
        cols = [table.c.id] + [query.column(f).label(f.name) for f in wanted]
        node: Node = Leaf("id", "in", ids)
        rule = self._rule_domain("read")
        if rule is not None:
            node = combine_and(node, rule)
        hide_trashed = (
            self._soft_delete
            and "deleted_at" in self._fields
            and not self.env.is_system
            and not self.env.context.get("include_deleted")
        )
        if hide_trashed:
            node = combine_and(node, Leaf("deleted_at", "is_not_set"))
        stmt = select(*cols).where(query.where(node))
        rows = self.env.cr.execute(stmt).mappings().all()
        seen: set[int] = set()
        for row in rows:
            rid = row["id"]
            seen.add(rid)
            for f in wanted:
                self.env.cache.set(f, rid, f.convert_to_cache(row[f.name], self), qualifiers[f])
        missing = [i for i in ids if i not in seen]
        if missing and self._ids[0] in missing:
            exists_stmt = select(table.c.id).where(table.c.id.in_(missing))
            if hide_trashed:
                exists_stmt = exists_stmt.where(table.c.deleted_at.is_(None))
            existing = {r[0] for r in self.env.cr.fetchall(exists_stmt)}
            if self._ids[0] in existing:
                raise AccessError(
                    f"You are not allowed to read this '{self._description}' record",
                    model=self._name,
                    id=self._ids[0],
                )
            raise MissingError(
                f"Record {self._name}({self._ids[0]}) does not exist or has been deleted",
                model=self._name,
                id=self._ids[0],
            )

    def _fetch_x2many(self, field: F._X2many) -> None:
        if not self._ids:
            return
        qualifier = self._cache_qualifier(field)
        ids = [i for i in self._prefetch_ids if not self.env.cache.contains(field, i, qualifier)] or list(self._ids)
        comodel = self.env[field.comodel_name or ""]
        co_table = self.env.registry.table_for(comodel._name)
        extra: list[Node] = []
        if field.domain:
            extra.append(parse(field.domain))
        if comodel.env.active_test and "active" in comodel._fields:
            extra.append(Leaf("active", "=", True))
        if comodel._soft_delete and "deleted_at" in comodel._fields and not self.env.context.get("include_deleted"):
            extra.append(Leaf("deleted_at", "is_not_set"))
        from tessera.orm.sql import Query

        co_query = Query(comodel)
        cond = co_query.where(combine_and(*extra) if extra else And(()))
        links: dict[int, list[int]] = {i: [] for i in ids}
        if isinstance(field, F.One2many):
            inv = co_table.c[field.inverse_name]
            stmt = select(co_table.c.id, inv).where(inv.in_(ids)).where(cond)
            stmt = co_query._apply_order(stmt, comodel._order)
            for cid, parent in self.env.cr.fetchall(stmt):
                links[parent].append(cid)
        else:
            assert isinstance(field, F.Many2many)
            rel = self.env.registry.m2m_tables[field.relation]
            stmt = (
                select(rel.c[field.column1], co_table.c.id)
                .select_from(rel.join(co_table, co_table.c.id == rel.c[field.column2]))
                .where(rel.c[field.column1].in_(ids))
                .where(cond)
            )
            stmt = co_query._apply_order(stmt, comodel._order)
            for parent, cid in self.env.cr.fetchall(stmt):
                links[parent].append(cid)
        qualifier = self._cache_qualifier(field)
        for rid, linked in links.items():
            self.env.cache.set(field, rid, tuple(linked), qualifier)
        del qualifier

    def read(self, fields: Sequence[str] | None = None, load: str = "display") -> list[dict[str, Any]]:
        self.check_access_rights("read")
        if not self._ids:
            return []
        names = (
            list(fields)
            if fields
            else [f.name for f in self._fields.values() if not isinstance(f, F.Binary) or f.name in (fields or ())]
        )
        unknown = [n for n in names if n not in self._fields]
        if unknown:
            raise ValidationError(f"Unknown field(s) {unknown} on {self._name}")
        for n in names:
            if not self._check_field_access(self._fields[n], "read"):
                raise AccessError(f"You are not allowed to read field '{n}' of {self._name}", model=self._name, field=n)
        stored = [n for n in names if n != "id" and self._fields[n].store and not isinstance(self._fields[n], F.Binary)]
        if stored:
            self._fetch(stored)
        out: list[dict[str, Any]] = []
        for rec in self:
            row: dict[str, Any] = {"id": rec._ids[0]}
            for n in names:
                if n == "id":
                    continue
                field = self._fields[n]
                value = rec[n]
                if isinstance(field, F.Many2one):
                    row[n] = (
                        (field.convert_to_read(value._ids[0], rec) if value else None)
                        if load == "display"
                        else (value._ids[0] if value else None)
                    )
                elif isinstance(field, F._X2many):
                    row[n] = value.ids
                elif isinstance(field, F.Reference):
                    row[n] = f"{value._name},{value._ids[0]}" if value else None
                else:
                    row[n] = field.convert_to_read(value, rec)
            out.append(row)
        return out

    def fields_get(self, allfields: Sequence[str] | None = None) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for name, field in self._fields.items():
            if allfields and name not in allfields:
                continue
            if not self._check_field_access(field, "read"):
                continue
            desc = field.describe()
            if isinstance(field, F.Selection):
                desc["selection"] = field.get_selection(self)
            out[name] = desc
        return out

    # ------------------------------------------------------------------ defaults
    def default_get(self, field_names: Sequence[str]) -> Vals:
        defaults: Vals = {}
        for name in field_names:
            field = self._fields.get(name)
            if field is None:
                continue
            ctx_key = f"default_{name}"
            if ctx_key in self.env.context:
                defaults[name] = self.env.context[ctx_key]
            elif field.has_default:
                value = field.default_value(self)
                if value is not None:
                    defaults[name] = value
        return defaults

    # ------------------------------------------------------------------ writing
    def _split_vals(self, vals: Vals) -> tuple[Vals, dict[str, list[tuple[Any, ...]]], Vals]:
        columns: Vals = {}
        x2many: dict[str, list[tuple[Any, ...]]] = {}
        inverse: Vals = {}
        for name, value in vals.items():
            field = self._fields.get(name)
            if field is None:
                raise ValidationError(f"Unknown field '{name}' on {self._name}", field=name)
            if name == "id":
                continue
            if not self._check_field_access(field, "write"):
                raise AccessError(
                    f"You are not allowed to write field '{name}' of {self._name}",
                    model=self._name,
                    field=name,
                )
            if isinstance(field, F._X2many):
                x2many[name] = F._X2many.normalize_commands(value)
            elif field.compute and field.inverse and not field.store:
                inverse[name] = value
            elif field.compute and not field.inverse and not self.env.context.get("_recompute_write"):
                raise ValidationError(f"Field '{name}' on {self._name} is computed and cannot be written", field=name)
            else:
                if field.inverse and field.compute:
                    inverse[name] = value
                columns[name] = field.convert_to_cache(value, self)
        return columns, x2many, inverse

    def create(self, vals_list: Vals | Sequence[Vals]) -> Model:
        batch: list[Vals] = [dict(vals_list)] if isinstance(vals_list, Mapping) else [dict(v) for v in vals_list]
        self.check_access_rights("create")
        self.env.registry.run_hooks("before_create", self, batch)
        table = self.env.registry.table_for(self._name)
        created_ids: list[int] = []
        pending: list[tuple[Vals, dict[str, list[tuple[Any, ...]]], Vals]] = []
        now = self.env.now()
        for vals in batch:
            missing = [n for n in self._fields if n not in vals]
            vals = {**self.default_get(missing), **vals}
            columns, x2many, inverse = self._split_vals(vals)
            for field in self._fields.values():
                if field.required and not field.compute and field.store and field.name != "id":
                    field.validate(columns.get(field.name), self)
            for name, value in list(columns.items()):
                self._fields[name].validate(value, self)
            row = self._columns_to_row(columns, create=True)
            if self._log_access:
                row.update(
                    {
                        "create_date": now,
                        "write_date": now,
                        "create_uid": self.env.uid,
                        "write_uid": self.env.uid,
                    }
                )
            stmt = insert(table).values(**row).returning(table.c.id)
            new_id = self._execute_dml(stmt, scalar=True)
            created_ids.append(int(new_id))
            for name, value in columns.items():
                self.env.cache.set(self._fields[name], int(new_id), value, self._cache_qualifier(self._fields[name]))
            pending.append((columns, x2many, inverse))
        records = self.browse(created_ids)
        for rec, (_columns, x2many, inverse) in zip(records, pending, strict=True):
            for name, commands in x2many.items():
                rec._write_x2many(self._fields[name], commands)  # type: ignore[arg-type]
            if inverse:
                rec._run_inverses(inverse)
        records.check_access_rule("create")
        changed: set[str] = set()
        for columns, x2many, inverse in pending:
            changed.update(columns)
            changed.update(x2many)
            changed.update(inverse)
        # every stored computed field gets an initial value, whatever its dependencies
        for field in self._fields.values():
            if field.compute and field.store:
                self.env.cache.invalidate_field(field, records.ids)
                self.env.add_to_compute(self._name, field.name, records.ids)
        records.modified(list(changed) or list(self._fields), create=True)
        self.env.flush()
        records._validate_constraints(list(changed) or list(self._fields))
        self.env.registry.run_hooks("after_create", records, batch)
        return records

    def write(self, vals: Vals) -> bool:
        if not self._ids:
            return True
        vals = dict(vals)
        if not vals:
            return True
        self.check_access_rights("write")
        self.check_access_rule("write")
        columns, x2many, inverse = self._split_vals(vals)
        for name, value in columns.items():
            self._fields[name].validate(value, self)
        relational_changed = [n for n in columns if isinstance(self._fields[n], F.Many2one)]
        old_values: dict[int, dict[str, Any]] = {}
        self.env.registry.run_hooks("before_write", self, vals, old_values)
        if relational_changed:
            self.modified(relational_changed)
        if columns:
            self._write_columns(columns)
        for name, commands in x2many.items():
            self._write_x2many(self._fields[name], commands)  # type: ignore[arg-type]
        if inverse:
            self._run_inverses(inverse)
        self.modified(list(vals))
        self.env.flush()
        self._validate_constraints(list(vals))
        self.env.registry.run_hooks("after_write", self, vals, old_values)
        return True

    def _columns_to_row(self, columns: Vals, *, create: bool) -> dict[str, Any]:
        row: dict[str, Any] = {}
        lang = self.env.lang
        default_lang = self.env.registry.default_lang
        company_key = str(self.env.company.ids[0] if self.env.company else 0)
        for name, value in columns.items():
            field = self._fields[name]
            if field.translate:
                payload = {lang: value}
                if create and lang != default_lang:
                    payload[default_lang] = value
                row[field.column_name] = payload
            elif field.company_dependent:
                row[field.column_name] = {company_key: value, "default": value} if create else {company_key: value}
            else:
                row[field.column_name] = field.convert_to_column(value, self)
        return row

    def _write_columns(self, columns: Vals, *, touch: bool = True) -> None:
        table = self.env.registry.table_for(self._name)
        row = self._columns_to_row(columns, create=False)
        values: dict[str, Any] = {}
        for name, value in row.items():
            field = self._fields[name]
            if field.translate or field.company_dependent:
                values[name] = func.coalesce(table.c[name], sa_cast(literal("{}"), JSONB)).op("||")(
                    bindparam(f"_j_{name}", value, type_=JSONB)
                )
            else:
                values[name] = value
        if touch and self._log_access:
            values["write_date"] = self.env.now()
            values["write_uid"] = self.env.uid
        stmt = update(table).where(table.c.id.in_(list(self._ids))).values(**values)
        self._execute_dml(stmt)
        for name, value in columns.items():
            field = self._fields[name]
            qualifier = self._cache_qualifier(field)
            self.env.cache.invalidate_field(field, self._ids)  # every scope: other users must re-fetch
            for rid in self._ids:
                self.env.cache.set(field, rid, value, qualifier)
        if touch and self._log_access:
            for fname in ("write_date", "write_uid"):
                self.env.cache.invalidate_field(self._fields[fname], self._ids)
            for rid in self._ids:
                self.env.cache.set(
                    self._fields["write_date"],
                    rid,
                    values["write_date"],
                    self._cache_qualifier(self._fields["write_date"]),
                )
                self.env.cache.set(
                    self._fields["write_uid"],
                    rid,
                    self.env.uid,
                    self._cache_qualifier(self._fields["write_uid"]),
                )

    def _execute_dml(self, stmt: Any, *, scalar: bool = False) -> Any:
        try:
            with self.env.cr.savepoint():
                result = self.env.cr.execute(stmt)
                return result.scalar() if scalar else result.rowcount
        except IntegrityError as exc:
            raise self._integrity_error(exc) from exc

    def _integrity_error(self, exc: IntegrityError) -> UserError:
        orig = getattr(exc, "orig", None)
        diag = getattr(orig, "diag", None)
        constraint = getattr(diag, "constraint_name", "") or ""
        sqlstate = getattr(orig, "sqlstate", "") or ""
        for name, _definition, message in self._sql_constraints:
            if constraint == f"{self._table}_{name}"[:63]:
                return ValidationError(message, constraint=name)
        if sqlstate == "23503":
            detail = getattr(diag, "message_detail", "") or ""
            if "is not present in table" in detail:
                return ValidationError(
                    f"A referenced record does not exist ({self._name}): {detail}",
                    detail=detail,
                    constraint=constraint,
                )
            return UserError(
                f"This '{self._description}' record is still referenced by other records "
                "and cannot be deleted or modified.",
                detail=detail,
                constraint=constraint,
            )
        if sqlstate == "23505":
            return ValidationError("A record with the same unique value already exists.", constraint=constraint)
        return UserError(f"Database constraint violated: {constraint or sqlstate}")

    def _run_inverses(self, inverse: Vals) -> None:
        by_method: dict[str, list[str]] = defaultdict(list)
        for name in inverse:
            by_method[self._fields[name].inverse or ""].append(name)
        for method_name, names in by_method.items():
            for rec in self:
                for name in names:
                    self.env.cache.set(
                        self._fields[name],
                        rec._ids[0],
                        self._fields[name].convert_to_cache(inverse[name], rec),
                        self._cache_qualifier(self._fields[name]),
                    )
            if method_name == "_inverse_related":
                for name in names:
                    self._inverse_related(self._fields[name])
            else:
                getattr(self, method_name)()

    def _write_x2many(self, field: F.One2many | F.Many2many, commands: list[tuple[Any, ...]]) -> None:
        comodel = self.env[field.comodel_name or ""]
        for rec in self:
            rid = rec._ids[0]
            current = list(rec.with_context(active_test=False)[field.name]._ids)
            for cmd in commands:
                kind = cmd[0]
                if kind == "set":
                    target = [int(i) for i in cmd[1]]
                    if isinstance(field, F.One2many):
                        to_unlink = [i for i in current if i not in target]
                        if to_unlink:
                            rec._o2m_detach(field, comodel.browse(to_unlink))
                        to_link = [i for i in target if i not in current]
                        if to_link:
                            comodel.browse(to_link).write({field.inverse_name: rid})
                    else:
                        rec._m2m_replace(field, target)
                    current = target
                elif kind == "add":
                    cid = int(cmd[1])
                    if cid not in current:
                        if isinstance(field, F.One2many):
                            comodel.browse([cid]).write({field.inverse_name: rid})
                        else:
                            rec._m2m_insert(field, [cid])
                        current.append(cid)
                elif kind == "remove":
                    cid = int(cmd[1])
                    if isinstance(field, F.One2many):
                        rec._o2m_detach(field, comodel.browse([cid]))
                    else:
                        rec._m2m_delete(field, [cid])
                    current = [i for i in current if i != cid]
                elif kind == "delete":
                    cid = int(cmd[1])
                    comodel.browse([cid]).unlink()
                    current = [i for i in current if i != cid]
                elif kind == "create":
                    vals = dict(cmd[1])
                    if isinstance(field, F.One2many):
                        vals[field.inverse_name] = rid
                        new = comodel.create(vals)
                    else:
                        new = comodel.create(vals)
                        rec._m2m_insert(field, new.ids)
                    current.extend(new.ids)
                elif kind == "update":
                    comodel.browse([int(cmd[1])]).write(dict(cmd[2]))
                elif kind == "clear":
                    if isinstance(field, F.One2many):
                        if current:
                            rec._o2m_detach(field, comodel.browse(current))
                    else:
                        rec._m2m_replace(field, [])
                    current = []
                else:
                    raise ValidationError(f"Unknown x2many command '{kind}'")
            self.env.cache.invalidate_field(field, [rid])
            self.env.cache.set(field, rid, tuple(current), self._cache_qualifier(field))

    def _o2m_detach(self, field: F.One2many, children: Model) -> None:
        inverse = children._fields[field.inverse_name]
        if (isinstance(inverse, F.Many2one) and inverse.ondelete == "cascade") or inverse.required:
            children.unlink()
        else:
            children.write({field.inverse_name: None})

    def _m2m_insert(self, field: F.Many2many, ids: Sequence[int]) -> None:
        if not ids:
            return
        rel = self.env.registry.m2m_tables[field.relation]
        rid = self._single_id()
        existing = {
            r[0]
            for r in self.env.cr.fetchall(
                select(rel.c[field.column2])
                .where(rel.c[field.column1] == rid)
                .where(rel.c[field.column2].in_(list(ids)))
            )
        }
        rows = [{field.column1: rid, field.column2: cid} for cid in ids if cid not in existing]
        if rows:
            self._execute_dml(insert(rel).values(rows))
        self._touch_m2m_inverse(field, ids)

    def _m2m_delete(self, field: F.Many2many, ids: Sequence[int]) -> None:
        rel = self.env.registry.m2m_tables[field.relation]
        self._execute_dml(
            delete(rel).where(rel.c[field.column1] == self._single_id()).where(rel.c[field.column2].in_(list(ids)))
        )
        self._touch_m2m_inverse(field, ids)

    def _m2m_replace(self, field: F.Many2many, ids: Sequence[int]) -> None:
        rel = self.env.registry.m2m_tables[field.relation]
        rid = self._single_id()
        current = [r[0] for r in self.env.cr.fetchall(select(rel.c[field.column2]).where(rel.c[field.column1] == rid))]
        self._execute_dml(delete(rel).where(rel.c[field.column1] == rid))
        if ids:
            self._execute_dml(
                insert(rel).values([{field.column1: rid, field.column2: cid} for cid in dict.fromkeys(ids)])
            )
        self._touch_m2m_inverse(field, list(set(current) | set(ids)))

    def _touch_m2m_inverse(self, field: F.Many2many, ids: Sequence[int]) -> None:
        """Invalidate the mirror many2many on the comodel (same relation table) and signal it changed."""
        if not ids:
            return
        comodel_cls = self.env.registry.models[field.comodel_name or ""]
        for other in comodel_cls._fields.values():
            if isinstance(other, F.Many2many) and other.relation == field.relation and other.comodel_name == self._name:
                self.env.cache.invalidate_field(other, ids)
                self.env[comodel_cls._name].browse(list(ids)).modified([other.name])

    # ------------------------------------------------------------------ deletion
    def unlink(self) -> bool:
        if not self._ids:
            return True
        self.check_access_rights("unlink")
        self.check_access_rule("unlink")
        records = self.exists()
        if not records:
            return True
        self.env.registry.run_hooks("before_unlink", records)
        # recompute dependants that point at these records before they disappear
        records._modified_dependants()
        if self._soft_delete and "deleted_at" in self._fields and not self.env.context.get("force_delete"):
            records.elevated("soft delete").with_context(_recompute_write=True)._write_columns(
                {"deleted_at": self.env.now(), "deleted_by": self.env.uid}
            )
        else:
            table = self.env.registry.table_for(self._name)
            records._execute_dml(delete(table).where(table.c.id.in_(records.ids)))
        self.env.cache.invalidate(self._name, None, records.ids)
        records._invalidate_inverse_caches()
        self.env.registry.run_hooks("after_unlink", self, records.ids)
        self.env.flush()
        return True

    def _invalidate_inverse_caches(self) -> None:
        """Drop cached relation values on other models that may point at ``self``."""
        for cls in self.env.registry.models.values():
            for field in cls._fields.values():
                if field.relational and field.comodel_name == self._name:
                    self.env.cache.invalidate(cls._name, [field.name])

    def restore(self) -> bool:
        """Undo a soft delete."""
        if not self._soft_delete:
            raise UserError(f"{self._description} records cannot be restored")
        self.check_access_rights("unlink")
        self.with_context(include_deleted=True).check_access_rule("unlink")
        self.elevated("restore").with_context(_recompute_write=True)._write_columns(
            {"deleted_at": None, "deleted_by": None}
        )
        return True

    # ------------------------------------------------------------------ recomputation
    def modified(self, field_names: Iterable[str], *, create: bool = False) -> None:
        """Signal that ``field_names`` changed on ``self``: schedule dependent recomputations."""
        registry = self.env.registry
        if not self._ids:
            return
        for fname in field_names:
            field = self._fields.get(fname)
            if isinstance(field, F.Many2one) and field.comodel_name:
                # the parent's one2many lists pointing back through this field are now stale
                co_cls = registry.models.get(field.comodel_name)
                if co_cls is not None:
                    for cofield in co_cls._fields.values():
                        if (
                            isinstance(cofield, F.One2many)
                            and cofield.comodel_name == self._name
                            and cofield.inverse_name == fname
                        ):
                            self.env.cache.invalidate(co_cls._name, [cofield.name])
            for trigger in registry.triggers.get((self._name, fname), ()):
                target_cls = registry.models.get(trigger.model)
                if target_cls is None:
                    continue
                target_field = target_cls._fields[trigger.field]
                if trigger.path:
                    affected = (
                        self.env[trigger.model]
                        .elevated()
                        .with_context(active_test=False, include_deleted=True)
                        .search([(trigger.path, "in", self.ids)])
                    )
                else:
                    affected = self.env[trigger.model].browse(self._ids)
                if not affected:
                    continue
                if target_field.store:
                    self.env.cache.invalidate_field(target_field, affected.ids)
                    self.env.add_to_compute(trigger.model, trigger.field, affected.ids)
                else:
                    self.env.cache.invalidate_field(target_field, affected.ids)

    def _modified_dependants(self) -> None:
        """Before deleting ``self``, mark records whose computed fields depend on them."""
        registry = self.env.registry
        seen: set[tuple[str, str]] = set()
        for (source_model, _source_field), triggers in registry.triggers.items():
            if source_model != self._name:
                continue
            for trigger in triggers:
                if not trigger.path or (trigger.model, trigger.field) in seen:
                    continue
                seen.add((trigger.model, trigger.field))
                affected = (
                    self.env[trigger.model]
                    .elevated()
                    .with_context(active_test=False, include_deleted=True)
                    .search([(trigger.path, "in", self.ids)])
                )
                if affected:
                    target_field = registry.models[trigger.model]._fields[trigger.field]
                    self.env.cache.invalidate_field(target_field, affected.ids)
                    if target_field.store:
                        self.env.add_to_compute(trigger.model, trigger.field, affected.ids)

    def _compute_field(self, field: F.Field) -> None:
        key = (self._name, field.name)
        cache = self.env.cache
        qualifier = self._cache_qualifier(field)
        ids = [i for i in self._prefetch_ids if not cache.contains(field, i, qualifier)] or list(self._ids)
        records = self.browse(ids)
        if key in cache.computing:
            for rid in ids:
                if not cache.contains(field, rid, qualifier):
                    cache.set(field, rid, None, qualifier)
            return
        cache.computing.add(key)
        try:
            if field.related:
                records._compute_related(field)
            elif field.compute == "_compute_display_name":
                records._compute_display_name()
            else:
                getattr(records, field.compute or "")()
        finally:
            cache.computing.discard(key)
        for rid in ids:
            if not cache.contains(field, rid, qualifier):
                cache.set(field, rid, None if not isinstance(field, F._X2many) else (), qualifier)

    def _recompute_field(self, field_name: str) -> None:
        """Compute a stored field for ``self`` and persist it (called by ``env.flush``)."""
        field = self._fields[field_name]
        existing = self.exists()
        if not existing:
            return
        qualifier = existing._cache_qualifier(field)
        existing.env.cache.invalidate_field(field, existing.ids)
        existing._compute_field(field)
        by_value: dict[Any, list[int]] = defaultdict(list)
        for rid in existing._ids:
            value = existing.env.cache.get(field, rid, qualifier, default=None)
            by_value[_hashable(value)].append(rid)
        for _, rids in by_value.items():
            value = existing.env.cache.get(field, rids[0], qualifier, default=None)
            existing.browse(rids).with_context(_recompute_write=True)._write_columns({field_name: value}, touch=False)
        existing.modified([field_name])

    def _compute_related(self, field: F.Field) -> None:
        path = (field.related or "").split(".")
        qualifier = self._cache_qualifier(field)
        for rec in self:
            value: Any = rec.elevated("related read")
            for seg in path:
                if isinstance(value, Model):
                    value = value[seg] if value else None
                else:
                    value = None
                    break
            if isinstance(value, Model):
                cache_value: Any = (
                    tuple(value._ids) if isinstance(field, F._X2many) else (value._ids[0] if value._ids else None)
                )
            else:
                cache_value = field.convert_to_cache(value, rec) if value is not None else None
            self.env.cache.set(field, rec._ids[0], cache_value, qualifier)

    def _inverse_related(self, field: F.Field) -> None:
        path = (field.related or "").split(".")
        for rec in self:
            target: Any = rec
            for seg in path[:-1]:
                target = target[seg]
            if isinstance(target, Model) and target:
                target.write({path[-1]: rec[field.name]})

    def _compute_display_name(self) -> None:
        rec_name = self._rec_name if self._rec_name in self._fields and self._rec_name != "display_name" else None
        field = self._fields["display_name"]
        qualifier = self._cache_qualifier(field)
        for rec in self:
            if rec_name:
                value = rec[rec_name]
                if isinstance(value, Model):
                    value = value.display_name if value else None
                name = str(value) if value not in (None, "", False) else f"{self._description} #{rec._ids[0]}"
            else:
                name = f"{self._description} #{rec._ids[0]}"
            self.env.cache.set(field, rec._ids[0], name, qualifier)

    def _validate_constraints(self, field_names: Iterable[str]) -> None:
        changed = set(field_names)
        for fields_, method in self._constraint_methods:
            if not fields_ or changed & set(fields_):
                getattr(self, method)()

    # ------------------------------------------------------------------ convenience
    def copy_data(self, default: Vals | None = None, *, nested: bool = False) -> list[Vals]:
        out: list[Vals] = []
        for rec in self:
            vals: Vals = {}
            for name, field in self._fields.items():
                if (
                    name in ("id", "create_date", "write_date", "create_uid", "write_uid", "display_name")
                    or not field.copy
                ):
                    continue
                if field.compute and not field.inverse and not field.store:
                    continue
                if field.compute and field.store:
                    continue
                value = rec[name]
                if isinstance(field, F.One2many):
                    children = value
                    vals[name] = [
                        ("create", {k: v for k, v in child.items() if k != field.inverse_name})
                        for child in children.copy_data(nested=True)
                    ]
                elif isinstance(field, F.Many2many):
                    vals[name] = [("set", value.ids)]
                elif isinstance(field, F.Many2one):
                    vals[name] = value._ids[0] if value else None
                else:
                    vals[name] = value
            if (
                not nested
                and self._rec_name in vals
                and isinstance(vals.get(self._rec_name), str)
                and not (default and self._rec_name in default)
            ):
                vals[self._rec_name] = f"{vals[self._rec_name]} (copy)"
            vals.update(default or {})
            out.append(vals)
        return out

    def copy(self, default: Vals | None = None) -> Model:
        self.ensure_one()
        return self.create(self.copy_data(default)[0])

    def name_create(self, name: str) -> tuple[int, str]:
        rec = self.create({self._rec_name: name})
        return rec._ids[0], rec.display_name

    def toggle_active(self) -> None:
        if "active" not in self._fields:
            raise UserError(f"{self._description} records cannot be archived")
        for rec in self:
            rec.write({"active": not rec["active"]})

    def action_archive(self) -> bool:
        if "active" in self._fields:
            self.filtered(lambda r: r["active"]).write({"active": False})
        return True

    def action_unarchive(self) -> bool:
        if "active" in self._fields:
            self.with_context(active_test=False).filtered(lambda r: not r["active"]).write({"active": True})
        return True

    def get_external_id(self) -> dict[int, str | None]:
        if not self._ids:
            return {}
        rows = self.env.cr.fetchall(
            "SELECT res_id, module || '.' || name FROM core_module_record WHERE model = :model AND res_id = ANY(:ids)",
            {"model": self._name, "ids": list(self._ids)},
        )
        found = {r[0]: r[1] for r in rows}
        return {i: found.get(i) for i in self._ids}


def _hashable(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, dict):
        return tuple(sorted(value.items()))
    return value


def _granularity_end(start: Any, granularity: str) -> Any:
    if not isinstance(start, dt.date | dt.datetime):
        return start
    if granularity == "day":
        return start + dt.timedelta(days=1)
    if granularity == "week":
        return start + dt.timedelta(weeks=1)
    if granularity == "month":
        year, month = (start.year + (start.month // 12), start.month % 12 + 1)
        return start.replace(year=year, month=month, day=1)
    if granularity == "quarter":
        month = start.month + 3
        year = start.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        return start.replace(year=year, month=month, day=1)
    return start.replace(year=start.year + 1, month=1, day=1)
