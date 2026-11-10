from __future__ import annotations

from tessera.orm import Model, fields

MODULE_STATES = [
    ("uninstalled", "Not installed"),
    ("installing", "Installing"),
    ("installed", "Installed"),
    ("to_upgrade", "To upgrade"),
    ("to_remove", "To remove"),
]


class Module(Model):
    _name = "core.module"
    _description = "Module"
    _order = "phase, name"
    _studio = False
    _sql_constraints = [("name_uniq", "UNIQUE (name)", "Module names are unique")]

    name = fields.Char(required=True, index=True)
    version = fields.Char("Available version", required=True)
    installed_version = fields.Char()
    state = fields.Selection(MODULE_STATES, default="uninstalled", required=True, index=True)
    summary = fields.Char()
    description = fields.Text()
    category = fields.Char()
    phase = fields.Integer(default=0)
    depends = fields.Json(default=list)
    auto_install = fields.Boolean(default=False)
    application = fields.Boolean(default=False)
    installable = fields.Boolean(default=True)
    installed_at = fields.Datetime()


class ModuleRecord(Model):
    """External ids: ``module.name`` → (model, res_id)."""

    _name = "core.module_record"
    _description = "External identifier"
    _order = "module, name"
    _rec_name = "name"
    _studio = False
    _sql_constraints = [("module_name_uniq", "UNIQUE (module, name)", "External ids must be unique per module")]
    _indexes = [("model_res_id", ["model", "res_id"])]

    module = fields.Char(required=True, index=True)
    name = fields.Char(required=True)
    model = fields.Char(required=True)
    res_id = fields.Integer(required=True)
    noupdate = fields.Boolean(default=False)

    def complete_name(self) -> str:
        self.ensure_one()
        return f"{self.module}.{self.name}"


class ModuleMigrationLog(Model):
    _name = "core.module_migration_log"
    _description = "Module migration log"
    _order = "id desc"
    _rec_name = "module"
    _studio = False

    module = fields.Char(required=True, index=True)
    from_version = fields.Char()
    to_version = fields.Char()
    step = fields.Selection(
        [("pre", "Pre-migration"), ("post", "Post-migration"), ("schema", "Schema sync")], required=True
    )
    status = fields.Selection([("running", "Running"), ("done", "Done"), ("failed", "Failed")], default="running")
    started_at = fields.Datetime(default=lambda self: self.env.now())
    finished_at = fields.Datetime()
    error = fields.Text()
