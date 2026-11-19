# Workflows and automations

The `workflow` module adds two things to any model, configured as data (Settings → Automation):

* **Workflows** — a state machine over one field, shown as a status bar with buttons.
* **Automations** — "when X happens, if Y, do Z", including time-based triggers.

## Workflows

```
workflow.definition (model, state_field, strict)
 ├─ workflow.state       key, name, sequence, fold, is_final
 └─ workflow.transition  name, from_state_ids (empty = any), to_state_id,
                         guard_domain, guard_message, group_ids, confirm, highlight
```

* The state field may be a selection (keys), a char, or a many2one (state key = record id).
* One active workflow per model.
* A transition is **available** when the record is in one of its `from` states, the user is in one of its
  groups (if any), and the record matches its `guard_domain`.
* `GET /api/v1/workflow/{model}/{id}/statusbar` returns the states and only the available transitions.
* `POST /api/v1/workflow/{model}/{id}/transitions/{transition_id}` takes one: **409** when it is not
  available (wrong state or failing guard, with `guard_message`), **403** when the user lacks the group.
* `strict = true` (default) refuses direct writes to the state field with 409 — state changes must go
  through transitions. Data files and platform code (system user) are exempt.

Example (as JSON data in a module):

```json
[
  {"id": "wf_deal", "model": "workflow.definition",
   "values": {"name": "Deal pipeline", "model_name": "sales.deal", "state_field": "state"}},
  {"id": "st_new", "model": "workflow.state", "values": {"definition_id": "mymod.wf_deal", "key": "new", "name": "New", "sequence": 1}},
  {"id": "st_won", "model": "workflow.state", "values": {"definition_id": "mymod.wf_deal", "key": "won", "name": "Won", "sequence": 9, "is_final": true}},
  {"id": "tr_win", "model": "workflow.transition",
   "values": {"definition_id": "mymod.wf_deal", "name": "Mark won", "to_state_id": "mymod.st_won",
              "from_state_ids": ["mymod.st_new"], "guard_domain": [["amount", ">", 0]],
              "guard_message": "Set an amount first", "highlight": true}}
]
```

## Automations

| Trigger | Fires | Extra settings |
|---|---|---|
| `on_create` | after records are created | — |
| `on_write` | after a write | `watched_fields`, `pre_filter_domain` |
| `on_create_or_write` | both | as above |
| `on_delete` | before deletion | — |
| `on_time` | when *date field + offset_days* is reached | `date_field`, `offset_days` (negative = before) |
| `on_transition` | after a workflow transition | `transition_id` (empty = any) |
| `on_webhook` | `POST /api/v1/workflow/hooks/{token}` with `{"record_id": …}` | the URL token is the secret |

Filters: `filter_domain` (record must match *after* the change), `pre_filter_domain` (must have matched
*before* — "crossed a threshold"), and `condition`, an expression.

Actions (`actions`, a JSON list, run in order inside one savepoint):

| Type | Keys | Effect |
|---|---|---|
| `update` | `values: {field: expr}` | write the record |
| `post_message` | `body: expr`, `internal` | log a note / post a message |
| `schedule_activity` | `activity_type`, `summary: expr`, `days`, `user: expr` | plan an activity |
| `notify` | `title: expr`, `body: expr`, `users: expr` | in-app notification (default: followers) |
| `create_record` | `model`, `values` | create another record |
| `call_webhook` | `url`, `fields` | POST the record as JSON (a job, retried) |
| `call_method` | `method` | call an `@api.expose` method of the record |

Execution rules:

* Automations run elevated (they are configured by administrators) but keep the triggering user as uid.
* An automation never re-triggers itself, and chains stop at depth 3.
* `on_error = log` (default) records the failure in the run log and lets the operation succeed;
  `raise` blocks the operation.
* **Time triggers fire once.** Each run claims a unique key *automation:record:moment*; overlapping or
  repeated checks cannot fire twice. Only moments after the automation was activated are considered.
  Changing the record's date creates a new moment.

## Expressions

`tessera.expr` evaluates Python-syntax *expressions* by walking a whitelisted AST:

```
record.amount * 1.2 if record.stage_id.name == 'Won' else 0
'Follow up with ' + record.partner_id.name
today + days(3)
contains(record.email, '@example.com') and len(record.tag_ids) > 2
```

Available names: `record`, `old` (values before the write, for watched fields), `user`, `uid`, `now`,
`today`, plus `payload` (webhooks) and `transition` / `old_state` (transitions). Functions: `len str int
float bool round abs min max sum upper lower strip contains matches days hours date coalesce ids`.

There are no statements, comprehensions, lambdas, method calls, or attribute access other than record
fields and date parts; names starting with `_` are rejected at parse time; evaluation has a step and
size budget. Invalid expressions are rejected when the automation is saved.
