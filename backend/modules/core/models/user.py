from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from tessera.exceptions import AccessDenied, ValidationError
from tessera.orm import Model, api, fields

_hasher = PasswordHasher()
MIN_PASSWORD_LENGTH = 8


class Group(Model):
    _name = "core.group"
    _description = "Security group"
    _order = "category, name"

    name = fields.Char(required=True, translate=True)
    category = fields.Char(default="Other", index=True)
    comment = fields.Text()
    implied_ids = fields.Many2many(
        "core.group",
        "Implied groups",
        relation="core_group__implied_ids",
        column1="core_group_id",
        column2="implied_group_id",
        help="Members of this group are automatically members of the implied groups",
    )
    user_ids = fields.Many2many(
        "core.user", "Users", relation="core_user__group_ids", column1="core_group_id", column2="core_user_id"
    )
    full_name = fields.Char(compute="_compute_full_name")

    @api.depends("name", "category")
    def _compute_full_name(self) -> None:
        for group in self:
            group.full_name = f"{group.category} / {group.name}" if group.category else group.name

    def _compute_display_name(self) -> None:
        field = self._fields["display_name"]
        for group in self:
            self.env.cache.set(field, group.ids[0], group.full_name, group._cache_qualifier(field))

    def transitive_implied(self) -> Model:
        result = self
        frontier = self
        while frontier:
            nxt = frontier.mapped("implied_ids") - result
            result |= nxt
            frontier = nxt
        return result


class User(Model):
    _name = "core.user"
    _description = "User"
    _order = "name, login"
    _audit = True
    _sql_constraints = [("login_uniq", "UNIQUE (login)", "Login must be unique")]

    login = fields.Char(required=True, index=True, size=128)
    name = fields.Char(required=True, index=True)
    email = fields.Char(index=True, pii=True)
    password_hash = fields.Char(groups="core.group_system", copy=False)
    password = fields.Char(
        compute="_compute_password",
        inverse="_inverse_password",
        store=False,
        copy=False,
        help="Write-only: setting it stores an Argon2id hash in password_hash",
    )
    active = fields.Boolean(default=True)
    lang = fields.Char(default=lambda self: self.env.registry.default_lang, size=16)
    tz = fields.Char(default="UTC", size=64)
    company_id = fields.Many2one(
        "core.company",
        "Current company",
        required=True,
        ondelete="restrict",
        default=lambda self: self.env.company.ids[0] if self.env.company else None,
    )
    company_ids = fields.Many2many(
        "core.company",
        "Allowed companies",
        relation="core_user__company_ids",
        column1="core_user_id",
        column2="core_company_id",
    )
    group_ids = fields.Many2many(
        "core.group",
        "Groups",
        relation="core_user__group_ids",
        column1="core_user_id",
        column2="core_group_id",
    )
    share = fields.Boolean("External user", default=False, help="Portal/public users have no access to the back office")
    totp_secret = fields.Char(groups="core.group_system", copy=False)
    totp_enabled = fields.Boolean(default=False)
    last_login = fields.Datetime(readonly=True, copy=False)
    signature = fields.Html()
    notification_preference = fields.Selection([("inbox", "In-app"), ("email", "Email")], default="inbox")

    # -- validation ----------------------------------------------------------------------
    @api.constrains("login")
    def _check_login(self) -> None:
        for user in self:
            if not user.login or user.login != user.login.strip().lower():
                raise ValidationError("Login must be lowercase without surrounding spaces", field="login")

    @api.constrains("company_id", "company_ids")
    def _check_companies(self) -> None:
        for user in self:
            if user.company_id and user.company_ids and user.company_id not in user.company_ids:
                raise ValidationError("The current company must be one of the allowed companies", field="company_id")

    def create(self, vals_list: dict[str, Any] | Sequence[dict[str, Any]]) -> Model:
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        for vals in batch:
            if "login" in vals and isinstance(vals["login"], str):
                vals["login"] = vals["login"].strip().lower()
            if vals.get("company_id") and not vals.get("company_ids"):
                vals["company_ids"] = [("set", [vals["company_id"]])]
        return super().create(batch)

    def write(self, vals: dict[str, Any]) -> bool:
        vals = dict(vals)
        if "login" in vals and isinstance(vals["login"], str):
            vals["login"] = vals["login"].strip().lower()
        return super().write(vals)

    # -- passwords -----------------------------------------------------------------------
    def _compute_password(self) -> None:
        for user in self:
            user.password = None  # never readable

    def _inverse_password(self) -> None:
        field = self._fields["password"]
        for user in self:
            qualifier = user._cache_qualifier(field)
            raw = self.env.cache.get(field, user.ids[0], qualifier, default=None)
            if raw:
                user.elevated("set password").with_context(_recompute_write=True)._write_columns(
                    {"password_hash": self._hash_password(str(raw))}
                )
            self.env.cache.set(field, user.ids[0], None, qualifier)

    @staticmethod
    def _hash_password(password: str) -> str:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValidationError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters", field="password")
        return _hasher.hash(password)

    def set_password(self, password: str) -> None:
        self.ensure_one()
        self.elevated("set password").write({"password_hash": self._hash_password(password)})

    def check_password(self, password: str) -> bool:
        self.ensure_one()
        stored = self.elevated("check password").password_hash
        if not stored or not password:
            return False
        try:
            return bool(_hasher.verify(stored, password))
        except VerifyMismatchError:
            return False
        except Exception:
            return False

    def authenticate(self, login: str, password: str) -> Model:
        """Return the user record for valid credentials; raise ``AccessDenied`` otherwise."""
        users = self.elevated("authenticate")
        user = users.search([("login", "=", (login or "").strip().lower()), ("active", "=", True)], limit=1)
        if not user or not user.check_password(password):
            raise AccessDenied("Invalid login or password")
        user.write({"last_login": self.env.now()})
        return self.browse(user.ids)

    # -- groups ----------------------------------------------------------------------------
    def has_group(self, external_id: str) -> bool:
        self.ensure_one()
        return self.env.registry.security.has_groups(self.env.with_user(self.ids[0]), [external_id])

    def all_group_ids(self) -> list[int]:
        self.ensure_one()
        return list(self.elevated().group_ids.transitive_implied().ids)
