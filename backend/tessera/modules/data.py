"""JSON data files.

A data file is a list of entries::

    [
      {"id": "main_company", "model": "core.company", "values": {"name": "My Company", "currency_id": "core.USD"}},
      {"id": "group_user", "model": "core.group", "noupdate": true, "values": {"name": "Internal user"}},
      {"id": "old_record", "delete": true}
    ]

* ``id`` is the external id; without a module prefix it belongs to the loading module.
* a **string** value for a many2one field is an external id; a list of strings for an
  x2many field is a list of external ids; ``{"$ref": "module.name"}`` works for any field.
* a **dict keyed by language** (``{"en_US": …, "fr_FR": …}``) for a translatable field
  writes every language.
* ``noupdate`` records are created on install and left untouched on upgrade.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tessera.exceptions import ModuleError
from tessera.logging import get_logger
from tessera.orm import fields as F

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

log = get_logger(__name__)


def qualify(external_id: str, module: str) -> str:
    return external_id if "." in external_id else f"{module}.{external_id}"


class DataLoader:
    def __init__(self, env: Environment, module: str, *, mode: str = "install") -> None:
        self.env = env
        self.module = module
        self.mode = mode  # install | upgrade | missing (create absent records only: new demo data on upgrade)
        self.stats = {"created": 0, "updated": 0, "skipped": 0, "deleted": 0}

    # -- entry points -----------------------------------------------------------------------
    def load_file(self, path: Path) -> None:
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ModuleError(f"Invalid JSON in {path}: {exc}") from exc
        if not isinstance(entries, list):
            raise ModuleError(f"{path} must contain a list of records")
        for index, entry in enumerate(entries):
            try:
                self.load_entry(entry)
            except Exception as exc:
                raise ModuleError(f"{path.name}[{index}] ({entry.get('id', '?')}): {exc}") from exc
        log.debug("data file loaded", module=self.module, file=path.name, **self.stats)

    def load_entry(self, entry: dict[str, Any]) -> Model | None:
        if not isinstance(entry, dict):
            raise ModuleError("Each data entry must be an object")
        external_id = entry.get("id")
        if entry.get("delete"):
            if not external_id:
                raise ModuleError("A delete entry needs an id")
            existing = self.env.ref(qualify(external_id, self.module), raise_if_not_found=False)
            if existing is not None and existing.exists():
                existing.unlink()
                self.stats["deleted"] += 1
            return None
        if self.mode == "missing":
            # only records the database does not have yet; entries without an id cannot be told apart
            if not external_id:
                self.stats["skipped"] += 1
                return None
            found = self.env.ref(qualify(external_id, self.module), raise_if_not_found=False)
            if found is not None and found.exists():
                self.stats["skipped"] += 1
                return found
        model_name = entry.get("model")
        if not model_name:
            raise ModuleError("Data entry has no model")
        model = self.env[model_name]
        values = self.resolve_values(model, dict(entry.get("values", {})))
        noupdate = bool(entry.get("noupdate", False))
        translations = {k: v for k, v in values.items() if isinstance(v, dict) and model._fields[k].translate}
        for k, per_lang in translations.items():
            values[k] = (
                per_lang.get(self.env.lang)
                or per_lang.get(self.env.registry.default_lang)
                or next(iter(per_lang.values()))
            )

        record: Model | None = None
        if external_id:
            xmlid = qualify(external_id, self.module)
            record = self.env.ref(xmlid, raise_if_not_found=False)
            if record is not None and not record.exists():
                record = None
        if record is None:
            record = model.create(values)
            self.stats["created"] += 1
            if external_id:
                self.env.set_ref(qualify(external_id, self.module), record, noupdate=noupdate)
        elif self.mode == "install" or not self._is_noupdate(qualify(external_id or "", self.module)):
            record.write(values)
            self.stats["updated"] += 1
        else:
            self.stats["skipped"] += 1
            return record
        for fname, per_lang in translations.items():
            for lang, text in per_lang.items():
                record.with_context(lang=lang).write({fname: text})
        return record

    def _is_noupdate(self, xmlid: str) -> bool:
        module, _, name = xmlid.partition(".")
        row = self.env.cr.fetchone(
            "SELECT noupdate FROM core_module_record WHERE module = :m AND name = :n",
            {"m": module, "n": name},
        )
        return bool(row and row[0])

    # -- value resolution -------------------------------------------------------------------
    def resolve_values(self, model: Model, values: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, value in values.items():
            field = model._fields.get(name)
            if field is None:
                raise ModuleError(f"Unknown field '{name}' on {model._name}")
            out[name] = self.resolve_value(field, value)
        return out

    def resolve_value(self, field: F.Field, value: Any) -> Any:
        if isinstance(value, dict) and "$ref" in value:
            return self._ref_id(value["$ref"])
        if isinstance(value, dict) and "$refs" in value:
            return [("set", [self._ref_id(x) for x in value["$refs"]])]
        if isinstance(value, dict) and "$add" in value:  # extend another module's links without replacing them
            return [("add", self._ref_id(x)) for x in value["$add"]]
        if isinstance(field, F.Many2one) and isinstance(value, str):
            return self._ref_id(value)
        if isinstance(field, F._X2many) and isinstance(value, list):
            ids: list[int] = []
            commands: list[tuple[Any, ...]] = []
            for item in value:
                if isinstance(item, str):
                    ids.append(self._ref_id(item))
                elif isinstance(item, int):
                    ids.append(item)
                elif isinstance(item, dict):
                    comodel = self.env[field.comodel_name or ""]
                    commands.append(("create", self.resolve_values(comodel, item)))
                else:
                    raise ModuleError(f"Invalid x2many item {item!r} for '{field.name}'")
            if ids:
                commands.insert(0, ("set", ids))
            return commands
        if isinstance(field, F.Reference) and isinstance(value, str) and "," not in value:
            rec = self.env.ref(qualify(value, self.module))
            return rec
        return value

    def _ref_id(self, external_id: str) -> int:
        rec = self.env.ref(qualify(external_id, self.module))
        assert rec is not None
        return rec._single_id()
