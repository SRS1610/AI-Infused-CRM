"""Write catalogue translations into the JSONB values of translatable records.

Menus, actions, stages, groups, workflow states… are module data stored in ``translate=True`` fields
(``{"en_US": …, "fr_FR": …}``, D-009). When a module is installed or upgraded — and on
``tessera i18n load`` — the English value of every translatable field of the records the module owns
(``core.module_record``) is looked up in the catalogues and each translation is written under its
language key. A record flagged ``noupdate`` (data the administrator may have edited) only gets the
languages it does not have yet, unless ``overwrite`` is set. A value without a catalogue entry is left
as it is.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tessera.i18n.catalog import SOURCE_LANG, available_languages, find, reset_catalogs
from tessera.logging import get_logger

if TYPE_CHECKING:
    from tessera.orm.environment import Environment

log = get_logger(__name__)


@dataclass
class ApplyResult:
    records: int = 0
    values: int = 0


def apply_module_translations(
    env: Environment,
    module: str,
    *,
    langs: Iterable[str] | None = None,
    overwrite: bool = False,
    modules: Sequence[str] | None = None,
) -> ApplyResult:
    """Translate the translatable fields of ``module``'s records into ``langs`` (default: every language
    with a catalogue among ``modules``, the installed modules)."""
    registry = env.registry
    modules = list(modules) if modules is not None else list(registry.module_order)
    source = registry.default_lang or SOURCE_LANG
    targets = [lang for lang in (langs if langs is not None else available_languages(modules)) if lang != source]
    result = ApplyResult()
    if not targets:
        return result
    rows = env.cr.fetchall("SELECT model, res_id, noupdate FROM core_module_record WHERE module = :m", {"m": module})
    by_model: dict[str, dict[int, bool]] = defaultdict(dict)
    for model_name, res_id, noupdate in rows:
        by_model[model_name][int(res_id)] = bool(noupdate)
    for model_name, owned in by_model.items():
        cls = registry.models.get(model_name)
        if cls is None or not cls._auto:
            continue
        fields = [f for f in cls._fields.values() if f.translate and f.store and f.sql_type == "jsonb"]
        if not fields:
            continue
        columns = ", ".join(f'"{f.column_name}"' for f in fields)
        found = env.cr.fetchall(
            f'SELECT id, {columns} FROM "{cls._table}" WHERE id = ANY(:ids)',  # noqa: S608 - registry names
            {"ids": list(owned)},
        )
        for record in found:
            rid, values = int(record[0]), record[1:]
            patches: dict[str, dict[str, str]] = {}
            for field, value in zip(fields, values, strict=True):
                patch = _patch(value, source, targets, owned[rid] and not overwrite, modules, module)
                if patch:
                    patches[field.column_name] = patch
            if not patches:
                continue
            assignments = ", ".join(
                f'"{col}" = COALESCE("{col}", \'{{}}\'::jsonb) || CAST(:p{i} AS jsonb)' for i, col in enumerate(patches)
            )
            params: dict[str, Any] = {f"p{i}": json.dumps(patch) for i, patch in enumerate(patches.values())}
            params["id"] = rid
            env.cr.execute(f'UPDATE "{cls._table}" SET {assignments} WHERE id = :id', params)  # noqa: S608
            result.records += 1
            result.values += sum(len(p) for p in patches.values())
    if result.records:
        env.cache.clear()
        log.info("translations applied", module=module, records=result.records, values=result.values)
    return result


def _patch(
    value: Any, source: str, targets: list[str], keep_existing: bool, modules: Sequence[str], module: str
) -> dict[str, str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    if not isinstance(value, dict):
        return {}
    text = value.get(source)
    if not isinstance(text, str) or not text:
        return {}
    patch: dict[str, str] = {}
    for lang in targets:
        if keep_existing and value.get(lang):
            continue
        translated = find(text, lang, modules=modules, module=module)
        if translated and value.get(lang) != translated:
            patch[lang] = translated
    return patch


def apply_translations(
    env: Environment,
    modules: Sequence[str] | None = None,
    *,
    langs: Iterable[str] | None = None,
    overwrite: bool = False,
) -> dict[str, ApplyResult]:
    """Re-read the catalogues and apply them for ``modules`` (default: every installed module)."""
    reset_catalogs()
    installed = list(env.registry.module_order)
    targets = [m for m in (modules or installed) if m in installed]
    wanted = list(langs) if langs is not None else None
    return {
        name: apply_module_translations(env, name, langs=wanted, overwrite=overwrite, modules=installed)
        for name in targets
    }
