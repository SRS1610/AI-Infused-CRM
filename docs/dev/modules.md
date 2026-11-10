# Developer guide — writing a module

A module is a directory under `backend/modules/<name>/` with a `manifest.toml`. Scaffold one with:

```bash
cd backend && uv run tessera module scaffold my_module --depends core,web,chatter
```

## Layout

```
my_module/
├── manifest.toml          # name, version, depends, load lists
├── __init__.py
├── models/                # Python model definitions (every submodule is imported)
├── hooks.py               # optional: @hook(...) registry hooks, post_init(env)
├── security/              # groups.json, access.json, rules.json (JSON records)
├── data/                  # seed data (JSON records); data/demo/ for demo data
├── views/ menus.json actions.json   # loaded by the `web` module (Phase 1.2)
├── ai/                    # features.yaml, prompts/*.yaml, tools.py, evals/*.jsonl (Phase 1.5)
├── migrations/<version>/  # pre.py, post.py, down.py — each exposes migrate(env, from_version)
├── i18n/*.po
└── tests/
```

## Manifest

```toml
name = "crm"
version = "1.0.0"
summary = "Leads, opportunities, pipelines and sales teams"
category = "Sales"
phase = 1
depends = ["contacts", "chatter", "calendar", "product", "ai_core"]   # core is implied
auto_install = false
post_init = "hooks:post_init"          # modules.crm.hooks.post_init(env), runs once after install

[load]
security = ["security/groups.json", "security/access.json", "security/rules.json"]
data = ["data/stages.json"]
demo = ["data/demo/leads.json"]
```

Versions follow PEP 440. Bumping the version and adding `migrations/<new version>/pre.py` /
`post.py` runs those scripts on `tessera db migrate` (`down.py` is required when a migration
drops or renames anything).

## Models

```python
from tessera.orm import Model, api, fields

class Lead(Model):
    _name = "crm.lead"
    _description = "Lead / opportunity"
    _inherit = ["chatter.thread", "core.multi_company"]   # abstract mixins
    _order = "priority desc, id desc"
    _rec_name = "name"
    _audit = True                                            # write core.audit_log rows
    _soft_delete = True                                      # deleted_at / 30-day trash
    _sql_constraints = [("probability_range", "CHECK (probability BETWEEN 0 AND 100)", "Probability must be 0–100")]
    _indexes = [("team_stage", ["team_id", "stage_id"])]

    name = fields.Char(required=True, index="trigram", tracking=True)
    partner_id = fields.Many2one("contacts.partner", ondelete="set_null")
    partner_email = fields.Related("partner_id.email")
    stage_id = fields.Many2one("crm.stage", required=True, ondelete="restrict")
    tag_ids = fields.Many2many("crm.tag")
    activity_ids = fields.One2many("chatter.activity", "res_id", domain=[["model", "=", "crm.lead"]])
    expected_revenue = fields.Monetary(currency_field="currency_id")
    currency_id = fields.Many2one("core.currency", default=lambda self: self.env.company.currency_id.ids[0])
    probability = fields.Float(digits=(5, 2), default=0)
    description = fields.Html(translate=False)
    notes = fields.Text(translate=True)                      # JSONB per language
    priority = fields.Selection([("0", "Low"), ("1", "Normal"), ("2", "High")], default="1")
    weighted_revenue = fields.Monetary(compute="_compute_weighted", store=True)
    secret_margin = fields.Float(groups="crm.group_manager")  # field-level visibility

    @api.depends("expected_revenue", "probability")
    def _compute_weighted(self):
        for lead in self:
            lead.weighted_revenue = (lead.expected_revenue or 0) * (lead.probability or 0) / 100

    @api.constrains("probability")
    def _check_probability(self):
        for lead in self:
            if not 0 <= lead.probability <= 100:
                raise ValidationError("Probability must be between 0 and 100", field="probability")

    @api.expose                                              # callable from the API
    def action_set_won(self):
        self.write({"stage_id": self.env.ref("crm.stage_won").ids[0], "probability": 100})
        return True
```

### Extending another module's model

