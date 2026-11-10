"""Field types.

A field is a descriptor on a model class *and* a piece of metadata. The registry
composes fields contributed by several modules (``_extend``), resolves ``related``
fields, computes dependency triggers and emits the schema. Values travel in four
shapes:

* **column** — what is stored in PostgreSQL (``convert_to_column``)
* **cache** — the Python value kept in the transaction cache (``convert_to_cache``)
* **record** — what ``record.field`` returns (recordsets for relations)
* **read** — JSON-friendly API shape (``convert_to_read``)

Only *explicitly passed* constructor arguments are recorded in ``_args``; when a later
module redeclares a field, its explicit arguments merge over the earlier definition and
everything else is kept (``fields.Field(help="…")`` changes attributes without knowing the
type).

x2many fields are written with command tuples::

    ("set", [ids])          replace all links
    ("add", id)             link one existing record
    ("remove", id)          unlink one record (many2many) / set inverse to NULL (one2many)
    ("delete", id)          unlink and delete the record
    ("create", {vals})      create and link
    ("update", id, {vals})  write on a linked record
    ("clear",)              remove every link

A plain list of ids is shorthand for ``("set", ids)``.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
from collections.abc import Callable, Iterable, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any, ClassVar

from tessera.exceptions import ValidationError

if TYPE_CHECKING:
    from tessera.orm.model import Model

Default = Any | Callable[["Model"], Any]
IndexSpec = bool | str  # True (btree) | "trigram" | "unique" | "btree_not_null"

_UNSET: Any = object()

# generic options and their defaults
_FIELD_OPTIONS: dict[str, Any] = {
    "required": False,
    "readonly": False,
    "index": False,
    "help": "",
    "compute": None,
    "depends": (),
    "store": None,
    "inverse": None,
    "search": None,
    "related": None,
    "translate": False,
    "company_dependent": False,
    "tracking": False,
    "groups": None,
    "copy": None,
    "pii": False,
    "readonly_if": None,
}


def _explicit(**candidates: Any) -> dict[str, Any]:
    """Keep only the type-specific arguments the caller actually passed (``_UNSET``/None skipped)."""
    return {k: v for k, v in candidates.items() if v is not None and v is not _UNSET}


class Field:
    """Base class of every field type."""

    type_name: ClassVar[str] = "field"
    column_type: ClassVar[str | None] = None  # SQL type; None → never stored
    relational: ClassVar[bool] = False
    comodel_name: str | None = None

    # populated by the registry
    name: str = ""
    model_name: str = ""
    module: str = ""

    def __init__(self, string: str | None = None, **kwargs: Any) -> None:
        self._args: dict[str, Any] = {}
        if string is not None:
            self._args["string"] = string
        self._args.update(kwargs)

        self.string = string
        default = kwargs.get("default", _UNSET)
        self.default = _UNSET if default is None else default
        self.required: bool = kwargs.get("required", False)
        self.readonly: bool = kwargs.get("readonly", False)
        self.index: IndexSpec = kwargs.get("index", False)
        self.help: str = kwargs.get("help", "")
        self.compute: str | None = kwargs.get("compute")
        self.depends: tuple[str, ...] = tuple(kwargs.get("depends", ()))
        self._store: bool | None = kwargs.get("store")
        self.inverse: str | None = kwargs.get("inverse")
        self.search: str | None = kwargs.get("search")
        self.related: str | None = kwargs.get("related")
        self.translate: bool = kwargs.get("translate", False)
        self.company_dependent: bool = kwargs.get("company_dependent", False)
        self.tracking: bool | int = kwargs.get("tracking", False)
        groups = kwargs.get("groups")
        self.groups: tuple[str, ...] = (groups,) if isinstance(groups, str) else tuple(groups or ())
        self._copy: bool | None = kwargs.get("copy")
        self.pii: bool = kwargs.get("pii", False)
        self.readonly_if: Any = kwargs.get("readonly_if")
        self.extra: dict[str, Any] = {k: v for k, v in kwargs.items() if k not in _FIELD_OPTIONS and k != "default"}

    # -- metadata ------------------------------------------------------------------
    @property
    def label(self) -> str:
        """Human label: ``string``, else derived from the name (``country_id`` → Country, ``tag_ids`` → Tags)."""
        if self.string:
            return self.string
        name = self.name
        if name.endswith("_ids") and len(name) > 4:
            return name[:-4].replace("_", " ").title() + "s"
        if name.endswith("_id") and len(name) > 3:
            name = name[:-3]
        return name.replace("_", " ").title()

    @property
    def has_default(self) -> bool:
        return self.default is not _UNSET

    @property
    def computed(self) -> bool:
        return bool(self.compute or self.related)

    @property
    def store(self) -> bool:
        if self._store is not None:
            return self._store
        return not self.computed and self.column_type is not None

    @property
    def recompute_stored(self) -> bool:
        """A computed value persisted by ``flush``: a column, or the join table of a computed many2many
        declared ``store=True`` (x2many fields have no column, so ``store`` is False for them)."""
        return bool(self.compute) and (self.store or bool(self._store))

    @property
    def copy(self) -> bool:
        if self._copy is not None:
            return self._copy
        return not self.computed

    @property
    def column_name(self) -> str:
        return self.name

    @property
    def sql_type(self) -> str | None:
        if self.translate or self.company_dependent:
            return "jsonb"
        return self.column_type

    def merge(self, other: Field) -> Field:
        """Return a new field combining ``self`` (earlier definition) with ``other`` (later)."""
        cls = type(other) if type(other) is not Field else type(self)
        args = {**self._args, **other._args}
        merged = cls(**args)
        merged.name, merged.model_name, merged.module = (
            self.name,
            self.model_name,
            other.module or self.module,
        )
        return merged

    def clone(self) -> Field:
        if isinstance(self, Id):
            return Id()
        new = type(self)(**self._args)
        new.name, new.model_name, new.module = self.name, self.model_name, self.module
        return new

    def describe(self) -> dict[str, Any]:
        """Metadata dictionary for ``core.model_field`` and the client."""
        return {
            "name": self.name,
            "type": self.type_name,
            "string": self.label,
            "help": self.help,
            "required": self.required,
            "readonly": self.readonly or (self.computed and not self.inverse),
            "store": self.store,
            "compute": self.compute,
            "related": self.related,
            "depends": list(self.depends),
            "translate": self.translate,
            "company_dependent": self.company_dependent,
            "tracking": bool(self.tracking),
            "groups": list(self.groups),
            "index": self.index if isinstance(self.index, str) else bool(self.index),
            "pii": self.pii,
            "comodel": self.comodel_name,
            "readonly_if": self.readonly_if,
            "copy": self.copy,
        }

    # -- conversions -----------------------------------------------------------------
    def convert_to_column(self, value: Any, record: Model) -> Any:
        return self.convert_to_cache(value, record)

    def convert_to_cache(self, value: Any, record: Model | None = None) -> Any:
        return value

    def convert_to_record(self, value: Any, record: Model) -> Any:
        return value

    def convert_to_read(self, value: Any, record: Model) -> Any:
        return value

    def convert_to_write(self, value: Any, record: Model) -> Any:
        return value

    def validate(self, value: Any, record: Model) -> None:
        if self.required and value in (None, "", False) and not isinstance(value, bool | int | float):
            raise ValidationError(f"Field '{self.string or self.name}' is required", field=self.name)

    def default_value(self, model: Model) -> Any:
        if not self.has_default:
            return None
        if self.default in (list, dict, set, tuple):  # default=list means "a new empty list", not list(records)
            return self.default()
        return self.default(model) if callable(self.default) else self.default

    # -- descriptor ------------------------------------------------------------------
    def __get__(self, record: Model | None, owner: type[Model] | None = None) -> Any:
        if record is None:
            return self
        return record._get_field_value(self)

    def __set__(self, record: Model, value: Any) -> None:
        record._set_field_value(self, value)

    def __set_name__(self, owner: type, name: str) -> None:
        if not self.name:
            self.name = name

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.model_name}.{self.name}>"


# -- scalar types ------------------------------------------------------------------------


class Boolean(Field):
    type_name = "boolean"
    column_type = "boolean"

    def convert_to_cache(self, value: Any, record: Model | None = None) -> Any:
        return bool(value) if value is not None else False

    def validate(self, value: Any, record: Model) -> None:  # booleans are never "missing"
        return None


class Integer(Field):
    type_name = "integer"
    column_type = "bigint"

    def convert_to_cache(self, value: Any, record: Model | None = None) -> Any:
        if value is None:
            return None
        if value in ("", False):
            return 0
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"'{value}' is not an integer for field '{self.name}'", field=self.name) from exc


class Float(Field):
    type_name = "float"
    column_type = "double precision"

    def __init__(self, string: str | None = None, *, digits: int | tuple[int, int] | None = None, **kw: Any) -> None:
        super().__init__(string, **_explicit(digits=digits), **kw)
        self.digits = digits

    @property
    def sql_type(self) -> str | None:
        if self.translate or self.company_dependent:
            return "jsonb"
        if self.digits is not None:
            precision, scale = self.digits if isinstance(self.digits, tuple) else (16, self.digits)
            return f"numeric({precision},{scale})"
        return self.column_type

    def convert_to_cache(self, value: Any, record: Model | None = None) -> Any:
        if value in (None, ""):
            return None
        try:
            value = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"'{value}' is not a number for field '{self.name}'", field=self.name) from exc
        if self.digits is not None:
            scale = self.digits[1] if isinstance(self.digits, tuple) else self.digits
            value = float(Decimal(str(value)).quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP))
        return value


class Monetary(Field):
    """Amount in the currency referenced by ``currency_field`` (default ``currency_id``)."""

    type_name = "monetary"
    column_type = "numeric(16,4)"

    def __init__(self, string: str | None = None, *, currency_field: str | None = None, **kw: Any) -> None:
        super().__init__(string, **_explicit(currency_field=currency_field), **kw)
        self.currency_field = currency_field or "currency_id"

    def convert_to_cache(self, value: Any, record: Model | None = None) -> Any:
        if value in (None, ""):
            return None
        try:
            return float(Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        except Exception as exc:
            raise ValidationError(f"'{value}' is not an amount for field '{self.name}'", field=self.name) from exc

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "currency_field": self.currency_field}


class Char(Field):
    type_name = "char"
    column_type = "varchar"

    def __init__(
        self, string: str | None = None, *, size: int | None = None, trim: bool | None = None, **kw: Any
    ) -> None:
        super().__init__(string, **_explicit(size=size, trim=trim), **kw)
        self.size = size
        self.trim = True if trim is None else trim

    def convert_to_cache(self, value: Any, record: Model | None = None) -> Any:
        if value is None or value is False:
            return None
        value = str(value)
        if self.trim:
            value = value.strip()
        if self.size and len(value) > self.size:
            raise ValidationError(f"Value too long for field '{self.name}' (max {self.size})", field=self.name)
        return value


class Text(Field):
    type_name = "text"
    column_type = "text"

    def convert_to_cache(self, value: Any, record: Model | None = None) -> Any:
        return None if value in (None, False) else str(value)


class Html(Field):
    """Rich text. Sanitised with ``nh3`` on write unless ``sanitize=False`` (trusted templates only)."""

    type_name = "html"
    column_type = "text"

    def __init__(self, string: str | None = None, *, sanitize: bool | None = None, **kw: Any) -> None:
        super().__init__(string, **_explicit(sanitize=sanitize), **kw)
        self.sanitize = True if sanitize is None else sanitize

    def convert_to_cache(self, value: Any, record: Model | None = None) -> Any:
        if value in (None, False):
            return None
        value = str(value)
        if self.sanitize:
            import nh3

            value = nh3.clean(value)
        return value


class Date(Field):
    type_name = "date"
    column_type = "date"

    @staticmethod
    def to_date(value: Any) -> dt.date | None:
        if value in (None, "", False):
            return None
        if isinstance(value, dt.datetime):
            return value.date()
        if isinstance(value, dt.date):
            return value
        if isinstance(value, str):
            return dt.date.fromisoformat(value[:10])
        raise ValidationError(f"'{value}' is not a date")

    def convert_to_cache(self, value: Any, record: Model | None = None) -> Any:
        try:
            return self.to_date(value)
        except ValueError as exc:
            raise ValidationError(f"'{value}' is not a date for field '{self.name}'", field=self.name) from exc

    def convert_to_read(self, value: Any, record: Model) -> Any:
        return value.isoformat() if value else None

    @staticmethod
    def today() -> dt.date:
        return dt.datetime.now(dt.UTC).date()


class Datetime(Field):
    """Stored as ``timestamptz`` in UTC; always timezone-aware in Python."""

    type_name = "datetime"
    column_type = "timestamptz"

    @staticmethod
    def to_datetime(value: Any) -> dt.datetime | None:
        if value in (None, "", False):
            return None
        if isinstance(value, dt.datetime):
            return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
        if isinstance(value, dt.date):
            return dt.datetime(value.year, value.month, value.day, tzinfo=dt.UTC)
        if isinstance(value, str):
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
        raise ValidationError(f"'{value}' is not a datetime")

    def convert_to_cache(self, value: Any, record: Model | None = None) -> Any:
        try:
            value = self.to_datetime(value)
        except ValueError as exc:
            raise ValidationError(f"'{value}' is not a datetime for field '{self.name}'", field=self.name) from exc
        return value.astimezone(dt.UTC) if value else None

    def convert_to_read(self, value: Any, record: Model) -> Any:
        return value.astimezone(dt.UTC).isoformat() if value else None

    @staticmethod
    def now() -> dt.datetime:
        return dt.datetime.now(dt.UTC).replace(microsecond=0)


SelectionSpec = Sequence[tuple[str, str]] | Callable[["Model"], Sequence[tuple[str, str]]] | str


class Selection(Field):
    type_name = "selection"
    column_type = "varchar"

    def __init__(self, selection: SelectionSpec | None = None, string: str | None = None, **kw: Any) -> None:
        selection = kw.pop("selection", None) if selection is None else selection
        super().__init__(string, **_explicit(selection=selection), **kw)
        self.selection = selection

    def get_selection(self, model: Model) -> list[tuple[str, str]]:
        sel = self.selection
        if sel is None:
            return []
        if isinstance(sel, str):
            return list(getattr(model, sel)())
        if callable(sel):
            return list(sel(model))
        return [(str(item[0]), str(item[1])) for item in sel]

    def convert_to_cache(self, value: Any, record: Model | None = None) -> Any:
        return None if value in (None, "", False) else str(value)

    def validate(self, value: Any, record: Model) -> None:
        super().validate(value, record)
        if value is None:
            return
        allowed = {k for k, _ in self.get_selection(record)}
        if allowed and value not in allowed:
            raise ValidationError(
                f"'{value}' is not a valid choice for field '{self.string or self.name}'",
                field=self.name,
                allowed=sorted(allowed),
            )

    def describe(self) -> dict[str, Any]:
        data = super().describe()
        data["selection"] = (
            [list(item) for item in self.selection] if isinstance(self.selection, list | tuple) else None
        )
        return data


class Json(Field):
    type_name = "json"
    column_type = "jsonb"

    def convert_to_cache(self, value: Any, record: Model | None = None) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValidationError(f"Invalid JSON for field '{self.name}'", field=self.name) from exc
        return value


class Binary(Field):
    """Binary content. Stored inline (``bytea``) when ``attachment=False``; otherwise the column
    holds nothing and the content lives in ``core.attachment`` (resolved by the ``core`` module)."""

    type_name = "binary"
    column_type = "bytea"

    def __init__(self, string: str | None = None, *, attachment: bool | None = None, **kw: Any) -> None:
        super().__init__(string, **_explicit(attachment=attachment), **kw)
        self.attachment = bool(attachment)

    @property
    def store(self) -> bool:
        if self.attachment:
            return False
        return super().store

    def convert_to_cache(self, value: Any, record: Model | None = None) -> Any:
        if value in (None, "", False):
            return None
        if isinstance(value, bytes | memoryview):
            return bytes(value)
        if isinstance(value, str):
            return base64.b64decode(value)
        raise ValidationError(f"Binary field '{self.name}' expects bytes or base64", field=self.name)

    def convert_to_read(self, value: Any, record: Model) -> Any:
        return base64.b64encode(value).decode("ascii") if value else None


class Image(Binary):
    type_name = "image"

    def __init__(self, string: str | None = None, *, max_size: tuple[int, int] | None = _UNSET, **kw: Any) -> None:
        super().__init__(string, **_explicit(max_size=max_size), **kw)
        self.max_size = (1920, 1920) if max_size is _UNSET else max_size


# -- relational types ----------------------------------------------------------------------

OnDelete = str  # "set_null" | "restrict" | "cascade"


class Many2one(Field):
    type_name = "many2one"
    column_type = "bigint"
    relational = True

    def __init__(
        self,
        comodel_name: str,
        string: str | None = None,
        *,
        ondelete: OnDelete | None = None,
        domain: Any = None,
        context: dict[str, Any] | None = None,
        fk: bool | None = None,
        **kw: Any,
    ) -> None:
        kw.setdefault("index", True)
        super().__init__(
            string,
            comodel_name=comodel_name,
            **_explicit(ondelete=ondelete, domain=domain, context=context, fk=fk),
            **kw,
        )
        self.comodel_name = comodel_name
        self.ondelete = ondelete or "set_null"
        self.domain = domain
        self.context = context or {}
        self.fk = True if fk is None else fk  # False → no database foreign key

    def convert_to_cache(self, value: Any, record: Model | None = None) -> Any:
        if value in (None, False, "", 0):
            return None
        if isinstance(value, int):
            return value
        if hasattr(value, "_ids"):
            ids = value._ids
            if len(ids) > 1:
                raise ValidationError(f"Many2one '{self.name}' expects a single record", field=self.name)
            return ids[0] if ids else None
        if isinstance(value, dict) and "id" in value:
            return int(value["id"])
        if isinstance(value, str) and value.isdigit():
            return int(value)
        raise ValidationError(f"'{value!r}' is not a valid reference for field '{self.name}'", field=self.name)

    def convert_to_record(self, value: Any, record: Model) -> Any:
        comodel = record.env[self.comodel_name or ""]
        return comodel.browse([value] if value else [])

    def convert_to_read(self, value: Any, record: Model) -> Any:
        if not value:
            return None
        target = record.env[self.comodel_name or ""].browse([value])
        try:
            name = target.display_name
        except Exception:
            name = None
        return {"id": value, "display_name": name}

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "ondelete": self.ondelete, "domain": self.domain}


class _X2many(Field):
    relational = True
    column_type = None

    def __init__(self, comodel_name: str, string: str | None = None, *, domain: Any = None, **kw: Any) -> None:
        super().__init__(string, comodel_name=comodel_name, **_explicit(domain=domain), **kw)
        self.comodel_name = comodel_name
        self.domain = domain

    @property
    def store(self) -> bool:
        return False  # links live in the comodel (one2many) or a join table (many2many)

    def convert_to_cache(self, value: Any, record: Model | None = None) -> Any:
        if value in (None, False):
            return ()
        if hasattr(value, "_ids"):
            return tuple(value._ids)
        return tuple(int(v) for v in value)

    def convert_to_record(self, value: Any, record: Model) -> Any:
        return record.env[self.comodel_name or ""].browse(list(value or ()))

    def convert_to_read(self, value: Any, record: Model) -> Any:
        return list(value or ())

    def validate(self, value: Any, record: Model) -> None:
        if self.required and not value:
            raise ValidationError(f"Field '{self.string or self.name}' is required", field=self.name)

    @staticmethod
    def normalize_commands(value: Any) -> list[tuple[Any, ...]]:
        """Turn write values into a list of command tuples."""
        if value in (None, False):
            return [("clear",)]
        if hasattr(value, "_ids"):
            return [("set", list(value._ids))]
        if isinstance(value, tuple) and value and isinstance(value[0], str):
            return [value]
        if isinstance(value, list | tuple):
            if all(isinstance(v, int) for v in value):
                return [("set", list(value))]
            commands: list[tuple[Any, ...]] = []
            for item in value:
                if isinstance(item, tuple | list) and item and isinstance(item[0], str):
                    commands.append(tuple(item))
                elif isinstance(item, int):
                    commands.append(("add", item))
                elif isinstance(item, dict):
                    commands.append(("create", item))
                else:
                    raise ValidationError(f"Invalid x2many command {item!r}")
            return commands
        raise ValidationError(f"Invalid x2many value {value!r}")


class One2many(_X2many):
    type_name = "one2many"

    def __init__(self, comodel_name: str, inverse_name: str, string: str | None = None, **kw: Any) -> None:
        super().__init__(comodel_name, string, inverse_name=inverse_name, **kw)
        self.inverse_name = inverse_name

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "inverse_name": self.inverse_name, "domain": self.domain}


class Many2many(_X2many):
    type_name = "many2many"

    def __init__(
        self,
        comodel_name: str,
        string: str | None = None,
        *,
        relation: str | None = None,
        column1: str | None = None,
        column2: str | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(comodel_name, string, **_explicit(relation=relation, column1=column1, column2=column2), **kw)
        self._relation = relation
        self._column1 = column1
        self._column2 = column2

    @property
    def relation(self) -> str:
        """Join table name: ``<table>__<field>`` unless given (D-006)."""
        if self._relation:
            return self._relation
        return f"{self.model_name.replace('.', '_')}__{self.name}"

    @property
    def column1(self) -> str:
        return self._column1 or f"{self.model_name.replace('.', '_')}_id"

    @property
    def column2(self) -> str:
        col = self._column2 or f"{(self.comodel_name or '').replace('.', '_')}_id"
        if col == self.column1:
            col = col[:-3] + "_other_id"
        return col

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "relation": self.relation, "domain": self.domain}


class Id(Field):
    """The primary key. Read-only, never written."""

    type_name = "integer"
    column_type = "bigint"

    def __init__(self) -> None:
        super().__init__("ID", readonly=True)
        self.name = "id"

    def __get__(self, record: Model | None, owner: type[Model] | None = None) -> Any:
        if record is None:
            return self
        return record._single_id()

    def __set__(self, record: Model, value: Any) -> None:
        raise ValidationError("The id field cannot be written")


class Related(Field):
    """Placeholder resolved by the registry into the target field's type with ``related=path``."""

    type_name = "related"

    def __init__(self, path: str | None = None, string: str | None = None, **kw: Any) -> None:
        related = kw.pop("related", None) or path
        if not related:
            raise ValidationError("Related() needs a field path")
        super().__init__(string, related=related, **kw)


