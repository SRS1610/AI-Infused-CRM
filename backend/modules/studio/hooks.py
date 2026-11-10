"""Studio fields join the model registry as dynamic fragments, built from ``studio_field`` rows of the
registry's own database (so two databases served by one process keep their own customisations)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from tessera.orm import Model
from tessera.orm import fields as F
from tessera.orm.hooks import hook

COLUMNS = (
    'model_name, name, ttype, label, help, required, readonly, readonly_if, required_if, "default", selection, '
    "comodel, currency_field, groups, tracking"
)


def make_field(row: Mapping[str, Any]) -> F.Field:
    kwargs: dict[str, Any] = {"help": row.get("help") or ""}
    if row.get("required"):
        kwargs["required"] = True
    if row.get("readonly"):
        kwargs["readonly"] = True
    if row.get("readonly_if"):
        kwargs["readonly_if"] = row["readonly_if"]
    if row.get("required_if"):
        kwargs["required_if"] = row["required_if"]
    if row.get("default") is not None:
        kwargs["default"] = row["default"]
    if row.get("groups"):
        kwargs["groups"] = tuple(row["groups"])
    if row.get("tracking"):
        kwargs["tracking"] = True
    label = row["label"]
    t = row["ttype"]
    if t == "char":
        return F.Char(label, **kwargs)
    if t == "text":
        return F.Text(label, **kwargs)
    if t == "html":
        return F.Html(label, **kwargs)
    if t == "integer":
        return F.Integer(label, **kwargs)
    if t == "float":
        return F.Float(label, **kwargs)
    if t == "monetary":
        return F.Monetary(label, currency_field=row.get("currency_field") or "currency_id", **kwargs)
    if t == "boolean":
        return F.Boolean(label, **kwargs)
    if t == "date":
        return F.Date(label, **kwargs)
    if t == "datetime":
        return F.Datetime(label, **kwargs)
    if t == "selection":
        return F.Selection([tuple(x) for x in row.get("selection") or []], label, **kwargs)
    if t == "many2one":
        return F.Many2one(row["comodel"], label, ondelete="set_null", **kwargs)
    if t == "many2many":
        table = row["model_name"].replace(".", "_")
        relation = f"x_{table}_{row['name']}_rel"[:63]
        return F.Many2many(
            row["comodel"], relation=relation, column1="src_id", column2="dst_id", string=label, **kwargs
        )
    raise ValueError(f"Unsupported Studio field type '{t}'")


def build_fragments(rows: Iterable[Mapping[str, Any]]) -> list[type[Model]]:
    by_model: dict[str, dict[str, F.Field]] = {}
    for row in rows:
        by_model.setdefault(row["model_name"], {})[row["name"]] = make_field(row)
    fragments = []
    for model, flds in by_model.items():
        attrs: dict[str, Any] = {
            "_extend": model,
            "_register": False,
            "_module": "studio",
            "__module__": "modules.studio.fields",
        }
        attrs.update(flds)
        fragments.append(type(f"Studio_{model.replace('.', '_')}", (Model,), attrs))
    return fragments


def rows_from_cursor(cr: Any) -> list[dict[str, Any]]:
    if not cr.scalar("SELECT to_regclass('studio_field') IS NOT NULL"):
        return []
    return [dict(r._mapping) for r in cr.fetchall(f"SELECT {COLUMNS} FROM studio_field WHERE active ORDER BY id")]  # noqa: S608


@hook("before_build")
def studio_fragments(registry: Any) -> None:
    """Called at every registry build: read the committed Studio fields of this database."""
    from tessera.db.engine import Cursor, get_engine

    cr = Cursor(get_engine(registry.db_url))
    try:
        registry.dynamic_fragments = build_fragments(rows_from_cursor(cr))
    finally:
        cr.close()


@hook("after_create")
def studio_field_loaded(records: Any, vals_list: list[dict[str, Any]]) -> None:
    """Fields created from data files (an exported customisation module) join the registry too."""
    if records._name != "studio.field" or records.env.context.get("studio_internal"):
        return
    from modules.studio.service import _rebuild

    models = sorted({r.model_name for r in records})
    _rebuild(records.env, models)
    records.env["core.model_field"].elevated().search(
        [("model_name", "in", models), ("name", "in", records.mapped("name"))]
    ).write({"studio": True})
