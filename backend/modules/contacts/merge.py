"""Merging duplicate contacts.

Every reference to a merged contact is moved to the survivor, across all installed models:

* stored many2one columns pointing at ``contacts.partner`` (direct SQL, one UPDATE per column),
* many2many relation rows (re-pointed; rows that would duplicate an existing link are dropped),
* generic references (``model`` + ``res_id``): chatter messages, activities, followers, attachments.

The merged contacts are archived, not deleted, and everything moved is recorded in ``contacts.merge_log``
so the merge can be undone within the undo window.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.orm import fields as F

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

UNDO_WINDOW = dt.timedelta(days=7)
GENERIC = [  # (table, model column, id column, unique companions that may collide)
    ("chatter_message", "model", "res_id", None),
    ("chatter_activity", "model", "res_id", None),
    ("core_attachment", "model", "res_id", None),
    ("chatter_follower", "model", "res_id", "user_id"),
]
SKIP_FIELDS = {("contacts.partner", "commercial_partner_id")}  # recomputed instead


def _references(env: Environment) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str, str]]]:
    """(many2one: model, table, column), (many2many: model, relation table, own column, partner column)."""
    m2o: list[tuple[str, str, str]] = []
    m2m: list[tuple[str, str, str, str]] = []
    seen_rel: set[str] = set()
    for name, cls in env.registry.models.items():
        for fname, field in cls._fields.items():
            if (name, fname) in SKIP_FIELDS or getattr(field, "comodel_name", None) != "contacts.partner":
                continue
            if isinstance(field, F.Many2one) and field.store:
                m2o.append((name, cls._table, fname))
            elif isinstance(field, F.Many2many) and field.relation not in seen_rel:
                seen_rel.add(field.relation)
                m2m.append((name, field.relation, field.column1, field.column2))
    return m2o, m2m


def _table_exists(env: Environment, table: str) -> bool:
    return bool(env.cr.scalar("SELECT to_regclass(:t) IS NOT NULL", {"t": table}))


def merge_partners(env: Environment, survivor: Model, others: Model, field_choices: dict[str, int]) -> Model:
    survivor.ensure_one()
    others = others - survivor
    if not others:
        raise UserError(_("Select at least two different contacts to merge"))
    if len(others) > 10:
        raise UserError(_("Merge at most 11 contacts at once"))
    (survivor | others).check_access("write")
    if any(o.ids[0] in survivor.elevated()._ancestor_ids() for o in others):
        raise UserError(_("A company cannot be merged into one of its own contacts"))
    sys_env = env.elevated("contact merge")
    survivor_s = survivor.with_env(sys_env)
    other_ids = list(others.ids)
    sid = survivor.ids[0]
    relinked: dict[str, Any] = {"m2o": [], "m2m": [], "generic": []}
    cr = env.cr

    # 1. chosen field values onto the survivor (before re-pointing, so x2many choices see original rows)
    values: dict[str, Any] = {}
    for fname, source_id in (field_choices or {}).items():
        if source_id in other_ids and fname in survivor._fields and fname not in ("id", "display_name"):
            value = others.browse([source_id]).with_env(sys_env)[fname]
            field = survivor._fields[fname]
            values[fname] = (
                value.ids[0]
                if isinstance(field, F.Many2one) and value
                else ([("set", value.ids)] if hasattr(value, "_ids") else value)
            )
    for fname in ("email", "phone", "mobile", "website", "vat", "street", "city", "zip", "country_id", "function"):
        if fname not in values and not survivor_s[fname]:  # fill blanks from the merged contacts
            for other in others.with_env(sys_env):
                value = other[fname]
                if value:
                    values[fname] = value.ids[0] if hasattr(value, "_ids") else value
                    break
    for fname, field in survivor._fields.items():  # union the survivor's own many2many values (tags, …)
        if isinstance(field, F.Many2many) and fname not in values:
            union = set(survivor_s[fname].ids)
            for other in others.with_env(sys_env):
                union |= set(other[fname].ids)
            if union != set(survivor_s[fname].ids):
                values[fname] = [("set", sorted(union))]
    before = {k: _plain(survivor_s[k]) for k in values}
    if values:
        survivor_s.write(values)

    # 2. many2one columns everywhere
    m2o, m2m = _references(env)
    for model, table, column in m2o:
        if not _table_exists(env, table):
            continue
        rows = cr.fetchall(f"SELECT id, {column} FROM {table} WHERE {column} = ANY(:ids)", {"ids": other_ids})
        rows = [r for r in rows if not (model == "contacts.partner" and r[0] == sid)]
        if not rows:
            continue
        cr.execute(
            f"UPDATE {table} SET {column} = :sid WHERE id = ANY(:rids)", {"sid": sid, "rids": [r[0] for r in rows]}
        )
        relinked["m2o"].append({"model": model, "table": table, "column": column, "rows": [[r[0], r[1]] for r in rows]})
        env.cache.invalidate(model, [column], [r[0] for r in rows])
        sys_env[model].browse([r[0] for r in rows]).modified([column])

    # 3. many2many relation rows
    for model, relation, own, partner_col in m2m:
        if not _table_exists(env, relation):
            continue
        rows = cr.fetchall(
            f"SELECT {own}, {partner_col} FROM {relation} WHERE {partner_col} = ANY(:ids)", {"ids": other_ids}
        )
        if not rows:
            continue
        existing = {r[0] for r in cr.fetchall(f"SELECT {own} FROM {relation} WHERE {partner_col} = :sid", {"sid": sid})}
        moved, dropped = [], []
        for owner, partner in rows:
            (dropped if owner in existing else moved).append([owner, partner])
            existing.add(owner)
        cr.execute(f"DELETE FROM {relation} WHERE {partner_col} = ANY(:ids)", {"ids": other_ids})
        if moved:
            cr.execute(
                f"INSERT INTO {relation} ({own}, {partner_col}) VALUES (:o, :p)", [{"o": o, "p": sid} for o, _ in moved]
            )
        relinked["m2m"].append(
            {
                "model": model,
                "relation": relation,
                "own": own,
                "partner": partner_col,
                "moved": moved,
                "dropped": dropped,
            }
        )
        env.cache.invalidate(model)

    # 4. generic references: chatter history, activities, attachments, followers
    for table, model_col, id_col, companion in GENERIC:
        if not _table_exists(env, table):
            continue
        rows = cr.fetchall(
            f"SELECT id, {id_col}{', ' + companion if companion else ''} FROM {table} "
            f"WHERE {model_col} = 'contacts.partner' AND {id_col} = ANY(:ids)",
            {"ids": other_ids},
        )
        if not rows:
            continue
        if companion:  # followers: one row per (record, user)
            taken = {
                r[0]
                for r in cr.fetchall(
                    f"SELECT {companion} FROM {table} WHERE {model_col} = 'contacts.partner' AND {id_col} = :sid",
                    {"sid": sid},
                )
            }
            keep = []
            for row in rows:
                if row[2] in taken:
                    continue
                taken.add(row[2])
                keep.append(row)
            rows = keep
        if rows:
            cr.execute(
                f"UPDATE {table} SET {id_col} = :sid WHERE id = ANY(:rids)", {"sid": sid, "rids": [r[0] for r in rows]}
            )
            relinked["generic"].append({"table": table, "column": id_col, "rows": [[r[0], r[1]] for r in rows]})
    env.cache.invalidate()

    # 5. archive the merged contacts, recompute what depended on them, log
    merged = others.with_env(sys_env).with_context(tracking_disable=True)
    names = merged.mapped("display_name")
    merged.write({"active": False})
    survivor_s.modified(["parent_id", "name"])
    log = sys_env["contacts.merge_log"].create(
        {
            "kept_partner_id": sid,
            "merged_partner_ids": other_ids,
            "field_choices": {
                "values": {k: _plain(v) if not isinstance(v, list) else v for k, v in values.items()},
                "before": before,
            },
            "relinked": relinked,
            "method": "manual",
        }
    )
    from modules.chatter.thread import post_message

    post_message(
        env,
        "contacts.partner",
        sid,
        f"<p>Merged {', '.join(str(n) for n in names)} into this contact.</p>",
        message_type="notification",
        notify=False,
    )
    return log


def _plain(value: Any) -> Any:
    if hasattr(value, "_ids"):
        return list(value.ids) if len(value.ids) != 1 else value.ids[0]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def undo_merge(env: Environment, log: Model) -> None:
    """Put every moved reference back, restore the survivor's previous values, unarchive the merged contacts."""
    log = log.elevated()
    if log.undone:
        raise UserError(_("This merge was already undone"))
    if log.create_date and env.now() - log.create_date > UNDO_WINDOW:
        raise UserError(_("The undo window for this merge has passed"))
    cr = env.cr
    data = log.relinked or {}
    for item in data.get("m2o", []):
        for rid, old in item["rows"]:
            cr.execute(f"UPDATE {item['table']} SET {item['column']} = :old WHERE id = :id", {"old": old, "id": rid})
        env[item["model"]].elevated().browse([r[0] for r in item["rows"]]).modified([item["column"]])
    for item in data.get("m2m", []):
        sid = log.kept_partner_id.ids[0]
        moved_owners = [o for o, _ in item["moved"]]
        if moved_owners:
            cr.execute(
                f"DELETE FROM {item['relation']} WHERE {item['partner']} = :sid AND {item['own']} = ANY(:owners)",
                {"sid": sid, "owners": moved_owners},
            )
        rows = item["moved"] + item["dropped"]
        if rows:
            cr.execute(
                f"INSERT INTO {item['relation']} ({item['own']}, {item['partner']}) VALUES (:o, :p) "
                "ON CONFLICT DO NOTHING",
                [{"o": o, "p": p} for o, p in rows],
            )
    for item in data.get("generic", []):
        for rid, old in item["rows"]:
            cr.execute(f"UPDATE {item['table']} SET {item['column']} = :old WHERE id = :id", {"old": old, "id": rid})
    env.cache.invalidate()
    survivor = log.kept_partner_id.with_context(tracking_disable=True)
    before = (log.field_choices or {}).get("before", {})
    restore: dict[str, Any] = {}
    for fname, value in before.items():
        field = survivor._fields.get(fname)
        if field is None:
            continue
        restore[fname] = (
            [("set", value if isinstance(value, list) else [value])] if isinstance(field, F.Many2many) else value
        )
    if restore:
        survivor.write(restore)
    env["contacts.partner"].elevated().with_context(active_test=False).browse(log.merged_partner_ids or []).write(
        {"active": True}
    )
    log.write({"undone": True, "undone_at": env.now()})
