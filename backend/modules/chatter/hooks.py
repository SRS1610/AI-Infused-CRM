"""Registry hooks: auto-follow on create, field tracking on write, thread cleanup on delete."""

from __future__ import annotations

from typing import Any

from modules.chatter.thread import SYSTEM_UID, display_for_tracking, is_thread, post_message, subscribe, tracked_fields
from tessera.orm import Model
from tessera.orm.hooks import hook

TRACKING_KEY = "__chatter_tracking__"


def _responsible(rec: Model) -> bool:
    """The record has a ``user_id`` pointing at a user (salesperson, owner, …) that should follow it."""
    field = rec._fields.get("user_id")
    return bool(field is not None and getattr(field, "comodel_name", None) == "core.user" and rec.user_id)


def _enabled(records: Model) -> bool:
    return is_thread(records) and not records.env.context.get("tracking_disable") and bool(records._ids)


@hook("after_create")
def thread_on_create(records: Model, vals_list: list[dict[str, Any]]) -> None:
    if not _enabled(records):
        return
    env = records.env
    for rec in records.elevated("thread create"):
        followers = [env.uid] if env.uid != SYSTEM_UID else []
        if _responsible(rec):
            followers.append(rec.user_id.ids[0])
        subscribe(env, rec._name, rec.ids[0], followers)
        if type(records)._track_create:
            post_message(
                env,
                rec._name,
                rec.ids[0],
                f"<p>{rec._description} created</p>",
                message_type="notification",
                notify=False,
            )


@hook("before_write")
def thread_capture_tracking(records: Model, vals: dict[str, Any], old_values: dict[Any, Any]) -> None:
    if not _enabled(records):
        return
    names = [n for n in tracked_fields(records) if n in vals]
    if not names:
        return
    snapshot: dict[int, dict[str, tuple[str | None, Any]]] = {}
    for rec in records.elevated("tracking"):
        snapshot[rec.ids[0]] = {n: display_for_tracking(rec, n) for n in names}
    old_values[TRACKING_KEY] = snapshot


@hook("after_write")
def thread_post_tracking(records: Model, vals: dict[str, Any], old_values: dict[Any, Any]) -> None:
    snapshot = old_values.get(TRACKING_KEY)
    if not snapshot or not _enabled(records):
        return
    env = records.env
    for rec in records.elevated("tracking"):
        before = snapshot.get(rec.ids[0], {})
        changes = []
        for name, (old_display, old_raw) in before.items():
            new_display, new_raw = display_for_tracking(rec, name)
            if old_raw != new_raw:
                changes.append(
                    {
                        "field_name": name,
                        "field_label": rec._fields[name].label,
                        "old_display": old_display,
                        "new_display": new_display,
                        "old_value": {"v": old_raw},  # wrapped: a Json field reads a bare str as JSON text
                        "new_value": {"v": new_raw},
                    }
                )
        if changes:
            post_message(env, rec._name, rec.ids[0], "", message_type="notification", tracking_values=changes)
        if "user_id" in vals and _responsible(rec):
            subscribe(env, rec._name, rec.ids[0], [rec.user_id.ids[0]])


@hook("before_unlink")
def thread_cleanup(records: Model) -> None:
    if not is_thread(records) or not records._ids:
        return
    if records._soft_delete and not records.env.context.get("force_delete"):
        return  # soft-deleted records keep their history (restorable)
    cr = records.env.cr
    params = {"m": records._name, "ids": list(records.ids)}
    cr.execute("DELETE FROM chatter_message WHERE model = :m AND res_id = ANY(:ids)", params)
    cr.execute("DELETE FROM chatter_follower WHERE model = :m AND res_id = ANY(:ids)", params)
    cr.execute("DELETE FROM chatter_activity WHERE model = :m AND res_id = ANY(:ids)", params)
