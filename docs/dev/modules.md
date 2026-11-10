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
├── i18n/                  # <module>.pot (generated) and <lang>.po catalogues — see Translations
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

## Translations (i18n)

Everything the server shows to users is written in English in the source and translated through
per-module gettext catalogues: `i18n/<module>.pot` (the template, generated) and `i18n/<lang>.po`
(one per language, e.g. `i18n/fr_FR.po`). Catalogues are found by convention; no manifest entry is
needed. The web client's own strings live in `frontend/src/i18n/` and are not part of this.

**What is translated, and how**

| Text | Marked by | Translated when |
|---|---|---|
| Model `_description`, field labels (explicit `string` or the label derived from the name), `help`, static selection labels, `_sql_constraints` messages | nothing — extracted from the model definitions | `fields_get` / `/web/views` / `/metadata` answer in the user's language |
| View attributes `label`, `title`, `string`, `help`, `placeholder`, `confirm`, `text`, `tooltip` (top level of a definition: `label`, `string`, `help` only — a calendar's `title` names a field) | nothing | `/web/views` resolves the view for the user's language |
| Menu names, action names and help, and every `translate=True` value in `security/` and `data/` files (stages, groups, workflow states and transitions, …) | nothing | on install and upgrade the catalogues are written into the records' JSONB values (`{"en_US": …, "fr_FR": …}`) |
| Messages in Python: exceptions, notifications, ranking reasons… | `_("…")` | when rendered — see below |
| Report templates | `{{ _("Valid until: {date}", date=…) }}` | at rendering, in the document partner's language (else the user's) |

**Marking messages.** Import `_` and give it a literal English text with `{name}` placeholders:

```python
from tessera.i18n import _, translatable

raise ValidationError(_("'{value}' is not a valid choice for field '{field}'", value=value, field=translatable(label)))
env["core.notification"].notify([uid], _("Import finished: {filename}", filename=name))
```

`_()` returns a `Message`: a `str` rendered in the current language (the caller's inside an API
request, English in jobs and tests) that remembers its msgid, so the API error handlers render
exception messages in the caller's language and `core.notification` renders titles and bodies in
each recipient's language. Rules:

* the first argument must be a **literal** (the extractor only collects literals); never build it with
  an f-string or `+`, pass values as keyword parameters;
* parameters are data and are not translated, except other messages — wrap a label that comes from
  metadata (a field label, a model description) in `translatable(...)`;
* one message per plural form (`_("Due in {count} days", count=n) if n > 1 else _("Due tomorrow")`);
* compare messages by `msgid` (`getattr(reason, "msgid", reason)`), never by their rendered text;
* never translate log messages, prompts sent to models, technical identifiers or data.

**Extract, translate, check.**

```bash
cd backend
uv run tessera i18n extract crm              # writes i18n/crm.pot, merges new msgids into every i18n/*.po
uv run tessera i18n extract crm --lang de_DE # also creates i18n/de_DE.po
uv run tessera i18n stats --missing          # completeness per module (fr_FR by default)
```

Translate the empty `msgstr` lines of `i18n/<lang>.po` (any PO editor works). Keep every `{placeholder}`
exactly as in the msgid; entries flagged `#, fuzzy` are ignored. `extract` keeps existing translations
and drops msgids that disappeared. `tessera/i18n/tests/test_i18n.py` fails when a Phase 1 module's
`fr_FR.po` translates less than 95 % of its strings or a translation changes the placeholders — run
`extract` and translate the new lines when you add strings.

**Loading.** Installing or upgrading a module writes its catalogues into its translatable records;
`tessera i18n load [module …]` re-applies them to an existing database without an upgrade (see
[Administration → Translations](../admin/i18n.md)). Field labels, views and messages are read from
the catalogues at run time and need no loading. Lookups fall back from the user's language to another
catalogue of the same language (`fr_BE` → `fr_FR`) and then to the English source text.

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
