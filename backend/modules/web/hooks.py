"""Publish record changes to the realtime hub after commit."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tessera.orm.hooks import hook
from tessera.realtime import publish_after_commit

if TYPE_CHECKING:
    from tessera.orm.model import Model

SILENT_PREFIXES = (
    "core.audit_log",
    "core.pii_access_log",
    "core.session",
    "core.webhook_delivery",
    "core.module",
    "core.model",
)


def _silent(records: Model) -> bool:
    return records._name.startswith(SILENT_PREFIXES) or not records._ids


def _emit(records: Model, event: str, fields: list[str]) -> None:
    payload = {"model": records._name, "fields": fields, "uid": records.env.uid}
    for rid in records.ids:
        publish_after_commit(records.env.cr, f"record:{records._name}:{rid}", event, {**payload, "id": rid})
    publish_after_commit(records.env.cr, f"model:{records._name}", event, {**payload, "ids": records.ids})


@hook("after_create")
def realtime_create(records: Model, vals_list: list[dict[str, Any]]) -> None:
    if not _silent(records):
        _emit(records, "create", sorted({k for v in vals_list for k in v}))


@hook("after_write")
def realtime_write(records: Model, vals: dict[str, Any], old_values: dict[int, dict[str, Any]]) -> None:
    if not _silent(records):
        _emit(records, "write", sorted(vals))


@hook("after_unlink")
def realtime_unlink(model: Model, ids: list[int]) -> None:
    if ids and not model._name.startswith(SILENT_PREFIXES):
        _emit(model.browse(ids), "unlink", [])
