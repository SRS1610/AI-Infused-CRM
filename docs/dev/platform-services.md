# Platform services for module authors

## Discussion threads (`chatter`)

```python
class Deal(Model):
    _name = "sales.deal"
    _inherit = ["chatter.thread"]
    stage = fields.Selection([...], tracking=True)   # changes are logged with old → new values
    user_id = fields.Many2one("core.user", tracking=True)  # the responsible user follows automatically
```

* `record.message_post(body, internal=False, attachment_ids=[], mention_user_ids=[])`
* `record.message_subscribe(user_ids)`, `message_unsubscribe(user_ids)`
* `record.activity_schedule("chatter.activity_type_call", summary, date_deadline, user_id)`
* Override `message_new(msg, defaults)` / `message_update(msg)` to map inbound emails.
* `_track_create = False` skips the "created" log line; `with_context(tracking_disable=True)` skips tracking.
* Messages, followers and activities reference records generically. Access always derives from the
  record: the chatter API checks `record.check_access(...)` before reading the thread.
* Forms of thread models get the chatter panel automatically (generated views set `chatter: true`).

## Reports (`reports`)

Templates are Jinja2 in a sandbox: `{{ doc.field }}`, `{{ doc.amount | money(doc.currency_id) }}`,
`{{ qr('https://…') }}`, `{{ doc.date | date('%d/%m/%Y') }}`. `doc` exposes fields only, rendered as the
requesting user. Layouts (`reports.layout`) carry paper, margins, colours, logo and header/footer
templates per company. Publish versions with `template.publish_version(body=…)` or from editor blocks
(`[{"type": "heading", "text": "Quote"}, {"type": "table", "source": "line_ids", "columns": [...]}]`);
`rollback_to(n)` restores an older version. Render with `GET /api/v1/reports/{id}/render?ids=1,2` or in the
worker with `POST /api/v1/reports/{id}/render-async`.

## Import / export

Any model is importable from CSV/XLSX through `/import/:model` in the client. Column headers are matched
to fields by name or label; relations accept a display name, `field/id` (external id) or `field/.id`
(database id); an `id` column makes rows create-or-update by external id. A dry run executes the import
inside a rolled-back savepoint, so its error list is exactly what the real run will hit. Exports follow
relation paths (`partner_id/country_id/code`) and silently drop fields the user cannot read.

## Global search

Add `_global_search = ["name", "email", "partner_id"]` to a model: its records are indexed in
`web.search_index` (trigram) on every create/write/delete/restore and appear in the command palette.
Results are re-checked with the caller's rights before they are returned. Rebuild with
`POST /api/v1/web/search/rebuild` (administrators).
