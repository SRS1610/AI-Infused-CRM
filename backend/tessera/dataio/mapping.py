"""Which fields can be imported, and suggestions mapping file headers to field paths."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

from tessera.orm import fields as F

if TYPE_CHECKING:
    from tessera.orm.model import Model

SKIP = {"id", "create_uid", "create_date", "write_uid", "write_date", "display_name", "deleted_at", "deleted_by"}


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def importable_fields(model: Model) -> list[dict[str, Any]]:
    """Fields the current user may set through an import, with the sub-paths offered for relations."""
    out = []
    for f in model._fields.values():
        settable = f.store or isinstance(f, F.Many2many) or getattr(f, "inverse", None)
        if f.name in SKIP or f.readonly or not settable:
            continue
        if isinstance(f, F.One2many | F.Binary) or not model._check_field_access(f, "write"):
            continue
        entry = {"name": f.name, "label": f.label, "type": f.type_name, "required": bool(f.required)}
        if isinstance(f, F.Many2one | F.Many2many):
            entry["comodel"] = f.comodel_name
            entry["paths"] = [f.name, f"{f.name}/id", f"{f.name}/.id"]
        out.append(entry)
    return out


def suggest_mapping(model: Model, headers: list[str], saved: dict[str, str] | None = None) -> dict[str, str | None]:
    """header → field path (or None). Exact name/label matches first, then fuzzy label similarity ≥ 0.8.
    A saved mapping for the same headers wins."""
    fields_ = importable_fields(model)
    by_key: dict[str, str] = {}
    for f in fields_:
        name = f["name"]
        stem = name.removesuffix("_ids").removesuffix("_id")
        for key in (name, f["label"] or "", stem, stem + "s" if name.endswith("_ids") else stem):
            if _norm(key):
                by_key.setdefault(_norm(key), name)
    used: set[str] = set()
    result: dict[str, str | None] = {}
    for header in headers:
        if saved and header in saved:
            result[header] = saved[header]
            continue
        raw = header.strip()
        base, _, suffix = raw.partition("/")
        key = _norm(base)
        model_prefix = _norm(model._description or "")
        if model_prefix and key.startswith(model_prefix) and key != model_prefix and key[len(model_prefix) :] in by_key:
            key = key[len(model_prefix) :]  # "Company Name" on companies → name
        if key in ("id", "externalid", "xmlid"):
            result[header] = "id"
            continue
        target = by_key.get(key)
        if target is None:
            best, score = None, 0.0
            for f in fields_:
                for candidate in (f["name"], f["label"] or ""):
                    s = SequenceMatcher(None, key, _norm(candidate)).ratio()
                    if s > score:
                        best, score = f["name"], s
            target = best if score >= 0.8 else None
        if target and target not in used:
            used.add(target)
            result[header] = f"{target}/{suffix}" if suffix in ("id", ".id") else target
        else:
            result[header] = None
    return result
