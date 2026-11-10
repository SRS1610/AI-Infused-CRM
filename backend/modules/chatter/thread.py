"""The ``chatter.thread`` mixin and the posting / following / tracking services behind it.

A model gets a chatter by inheriting the mixin::

    class Lead(Model):
        _name = "crm.lead"
        _inherit = ["chatter.thread"]
        stage_id = fields.Many2one("crm.stage", tracking=True)

Messages, followers and activities reference records generically (``model`` + ``res_id``), so access
to them is always derived from access to the record: callers check the record first, then read the
thread elevated (see ``check_thread_access``).
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import TYPE_CHECKING, Any, ClassVar

from tessera.config import get_settings
from tessera.exceptions import AccessError, UserError
from tessera.orm import Model, api, fields
from tessera.realtime import publish_after_commit

if TYPE_CHECKING:
    from tessera.orm.environment import Environment

SYSTEM_UID = 1
MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_.\-]{2,64})")


class Thread(Model):
    _name = "chatter.thread"
    _abstract = True
    _description = "Discussion thread mixin"
    _thread: ClassVar[bool] = True
    _track_create: ClassVar[bool] = True  # post a "created" entry in the thread
    _view_hidden_fields: ClassVar[tuple[str, ...]] = (
        "activity_state",
        "activity_date_deadline",
        "message_follower_count",
    )

    activity_state = fields.Selection(
        [("overdue", "Overdue"), ("today", "Today"), ("planned", "Planned")],
        compute="_compute_activity_state",
        help="State of the most urgent open activity",
    )
    activity_date_deadline = fields.Date("Next activity", compute="_compute_activity_state")
    message_follower_count = fields.Integer("Followers", compute="_compute_follower_count")

    def _compute_activity_state(self) -> None:
        today = self.env.today()
        nearest: dict[int, Any] = {}
        if self.ids:
            rows = self.env.cr.fetchall(
                """SELECT res_id, MIN(date_deadline) FROM chatter_activity
                   WHERE model = :m AND res_id = ANY(:ids) AND NOT done GROUP BY res_id""",
                {"m": self._name, "ids": list(self.ids)},
            )
            nearest = {r[0]: r[1] for r in rows}
        for rec in self:
            deadline = nearest.get(rec.ids[0]) if rec.ids else None
            rec.activity_date_deadline = deadline
            if deadline is None:
                rec.activity_state = None
            else:
                rec.activity_state = "overdue" if deadline < today else "today" if deadline == today else "planned"

    def _compute_follower_count(self) -> None:
        counts: dict[int, int] = {}
        if self.ids:
            rows = self.env.cr.fetchall(
                "SELECT res_id, COUNT(*) FROM chatter_follower WHERE model = :m AND res_id = ANY(:ids) GROUP BY res_id",
                {"m": self._name, "ids": list(self.ids)},
            )
            counts = {r[0]: r[1] for r in rows}
        for rec in self:
            rec.message_follower_count = counts.get(rec.ids[0], 0) if rec.ids else 0

    # -- exposed API (REST method calls / JSON-RPC) ------------------------------------------------

    @api.expose
    def message_post(
        self,
        body: str,
        subject: str | None = None,
        internal: bool = False,
        attachment_ids: list[int] | None = None,
        mention_user_ids: list[int] | None = None,
        parent_id: int | None = None,
    ) -> list[int]:
        ids = []
        for rec in self:
            msg = post_message(
                rec.env,
                rec._name,
                rec.ids[0],
                body,
                subject=subject,
                message_type="note" if internal else "comment",
                attachment_ids=attachment_ids,
                mention_user_ids=mention_user_ids,
                parent_id=parent_id,
                check_access=True,
            )
            ids.extend(msg.ids)
        return ids

    @api.expose
    def message_subscribe(self, user_ids: list[int] | None = None, subtype_ids: list[int] | None = None) -> bool:
        targets = user_ids or [self.env.uid]
        if any(uid != self.env.uid for uid in targets):
            self.check_access("write")  # adding someone else needs write access on the record
        else:
            self.check_access("read")
        for rec in self:
            subscribe(rec.env, rec._name, rec.ids[0], targets, subtype_ids)
        return True

    @api.expose
    def message_unsubscribe(self, user_ids: list[int] | None = None) -> bool:
        targets = user_ids or [self.env.uid]
        if any(uid != self.env.uid for uid in targets):
            self.check_access("write")
        followers = self.env["chatter.follower"].elevated("unsubscribe")
        followers.search([("model", "=", self._name), ("res_id", "in", self.ids), ("user_id", "in", targets)]).unlink()
        return True

    @api.expose
    def activity_schedule(
        self,
        activity_type: str | int = "chatter.activity_type_todo",
        summary: str | None = None,
        date_deadline: str | None = None,
        user_id: int | None = None,
        note: str | None = None,
    ) -> list[int]:
        self.check_access("write")
        ids = []
        for rec in self:
            act = schedule_activity(
                rec.env, rec._name, rec.ids[0], activity_type, summary, date_deadline, user_id, note
            )
            ids.extend(act.ids)
        return ids

    # -- inbound email hooks (override per model) ----------------------------------------------------

    @api.model
    def message_new(self, msg: dict[str, Any], defaults: dict[str, Any] | None = None) -> Model:
        """Create a record from an inbound email sent to an alias. Default: set the record name from
        the subject; models override this to map more (contact, description, …)."""
        vals = dict(defaults or {})
        rec_name = self._rec_name or "name"
        if rec_name in self._fields and rec_name not in vals:
            vals[rec_name] = msg.get("subject") or "(no subject)"
        return self.create(vals)

    def message_update(self, msg: dict[str, Any]) -> bool:
        """Called when a reply arrives for an existing record. Default: nothing beyond posting it."""
        return True


def is_thread(model: Model | type) -> bool:
    return bool(getattr(model, "_thread", False))


# -- access -----------------------------------------------------------------------------------------


def check_thread_access(env: Environment, model: str, res_id: int, operation: str = "read") -> Model:
    """The record a thread belongs to, after checking the caller may ``operation`` it."""
    if model not in env.registry.models:
        raise UserError(f"Unknown model '{model}'")
    record = env[model].browse([int(res_id)])
    if not is_thread(record):
        raise UserError(f"'{model}' has no discussion thread")
    record.check_access(operation)
    return record


def is_internal_user(env: Environment, uid: int | None = None) -> bool:
    uid = env.uid if uid is None else uid
    if uid == SYSTEM_UID:
        return True
    if uid <= 0:
        return False
    user = env["core.user"].elevated().browse([uid]).exists()
    return bool(user) and not user.share


# -- followers --------------------------------------------------------------------------------------


def default_subtype_ids(env: Environment, model: str) -> list[int]:
    subtypes = env["chatter.subtype"].elevated().search([("default", "=", True)])
    return [s.ids[0] for s in subtypes if not s.model_name or s.model_name == model]


def subscribe(
    env: Environment, model: str, res_id: int, user_ids: list[int], subtype_ids: list[int] | None = None
) -> None:
    followers = env["chatter.follower"].elevated("subscribe")
    existing = {f.user_id.ids[0]: f for f in followers.search([("model", "=", model), ("res_id", "=", res_id)])}
    subtypes = subtype_ids if subtype_ids is not None else default_subtype_ids(env, model)
    for uid in dict.fromkeys(user_ids):
        if not uid or uid == SYSTEM_UID:
            continue
        if uid in existing:
            if subtype_ids is not None:
                existing[uid].write({"subtype_ids": [("set", subtypes)]})
            continue
        followers.create({"model": model, "res_id": res_id, "user_id": uid, "subtype_ids": [("set", subtypes)]})


# -- posting ----------------------------------------------------------------------------------------

SUBTYPE_BY_TYPE = {
    "comment": "chatter.subtype_comment",
    "email": "chatter.subtype_comment",
    "note": "chatter.subtype_note",
    "notification": "chatter.subtype_tracking",
    "system": "chatter.subtype_tracking",
    "activity_done": "chatter.subtype_activity",
}


def _resolve_mentions(env: Environment, body: str, explicit: list[int] | None) -> list[int]:
    ids = [int(u) for u in explicit or []]
    logins = {m.lower() for m in MENTION_RE.findall(re.sub(r"<[^>]+>", " ", body or ""))}
    if logins:
        users = env["core.user"].elevated("mentions").search([("login", "in", sorted(logins))])
        ids.extend(users.ids)
    return list(dict.fromkeys(ids))


def post_message(
    env: Environment,
    model: str,
    res_id: int,
    body: str,
    *,
    subject: str | None = None,
    message_type: str = "comment",
    attachment_ids: list[int] | None = None,
    mention_user_ids: list[int] | None = None,
    parent_id: int | None = None,
    author_id: int | None = None,
    email_from: str | None = None,
    message_id: str | None = None,
    tracking_values: list[dict[str, Any]] | None = None,
    notify: bool = True,
    check_access: bool = False,
    ai_run_id: int | None = None,
) -> Model:
    """Post to a record's thread, subscribe the author, notify followers and mentioned users."""
    record = check_thread_access(env, model, res_id, "read") if check_access else env[model].browse([res_id])
    author = author_id if author_id is not None else (env.uid if env.uid != SYSTEM_UID else None)
    internal = message_type in ("note", "notification", "system", "activity_done")
    if check_access and internal and not is_internal_user(env):
        raise AccessError("Portal users can only post public messages")
    subtype = env.ref(SUBTYPE_BY_TYPE.get(message_type, "chatter.subtype_comment"), raise_if_not_found=False)
    mentions = _resolve_mentions(env, body, mention_user_ids)
    if attachment_ids:
        atts = env["core.attachment"].browse(attachment_ids)
        atts.check_record_access("read")
        atts.elevated().filtered(lambda a: not a.res_id).write({"model": model, "res_id": res_id})
    rec_name = record.elevated().display_name if record.ids else None
    msg = (
        env["chatter.message"]
        .elevated("post message")
        .create(
            {
                "model": model,
                "res_id": res_id,
                "record_name": rec_name,
                "message_type": message_type,
                "subtype_id": subtype.ids[0] if subtype else None,
                "is_internal": internal,
                "subject": subject,
                "body": body,
                "author_id": author,
                "email_from": email_from,
                "parent_id": parent_id,
                "attachment_ids": [("set", attachment_ids or [])],
                "mention_user_ids": [("set", mentions)],
                "message_id": message_id,
                "ai_run_id": ai_run_id,
                "tracking_value_ids": [("create", tv) for tv in tracking_values or []],
            }
        )
    )
    if author and message_type in ("comment", "note", "email"):
        subscribe(env, model, res_id, [author])
    publish_after_commit(env.cr, f"record:{model}:{res_id}", "chatter", {"message_id": msg.ids[0], "uid": env.uid})
    if notify:
        _notify(env, msg, mentions)
    return msg


