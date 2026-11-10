"""The model registry: composes model fragments from installed modules into final classes.

One registry exists per database. Building it:

1. collect fragments (classes with ``_name`` or ``_extend``) in module load order;
2. compose one class per model name (MRO = later fragments first, then mixins, then ``Model``);
3. merge fields, resolve ``related`` fields and placeholders, attach inverse links;
4. build SQLAlchemy ``Table`` objects for stored fields and many2many join tables;
5. compute dependency triggers for computed fields;
6. collect constraints, onchange handlers and exposed methods.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB

from tessera.db.engine import database_name, normalize_url
from tessera.exceptions import RegistryError
from tessera.logging import get_logger
from tessera.orm import fields as F
from tessera.security.provider import PermissiveProvider, SecurityProvider

if TYPE_CHECKING:
    from tessera.orm.model import Model

log = get_logger(__name__)

# fragments registered at class-definition time, grouped by owning module
_fragments_by_module: dict[str, list[type[Model]]] = defaultdict(list)
_loading_module: threading.local = threading.local()

HookName = str  # "after_create" | "after_write" | "before_unlink" | "after_unlink" | "registry_built"


def set_loading_module(name: str | None) -> None:
    _loading_module.name = name


def current_loading_module() -> str | None:
    return getattr(_loading_module, "name", None)


def register_fragment(cls: type[Model], module: str) -> None:
    cls._module = module
    _fragments_by_module[module].append(cls)


def fragments_for(module: str) -> list[type[Model]]:
    return list(_fragments_by_module.get(module, ()))


def forget_module_fragments(module: str) -> None:
    _fragments_by_module.pop(module, None)


# -- SQL type mapping -----------------------------------------------------------------------


def sa_type(sql_type: str) -> Any:
    t = sql_type.lower()
    if t == "varchar":
        return String()
    if t == "text":
        return Text()
    if t == "bigint":
        return BigInteger()
    if t == "boolean":
        return Boolean()
    if t == "double precision":
        return Float()
    if t.startswith("numeric"):
        inner = t[t.find("(") + 1 : t.find(")")] if "(" in t else ""
        if inner:
            p, s = (int(x) for x in inner.split(","))
            return Numeric(p, s)
        return Numeric()
    if t == "date":
        return Date()
    if t == "timestamptz":
        return DateTime(timezone=True)
    if t == "jsonb":
        return JSONB()
    if t == "bytea":
        return LargeBinary()
    if t.startswith("vector"):
        from pgvector.sqlalchemy import Vector

        dim = int(t[t.find("(") + 1 : t.find(")")]) if "(" in t else None
        return Vector(dim)
    raise RegistryError(f"Unsupported SQL type '{sql_type}'")


class Trigger:
    """Changing (``source_model``, ``source_field``) requires recomputing ``field`` on
    ``model`` records found through ``path`` (a domain path from ``model`` to the source)."""

    __slots__ = ("field", "model", "path")

    def __init__(self, model: str, field: str, path: str) -> None:
        self.model = model
        self.field = field
        self.path = path  # "" when the source is the same model

    def __repr__(self) -> str:
        return f"<Trigger {self.model}.{self.field} via '{self.path}'>"


class Registry:
    _registries: dict[str, Registry] = {}
    _lock = threading.RLock()

    def __init__(self, db_url: str) -> None:
        self.db_url = normalize_url(db_url)
        self.dbname = database_name(db_url)
        self.models: dict[str, type[Model]] = {}
        self.abstract: dict[str, type[Model]] = {}
        self.metadata = MetaData()
        self.tables: dict[str, Table] = {}
        self.m2m_tables: dict[str, Table] = {}
        self.module_order: list[str] = []  # modules whose code is loaded, in load order
        self.installed_versions: dict[str, str] = {}
        self.triggers: dict[tuple[str, str], list[Trigger]] = defaultdict(list)
        self.hooks: dict[HookName, list[Callable[..., Any]]] = defaultdict(list)
        self.security: SecurityProvider = PermissiveProvider()
        self.default_lang = "en_US"
        self.ready = False
        self.lock = threading.RLock()
        self.extra: dict[str, Any] = {}  # per-registry caches owned by platform modules
        # loaders for manifest `load` kinds other than security/data/demo (views, menus, ...)
        self.data_loaders: dict[str, Callable[..., Any]] = {}

    # -- lifecycle ---------------------------------------------------------------------------
    @classmethod
    def get(cls, db_url: str, *, load: bool = True) -> Registry:
        key = normalize_url(db_url)
        with cls._lock:
            reg = cls._registries.get(key)
            if reg is None:
                reg = cls(db_url)
                cls._registries[key] = reg
                if load:
                    from tessera.modules.installer import load_installed

                    load_installed(reg)
            return reg

    @classmethod
    def new(cls, db_url: str) -> Registry:
        """A fresh, empty registry replacing any cached one (used by install and tests)."""
        key = normalize_url(db_url)
        with cls._lock:
            reg = cls(db_url)
            cls._registries[key] = reg
            return reg

    @classmethod
    def forget(cls, db_url: str) -> None:
        with cls._lock:
            cls._registries.pop(normalize_url(db_url), None)

    def is_loaded(self, module: str) -> bool:
        return module in self.module_order

    # -- building ----------------------------------------------------------------------------
    def build(self) -> None:
        """(Re)compose every model from the fragments of ``module_order``."""
        with self.lock:
            from tessera.orm.model import Model

            self.models.clear()
            self.abstract.clear()
            self.metadata = MetaData()
            self.tables.clear()
            self.m2m_tables.clear()
            self.triggers.clear()

            # collect every fragment first: within a module, import order (alphabetical files) must
            # not matter, so an extension may be registered before the fragment that defines the model
            fragments: dict[str, list[type[Model]]] = defaultdict(list)
            order: list[str] = []
            extension_owner: dict[str, str] = {}
            for module in self.module_order:
                for frag in fragments_for(module):
                    name = frag.__dict__.get("_name") or frag.__dict__.get("_extend")
                    if not name:
                        raise RegistryError(f"{frag.__module__}.{frag.__name__} has neither _name nor _extend")
                    if "_extend" in frag.__dict__:
                        extension_owner.setdefault(name, module)
                    if name not in fragments:
                        order.append(name)
                    fragments[name].append(frag)
            for name, frags in fragments.items():
                bases = [f for f in frags if "_name" in f.__dict__]
                if not bases:
                    raise RegistryError(f"Module '{extension_owner.get(name, '?')}' extends unknown model '{name}'")
                # base definition(s) first, then extensions, each group in registration order
                fragments[name] = bases + [f for f in frags if "_name" not in f.__dict__]

            composed: dict[str, type[Model]] = {}

            def compose(name: str) -> type[Model]:
                if name in composed:
                    return composed[name]
                frags = fragments[name]
                mixin_names: list[str] = []
                for frag in frags:
                    for m in frag.__dict__.get("_inherit", ()):
                        if m not in fragments:
                            raise RegistryError(f"Model '{name}' inherits unknown mixin '{m}'")
                        if m not in mixin_names:
                            mixin_names.append(m)
                mixins = [compose(m) for m in mixin_names]
                bases: tuple[type, ...] = (*reversed(frags), *mixins, Model)
                attrs: dict[str, Any] = {
                    "_register": False,
                    "_name": name,
                    "_abstract": any(f.__dict__.get("_abstract", False) for f in frags if "_name" in f.__dict__),
                    "_fragments": tuple(frags),
                    "_mixin_names": tuple(mixin_names),
                    "__module__": "tessera.orm.registry",
                    "__qualname__": name,
                }
                cls = type(name.replace(".", "_"), bases, attrs)
                composed[name] = cls
                return cls

            for name in order:
                compose(name)

            for name, cls in composed.items():
                if cls._abstract:
                    self.abstract[name] = cls
                else:
                    self.models[name] = cls

            for cls in composed.values():
                self._setup_class_metadata(cls)
            for cls in composed.values():
                self._collect_fields(cls)
            for cls in composed.values():
                self._resolve_related(cls)
            for cls in self.models.values():
                self._link_inverses(cls)
            for cls in self.models.values():
                self._build_table(cls)
            for cls in self.models.values():
                self._build_triggers(cls)
            for cls in composed.values():
                self._collect_methods(cls)
            self.ready = True
            for hook in self.hooks.get("registry_built", ()):
                hook(self)
            from tessera.api.app import mount_new_routers

            mount_new_routers()
            log.info("registry built", db=self.dbname, models=len(self.models), modules=len(self.module_order))

    def _setup_class_metadata(self, cls: type[Model]) -> None:
        cls._table = cls.__dict__.get("_table") or cls._name.replace(".", "_")
        if not cls.__dict__.get("_description"):
            desc = next((f.__dict__["_description"] for f in cls._fragments if "_description" in f.__dict__), None)
            cls._description = desc or cls._name
        for attr in (
            "_order",
            "_rec_name",
            "_parent_name",
            "_log_access",
            "_soft_delete",
            "_studio",
            "_transient",
            "_auto",
            "_audit",
        ):
            for frag in reversed(cls._fragments):
                if attr in frag.__dict__:
                    setattr(cls, attr, frag.__dict__[attr])
                    break
        # list-valued attributes accumulate across fragments and mixins
        for attr in ("_sql_constraints", "_global_search", "_ai_index", "_indexes"):
            values: list[Any] = []
            for base in reversed(cls.__mro__):
                for item in base.__dict__.get(attr, ()):
                    if item not in values:
                        values.append(item)
            setattr(cls, attr, values)
        cls._registry = self
        cls._module = cls._fragments[0]._module

    def _collect_fields(self, cls: type[Model]) -> None:
        merged: dict[str, F.Field] = {}
        # walk base → derived so later definitions merge over earlier ones
        for base in reversed(cls.__mro__):
            if base is object:
                continue
            owner_module = getattr(base, "_module", "kernel")
            for attr_name, value in list(base.__dict__.items()):
                if not isinstance(value, F.Field):
                    continue
                field_copy = value.clone()
                field_copy.name = attr_name
                field_copy.model_name = cls._name
                field_copy.module = owner_module
                if attr_name in merged and type(value) is not F.Id:
                    field_copy = merged[attr_name].merge(field_copy)
                merged[attr_name] = field_copy
        if "id" not in merged:
            idf = F.Id()
            idf.model_name = cls._name
            idf.module = cls._module
            merged["id"] = idf
        if cls._log_access and not cls._abstract:
            for name, fld in (
                ("create_date", F.Datetime("Created on", readonly=True)),
                ("write_date", F.Datetime("Last updated on", readonly=True)),
                (
                    "create_uid",
                    F.Many2one("core.user", "Created by", readonly=True, ondelete="set_null", index=False),
                ),
                (
                    "write_uid",
                    F.Many2one("core.user", "Last updated by", readonly=True, ondelete="set_null", index=False),
                ),
            ):
                if name not in merged:
                    fld.name, fld.model_name, fld.module = name, cls._name, cls._module
                    merged[name] = fld
        if cls._soft_delete and not cls._abstract:
            for name, fld in (
                ("deleted_at", F.Datetime("Deleted on", readonly=True, index=True)),
                (
                    "deleted_by",
                    F.Many2one("core.user", "Deleted by", readonly=True, ondelete="set_null", index=False),
                ),
            ):
                if name not in merged:
                    fld.name, fld.model_name, fld.module = name, cls._name, cls._module
                    merged[name] = fld
        if "display_name" not in merged and not cls._abstract:
            dn = F.Char("Display name", compute="_compute_display_name", store=False, readonly=True)
            dn.name, dn.model_name, dn.module = "display_name", cls._name, cls._module
            merged["display_name"] = dn
        cls._fields = merged
        for attr_name, field_obj in merged.items():
            setattr(cls, attr_name, field_obj)

    def _resolve_related(self, cls: type[Model]) -> None:
        for name, fld in list(cls._fields.items()):
            if not fld.related:
                continue
            _target_cls, target_field = self._resolve_path(cls, fld.related)
            if isinstance(fld, F.Related) or type(fld) is F.Field:
                args = {**target_field._args}
                args.pop("compute", None)
                args.pop("related", None)
                args.pop("depends", None)
                args.pop("default", None)
                args.pop("required", None)
                args.pop("inverse", None)
                args.pop("search", None)
                args.pop("index", None)
                args.update({k: v for k, v in fld._args.items() if k not in ("related",)})
                args["related"] = fld.related
                args.setdefault("string", target_field.string or target_field.name.replace("_", " ").title())
                new: F.Field
                if isinstance(target_field, F.Many2one | F._X2many):
                    new = type(target_field)(
                        target_field.comodel_name or "", **{k: v for k, v in args.items() if k != "comodel_name"}
                    )
                elif isinstance(target_field, F.Selection | F.Reference):
                    new = type(target_field)(
                        target_field.selection, **{k: v for k, v in args.items() if k != "selection"}
                    )
                else:
                    new = type(target_field)(**args)
                new.name, new.model_name, new.module = name, cls._name, fld.module
                if "store" not in fld._args:
                    new._store = False
                if "readonly" not in fld._args:
                    new.readonly = not bool(fld.inverse)
                cls._fields[name] = new
                setattr(cls, name, new)
                fld = new
            fld.depends = (str(fld.related),)
            fld.compute = "_compute_related"
            if not fld.inverse and "readonly" not in fld._args:
                fld.readonly = True

    def _resolve_path(self, cls: type[Model], path: str) -> tuple[type[Model], F.Field]:
        current = cls
        segments = path.split(".")
        for i, seg in enumerate(segments):
            field = current._fields.get(seg)
            if field is None:
                raise RegistryError(f"Unknown field '{seg}' in path '{path}' on model '{cls._name}'")
            if i == len(segments) - 1:
                return current, field
            if not field.relational or not field.comodel_name:
                raise RegistryError(f"Field '{seg}' in path '{path}' on '{cls._name}' is not relational")
            nxt = self.models.get(field.comodel_name) or self.abstract.get(field.comodel_name)
            if nxt is None:
                raise RegistryError(f"Unknown comodel '{field.comodel_name}' for '{current._name}.{seg}'")
            current = nxt
        raise RegistryError(f"Empty path on model '{cls._name}'")

    def _link_inverses(self, cls: type[Model]) -> None:
        for fld in cls._fields.values():
            if (
                fld.relational
                and fld.comodel_name
                and fld.comodel_name not in self.models
                and fld.comodel_name not in self.abstract
            ):
                raise RegistryError(f"Field '{cls._name}.{fld.name}' references unknown model '{fld.comodel_name}'")
            if isinstance(fld, F.One2many):
                comodel = self.models.get(fld.comodel_name or "")
                if comodel is None:
                    raise RegistryError(f"One2many '{cls._name}.{fld.name}' targets abstract/unknown model")
                inverse = comodel._fields.get(fld.inverse_name)
                if not isinstance(inverse, F.Many2one):
                    raise RegistryError(
                        f"One2many '{cls._name}.{fld.name}' inverse '{fld.inverse_name}' "
                        f"is not a Many2one on '{comodel._name}'"
                    )

    def _build_table(self, cls: type[Model]) -> None:
        if not cls._auto:
            return
        columns: list[Column[Any]] = [Column("id", BigInteger(), primary_key=True)]
        for fld in cls._fields.values():
            if fld.name == "id" or not fld.store:
                continue
            sql_type = fld.sql_type
            if sql_type is None:
                continue
            columns.append(Column(fld.column_name, sa_type(sql_type), nullable=True))
        table = Table(cls._table, self.metadata, *columns)
        self.tables[cls._name] = table
        for fld in cls._fields.values():
            if isinstance(fld, F.Many2many) and fld.relation not in self.m2m_tables:
                rel = Table(
                    fld.relation,
                    self.metadata,
                    Column(fld.column1, BigInteger(), primary_key=True),
                    Column(fld.column2, BigInteger(), primary_key=True),
                )
                self.m2m_tables[fld.relation] = rel

    @staticmethod
    def _method_depends(model_cls: type[Model], method_name: str) -> list[str]:
        """Union of ``@api.depends`` paths across every definition of the method in the MRO
        (a later module may override a compute method without repeating the decorator)."""
        out: list[str] = []
        for base in model_cls.__mro__:
            func = base.__dict__.get(method_name)
            for path in getattr(func, "_depends", ()):
                if path not in out:
                    out.append(path)
        return out

    def _build_triggers(self, cls: type[Model]) -> None:
        for fld in cls._fields.values():
            if not fld.compute:
                continue
            depends = list(fld.depends)
            if fld.compute != "_compute_display_name" and not fld.related:
                for path in self._method_depends(cls, fld.compute):
                    if path not in depends:
                        depends.append(path)
                fld.depends = tuple(depends)
            if fld.compute == "_compute_display_name" and not depends:
                rec = cls._rec_name
                if rec in cls._fields and rec != "display_name":
                    depends = [rec]
                    fld.depends = (rec,)
            for path in depends:
                self._register_trigger(cls, fld, path)

    def _register_trigger(self, cls: type[Model], fld: F.Field, path: str) -> None:
        segments = path.split(".")
        current: type[Model] = cls
        walked: list[str] = []
        for i, seg in enumerate(segments):
            source_field = current._fields.get(seg)
            if source_field is None:
                raise RegistryError(f"Compute dependency '{path}' of '{cls._name}.{fld.name}': unknown field '{seg}'")
            trigger = Trigger(cls._name, fld.name, ".".join(walked))
            self.triggers[(current._name, seg)].append(trigger)
            if isinstance(source_field, F.One2many) and source_field.comodel_name:
                # the link of a one2many changes through the child's inverse many2one
                inverse_trigger = Trigger(cls._name, fld.name, ".".join([*walked, seg]))
                self.triggers[(source_field.comodel_name, source_field.inverse_name)].append(inverse_trigger)
            if i < len(segments) - 1:
                if not source_field.relational or not source_field.comodel_name:
                    raise RegistryError(
                        f"Compute dependency '{path}' of '{cls._name}.{fld.name}': '{seg}' is not relational"
                    )
                walked.append(seg)
                nxt = self.models.get(source_field.comodel_name) or self.abstract.get(source_field.comodel_name)
                if nxt is None:
                    raise RegistryError(f"Unknown comodel '{source_field.comodel_name}'")
                current = nxt

    def _collect_methods(self, cls: type[Model]) -> None:
        """Decorator metadata is the union over every definition in the MRO, so an override in a
        later module keeps the constraints, onchange triggers and API exposure of the original."""
        constraints: dict[str, list[str]] = {}
        onchanges: dict[str, list[str]] = defaultdict(list)
        exposed: set[str] = set()
        for base in cls.__mro__:
            for attr_name, value in base.__dict__.items():
                if not callable(value):
                    continue
                if hasattr(value, "_constrains"):
                    bucket = constraints.setdefault(attr_name, [])
                    for fname in value._constrains:
                        if fname not in bucket:
                            bucket.append(fname)
                if hasattr(value, "_onchange"):
                    for fname in value._onchange:
                        if attr_name not in onchanges[fname]:
                            onchanges[fname].append(attr_name)
                if getattr(value, "_exposed", False):
                    exposed.add(attr_name)
        cls._constraint_methods = [(tuple(fields_), method) for method, fields_ in constraints.items()]
        cls._onchange_methods = dict(onchanges)
        cls._exposed_methods = exposed

    # -- helpers --------------------------------------------------------------------------
    def table_for(self, model_name: str) -> Table:
        try:
            return self.tables[model_name]
        except KeyError as exc:
            raise RegistryError(f"Model '{model_name}' has no table") from exc

    def iter_models(self) -> Iterable[type[Model]]:
        return self.models.values()

    def register_hook(self, name: HookName, callback: Callable[..., Any]) -> None:
        if callback not in self.hooks[name]:
            self.hooks[name].append(callback)

    def run_hooks(self, name: HookName, *args: Any, **kwargs: Any) -> None:
        for hook in self.hooks.get(name, ()):
            hook(*args, **kwargs)

    def describe_model(self, model_name: str) -> dict[str, Any]:
        cls = self.models[model_name]
        return {
            "name": cls._name,
            "table": cls._table,
            "description": cls._description,
            "module": cls._module,
            "order": cls._order,
            "rec_name": cls._rec_name,
            "log_access": cls._log_access,
            "soft_delete": cls._soft_delete,
            "studio": cls._studio,
            "mixins": list(cls._mixin_names),
            "fields": {name: fld.describe() for name, fld in cls._fields.items()},
        }
