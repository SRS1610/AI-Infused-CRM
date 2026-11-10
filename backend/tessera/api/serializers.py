"""Shared helpers for turning request payloads into ORM values and records into JSON."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

from tessera.exceptions import ValidationError
from tessera.i18n import _
from tessera.orm import fields as F
from tessera.orm.model import Model

READ_ONLY_INPUT = {
    "id",
    "create_date",
    "write_date",
    "create_uid",
    "write_uid",
    "display_name",
    "deleted_at",
    "deleted_by",
}


def clean_input_values(model: Model, values: dict[str, Any]) -> dict[str, Any]:
    """Drop server-managed fields; convert JSON x2many command lists to tuples."""
    out: dict[str, Any] = {}
    for name, value in values.items():
        if name in READ_ONLY_INPUT:
            continue
        field = model._fields.get(name)
        if field is None:
            raise ValidationError(_("Unknown field '{field}' on {model}", field=name, model=model._name), field=name)
        if isinstance(field, F._X2many) and isinstance(value, list):
            out[name] = [
                tuple(cmd) if isinstance(cmd, list) and cmd and isinstance(cmd[0], str) else cmd for cmd in value
            ]
        elif isinstance(field, F.Many2one) and isinstance(value, dict):
            out[name] = value.get("id")
        else:
            out[name] = value
    return out


def default_read_fields(model: Model) -> list[str]:
    return [
        f.name for f in model._fields.values() if not isinstance(f, F.Binary) and model._check_field_access(f, "read")
    ]


def etag_for(record: Model) -> str | None:
    if "write_date" not in record._fields:
        return None
    try:
        stamp = record.write_date
    except Exception:
        return None
    if not stamp:
        return None
    digest = hashlib.sha256(f"{record._name}:{record.ids[0]}:{stamp.isoformat()}".encode()).hexdigest()
    return f'"{digest[:20]}"'


def parse_fields_param(raw: str | None, model: Model) -> list[str] | None:
    if not raw:
        return None
    names = [n.strip() for n in raw.split(",") if n.strip()]
    unknown = [n for n in names if n not in model._fields]
    if unknown:
        raise ValidationError(_("Unknown field(s) {unknown} on {name}", unknown=unknown, name=model._name))
    return names


def content_disposition(filename: str, *, inline: bool = False) -> str:
    """A ``Content-Disposition`` value that cannot break the header: an ASCII fallback name with quotes,
    backslashes and control characters removed, plus the exact name RFC 5987-encoded (``filename*``)."""
    kind = "inline" if inline else "attachment"
    name = "".join(ch for ch in str(filename) if ch.isprintable()).strip() or "download"
    fallback = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "_" for ch in name)
    return f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


def ensure_sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, list | tuple) else [value]
