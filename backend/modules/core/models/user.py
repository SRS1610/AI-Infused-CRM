from __future__ import annotations

import hashlib
import secrets
from collections.abc import Sequence
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from tessera.config import get_settings
from tessera.exceptions import AccessDenied, ValidationError
from tessera.i18n import _
from tessera.logging import get_logger
from tessera.orm import Model, api, fields
from tessera.runtime import current_remote_ip

_hasher = PasswordHasher()
log = get_logger(__name__)
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
    share = fields.Boolean(
        "External user",
        compute="_compute_share",
        store=True,
        help="Portal/public users (not in Internal user, directly or via implied groups) have no back-office access",
    )
    totp_secret = fields.Char(groups="core.group_system", copy=False)
    totp_enabled = fields.Boolean(default=False)
    last_login = fields.Datetime(readonly=True, copy=False)
    failed_login_count = fields.Integer(default=0, readonly=True, copy=False, groups="core.group_system")
    locked_until = fields.Datetime(readonly=True, copy=False, groups="core.group_system")
    totp_recovery_hashes = fields.Json(copy=False, groups="core.group_system", help="SHA-256 of unused recovery codes")
    signature = fields.Html()
    notification_preference = fields.Selection([("inbox", "In-app"), ("email", "Email")], default="inbox")

    @api.depends("group_ids", "group_ids.implied_ids")
    def _compute_share(self) -> None:
        internal = self.env.ref("core.group_user", raise_if_not_found=False)
        internal_id = internal.ids[0] if internal else None
        for user in self:
            groups = user.elevated().group_ids.transitive_implied()
            user.share = internal_id is None or internal_id not in groups.ids

    # -- validation ----------------------------------------------------------------------
    @api.constrains("login")
    def _check_login(self) -> None:
        for user in self:
            if not user.login or user.login != user.login.strip().lower():
                raise ValidationError(_("Login must be lowercase without surrounding spaces"), field="login")

    @api.constrains("company_id", "company_ids")
    def _check_companies(self) -> None:
        for user in self:
            if user.company_id and user.company_ids and user.company_id not in user.company_ids:
                raise ValidationError(_("The current company must be one of the allowed companies"), field="company_id")

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
            raise ValidationError(
                _(
                    "Password must be at least {count} characters",
                    count=MIN_PASSWORD_LENGTH,
                ),
                field="password",
            )
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
        """Return the user record for valid credentials; raise ``AccessDenied`` otherwise.

        After ``login_lockout_attempts`` consecutive failures the account is locked for
        ``login_lockout_minutes``. Failures are recorded in their own transaction so they
        survive the rollback of the failed request.
        """
        users = self.elevated("authenticate")
        user = users.search([("login", "=", (login or "").strip().lower()), ("active", "=", True)], limit=1)
        now = self.env.now()
        if user and user.locked_until and user.locked_until > now:
            log.warning("login refused: account locked", user_id=user.ids[0], ip=current_remote_ip())
            raise AccessDenied(_("Too many failed attempts; this account is temporarily locked"), locked=True)
        if not user:
            _equalize_timing(password)  # an unknown login costs as much as a wrong password
            log.warning("login failed: unknown account", ip=current_remote_ip())
            raise AccessDenied(_("Invalid login or password"))
        if not user.check_password(password):
            user.record_failed_login()
            log.warning("login failed: wrong password", user_id=user.ids[0], ip=current_remote_ip())
            raise AccessDenied(_("Invalid login or password"))
        vals: dict[str, Any] = {"last_login": now}
        # with a second factor the failure counter is cleared once the code is accepted (``login_succeeded``),
        # so wrong codes keep counting towards the lockout however often the password is entered again
        if (user.failed_login_count or user.locked_until) and not user.totp_enabled:
            vals.update({"failed_login_count": 0, "locked_until": None})
        user.with_context(_recompute_write=True)._write_columns(vals)
        return self.browse(user.ids)

    def record_failed_login(self) -> None:
        """Count a failed password or second-factor attempt (locks the account after too many)."""
        settings = get_settings()
        for user in self:
            log.info("failed authentication attempt counted", user_id=user.ids[0], ip=current_remote_ip())
            _record_failed_login(self.env, user.ids[0], settings.login_lockout_attempts, settings.login_lockout_minutes)

    def is_locked(self) -> bool:
        self.ensure_one()
        locked_until = self.elevated("lockout").locked_until
        return bool(locked_until and locked_until > self.env.now())

    def login_succeeded(self) -> None:
        """Both factors accepted: clear the failure counter."""
        me = self.elevated("lockout")
        if any(u.failed_login_count or u.locked_until for u in me):
            self.unlock()

    def unlock(self) -> None:
        self.elevated("unlock").with_context(_recompute_write=True)._write_columns(
            {"failed_login_count": 0, "locked_until": None}
        )

    # -- TOTP recovery codes ----------------------------------------------------------------
    def generate_recovery_codes(self, count: int = 10) -> list[str]:
        """Replace the user's recovery codes; the plain codes are returned exactly once."""
        self.ensure_one()
        codes = ["-".join(secrets.token_hex(3) for _ in range(2)) for _ in range(count)]
        hashes = [hashlib.sha256(c.encode()).hexdigest() for c in codes]
        self.elevated("recovery codes").with_context(_recompute_write=True)._write_columns(
            {"totp_recovery_hashes": hashes}
        )
        return codes

    def consume_recovery_code(self, code: str) -> bool:
        self.ensure_one()
        me = self.elevated("recovery codes")
        digest = hashlib.sha256((code or "").strip().lower().encode()).hexdigest()
        remaining = list(me.totp_recovery_hashes or [])
        if digest not in remaining:
            return False
        remaining.remove(digest)
        me.with_context(_recompute_write=True)._write_columns({"totp_recovery_hashes": remaining})
        return True

    # -- groups ----------------------------------------------------------------------------
    def has_group(self, external_id: str) -> bool:
        self.ensure_one()
        return self.env.registry.security.has_groups(self.env.with_user(self.ids[0]), [external_id])

    def all_group_ids(self) -> list[int]:
        self.ensure_one()
        return list(self.elevated().group_ids.transitive_implied().ids)


_DUMMY_HASH: str | None = None


def _equalize_timing(password: str) -> None:
    """Run one Argon2 verification against a throw-away hash."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))
    try:
        _hasher.verify(_DUMMY_HASH, password or "")
    except Exception:
        return


def _record_failed_login(env: Any, user_id: int, max_attempts: int, lock_minutes: int) -> None:
    """Increment the failure counter in an independent transaction (and lock if needed).

    ``SKIP LOCKED``: if the request's own transaction already holds the row, the attempt is not
    counted rather than deadlocking against ourselves."""
    from tessera.db.engine import Cursor

    cr = Cursor(env.cr.engine)
    try:
        cr.execute(
            """UPDATE core_user SET failed_login_count = COALESCE(failed_login_count, 0) + 1,
                   locked_until = CASE WHEN COALESCE(failed_login_count, 0) + 1 >= :max
                                       THEN now() + make_interval(mins => :mins) ELSE locked_until END
               WHERE id = (SELECT id FROM core_user WHERE id = :id FOR UPDATE SKIP LOCKED)""",
            {"max": max_attempts, "mins": lock_minutes, "id": user_id},
        )
        cr.commit()
    finally:
        cr.close()
