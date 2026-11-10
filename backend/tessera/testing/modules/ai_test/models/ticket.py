from __future__ import annotations

from tessera.orm import Model, fields


class Ticket(Model):
    _name = "ai_test.ticket"
    _description = "Support ticket"
    _inherit = ["chatter.thread", "core.multi_company"]
    _ai_index = ["description", "customer_name", "state"]

    name = fields.Char(required=True)
    description = fields.Text()
    customer_name = fields.Char(pii=True)
    customer_email = fields.Char(pii=True)
    state = fields.Selection([("new", "New"), ("open", "Open"), ("done", "Done")], default="new")
    amount = fields.Float()
    secret_note = fields.Char(groups="core.group_system")
