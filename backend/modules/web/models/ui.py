"""Menus, actions, views and saved filters."""

from __future__ import annotations

from tessera.exceptions import ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields
from tessera.orm.domain import parse

VIEW_TYPES = [
    ("form", "Form"),
    ("list", "List"),
    ("kanban", "Kanban"),
    ("calendar", "Calendar"),
    ("pivot", "Pivot"),
    ("graph", "Graph"),
    ("gantt", "Gantt"),
    ("map", "Map"),
    ("activity", "Activity"),
    ("search", "Search"),
]
ACTION_KINDS = [("window", "Window"), ("server", "Server"), ("url", "URL"), ("client", "Client"), ("report", "Report")]


class Action(Model):
    _name = "web.action"
    _description = "Action"
    _order = "sequence, name"
    _studio = False

    name = fields.Char(required=True, translate=True)
    kind = fields.Selection(ACTION_KINDS, required=True, default="window")
    model_name = fields.Char("Model", index=True)
    view_types = fields.Json(default=lambda self: ["list", "form"], help="Ordered view types for window actions")
    view_refs = fields.Json(default=dict, help='{"form": "module.view_xmlid"} to pin specific views')
    search_view_ref = fields.Char()
    domain = fields.Json()
    context = fields.Json(default=dict)
    target = fields.Selection(
        [("current", "Current"), ("new", "Dialog"), ("fullscreen", "Fullscreen")], default="current"
    )
    url = fields.Char(help="URL actions")
    client_tag = fields.Char(help="Client actions: registered client component tag")
    server_method = fields.Char(help="Server actions: exposed method on model_name")
    report_ref = fields.Char(help="Report actions: report template external id")
    limit = fields.Integer(default=80)
    group_ids = fields.Many2many(
        "core.group", relation="web_action__group_ids", column1="web_action_id", column2="core_group_id"
    )
    help = fields.Html(translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    @api.constrains("kind", "model_name", "url", "client_tag", "server_method")
    def _check_kind(self) -> None:
        for action in self:
            if action.kind in ("window", "server", "report") and not action.model_name:
                raise ValidationError(_("Action '{name}' needs a model", name=action.name), field="model_name")
            if action.kind == "url" and not action.url:
                raise ValidationError(_("URL action '{name}' needs a url", name=action.name), field="url")
            if action.kind == "client" and not action.client_tag:
                raise ValidationError(
                    _("Client action '{name}' needs a client_tag", name=action.name), field="client_tag"
                )
            if action.kind == "server" and not action.server_method:
                raise ValidationError(
                    _("Server action '{name}' needs a method", name=action.name), field="server_method"
                )

    @api.constrains("domain")
    def _check_domain(self) -> None:
        for action in self:
            if action.domain:
                parse(action.domain)

    def visible_for(self, env_user: Model) -> bool:
        self.ensure_one()
        if not self.group_ids:
            return True
        return self.env.registry.security.has_groups(
            env_user.env, [x for x in self.group_ids.get_external_id().values() if x]
        )


class Menu(Model):
    _name = "web.menu"
    _description = "Menu"
    _order = "sequence, id"
    _parent_name = "parent_id"
    _studio = False

    name = fields.Char(required=True, translate=True)
    parent_id = fields.Many2one("web.menu", ondelete="cascade", index=True)
    child_ids = fields.One2many("web.menu", "parent_id")
    sequence = fields.Integer(default=10)
    action_id = fields.Many2one("web.action", ondelete="set_null")
    group_ids = fields.Many2many(
        "core.group", relation="web_menu__group_ids", column1="web_menu_id", column2="core_group_id"
    )
    icon = fields.Char(help="Icon name for root menus (lucide icon id)")
    color = fields.Char()
    complete_name = fields.Char(compute="_compute_complete_name")
    active = fields.Boolean(default=True)

    @api.depends("name", "parent_id.complete_name")
    def _compute_complete_name(self) -> None:
        for menu in self:
            menu.complete_name = f"{menu.parent_id.complete_name} / {menu.name}" if menu.parent_id else menu.name

    @api.constrains("parent_id")
    def _check_cycle(self) -> None:
        for menu in self:
            seen: set[int] = set()
            current = menu
            while current:
                if current.ids[0] in seen:
                    raise ValidationError(_("Menu hierarchy cannot contain cycles"))
                seen.add(current.ids[0])
                current = current.parent_id


class View(Model):
    """A view definition (JSON) or an extension of another view (``inherit_id`` + ``operations``)."""

    _name = "web.view"
    _description = "View"
    _order = "priority, id"
    _studio = False

    name = fields.Char(required=True)
    model_name = fields.Char("Model", required=True, index=True)
    type = fields.Selection(VIEW_TYPES, required=True, index=True)
    priority = fields.Integer(default=16, help="Lower wins when several base views exist")
    definition = fields.Json(help="View JSON (base views)")
    inherit_id = fields.Many2one("web.view", ondelete="cascade", index=True, help="Set on extension views")
    operations = fields.Json(help="Inheritance operations applied to the parent view (extension views)")
    sequence = fields.Integer(default=10, help="Order among extensions of the same parent")
    module = fields.Char(index=True)
    studio = fields.Boolean(default=False, help="Created or modified in Studio")
    version = fields.Integer(default=1)
    active = fields.Boolean(default=True)

    @api.constrains("definition", "inherit_id", "operations", "type", "model_name")
    def _check_shape(self) -> None:
        from modules.web.views import validate_definition, validate_operations

        for view in self:
            if view.inherit_id:
                if view.inherit_id.model_name != view.model_name or view.inherit_id.type != view.type:
                    raise ValidationError(_("An extension view must have the same model and type as its parent"))
                validate_operations(view.operations or [])
            else:
                if not view.definition:
                    raise ValidationError(
                        _("Base view '{name}' needs a definition", name=view.name), field="definition"
                    )
                validate_definition(view.type, view.definition)
            if view.model_name not in self.env.registry.models:
                raise ValidationError(_("Unknown model '{model_name}'", model_name=view.model_name), field="model_name")


class Filter(Model):
    """Saved search filters and favourites (``user_id`` empty = shared with everyone)."""

    _name = "web.filter"
    _description = "Saved filter"
    _order = "name"

    name = fields.Char(required=True)
    model_name = fields.Char("Model", required=True, index=True)
    user_id = fields.Many2one("core.user", ondelete="cascade", index=True, help="Empty = shared")
    action_id = fields.Many2one("web.action", ondelete="cascade")
    domain = fields.Json(default=list)
    context = fields.Json(default=dict)
    group_bys = fields.Json(default=list)
    sort = fields.Char()
    is_default = fields.Boolean(default=False)
    is_favorite = fields.Boolean(default=True)

    @api.constrains("domain")
    def _check_domain(self) -> None:
        for flt in self:
            parse(flt.domain)