class Reference(Field):
    """Polymorphic reference stored as ``"model.name,id"``."""

    type_name = "reference"
    column_type = "varchar"

    def __init__(self, selection: SelectionSpec | None = None, string: str | None = None, **kw: Any) -> None:
        selection = kw.pop("selection", None) if selection is None else selection
        super().__init__(string, **_explicit(selection=selection), **kw)
        self.selection = selection

    def convert_to_cache(self, value: Any, record: Model | None = None) -> Any:
        if value in (None, "", False):
            return None
        if hasattr(value, "_ids") and hasattr(value, "_name"):
            return f"{value._name},{value._single_id()}"
        if isinstance(value, str) and re.match(r"^[a-z_][a-z0-9_.]*,\d+$", value):
            return value
        raise ValidationError(f"'{value!r}' is not a valid reference for field '{self.name}'", field=self.name)

    def convert_to_record(self, value: Any, record: Model) -> Any:
        if not value:
            return None
        model_name, res_id = value.split(",", 1)
        return record.env[model_name].browse([int(res_id)])


def is_relational(field: Field) -> bool:
    return field.relational


def iter_field_types() -> Iterable[type[Field]]:
    yield from (
        Boolean,
        Integer,
        Float,
        Monetary,
        Char,
        Text,
        Html,
        Date,
        Datetime,
        Selection,
        Json,
        Binary,
        Image,
        Many2one,
        One2many,
        Many2many,
        Reference,
    )
