"""Payment providers and saved payment methods (ERD_PHASE_2 §5).

Credentials (secret key, webhook signing secret) and saved-method references are encrypted at rest with the
platform's secret encryption (``tessera.security.crypto``), like SSO client secrets and AI provider keys, and
are readable by advisers and administrators only.
"""

from __future__ import annotations

from typing import Any

from tessera.exceptions import UserError, ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields
from tessera.security.crypto import decrypt, encrypt, is_encrypted

SECRET_FIELDS = ("secret_key", "webhook_secret")
SECRET_GROUPS = ("account.group_account_manager", "core.group_system")
TOKEN_USER_FIELDS = {"display", "active"}


def _encrypt_vals(vals: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    out = dict(vals)
    for name in names:
        value = out.get(name)
        if value and not is_encrypted(value):
            out[name] = encrypt(str(value))
    return out


class PaymentProvider(Model):
    _name = "payment.provider"
    _description = "Payment provider"
    _order = "sequence, id"
    _audit = True

    name = fields.Char(required=True, translate=True)
    code = fields.Selection([("stripe", "Stripe"), ("test", "Test (no real payment)")], required=True, default="test")
    state = fields.Selection(
        [("disabled", "Disabled"), ("test", "Test mode"), ("enabled", "Enabled")],
        required=True,
        default="disabled",
        tracking=True,
    )
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one(
        "core.company",
        required=True,
        ondelete="cascade",
        default=lambda self: self.env.company.ids[0] if self.env.company else None,
    )
    journal_id = fields.Many2one(
        "account.journal",
        "Payment journal",
        ondelete="restrict",
        domain=[["type", "in", ["bank", "cash"]]],
        help="Payments received through this provider are booked in this journal",
    )
    publishable_key = fields.Char("Publishable key", help="Stripe: pk_test_… / pk_live_…")
    secret_key = fields.Char("Secret key", groups=SECRET_GROUPS, help="Stored encrypted")
    webhook_secret = fields.Char("Webhook signing secret", groups=SECRET_GROUPS, help="Stored encrypted (whsec_…)")
    allow_tokenization = fields.Boolean("Save payment methods", help="Offer customers to save their card for later")
    display_as = fields.Char("Shown to customers as", translate=True)
    transaction_ids = fields.One2many("payment.transaction", "provider_id", "Transactions")

    @api.constrains("journal_id", "company_id")
    def _check_journal(self) -> None:
        for provider in self:
            journal = provider.journal_id.elevated()
            if journal and journal.company_id.ids != provider.company_id.ids:
                raise ValidationError(
                    _("The payment journal must belong to the provider's company"), field="journal_id"
                )

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        return super().create([_encrypt_vals(v, SECRET_FIELDS) for v in batch])

    def write(self, vals: dict[str, Any]) -> bool:
        return super().write(_encrypt_vals(vals, SECRET_FIELDS))

    def _secret(self, name: str) -> str | None:
        """The decrypted value of a credential (server side only)."""
        self.ensure_one()
        return decrypt(self.elevated()[name])

    def _adapter(self) -> Any:
        from modules.payment.providers import adapter_for

        return adapter_for(self.elevated())

    @api.model
    def _available(self, company: Model, currency: Model | None = None) -> Model:
        """Providers customers of ``company`` can pay with (enabled, or in test mode; never the test provider in
        production, see ``modules.payment.providers.adapter_for``)."""
        from tessera.config import get_settings

        domain: list[Any] = [
            ("company_id", "=", company.ids[0]),
            ("state", "in", ["enabled", "test"]),
            ("journal_id", "is_set"),
        ]
        if get_settings().is_production:
            domain.append(("code", "!=", "test"))
        return self.elevated().search(domain, order="sequence, id")


class PaymentToken(Model):
    _name = "payment.token"
    _description = "Saved payment method"
    _order = "id desc"

    provider_id = fields.Many2one("payment.provider", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one("core.company", related="provider_id.company_id", store=True, index=True)
    partner_id = fields.Many2one("contacts.partner", "Customer", required=True, ondelete="cascade", index=True)
    provider_ref = fields.Char("Provider reference", required=True, groups=SECRET_GROUPS, help="Stored encrypted")
    display = fields.Char("Payment method", required=True)
    active = fields.Boolean(default=True)

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        return super().create([_encrypt_vals(v, ("provider_ref",)) for v in batch])

    def write(self, vals: dict[str, Any]) -> bool:
        # a saved method stays the card of the customer who saved it: users may rename or archive it only
        # (U-9 SEC-53; re-pointing ``partner_id`` would charge one customer's card for another's invoices)
        locked = set(vals) - TOKEN_USER_FIELDS
        if locked and not self.env.su:
            raise UserError(_("A saved payment method can only be renamed or archived"), fields=sorted(locked))
        return super().write(_encrypt_vals(vals, ("provider_ref",)))

    @api.depends("display")
    def _compute_display_name(self) -> None:
        for token in self:
            token.display_name = token.display or ""

    def _provider_ref(self) -> str | None:
        self.ensure_one()
        return decrypt(self.elevated().provider_ref)

    @api.expose
    def action_charge(self, amount: float, reference: str | None = None) -> dict[str, Any]:
        """Charge this saved method (used by recurring billing)."""
        from modules.payment.service import charge_token

        self.ensure_one()
        self.check_access("write")  # charging moves the customer's money: not for read-only users (U-9 SEC-52)
        tx = charge_token(self, float(amount), reference=reference)
        return {"reference": tx.reference, "state": tx.state, "state_message": tx.state_message}
