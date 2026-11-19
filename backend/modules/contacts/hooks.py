"""Install hook: give every existing user a contact."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tessera.orm.environment import Environment


def post_init(env: Environment) -> None:
    sys_env = env.elevated("contacts install")
    users = sys_env["core.user"].with_context(active_test=False).search([("partner_id", "=", None)])
    partners = sys_env["contacts.partner"].with_context(tracking_disable=True)
    for user in users:
        partner = partners.create({"name": user.name or user.login, "email": user.email})
        user.write({"partner_id": partner.ids[0]})