def _notify(env: Environment, msg: Model, mentions: list[int]) -> None:
    msg = msg.elevated()
    author = msg.author_id.ids[0] if msg.author_id else None
    followers = env["chatter.follower"].elevated().search([("model", "=", msg.model), ("res_id", "=", msg.res_id)])
    subtype_id = msg.subtype_id.ids[0] if msg.subtype_id else None
    recipients: dict[int, str] = {}
    for follower in followers:
        uid = follower.user_id.ids[0]
        if uid != author and subtype_id in follower.subtype_ids.ids:
            recipients[uid] = "message"
    for uid in mentions:
        if uid != author:
            recipients[uid] = "mention"
    if not recipients:
        return
    users = env["core.user"].elevated().browse(list(recipients))
    author_name = msg.author_id.name if msg.author_id else "System"
    title_by_kind = {
        "message": f"{author_name} on {msg.record_name}",
        "mention": f"{author_name} mentioned you on {msg.record_name}",
    }
    preview = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", msg.body or "")).strip()[:280]
    notifications = env["core.notification"].elevated("notify")
    for user in users:
        uid = user.ids[0]
        if msg.is_internal and not is_internal_user(env, uid):
            continue  # portal followers never see internal content
        try:  # the recipient must still be allowed to read the record
            env[msg.model].with_user(uid).browse([msg.res_id]).check_access("read")
        except AccessError:
            continue
        kind = recipients[uid]
        notifications.create(
            {
                "user_id": uid,
                "title": title_by_kind[kind],
                "body": preview,
                "kind": kind,
                "model": msg.model,
                "res_id": msg.res_id,
                "url": f"/m/{msg.model}/{msg.res_id}",
            }
        )
        wants_email = user.notification_preference == "email" and bool(user.email)
        if wants_email and (not msg.is_internal or kind == "mention"):
            _email_notification(env, msg, user)