```python
class PartnerCrm(Model):
    _extend = "contacts.partner"

    opportunity_ids = fields.One2many("crm.lead", "partner_id")
    opportunity_count = fields.Integer(compute="_compute_opportunity_count", store=True)
    name = fields.Field(help="Shown on quotes and invoices")   # change attributes without knowing the type

    @api.depends("opportunity_ids")
    def _compute_opportunity_count(self):
        for partner in self:
            partner.opportunity_count = len(partner.opportunity_ids)

    def action_archive(self):              # override; super() chains across modules
        self.opportunity_ids.action_archive()
        return super().action_archive()
```

Only explicitly passed field arguments override the earlier definition; decorators
(`@api.depends`, `@api.constrains`, `@api.expose`) accumulate across overrides.

### Recordset API

| Call | Notes |
|---|---|
| `env["crm.lead"].search(domain, offset, limit, order)` | domain is nested JSON (`[["a","=",1]]`, `{"any": [...]}`); record rules and `active` filtering applied |
| `.search_count(domain)`, `.search_read(domain, fields)` | |
| `.read_group(domain, ["expected_revenue:sum"], ["stage_id", "create_date:month"])` | returns `__count` and `__domain` per group |
| `.create(vals or [vals])`, `.write(vals)`, `.unlink()`, `.restore()` | x2many values use command tuples `("set", ids)`, `("add", id)`, `("create", vals)` … |
| `.read(fields)`, `.fields_get()`, `.name_search(name)`, `.copy()` | |
| `rec.field`, `rec.field = value`, `rec["field"]` | descriptor access; assignment writes immediately |
| `.filtered(fn)`, `.mapped("partner_id.name")`, `.sorted("name")`, `a | b`, `a - b` | |
| `.with_context(lang="fr_FR")`, `.with_user(uid)`, `.with_company(company)`, `.elevated("reason")` | elevation is for kernel/platform code only and is logged |
| `env.ref("module.external_id")`, `env.company`, `env.companies`, `env.user`, `env.lang`, `env.tz` | |

### Hooks

```python
from tessera.orm.hooks import hook

@hook("after_write")
def notify(records, vals, old_values):
    ...
```

Hook names: `registry_built`, `before_create`, `after_create`, `before_write`, `after_write`,
`before_unlink`, `after_unlink`, `sync_metadata`, `module_installed`, `module_upgraded`,
`module_uninstalling`.

## Security data

```json
// security/groups.json
[{"id": "group_user", "model": "core.group", "values": {"name": "CRM / User", "category": "CRM", "implied_ids": ["core.group_user"]}}]

// security/access.json
[{"id": "access_lead_user", "model": "core.model_access",
  "values": {"name": "lead user", "model_name": "crm.lead", "group_id": "crm.group_user",
             "perm_read": true, "perm_write": true, "perm_create": true, "perm_unlink": false}}]

// security/rules.json
[{"id": "rule_lead_own_team", "model": "core.record_rule",
  "values": {"name": "Salespeople see their team's leads", "model_name": "crm.lead", "group_ids": ["crm.group_user"],
             "domain": {"any": [["user_id", "=", "$uid"], ["team_id.member_ids.user_id", "=", "$uid"]]}}}]
```

Rules: global rules (no groups) are AND-ed; group rules are OR-ed among themselves; models with a
`company_id` field get the implicit rule `company_id is_not_set OR company_id in $companies`.
Users without an access row for a model get nothing (deny by default).

## Data files

A JSON list of `{"id", "model", "values", "noupdate"}` entries. String values of many2one fields
and lists of strings for x2many fields are external ids (`"core.main_company"` or a bare name for
the current module). A dict keyed by language code on a translatable field writes every
translation. `noupdate` records are created on install and never overwritten on upgrade.

## Testing

```python
import pytest
pytestmark = pytest.mark.db

def test_lead_won(env, env_for):          # env = admin; env_for("login") = another user
    lead = env["crm.lead"].create({"name": "Big deal"})
    lead.action_set_won()
    assert lead.probability == 100
```

`conftest.py` builds the test database once per session (`core` + the kernel test modules + demo
data) and rolls every test back. Add your module to `KERNEL_TEST_MODULES` or install it in a
module-level fixture. Run `uv run pytest modules/crm` and keep coverage ≥ 80 %.
