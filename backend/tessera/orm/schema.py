"""Declarative schema synchronisation (D-007).

Only additive and safe changes are applied automatically: create tables, add nullable
columns, widen column types, add indexes, foreign keys, many2many join tables and
declared SQL constraints. Destructive changes are reported by :func:`check_drift` and
performed by explicit migration scripts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tessera.db.engine import Cursor
from tessera.logging import get_logger
from tessera.orm import fields as F

if TYPE_CHECKING:
    from tessera.orm.model import Model
    from tessera.orm.registry import Registry

log = get_logger(__name__)

# audit columns reference core_user but carry no FK: the system user row is created after
# the first tables it "created", and users must stay deletable without rewriting history
LOG_ACCESS_COLUMNS = frozenset({"create_uid", "write_uid"})

# information_schema.data_type names for our declared SQL types
_PG_TYPE_NAMES = {
    "varchar": "character varying",
    "text": "text",
    "bigint": "bigint",
    "boolean": "boolean",
    "double precision": "double precision",
    "date": "date",
    "timestamptz": "timestamp with time zone",
    "jsonb": "jsonb",
    "bytea": "bytea",
}
_WIDENING: dict[tuple[str, str], str] = {
    ("integer", "bigint"): "bigint",
    ("character varying", "text"): "text",
    ("smallint", "bigint"): "bigint",
    ("real", "double precision"): "double precision",
    ("timestamp without time zone", "timestamp with time zone"): "timestamptz",
}


@dataclass
class Drift:
    # missing_table | missing_column | type_mismatch | missing_index | missing_fk | extra_column | extra_table
    kind: str
    object: str
    detail: str


def _declared_pg_type(sql_type: str) -> str:
    t = sql_type.lower()
    if t.startswith("numeric"):
        return "numeric"
    if t.startswith("vector"):
        return "USER-DEFINED"
    return _PG_TYPE_NAMES.get(t, t)


class SchemaSync:
    def __init__(self, cr: Cursor, registry: Registry) -> None:
        self.cr = cr
        self.registry = registry
        self._tables: set[str] | None = None
        self._columns: dict[str, dict[str, dict[str, Any]]] = {}

    # -- introspection ----------------------------------------------------------------------
    def existing_tables(self) -> set[str]:
        if self._tables is None:
            rows = self.cr.fetchall("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
            self._tables = {r[0] for r in rows}
        return self._tables

    def existing_columns(self, table: str) -> dict[str, dict[str, Any]]:
        if table not in self._columns:
            rows = self.cr.fetchall(
                """SELECT column_name, data_type, udt_name, is_nullable, character_maximum_length,
                          numeric_precision, numeric_scale
                   FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = :t""",
                {"t": table},
            )
            self._columns[table] = {
                r[0]: {
                    "data_type": r[1],
                    "udt": r[2],
                    "nullable": r[3] == "YES",
                    "len": r[4],
                    "precision": r[5],
                    "scale": r[6],
                }
                for r in rows
            }
        return self._columns[table]

    def existing_indexes(self, table: str) -> set[str]:
        rows = self.cr.fetchall(
            "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() AND tablename = :t",
            {"t": table},
        )
        return {r[0] for r in rows}

    def existing_constraints(self, table: str) -> set[str]:
        rows = self.cr.fetchall(
            """SELECT c.conname FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
               JOIN pg_namespace n ON n.oid = t.relnamespace
               WHERE n.nspname = current_schema() AND t.relname = :t""",
            {"t": table},
        )
        return {r[0] for r in rows}

    def _reset_cache(self) -> None:
        self._tables = None
        self._columns.clear()

    # -- sync -------------------------------------------------------------------------------
    def sync(self, models: list[type[Model]] | None = None) -> None:
        models = models or [m for m in self.registry.models.values() if m._auto]
        for cls in models:
            self._create_table(cls)
        for fld in self._m2m_fields(models):
            self._create_m2m_table(fld)
        self._reset_cache()
        for cls in models:
            self._sync_columns(cls)
        self._reset_cache()
        for cls in models:
            self._sync_foreign_keys(cls)
            self._sync_indexes(cls)
            self._sync_constraints(cls)
        for fld in self._m2m_fields(models):
            self._sync_m2m_fks(fld)
        self._reset_cache()

    def _m2m_fields(self, models: list[type[Model]]) -> list[F.Many2many]:
        seen: set[str] = set()
        out: list[F.Many2many] = []
        for cls in models:
            for fld in cls._fields.values():
                if isinstance(fld, F.Many2many) and fld.relation not in seen:
                    seen.add(fld.relation)
                    out.append(fld)
        return out

    def _create_table(self, cls: type[Model]) -> None:
        if cls._table in self.existing_tables():
            return
        self.cr.execute(f'CREATE TABLE "{cls._table}" (id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY)')
        self.existing_tables().add(cls._table)
        log.debug("table created", table=cls._table)

    def _create_m2m_table(self, fld: F.Many2many) -> None:
        if fld.relation in self.existing_tables():
            return
        self.cr.execute(
            f'CREATE TABLE "{fld.relation}" ("{fld.column1}" bigint NOT NULL, "{fld.column2}" bigint NOT NULL, '
            f'PRIMARY KEY ("{fld.column1}", "{fld.column2}"))'
        )
        self.cr.execute(f'CREATE INDEX "{fld.relation}_{fld.column2}_idx" ON "{fld.relation}" ("{fld.column2}")')
        self.existing_tables().add(fld.relation)
        log.debug("m2m table created", table=fld.relation)

    def _backfill_default(self, cls: type[Model], fld: Any) -> None:
        """Existing rows of a column added by an upgrade get the field's static default (a new ``active``
        flag must not hide every existing record, a new selection must not be empty on old rows)."""
        if fld.computed or fld.translate or fld.company_dependent or fld.relational or not fld.has_default:
            return
        value = fld.default
        if callable(value) or not isinstance(value, bool | int | float | str):
            return
        self.cr.execute(
            f'UPDATE "{cls._table}" SET "{fld.column_name}" = :v WHERE "{fld.column_name}" IS NULL',  # noqa: S608
            {"v": value},
        )

    def _sync_columns(self, cls: type[Model]) -> None:
        existing = self.existing_columns(cls._table)
        for fld in cls._fields.values():
            if fld.name == "id" or not fld.store or fld.sql_type is None:
                continue
            col = existing.get(fld.column_name)
            if col is None:
                self.cr.execute(f'ALTER TABLE "{cls._table}" ADD COLUMN "{fld.column_name}" {fld.sql_type}')
                log.debug("column added", table=cls._table, column=fld.column_name, type=fld.sql_type)
                self._backfill_default(cls, fld)
                continue
            declared = _declared_pg_type(fld.sql_type)
            if col["data_type"] != declared:
                new_type = _WIDENING.get((col["data_type"], declared))
                if new_type:
                    self.cr.execute(f'ALTER TABLE "{cls._table}" ALTER COLUMN "{fld.column_name}" TYPE {new_type}')
                    log.info("column widened", table=cls._table, column=fld.column_name, type=new_type)
                elif declared == "jsonb" and col["data_type"] in ("character varying", "text"):
                    # becoming translatable: wrap existing values under the default language
                    lang = self.registry.default_lang
                    self.cr.execute(
                        f'ALTER TABLE "{cls._table}" ALTER COLUMN "{fld.column_name}" TYPE jsonb '
                        f'USING CASE WHEN "{fld.column_name}" IS NULL THEN NULL '
                        f"ELSE jsonb_build_object('{lang}', \"{fld.column_name}\") END"
                    )
                    log.info("column converted to translatable", table=cls._table, column=fld.column_name)
                else:
                    log.warning(
                        "column type mismatch left unchanged (needs migration)",
                        table=cls._table,
                        column=fld.column_name,
                        actual=col["data_type"],
                        declared=declared,
                    )

    def _fk_name(self, table: str, column: str) -> str:
        return f"{table}_{column}_fkey"[:63]

    def _sync_foreign_keys(self, cls: type[Model]) -> None:
        constraints = self.existing_constraints(cls._table)
        for fld in cls._fields.values():
            if not isinstance(fld, F.Many2one) or not fld.store or fld.name in LOG_ACCESS_COLUMNS or not fld.fk:
                continue
            co_cls = self.registry.models.get(fld.comodel_name or "")
            if co_cls is None or not co_cls._auto:
                continue
            name = self._fk_name(cls._table, fld.column_name)
            if name in constraints:
                continue
            action = {"set_null": "SET NULL", "cascade": "CASCADE", "restrict": "RESTRICT"}[fld.ondelete]
            self.cr.execute(
                f'ALTER TABLE "{cls._table}" ADD CONSTRAINT "{name}" FOREIGN KEY ("{fld.column_name}") '
                f'REFERENCES "{co_cls._table}" (id) ON DELETE {action}'
            )

    def _sync_m2m_fks(self, fld: F.Many2many) -> None:
        constraints = self.existing_constraints(fld.relation)
        cls = self.registry.models[fld.model_name]
        co_cls = self.registry.models[fld.comodel_name or ""]
        for col, target in ((fld.column1, cls._table), (fld.column2, co_cls._table)):
            name = self._fk_name(fld.relation, col)
            if name not in constraints:
                self.cr.execute(
                    f'ALTER TABLE "{fld.relation}" ADD CONSTRAINT "{name}" FOREIGN KEY ("{col}") '
                    f'REFERENCES "{target}" (id) ON DELETE CASCADE'
                )

    def _index_name(self, table: str, column: str, kind: str) -> str:
        suffix = {"btree": "idx", "unique": "uniq", "trigram": "trgm", "hnsw": "hnsw"}[kind]
        return f"{table}_{column}_{suffix}"[:63]

    def _sync_indexes(self, cls: type[Model]) -> None:
        existing = self.existing_indexes(cls._table)
        for fld in cls._fields.values():
            if fld.name == "id" or not fld.store or not fld.index:
                continue
            kind = fld.index if isinstance(fld.index, str) else "btree"
            if kind == "btree_not_null":
                kind = "btree"
            if kind not in ("btree", "unique", "trigram", "hnsw"):
                continue
            name = self._index_name(cls._table, fld.column_name, kind)
            if name in existing:
                continue
            col = f'"{fld.column_name}"'
            if fld.translate:
                col = f"(({col} ->> '{self.registry.default_lang}'))"
            if kind == "trigram":
                self.cr.execute(f'CREATE INDEX "{name}" ON "{cls._table}" USING gin ({col} gin_trgm_ops)')
            elif kind == "hnsw":
                self.cr.execute(f'CREATE INDEX "{name}" ON "{cls._table}" USING hnsw ({col} vector_cosine_ops)')
            elif kind == "unique":
                where = ' WHERE "deleted_at" IS NULL' if cls._soft_delete and "deleted_at" in cls._fields else ""
                self.cr.execute(f'CREATE UNIQUE INDEX "{name}" ON "{cls._table}" ({col}){where}')
            else:
                self.cr.execute(f'CREATE INDEX "{name}" ON "{cls._table}" ({col})')
        if cls._fields.get("active") and "active" in self.existing_columns(cls._table):
            name = f"{cls._table}_active_true_idx"[:63]
            if name not in existing:
                self.cr.execute(f'CREATE INDEX "{name}" ON "{cls._table}" (id) WHERE active')
        for idx_name, columns in cls._indexes:
            name = f"{cls._table}_{idx_name}_idx"[:63]
            if name in existing:
                continue
            cols = ", ".join(f'"{c}"' for c in columns)
            self.cr.execute(f'CREATE INDEX "{name}" ON "{cls._table}" ({cols})')

    def _sync_constraints(self, cls: type[Model]) -> None:
        if not cls._sql_constraints:
            return
        existing = self.existing_constraints(cls._table)
        for name, definition, _message in cls._sql_constraints:
            full = f"{cls._table}_{name}"[:63]
            if full in existing:
                continue
            with self.cr.savepoint():
                self.cr.execute(f'ALTER TABLE "{cls._table}" ADD CONSTRAINT "{full}" {definition}')

    # -- drift report -----------------------------------------------------------------------
    def drift(self) -> list[Drift]:
        out: list[Drift] = []
        tables = self.existing_tables()
        model_tables = {cls._table for cls in self.registry.models.values() if cls._auto}
        for cls in self.registry.models.values():
            if not cls._auto:
                continue
            if cls._table not in tables:
                out.append(Drift("missing_table", cls._table, f"model {cls._name}"))
                continue
            existing = self.existing_columns(cls._table)
            declared_cols = {"id"}
            for fld in cls._fields.values():
                if fld.name == "id" or not fld.store or fld.sql_type is None:
                    continue
                declared_cols.add(fld.column_name)
                col = existing.get(fld.column_name)
                if col is None:
                    out.append(Drift("missing_column", f"{cls._table}.{fld.column_name}", fld.sql_type))
                elif col["data_type"] != _declared_pg_type(fld.sql_type):
                    out.append(
                        Drift(
                            "type_mismatch",
                            f"{cls._table}.{fld.column_name}",
                            f"{col['data_type']} != {fld.sql_type}",
                        )
                    )
            for col_name in existing:
                if col_name not in declared_cols and not col_name.startswith("_removed_"):
                    out.append(
                        Drift("extra_column", f"{cls._table}.{col_name}", "not declared by any installed module")
                    )
            indexes = self.existing_indexes(cls._table)
            for fld in cls._fields.values():
                if fld.name == "id" or not fld.store or not fld.index:
                    continue
                kind = fld.index if isinstance(fld.index, str) else "btree"
                if kind == "btree_not_null":
                    kind = "btree"
                if (
                    kind in ("btree", "unique", "trigram", "hnsw")
                    and self._index_name(cls._table, fld.column_name, kind) not in indexes
                ):
                    out.append(Drift("missing_index", f"{cls._table}.{fld.column_name}", kind))
            constraints = self.existing_constraints(cls._table)
            for fld in cls._fields.values():
                if isinstance(fld, F.Many2one) and fld.store and fld.name not in LOG_ACCESS_COLUMNS and fld.fk:
                    co = self.registry.models.get(fld.comodel_name or "")
                    if co is not None and co._auto and self._fk_name(cls._table, fld.column_name) not in constraints:
                        out.append(Drift("missing_fk", f"{cls._table}.{fld.column_name}", f"→ {co._table}"))
        known = model_tables | set(self.registry.m2m_tables)
        for table in tables:
            if table not in known and not table.startswith("_removed_") and not table.startswith("pg_"):
                out.append(Drift("extra_table", table, "not declared by any installed module"))
        return out


def sync_schema(cr: Cursor, registry: Registry, models: list[type[Model]] | None = None) -> None:
    SchemaSync(cr, registry).sync(models)


def check_drift(cr: Cursor, registry: Registry) -> list[Drift]:
    return SchemaSync(cr, registry).drift()