def reply_address(model: str, res_id: int) -> str:
    """``reply+<token>@<domain>`` where the token names the record and is HMAC-signed."""
    settings = get_settings()
    payload = f"{model}:{res_id}".encode().hex()
    sig = hmac.new(settings.secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
    domain = settings.mail_catchall_domain or (
        settings.mail_from.rsplit("@", 1)[-1] if "@" in settings.mail_from else "tessera.local"
    )
    return f"reply+{payload}.{sig}@{domain}"


def parse_reply_token(local_part: str) -> tuple[str, int] | None:
    if not local_part.lower().startswith("reply+"):
        return None
    token = local_part[6:]
    payload, _, sig = token.rpartition(".")
    expected = hmac.new(get_settings().secret_key.encode(), payload.lower().encode(), hashlib.sha256).hexdigest()[:16]
    if not payload or not hmac.compare_digest(sig.lower(), expected):
        return None
    try:
        model, _, res_id = bytes.fromhex(payload).decode().rpartition(":")
        return model, int(res_id)
    except ValueError:
        return None


def _email_notification(env: Environment, msg: Model, user: Model) -> None:
    subject = msg.subject or f"Re: {msg.record_name}"
    author = msg.author_id.name if msg.author_id else "Tessera"
    body = (
        f"<p><b>{author}</b> on <b>{msg.record_name}</b>:</p>{msg.body or ''}"
        f"<hr/><p style='color:#666;font-size:12px'>Reply to this email to answer in the thread.</p>"
    )
    references = msg.parent_id.message_id if msg.parent_id else None
    env["core.mail_queue"].queue(
        {
            "email_to": user.email,
            "subject": subject,
            "body_html": body,
            "reply_to": reply_address(msg.model, msg.res_id),
            "model": msg.model,
            "res_id": msg.res_id,
            "chatter_message_id": msg.ids[0],
            "in_reply_to": references,
            "references": references,
            "message_id": msg.message_id or None,
        }
    )


# -- tracking -----------------------------------------------------------------------------------------


def tracked_fields(records: Model) -> list[str]:
    return [name for name, f in records._fields.items() if getattr(f, "tracking", False) and f.store]


def display_for_tracking(record: Model, name: str) -> tuple[str | None, Any]:
    field = record._fields[name]
    value = record[name]
    if field.type_name == "many2one":
        return (value.display_name if value else None), (value.ids[0] if value else None)
    if field.type_name in ("many2many", "one2many"):
        return (", ".join(value.mapped("display_name")) if value else None), list(value.ids)
    if field.type_name == "selection" and isinstance(field.selection, list | tuple):
        labels = dict(field.selection)
        return (str(labels.get(value, value)) if value else None), value
    if value is None or (value is False and field.type_name != "boolean"):
        return None, None
    if hasattr(value, "isoformat"):
        return value.isoformat(), value.isoformat()
    return str(value), value


# -- activities -------------------------------------------------------------------------------------


def schedule_activity(
    env: Environment,
    model: str,
    res_id: int,
    activity_type: str | int,
    summary: str | None = None,
    date_deadline: str | None = None,
    user_id: int | None = None,
    note: str | None = None,
) -> Model:
    import datetime as dt

    types = env["chatter.activity_type"].elevated()
    atype = env.ref(activity_type) if isinstance(activity_type, str) else types.browse([activity_type]).exists()
    if not atype:
        raise UserError(f"Unknown activity type '{activity_type}'")
    deadline = (
        dt.date.fromisoformat(date_deadline)
        if date_deadline
        else env.today() + dt.timedelta(days=atype.default_days or 0)
    )
    assignee = user_id or (env.uid if env.uid != SYSTEM_UID else None)
    if not assignee:
        raise UserError("An activity needs an assignee")
    record = env[model].browse([res_id])
    act = (
        env["chatter.activity"]
        .elevated("schedule activity")
        .create(
            {
                "model": model,
                "res_id": res_id,
                "res_name": record.elevated().display_name,
                "activity_type_id": atype.ids[0],
                "summary": summary or atype.default_summary,
                "date_deadline": deadline,
                "user_id": assignee,
                "note": note,
            }
        )
    )
    if assignee != env.uid:
        env["core.notification"].notify(
            [assignee],
            f"New activity: {act.summary or atype.name}",
            f"{record.elevated().display_name} — due {deadline.isoformat()}",
            kind="activity",
            model=model,
            res_id=res_id,
            url=f"/m/{model}/{res_id}",
        )
    publish_after_commit(env.cr, f"record:{model}:{res_id}", "chatter", {"activity_id": act.ids[0], "uid": env.uid})
    return act
