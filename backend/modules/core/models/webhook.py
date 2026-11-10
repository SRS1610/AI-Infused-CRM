"""Outbound webhooks on create / write / delete, with HMAC signatures and a delivery log.

Deliveries are created inside the transaction and dispatched after commit (never for rolled-back
work). Phase 1.2 dispatches from a background thread; Phase 1.3 moves dispatch to the job queue
with retry policies.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from typing import Any

import httpx

from tessera.logging import get_logger
from tessera.orm import Model, api, fields
from tessera.orm.domain import Domain
from tessera.orm.hooks import hook
from tessera.security import crypto

log = get_logger(__name__)

EVENTS = [("create", "Created"), ("write", "Updated"), ("unlink", "Deleted")]


class Webhook(Model):
    _name = "core.webhook"
    _description = "Webhook"
    _order = "name"
    _audit = True
    _inherit = ["core.multi_company"]

    name = fields.Char(required=True)
    model_name = fields.Char("Model", required=True, index=True)
    events = fields.Json(default=lambda self: ["create", "write", "unlink"], required=True)
    domain = fields.Json(help="Only records matching this domain trigger the webhook")
    fields_sent = fields.Json(help="Field names included in the payload; empty = all readable fields")
    url = fields.Char(required=True)
    secret = fields.Char(groups="core.group_system", help="HMAC-SHA256 signing secret (stored encrypted)")
    headers = fields.Json(default=dict)
    active = fields.Boolean(default=True)
    user_id = fields.Many2one(
        "core.user",
        "Run as",
        required=True,
        ondelete="cascade",
        default=lambda self: self.env.uid,
        help="Payloads are read with this user's permissions",
    )
    delivery_ids = fields.One2many("core.webhook_delivery", "webhook_id")

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        for vals in batch:
            if vals.get("secret") and not crypto.is_encrypted(vals["secret"]):
                vals["secret"] = crypto.encrypt(vals["secret"])
        return super().create(batch)

    def write(self, vals):  # type: ignore[no-untyped-def]
        vals = dict(vals)
        if vals.get("secret") and not crypto.is_encrypted(vals["secret"]):
            vals["secret"] = crypto.encrypt(vals["secret"])
        return super().write(vals)

    @api.expose
    def action_test(self) -> dict[str, Any]:
        self.ensure_one()
        delivery = (
            self.env["core.webhook_delivery"]
            .elevated()
            .create(
                {
                    "webhook_id": self.ids[0],
                    "event": "test",
                    "model": self.model_name,
                    "res_id": 0,
                    "payload": {"event": "test"},
                }
            )
        )
        delivery.deliver()
        return {"status": delivery.status, "response_status": delivery.response_status}


class WebhookDelivery(Model):
    _name = "core.webhook_delivery"
    _description = "Webhook delivery"
    _order = "id desc"
    _rec_name = "event"
    _studio = False

    webhook_id = fields.Many2one("core.webhook", required=True, ondelete="cascade", index=True)
    event = fields.Char(required=True)
    model = fields.Char(required=True)
    res_id = fields.Integer(required=True)
    payload = fields.Json(required=True)
    status = fields.Selection(
        [("pending", "Pending"), ("sent", "Sent"), ("failed", "Failed")], default="pending", required=True, index=True
    )
    attempts = fields.Integer(default=0)
    response_status = fields.Integer()
    response_body = fields.Text()
    error = fields.Text()
    next_attempt_at = fields.Datetime()
    sent_at = fields.Datetime()

    MAX_ATTEMPTS = 8

    @api.model
    def retry_due(self, limit: int = 100) -> int:
        """Re-send failed deliveries whose backoff has elapsed (called by the scheduler).
        Backoff is 2^attempts minutes, capped at 2 hours; after MAX_ATTEMPTS a delivery stays failed."""
        due = self.elevated().search(
            [
                ("status", "=", "failed"),
                ("attempts", "<", self.MAX_ATTEMPTS),
                ("next_attempt_at", "<=", self.env.now()),
            ],
            limit=limit,
            order="next_attempt_at, id",
        )
        for delivery in due:
            delivery.deliver()
        return len(due)

    def deliver(self, timeout: float = 10.0) -> bool:
        """Send one delivery synchronously and record the outcome."""
        self.ensure_one()
        webhook = self.webhook_id.elevated()
        body = json.dumps(self.payload, default=str, sort_keys=True).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Tessera-Event": self.event,
            "X-Tessera-Delivery": str(self.ids[0]),
        }
        headers.update({str(k): str(v) for k, v in (webhook.headers or {}).items()})
        secret = crypto.decrypt(webhook.secret)
        if secret:
            headers["X-Tessera-Signature"] = crypto.sign_payload(secret, body)
        vals: dict[str, Any] = {"attempts": (self.attempts or 0) + 1}
        try:
            resp = httpx.post(webhook.url, content=body, headers=headers, timeout=timeout)
            vals.update({"response_status": resp.status_code, "response_body": resp.text[:2000]})
            if 200 <= resp.status_code < 300:
                vals.update({"status": "sent", "sent_at": self.env.now(), "error": None})
            else:
                vals.update({"status": "failed", "error": f"HTTP {resp.status_code}"})
        except Exception as exc:
            vals.update({"status": "failed", "error": str(exc)[:1000]})
        vals["next_attempt_at"] = (
            None if vals["status"] == "sent" else self.env.now() + dt.timedelta(minutes=min(2 ** vals["attempts"], 120))
        )
        self.elevated().write(vals)
        return bool(vals["status"] == "sent")


# -- event collection ----------------------------------------------------------------------------


def _webhooks_for(records: Model, event: str) -> Model:
    hooks = records.env["core.webhook"].elevated().with_context(active_test=True)
    return hooks.search([("model_name", "=", records._name), ("active", "=", True)]).filtered(
        lambda w: event in (w.events or [])
    )


def _queue_deliveries(records: Model, event: str, changed: list[str] | None = None) -> None:
    if records._name.startswith("core.webhook") or not records._ids:
        return
    webhooks = _webhooks_for(records, event)
    if not webhooks:
        return
    deliveries = records.env["core.webhook_delivery"].elevated()
    created_ids: list[int] = []
    for webhook in webhooks:
        scoped = records.with_user(webhook.user_id.ids[0]).with_context(active_test=False, include_deleted=True)
        if webhook.domain and event != "unlink":
            try:
                scoped = scoped.search([("id", "in", scoped.ids), *([Domain(webhook.domain).node])])
            except Exception:
                log.warning("webhook domain failed", webhook=webhook.ids[0])
                continue
        for rec in scoped:
            data: dict[str, Any] | None = None
            if event != "unlink":
                try:
                    names = webhook.fields_sent or None
                    data = rec.read(names)[0]
                except Exception:
                    data = None
            payload = {
                "event": event,
                "model": records._name,
                "id": rec.ids[0],
                "changed_fields": changed or [],
                "data": data,
                "timestamp": records.env.now().isoformat(),
                "webhook": webhook.ids[0],
            }
            delivery = deliveries.create(
                {
                    "webhook_id": webhook.ids[0],
                    "event": event,
                    "model": records._name,
                    "res_id": rec.ids[0],
                    "payload": payload,
                }
            )
            created_ids.extend(delivery.ids)
    if created_ids:
        db_url = records.env.registry.db_url
        records.env.cr.after_commit(lambda: dispatch_deliveries(db_url, created_ids))


def dispatch_deliveries(db_url: str, delivery_ids: list[int]) -> None:
    """Deliver after commit, off the request thread (replaced by a job in Phase 1.3).
    Runs inline in the test environment so tests can assert on the outcome."""

    def run() -> None:
        from tessera.orm.environment import environment_for

        try:
            with environment_for(db_url, uid=1, su=True) as env:
                for delivery in env["core.webhook_delivery"].browse(delivery_ids):
                    delivery.deliver()
        except Exception:
            log.exception("webhook dispatch failed")

    from tessera.config import get_settings

    if get_settings().env == "test":
        run()
        return
    threading.Thread(target=run, name="webhook-dispatch", daemon=True).start()


@hook("after_create")
def webhook_on_create(records: Model, vals_list: list[dict[str, Any]]) -> None:
    _queue_deliveries(records, "create")


@hook("after_write")
def webhook_on_write(records: Model, vals: dict[str, Any], old_values: dict[int, dict[str, Any]]) -> None:
    _queue_deliveries(records, "write", list(vals))


@hook("before_unlink")
def webhook_on_unlink(records: Model) -> None:
    _queue_deliveries(records, "unlink")
