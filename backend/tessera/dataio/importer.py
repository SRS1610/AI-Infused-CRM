"""Run an import: convert each row with the mapping, create or update in batches, report errors by row
and column. A dry run executes everything inside a savepoint that is rolled back, so validation is
exactly what the real run will do — and nothing is written."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from dateutil import parser as dateparser

from tessera.exceptions import TesseraError, UserError, ValidationError
from tessera.i18n import _, translatable
from tessera.orm import fields as F

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

TRUE = {"1", "true", "yes", "y", "x", "on", "oui", "ja", "si", "sí"}
FALSE = {"0", "false", "no", "n", "", "off", "non", "nein"}
IMPORT_MODULE = "__import__"


class _DryRun(Exception):
    """Raised to roll back the dry-run savepoint."""


class CellError(Exception):
    pass


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    ids: list[int] = field(default_factory=list)
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "updated": self.updated,
            "errors": self.errors,
            "error_count": len(self.errors),
            "dry_run": self.dry_run,
            "ids": self.ids[:1000],
        }


class Converter:
    """Converts cell strings to ORM values for one model, with lookup caches for relations."""

    def __init__(self, model: Model, options: dict[str, Any]) -> None:
        self.model = model
        self.env = model.env
        self.dayfirst = bool(options.get("dayfirst", False))
        self.tz = ZoneInfo(options.get("tz") or str(self.env.tz))
        self._names: dict[tuple[str, str], int | None] = {}

    def convert(self, path: str, raw: Any) -> tuple[str, Any]:
        name, _sep, sub = path.partition("/")
        f = self.model._fields[name]
        text = "" if raw is None else str(raw).strip()
        if isinstance(f, F.Many2one):
            return name, self._relation_id(f, text, sub) if text else None
        if isinstance(f, F.Many2many):
            parts = [p.strip() for p in text.split(",") if p.strip()]
            return name, [("set", [self._relation_id(f, p, sub) for p in parts])]
        if not text:
            return name, False if isinstance(f, F.Boolean) else None
        if isinstance(f, F.Boolean):
            low = text.lower()
            if low in TRUE:
                return name, True
            if low in FALSE:
                return name, False
            raise CellError(_("'{text}' is not a yes/no value", text=text))
        if isinstance(f, F.Integer):
            try:
                number = float(text.replace(" ", "").replace(",", ""))
            except ValueError as exc:
                raise CellError(_("'{text}' is not a whole number", text=text)) from exc
            if not number.is_integer():
                raise CellError(_("'{text}' is not a whole number", text=text))
            return name, int(number)
        if isinstance(f, F.Float | F.Monetary):
            clean = text.replace(" ", "").replace(chr(0xA0), "")
            if "," in clean and "." in clean:
                clean = clean.replace(",", "")
            elif "," in clean:
                clean = clean.replace(",", ".")
            try:
                return name, float(clean)
            except ValueError as exc:
                raise CellError(_("'{text}' is not a number", text=text)) from exc
        if isinstance(f, F.Datetime):
            try:
                value = dateparser.parse(text, dayfirst=self.dayfirst)
            except (ValueError, OverflowError) as exc:
                raise CellError(_("'{text}' is not a date and time", text=text)) from exc
            if value.tzinfo is None:
                value = value.replace(tzinfo=self.tz)
            return name, value.astimezone(dt.UTC)
        if isinstance(f, F.Date):
            try:
                return name, dateparser.parse(text, dayfirst=self.dayfirst).date()
            except (ValueError, OverflowError) as exc:
                raise CellError(_("'{text}' is not a date", text=text)) from exc
        if isinstance(f, F.Selection):
            options = f.selection if isinstance(f.selection, list | tuple) else []
            for key, label in options:
                if text == key or text.lower() == str(label).lower() or text.lower() == str(key).lower():
                    return name, key
            raise CellError(
                _("'{text}' is not one of: {values}", text=text, values=", ".join(str(lbl) for _, lbl in options))
            )
        if isinstance(f, F.Json):
            import json

            try:
                return name, json.loads(text)
            except ValueError as exc:
                raise CellError(_("Invalid JSON")) from exc
        return name, text

    def _relation_id(self, f: F.Field, text: str, sub: str) -> int:
        comodel_name = str(getattr(f, "comodel_name", ""))
        comodel = self.env[comodel_name]
        key = (f"{comodel_name}:{sub}", text)
        if key in self._names:
            found = self._names[key]
        else:
            found = None
            if sub == ".id":
                if text.isdigit() and comodel.browse([int(text)]).exists():
                    found = int(text)
            elif sub == "id":
                ref = self.env.ref(text if "." in text else f"{IMPORT_MODULE}.{text}", raise_if_not_found=False)
                found = ref.ids[0] if ref and ref._name == comodel._name else None
            else:
                matches = comodel.name_search(text, operator="=", limit=2)
                if not matches:
                    matches = comodel.name_search(text, operator="ilike", limit=2)
                    matches = [m for m in matches if str(m[1]).lower() == text.lower()]
                if len(matches) > 1:
                    raise CellError(
                        _(
                            "'{text}' matches several {description} records",
                            text=text,
                            description=translatable(comodel._description),
                        )
                    )
                found = matches[0][0] if matches else None
            self._names[key] = found
        if found is None:
            raise CellError(
                _("No {description} named '{text}'", description=translatable(comodel._description), text=text)
            )
        return found


def run_import(
    env: Environment,
    model_name: str,
    headers: list[str],
    rows: list[list[Any]],
    mapping: dict[str, str | None],
    *,
    dry_run: bool = False,
    options: dict[str, Any] | None = None,
    batch_size: int = 500,
    progress: Callable[[int, int], None] | None = None,
) -> ImportResult:
    """Import ``rows``. Rows with errors are reported (row numbers count the header as row 1) and
    skipped; all other rows are imported. ``mapping`` maps header → field path ('' / None = ignore)."""
    options = options or {}
    model = env[model_name]
    model.check_access_rights("create")
    columns: list[tuple[int, str, str]] = [(i, h, str(mapping[h])) for i, h in enumerate(headers) if mapping.get(h)]
    unknown = [p for _, _, p in columns if p != "id" and p.split("/")[0] not in model._fields]
    if unknown:
        raise UserError(_("Unknown fields in the mapping: {unknown}", unknown=", ".join(unknown)))
    for _index, _header, path in columns:
        if path != "id" and not model._check_field_access(model._fields[path.split("/")[0]], "write"):
            raise UserError(_("You cannot import into '{path}'", path=path))
    field_to_column = {p.split("/")[0]: h for _, h, p in columns if p != "id"}
    required = [
        n
        for n, f in model._fields.items()
        if f.required and not f.has_default and f.store and n not in field_to_column and n != "id"
    ]
    result = ImportResult(dry_run=dry_run)
    has_xmlid = any(p == "id" for _, _, p in columns)  # rows may update existing records: check per row instead
    if required and not has_xmlid:
        labels = ", ".join(model._fields[n].label for n in required)
        result.errors.append(
            {"row": None, "column": None, "field": required[0], "message": f"Required fields not mapped: {labels}"}
        )
        return result
    converter = Converter(model, options)
    required_mapped = {n for n in field_to_column if model._fields[n].required and not model._fields[n].has_default}
    cr = env.cr

    def process() -> None:
        total = len(rows)
        for start in range(0, total, batch_size):
            batch = rows[start : start + batch_size]
            prepared: list[tuple[int, dict[str, Any], str | None]] = []
            for offset, row in enumerate(batch):
                row_no = start + offset + 2
                if not any(str(c).strip() for c in row):
                    continue
                vals: dict[str, Any] = {}
                xmlid = next((str(row[i]).strip() or None for i, _, p in columns if p == "id" and i < len(row)), None)
                row_ok = True
                for index, header, path in columns:
                    if path == "id":
                        continue
                    cell = row[index] if index < len(row) else ""
                    try:
                        fname, value = converter.convert(path, cell)
                    except CellError as exc:
                        result.errors.append(
                            {"row": row_no, "column": header, "field": path.split("/")[0], "message": str(exc)}
                        )
                        row_ok = False
                        continue
                    if value in (None, "") and fname in required_mapped and not xmlid:
                        result.errors.append(
                            {
                                "row": row_no,
                                "column": header,
                                "field": fname,
                                "message": f"{model._fields[fname].label} is required",
                            }
                        )
                        row_ok = False
                        continue
                    vals[fname] = value
                if row_ok:
                    prepared.append((row_no, vals, xmlid))
            _write_batch(model, prepared, field_to_column, result)
            if progress:
                progress(min(start + batch_size, total), total)

    if dry_run:
        try:
            with cr.savepoint():
                process()
                env.flush()
                raise _DryRun
        except _DryRun:
            pass
        result.ids = []
    else:
        process()
    result.errors.sort(key=lambda e: (e["row"] or 0, e["column"] or ""))
    return result


def _write_batch(
    model: Model,
    prepared: list[tuple[int, dict[str, Any], str | None]],
    field_to_column: dict[str, str],
    result: ImportResult,
) -> None:
    if not prepared:
        return
    env = model.env
    creates = [(n, v, x) for n, v, x in prepared if not (x and _existing(env, model, x))]
    updates = [(n, v, x) for n, v, x in prepared if x and _existing(env, model, x)]
    if creates:
        try:
            with env.cr.savepoint():
                records = model.create([v for _, v, _ in creates])
                env.flush()
            for (_, _, xmlid), rid in zip(creates, records.ids, strict=True):
                if xmlid:
                    _register_xmlid(env, model, xmlid, rid)
            result.created += len(records)
            result.ids.extend(records.ids)
        except Exception:
            for row_no, vals, xmlid in creates:  # isolate the failing rows
                _one(model, row_no, vals, xmlid, field_to_column, result)
    for row_no, vals, xmlid in updates:
        _one(model, row_no, vals, xmlid, field_to_column, result)


def _one(
    model: Model,
    row_no: int,
    vals: dict[str, Any],
    xmlid: str | None,
    field_to_column: dict[str, str],
    result: ImportResult,
) -> None:
    env = model.env
    try:
        with env.cr.savepoint():
            existing = _existing(env, model, xmlid) if xmlid else None
            if existing:
                existing.write(vals)
                result.updated += 1
                result.ids.append(existing.ids[0])
            else:
                rec = model.create(vals)
                if xmlid:
                    _register_xmlid(env, model, xmlid, rec.ids[0])
                result.created += 1
                result.ids.append(rec.ids[0])
            env.flush()
    except Exception as exc:  # the savepoint keeps the transaction usable; the row is reported
        context = exc.context if isinstance(exc, TesseraError) else {}
        fname = context.get("field")
        message = exc.message if isinstance(exc, TesseraError) and exc.message else f"{type(exc).__name__}: {exc}"
        result.errors.append(
            {"row": row_no, "column": field_to_column.get(fname or ""), "field": fname, "message": message}
        )


def _existing(env: Environment, model: Model, xmlid: str) -> Model | None:
    full = xmlid if "." in xmlid else f"{IMPORT_MODULE}.{xmlid}"
    ref = env.ref(full, raise_if_not_found=False)
    return ref if ref is not None and ref._name == model._name and ref.exists() else None


def _register_xmlid(env: Environment, model: Model, xmlid: str, res_id: int) -> None:
    module, _, name = (xmlid if "." in xmlid else f"{IMPORT_MODULE}.{xmlid}").partition(".")
    env["core.module_record"].elevated("import external id").create(
        {"module": module, "name": name, "model": model._name, "res_id": res_id}
    )
    env.transaction.xmlid_cache.pop(f"{module}.{name}", None)


__all__ = ["Converter", "ImportResult", "ValidationError", "run_import"]
