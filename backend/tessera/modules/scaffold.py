"""``tessera module scaffold``: create a module skeleton."""

from __future__ import annotations

import json
from pathlib import Path

from tessera.config import DEFAULT_MODULE_PATH
from tessera.exceptions import ModuleError
from tessera.modules.manifest import NAME_RE


def scaffold_module(
    name: str, *, path: Path | None = None, summary: str = "", depends: list[str] | None = None
) -> Path:
    if not NAME_RE.match(name):
        raise ModuleError(f"Invalid module name '{name}' (lowercase snake_case)")
    base = Path(path) if path else DEFAULT_MODULE_PATH
    target = base / name
    if target.exists():
        raise ModuleError(f"{target} already exists")
    depends = depends or ["core", "web", "chatter"]
    title = name.replace("_", " ").title()
    (target / "models").mkdir(parents=True)
    (target / "views").mkdir()
    (target / "security").mkdir()
    (target / "data").mkdir()
    (target / "ai" / "prompts").mkdir(parents=True)
    (target / "ai" / "evals").mkdir()
    (target / "tests").mkdir()
    (target / "i18n").mkdir()

    manifest_lines = [
        f'name = "{name}"',
        'version = "0.1.0"',
        f'summary = "{summary or title}"',
        'category = "Uncategorized"',
        "phase = 0",
        f"depends = {json.dumps(depends)}",
        'license = "Apache-2.0"',
        "installable = true",
        "",
        "[load]",
        'security = ["security/groups.json", "security/access.json"]',
        "data = []",
        f'views = ["views/{name}.json"]',
        'menus = "menus.json"',
        'actions = "actions.json"',
        'ai = "ai/features.yaml"',
    ]
    (target / "manifest.toml").write_text(
        "\n".join(manifest_lines) + "\n",
        encoding="utf-8",
    )
    (target / "__init__.py").write_text(f'"""{title} module."""\n', encoding="utf-8")
    (target / "models" / "__init__.py").write_text("", encoding="utf-8")
    model_name = f"{name}.item"
    (target / "models" / "item.py").write_text(
        f'''"""Models of the {name} module."""

from tessera.orm import Model, fields


class Item(Model):
    _name = "{model_name}"
    _description = "{title} item"
    _inherit = ["chatter.thread", "core.multi_company"]
    _order = "name"

    name = fields.Char(required=True, index=True, tracking=True)
    active = fields.Boolean(default=True)
    description = fields.Text()
''',
        encoding="utf-8",
    )
    (target / "security" / "groups.json").write_text(
        json.dumps(
            [
                {
                    "id": "group_user",
                    "model": "core.group",
                    "values": {
                        "name": f"{title} / User",
                        "category": title,
                        "implied_ids": ["core.group_user"],
                    },
                },
                {
                    "id": "group_manager",
                    "model": "core.group",
                    "values": {
                        "name": f"{title} / Manager",
                        "category": title,
                        "implied_ids": [f"{name}.group_user"],
                    },
                },
            ],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (target / "security" / "access.json").write_text(
        json.dumps(
            [
                {
                    "id": "access_item_user",
                    "model": "core.model_access",
                    "values": {
                        "name": f"{model_name} user",
                        "model_name": model_name,
                        "group_id": f"{name}.group_user",
                        "perm_read": True,
                        "perm_write": True,
                        "perm_create": True,
                        "perm_unlink": False,
                    },
                },
                {
                    "id": "access_item_manager",
                    "model": "core.model_access",
                    "values": {
                        "name": f"{model_name} manager",
                        "model_name": model_name,
                        "group_id": f"{name}.group_manager",
                        "perm_read": True,
                        "perm_write": True,
                        "perm_create": True,
                        "perm_unlink": True,
                    },
                },
            ],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (target / "views" / f"{name}.json").write_text(
        json.dumps(
            [
                {
                    "id": "view_item_form",
                    "model": model_name,
                    "type": "form",
                    "name": f"{model_name}.form",
                    "root": {
                        "kind": "sheet",
                        "children": [
                            {
                                "kind": "group",
                                "children": [
                                    {"kind": "field", "name": "name"},
                                    {"kind": "field", "name": "description"},
                                ],
                            }
                        ],
                    },
                    "chatter": True,
                },
                {
                    "id": "view_item_list",
                    "model": model_name,
                    "type": "list",
                    "name": f"{model_name}.list",
                    "columns": [{"field": "name"}, {"field": "company_id"}],
                },
                {
                    "id": "view_item_search",
                    "model": model_name,
                    "type": "search",
                    "name": f"{model_name}.search",
                    "fields": ["name"],
                    "filters": [{"name": "archived", "label": "Archived", "domain": [["active", "=", False]]}],
                },
            ],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (target / "menus.json").write_text(
        json.dumps([{"id": "menu_root", "name": title, "sequence": 100, "action": f"{name}.action_items"}], indent=2)
        + "\n",
        encoding="utf-8",
    )
    (target / "actions.json").write_text(
        json.dumps(
            [
                {
                    "id": "action_items",
                    "kind": "window",
                    "name": f"{title} items",
                    "model": model_name,
                    "views": ["list", "form"],
                }
            ],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (target / "ai" / "features.yaml").write_text("features: []\n", encoding="utf-8")
    (target / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (target / "tests" / f"test_{name}.py").write_text(
        f'''import pytest

pytestmark = pytest.mark.db


def test_create_item(env):
    item = env["{model_name}"].create({{"name": "First"}})
    assert item.name == "First"
''',
        encoding="utf-8",
    )
    return target
