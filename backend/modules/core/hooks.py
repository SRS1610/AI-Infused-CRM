"""Registry hooks of the core module: security provider, metadata sync, audit log, cleanup."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from modules.core.security import DatabaseSecurityProvider
from tessera.config import get_settings
from tessera.logging import get_logger
from tessera.orm import fields as F
from tessera.orm.hooks import hook
from tessera.runtime import current_remote_ip, current_request_id

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model
    from tessera.orm.registry import Registry

log = get_logger(__name__)

SECURITY_MODELS = {"core.user", "core.group", "core.model_access", "core.record_rule", "core.module_record"}


@hook("registry_built")
def install_security_provider(registry: Registry) -> None:
    registry.security = DatabaseSecurityProvider(registry)


def post_init(env: Environment) -> None:
    """Set the admin password and activate the default language."""
    settings = get_settings()
    admin = env.ref("core.user_admin")
    assert admin is not None
    password = settings.admin_password or ("admin1234" if not settings.is_production else None)
    if password is None:
        log.warning("TESSERA_ADMIN_PASSWORD not set: the admin user has no password and cannot log in")
    elif password == "admin1234":
        admin.set_password(password)
        log.warning("admin password is the development default 'admin1234' — change it before exposing this instance")
    else:
        admin.set_password(password)


# -- security cache --------------------------------------------------------------------------


USER_SECURITY_FIELDS = {"group_ids", "active", "share", "company_id", "company_ids"}


def _invalidate_if_security(records: Model, changed: set[str] | None = None) -> None:
    """Bump the security stamp when access can change. User writes count only for membership-related
    fields (not logins or preferences); external ids only for groups (cached xmlid → group id)."""
    name = records._name
    if name not in SECURITY_MODELS:
        return
    if name == "core.user" and changed is not None and not changed & USER_SECURITY_FIELDS:
        return
    if name == "core.module_record" and records._ids:
        models = {r.model for r in records.elevated()}
        if "core.group" not in models:
            return
    security = records.env.registry.security
    if hasattr(security, "bump"):
        security.bump(records.env)
    else:
        security.invalidate()


@hook("after_create")
def invalidate_security_on_create(records: Model, vals_list: list[dict[str, Any]]) -> None:
    _invalidate_if_security(records)


@hook("after_write")
def invalidate_security_on_write(records: Model, vals: dict[str, Any], old_values: dict[str, Any]) -> None:
    _invalidate_if_security(records, set(vals))


@hook("after_unlink")
def invalidate_security_on_unlink(model: Model, ids: list[int]) -> None:
    if model._name != "core.module_record":  # rows are gone: cannot tell; group xmlids are rarely deleted
        _invalidate_if_security(model)
    else:
        model.env.registry.security.invalidate()


# -- audit log ---------------------------------------------------------------------------------


def _serialize(value: Any) -> Any:
    if hasattr(value, "_ids"):
        return list(value._ids)
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _audit_fields(records: Model, names: list[str]) -> list[str]:
    return [
        n
        for n in names
        if (n in records._fields and not records._fields[n].compute)
        or (n in records._fields and records._fields[n].store)
    ]


@hook("before_write")
def capture_old_values(records: Model, vals: dict[str, Any], old_values: dict[int, dict[str, Any]]) -> None:
    if not records._audit or records._name == "core.audit_log":
        return
    names = _audit_fields(records, list(vals))
    if not names:
        return
    for rec in records.elevated("audit"):
        old_values[rec.ids[0]] = {n: _serialize(rec[n]) for n in names}


@hook("after_create")
def audit_create(records: Model, vals_list: list[dict[str, Any]]) -> None:
    if not records._audit or records._name == "core.audit_log":
        return
    logs = records.env["core.audit_log"].elevated("audit")
    for rec, vals in zip(records, vals_list, strict=False):
        logs.create(
            {
                "model": records._name,
                "res_id": rec.ids[0],
                "operation": "create",
                "user_id": records.env.uid,
                "company_id": records.env.company.ids[0] if records.env.company else None,
                "changes": {
                    k: {"old": None, "new": _serialize(v)}
                    for k, v in vals.items()
                    if k in records._fields and not isinstance(records._fields[k], F.Binary)
                },
                "request_id": current_request_id(),
                "ip": current_remote_ip(),
            }
        )


@hook("after_write")
def audit_write(records: Model, vals: dict[str, Any], old_values: dict[int, dict[str, Any]]) -> None:
    if not records._audit or records._name == "core.audit_log":
        return
    logs = records.env["core.audit_log"].elevated("audit")
    for rec in records.elevated("audit"):
        before = old_values.get(rec.ids[0], {})
        changes: dict[str, Any] = {}
        for name in before:
            new = _serialize(rec[name])
            if new != before[name]:
                changes[name] = {"old": before[name], "new": new}
        if not changes and before:
            continue
        logs.create(
            {
                "model": records._name,
                "res_id": rec.ids[0],
                "operation": "write",
                "user_id": records.env.uid,
                "company_id": records.env.company.ids[0] if records.env.company else None,
                "changes": changes or {k: {"new": _serialize(v)} for k, v in vals.items()},
                "request_id": current_request_id(),
                "ip": current_remote_ip(),
            }
        )


@hook("before_unlink")
def audit_unlink(records: Model) -> None:
    if not records._audit or records._name == "core.audit_log":
        return
    logs = records.env["core.audit_log"].elevated("audit")
    for rec in records.elevated("audit"):
        logs.create(
            {
                "model": records._name,
                "res_id": rec.ids[0],
                "operation": "unlink",
                "user_id": records.env.uid,
                "company_id": records.env.company.ids[0] if records.env.company else None,
                "changes": {"display_name": {"old": rec.display_name, "new": None}},
                "request_id": current_request_id(),
                "ip": current_remote_ip(),
            }
        )


# -- metadata sync ------------------------------------------------------------------------------


@hook("sync_metadata")
def sync_model_metadata(env: Environment, only: list[str] | None = None) -> None:
    """Refresh ``core.model`` / ``core.model_field`` from the registry (``only``: just these models, e.g.
    after Studio changed one of them; models are then never removed)."""
    registry = env.registry
    models = env["core.model"].elevated()
    fields_model = env["core.model_field"].elevated()
    existing_models = {m.name: m for m in models.with_context(active_test=False).search([])}
    seen_models: set[str] = set()
    classes = list(registry.models.values()) + list(registry.abstract.values())
    if only is not None:
        classes = [c for c in classes if c._name in only]
    for cls in classes:
        desc = (
            registry.describe_model(cls._name)
            if not cls._abstract
            else {
                "name": cls._name,
                "description": cls._description,
                "table": None,
                "module": cls._module,
                "fields": {},
            }
        )
        vals = {
            "name": cls._name,
            "description": cls._description or cls._name,
            "table": cls._table if not cls._abstract else None,
            "module": cls._module,
            "is_abstract": cls._abstract,
            "studio": cls._studio,
            "log_access": cls._log_access,
            "soft_delete": cls._soft_delete,
            "rec_name": cls._rec_name,
            "order": cls._order,
            "mixins": list(cls._mixin_names),
        }
        rec = existing_models.get(cls._name)
        if rec is None:
            rec = models.create(vals)
            existing_models[cls._name] = rec
        else:
            rec.write(vals)
        seen_models.add(cls._name)
        if cls._abstract:
            continue
        existing_fields = {
            f.name: f for f in fields_model.with_context(active_test=False).search([("model_name", "=", cls._name)])
        }
        seen_fields: set[str] = set()
        for fname, fdesc in desc["fields"].items():
            fvals = {
                "model_id": rec.ids[0],
                "model_name": cls._name,
                "name": fname,
                "type": fdesc["type"],
                "string": fdesc["string"],
                "help": fdesc.get("help") or None,
                "required": fdesc["required"],
                "readonly": fdesc["readonly"],
                "store": fdesc["store"],
                "compute": fdesc.get("compute"),
                "related": fdesc.get("related"),
                "depends": fdesc.get("depends", []),
                "translate": fdesc["translate"],
                "company_dependent": fdesc["company_dependent"],
                "tracking": fdesc["tracking"],
                "index": str(fdesc["index"]) if fdesc["index"] else None,
                "groups": fdesc.get("groups", []),
                "comodel": fdesc.get("comodel"),
                "selection": fdesc.get("selection"),
                "module": cls._fields[fname].module,
                "pii": fdesc.get("pii", False),
                "active": True,
            }
            frec = existing_fields.get(fname)
            if frec is None:
                fields_model.create(fvals)
            elif not frec.studio:
                frec.write(fvals)
            seen_fields.add(fname)
        for fname, frec in existing_fields.items():
            if fname not in seen_fields and not frec.studio and frec.active:
                frec.write({"active": False})
    for name, rec in existing_models.items():
        if name not in seen_models and only is None:
            rec.unlink()
