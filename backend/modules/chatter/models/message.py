"""Messages, followers, tracking values, reactions."""

from __future__ import annotations

from tessera.orm import Model, fields

MESSAGE_TYPES = [
    ("comment", "Comment"),
    ("note", "Internal note"),
    ("notification", "Notification"),
    ("email", "Email"),
    ("system", "System"),
    ("activity_done", "Activity done"),
]


class Subtype(Model):
    _name = "chatter.subtype"
    _description = "Message subtype"
    _order = "sequence, id"

    name = fields.Char(required=True, translate=True)
    internal = fields.Boolean(help="Hidden from portal users")
    default = fields.Boolean(default=True, help="Followers subscribe to it by default")
    model_name = fields.Char(help="Empty = available on every model")
    sequence = fields.Integer(default=10)


class Message(Model):
    _name = "chatter.message"
    _description = "Message"
    _order = "id desc"
    _rec_name = "subject"
    _studio = False
    _indexes = [("model_res_id", ["model", "res_id"])]

    model = fields.Char(required=True, index=True)
    res_id = fields.Integer(required=True)
    record_name = fields.Char()
    message_type = fields.Selection(MESSAGE_TYPES, default="comment", required=True)
    subtype_id = fields.Many2one("chatter.subtype", ondelete="set_null")
    is_internal = fields.Boolean(help="Internal note: never shown to portal users or emailed outside")
    subject = fields.Char()
    body = fields.Html()
    author_id = fields.Many2one("core.user", ondelete="set_null", fk=False, index=True)
    email_from = fields.Char()
    parent_id = fields.Many2one("chatter.message", ondelete="set_null")
    attachment_ids = fields.Many2many(
        "core.attachment", relation="chatter_message__attachment_ids", column1="message_id", column2="attachment_id"
    )
    tracking_value_ids = fields.One2many("chatter.tracking_value", "message_id")
    reaction_ids = fields.One2many("chatter.reaction", "message_id")
    mention_user_ids = fields.Many2many(
        "core.user", relation="chatter_message__mention_ids", column1="message_id", column2="user_id"
    )
    message_id = fields.Char("Message-ID", index=True, help="Email Message-ID for threading replies")
    ai_run_id = fields.Integer(help="Set when the content was drafted by AI and accepted by the author")
    date = fields.Datetime(default=lambda self: self.env.now(), index=True)


class Follower(Model):
    _name = "chatter.follower"
    _description = "Follower"
    _rec_name = "model"
    _studio = False
    _indexes = [("model_res_id", ["model", "res_id"])]
    _sql_constraints = [("user_uniq", "UNIQUE (model, res_id, user_id)", "This user already follows the record")]

    model = fields.Char(required=True, index=True)
    res_id = fields.Integer(required=True)
    user_id = fields.Many2one("core.user", required=True, ondelete="cascade", index=True)
    subtype_ids = fields.Many2many(
        "chatter.subtype", relation="chatter_follower__subtype_ids", column1="follower_id", column2="subtype_id"
    )


class TrackingValue(Model):
    _name = "chatter.tracking_value"
    _description = "Tracked field change"
    _order = "id"
    _rec_name = "field_name"
    _studio = False
    _log_access = False

    message_id = fields.Many2one("chatter.message", required=True, ondelete="cascade", index=True)
    field_name = fields.Char(required=True)
    field_label = fields.Char()
    old_display = fields.Char()
    new_display = fields.Char()
    old_value = fields.Json()
    new_value = fields.Json()


class Reaction(Model):
    _name = "chatter.reaction"
    _description = "Reaction"
    _rec_name = "emoji"
    _studio = False
    _sql_constraints = [("uniq", "UNIQUE (message_id, user_id, emoji)", "Already reacted")]

    message_id = fields.Many2one("chatter.message", required=True, ondelete="cascade", index=True)
    user_id = fields.Many2one("core.user", required=True, ondelete="cascade", default=lambda self: self.env.uid)
    emoji = fields.Char(required=True, size=16)
