# Tessera — Core Data Model (ERD)

**Status:** Draft for approval (pre–Phase 1)
**Scope:** the kernel/platform tables (`core`, `web`, `chatter`, `workflow`, `reports`, `studio`, `ai_core`) and the Phase 1 application tables (`contacts`, `crm`, `calendar`, `discuss`, `product`, `sale`, `portal`). Later phases (accounting, inventory, HR, Sign, …) get their own ERD at phase start; see §12.
**Companions:** [Architecture](ARCHITECTURE.md) · [Module dependency map](MODULE_DEPENDENCY_MAP.md) · [AI architecture note](AI_ARCHITECTURE.md) · [DECISIONS.md](../../DECISIONS.md)

---

## 1. Conventions

### 1.1 Naming
- Technical model name `module.model` (e.g. `crm.lead`); table name replaces the dot with an underscore (`crm_lead`) — D-006.
- Many-to-many join tables are named deterministically `<table>__<field>` (e.g. `core_user__company_ids`), with two FK columns and a composite primary key. Join tables are drawn in the diagrams only where the relation carries data of its own (`core_user_group`, `chatter_message_attachment`, `crm_team_member`, `discuss_channel_member`).
- Foreign-key columns end in `_id`; many-to-many fields end in `_ids`; the kernel exposes one-to-many fields (`child_ids`, `line_ids`) without any column of their own.

### 1.2 Common columns (omitted from every diagram)
Every model has:

| Column | Type | Notes |
|---|---|---|
| `id` | `bigint` identity PK | |
| `create_date`, `write_date` | `timestamptz` | set by the ORM |
| `create_uid`, `write_uid` | FK → `core_user` | set by the ORM |

Optional kernel conventions, present where stated in the column tables:

| Column | Meaning |
|---|---|
| `company_id` FK → `core_company` | multi-company model; the global record rule `company_id in $companies` applies |
| `active boolean default true` | archive (D-019) |
| `deleted_at timestamptz`, `deleted_by` FK → `core_user` | soft delete on `_soft_delete` models, 30-day trash (D-019) |
| `*_translated jsonb` | `translate=True` fields store `{"en_US": …, "fr_FR": …}` (D-009); the column keeps the field's name, the JSONB type is what marks it |
| `(model varchar, res_id bigint)` | polymorphic reference to any record, see §11.1 |

### 1.3 Types
`timestamptz` for all datetimes (UTC), `date` for calendar dates, `numeric(16,4)` for monetary amounts (rounded at display/posting with the currency's precision), `numeric` for floats where `digits=` is set, `text` for `Char`/`Text`/`Html` (the length limit is metadata, not a DB constraint), `jsonb` for `Json` and translatable fields, `vector(n)` from `pgvector` for embeddings. Binary data is never stored in a row: `core_attachment` holds an object-storage key.

### 1.4 Diagram notation
Mermaid `erDiagram`. `||--o{` = one-to-many, `}o--o{` = many-to-many, `||--||` = one-to-one, `o|` marks the optional side. Entity boxes list only the columns relevant to the diagram; the tables below each diagram list the non-obvious ones.

---

## 2. Identity and tenancy (`core`)

```mermaid
erDiagram
    core_company ||--o{ core_company : "parent_id"
    core_company }o--|| core_currency : "currency_id"
    core_company ||--o{ core_company_setting : "has"
    core_company ||--o{ core_user : "default company_id"
    core_user }o--o{ core_company : "company_ids (allowed)"
    core_user ||--|| contacts_partner : "partner_id"
    core_user ||--o{ core_user_group : "member of"
    core_group ||--o{ core_user_group : "members"
    core_group }o--o{ core_group : "implied_ids"
    core_user ||--o{ core_api_key : "owns"
    core_user ||--o{ core_session : "has"
    core_sso_provider ||--o{ core_user : "sso_provider_id (optional)"
    core_currency ||--o{ core_currency_rate : "rates"
    core_country ||--o{ core_country_state : "states"
    core_language ||--o{ core_user : "lang"

    core_company {
        text name
        bigint parent_id FK
        bigint currency_id FK
        bigint logo_attachment_id FK
        bigint report_layout_id FK
        text vat
        text email
        text phone
        text website
        bigint partner_id FK
        boolean active
        int sequence
    }
    core_user {
        text login UK
        text password_hash
        bigint partner_id FK
        bigint company_id FK
        text lang
        text tz
        text totp_secret_ref
        boolean totp_enabled
        bigint sso_provider_id FK
        text sso_subject
        text notification_mode
        timestamptz last_login_at
        boolean active
    }
    core_group {
        text name
        text category
        text technical_name UK
        boolean is_portal
        text comment
    }
    core_user_group {
        bigint user_id PK
        bigint group_id PK
        text granted_via
    }
    core_api_key {
        bigint user_id FK
        text name
        text key_prefix
        text key_hash UK
        jsonb scopes
        timestamptz expires_at
        timestamptz last_used_at
        inet last_used_ip
        boolean active
    }
    core_session {
        text token_hash UK
        bigint user_id FK
        bigint company_id FK
        jsonb company_ids
        timestamptz expires_at
        timestamptz last_seen_at
        inet ip
        text user_agent
        boolean mfa_passed
    }
    core_sso_provider {
        text name
        text protocol
        text issuer
        text client_id
        text client_secret_ref
        jsonb metadata
        boolean auto_provision
        bigint default_group_id FK
        boolean active
    }
    core_currency {
        text code UK
        text symbol
        int decimal_places
        numeric rounding
        text position
        boolean active
    }
    core_currency_rate {
        bigint currency_id FK
        bigint company_id FK
        date date
        numeric rate
        text source
    }
    core_country {
        text code UK
        jsonb name
        text phone_prefix
        text address_format
        text vat_label
    }
    core_country_state {
        bigint country_id FK
        text code
        jsonb name
    }
    core_language {
        text code UK
        text name
        text direction
        text date_format
        text time_format
        text decimal_point
        text thousands_sep
        boolean active
    }
    core_company_setting {
        bigint company_id FK
        text key
        text value_type
        jsonb value
    }
```

| Table.column | Notes |
|---|---|
| `core_company.parent_id` | company hierarchy; `child_of` domains use a recursive CTE. Consolidated reporting (Phase 2) walks this tree. |
| `core_company.partner_id` | the company's own contact record (address on documents, sender of emails). |
| `core_company.report_layout_id` | FK → `reports_layout`, per-company header/footer. |
| `core_user.partner_id` | one-to-one; every user is also a contact so chatter authorship, followers and portal access share one identity. Portal users have `is_portal` groups only. |
| `core_user.totp_secret_ref` | reference to an envelope-encrypted secret, never the secret itself. |
| `core_user.sso_subject` | stable subject identifier from the IdP; `UNIQUE (sso_provider_id, sso_subject)`. |
| `core_user_group.granted_via` | `direct` / `implied:<group_id>` — materialised closure of `implied_ids` so permission checks are a single index lookup. Recomputed when `implied_ids` changes. |
| `core_api_key.key_hash` | Argon2id hash of the secret; `key_prefix` (first 8 chars) is shown in the UI and indexed for lookup. `scopes` is a JSON array of `model:operation` grants intersected with the user's rights. |
| `core_session.company_ids` | the companies selected in the switcher for this session; drives `$companies` in record rules. |
| `core_currency_rate` | `UNIQUE (currency_id, company_id, date)`; `company_id NULL` = shared rate. `rate` is expressed as *units of this currency per one unit of the company currency*; conversion picks the latest row with `date <= :date`. |
| `core_company_setting` | `UNIQUE (company_id, key)`; typed via `value_type` (`bool`, `int`, `text`, `json`, `many2one:<model>`). |

---

## 3. Metadata registry (`core`, `web`)

```mermaid
erDiagram
    core_module ||--o{ core_module_dependency : "depends"
    core_module ||--o{ core_module_record : "owns"
    core_module ||--o{ core_module_migration_log : "history"
    core_module ||--o{ core_model : "defined/extended by"
    core_model ||--o{ core_model_field : "fields"
    core_model ||--o{ core_model_access : "ACL"
    core_model ||--o{ core_record_rule : "rules"
    core_model ||--o{ web_view : "views"
    web_view ||--o{ web_view : "inherit_id"
    web_view ||--o{ web_view_change : "ops"
    web_menu ||--o{ web_menu : "parent_id"
    web_menu }o--o| web_action : "action_id"
    web_action }o--o| core_model : "res_model"
    web_filter }o--|| core_model : "model_id"
    core_model ||--o{ web_search_index : "indexed records"

    core_module {
        text name UK
        text version_installed
        text version_available
        text state
        text summary
        text category
        int phase
        boolean auto_install
        jsonb manifest
        timestamptz installed_at
    }
    core_module_dependency {
        bigint module_id FK
        text depends_on
        boolean satisfied
    }
    core_module_record {
        bigint module_id FK
        text external_id UK
        text model
        bigint res_id
        boolean noupdate
    }
    core_module_migration_log {
        bigint module_id FK
        text from_version
        text to_version
        text step
        text status
        text error
        timestamptz started_at
        timestamptz finished_at
    }
    core_model {
        text name UK
        text table_name
        text description
        text order_by
        text rec_name
        boolean is_transient
        boolean multi_company
        boolean soft_delete
        boolean audit_enabled
        jsonb global_search_fields
        text origin_module
        boolean studio_created
    }
    core_model_field {
        bigint model_id FK
        text name
        text field_type
        text relation
        text relation_field
        text relation_table
        boolean required
        boolean readonly
        boolean store
        text compute
        jsonb depends
        text related
        boolean translate
        boolean company_dependent
        boolean tracking
        jsonb groups
        text index_kind
        jsonb selection
        text currency_field
        boolean pii
        text origin_module
        boolean studio_created
        jsonb ui_hints
    }
    core_model_access {
        bigint model_id FK
        bigint group_id FK
        boolean perm_read
        boolean perm_write
        boolean perm_create
        boolean perm_delete
    }
    core_record_rule {
        bigint model_id FK
        text name
        jsonb domain
        boolean is_global
        boolean perm_read
        boolean perm_write
        boolean perm_create
        boolean perm_delete
        boolean active
    }
    web_view {
        text name
        bigint model_id FK
        text view_type
        jsonb definition
        bigint inherit_id FK
        int priority
        text origin_module
        boolean active
    }
    web_view_change {
        bigint view_id FK
        int sequence
        jsonb operation
        text source
        bigint studio_change_id FK
    }
    web_menu {
        jsonb name
        bigint parent_id FK
        bigint action_id FK
        int sequence
        text icon
        jsonb group_ids
        text web_icon_path
        boolean active
    }
    web_action {
        text name
        text action_kind
        text res_model
        jsonb view_modes
        jsonb domain
        jsonb context
        bigint search_view_id FK
        text target
        jsonb payload
    }
    web_filter {
        text name
        bigint model_id FK
        bigint user_id FK
        bigint action_id FK
        jsonb domain
        jsonb context
        boolean is_default
        boolean is_shared
    }
    web_search_index {
        text model
        bigint res_id
        bigint company_id FK
        text summary
        tsvector summary_tsv
        timestamptz indexed_at
    }
```

| Table.column | Notes |
|---|---|
| `core_module.state` | `uninstalled / to_install / installed / to_upgrade / to_remove`; `manifest` caches the parsed `manifest.toml`. |
| `core_module_record.external_id` | `module.identifier` (e.g. `crm.stage_qualified`); the stable handle data files, tests and Studio use to reference seeded records. `UNIQUE (model, res_id)` as well, so each record has one owner. |
| `core_model` / `core_model_field` | materialised from the composed registry after every install/upgrade/Studio change — the client, API generator, Studio and AI tool schemas read these rather than importing Python. `UNIQUE (model_id, name)`. |
| `core_model_field.index_kind` | `none / btree / unique / trigram / expression`; `groups` lists group technical names that may see the field; `pii` enables PII access logging. |
| `core_model_access` | `UNIQUE (model_id, group_id)`; `group_id` FK → `core_group` (not drawn); `group_id NULL` = applies to every authenticated user. |
| `core_record_rule` | `group_ids` m2m → `core_group` (not drawn). `is_global` rules are ANDed; per-group rules are ORed, then ANDed with the globals. The `domain` is the JSON DSL (D-005) and may use `$uid`, `$companies`, `$user.<field>`. |
| `web_view.definition` | the JSON view (see Architecture §5.1); the *effective* view = base `definition` with all active `web_view_change` operations applied in `sequence`, cached in Redis by `(view_id, registry_version)`. |
| `web_view_change.source` | `module` (from a module's view file) or `studio` (links to `studio_change` for rollback). |
| `web_action.action_kind` | `window / server / report / url / client`; `payload` carries kind-specific data (server-action steps, report template id, URL, client tag). |
| `web_filter` | saved filters and favourites; `user_id NULL` + `is_shared` = shared with everyone who can see the action. |
| `web_search_index` | one row per record of models that opt in via `_global_search`; GIN index on `summary_tsv` plus trigram on `summary` for the command palette. Rebuilt by a job when `global_search_fields` changes. |

### 3.1 Sequences, translations, attachments

```mermaid
erDiagram
    core_company ||--o{ core_sequence : "per company"
    core_language ||--o{ core_translation : "lang"
    core_attachment }o--|| core_company : "company_id"
    core_attachment }o--o| core_user : "create_uid"

    core_sequence {
        text code
        bigint company_id FK
        text name
        text prefix
        text suffix
        int padding
        int number_next
        int step
        text implementation
        text reset_period
        text pg_sequence_name
        boolean active
    }
    core_translation {
        text lang FK
        text module
        text source_hash
        text source
        text value
        text state
    }
    core_attachment {
        text name
        text model
        bigint res_id
        text res_field
        bigint company_id FK
        text storage_key
        text mimetype
        bigint file_size
        text checksum_sha256
        text access_token
        boolean public
        jsonb image_variants
        text index_content
    }
```

| Table.column | Notes |
|---|---|
| `core_sequence` | `UNIQUE (code, company_id)`; `implementation = gapless` locks the row (`SELECT … FOR UPDATE`) and bumps `number_next`; `standard` delegates to the Postgres sequence named in `pg_sequence_name`. `reset_period` (`never / year / month`) is how `INV/2026/00001` restarts each fiscal year. |
| `core_translation` | UI strings only (field translations live in their JSONB column). `UNIQUE (lang, module, source_hash)`. |
| `core_attachment` | `storage_key` is the S3/MinIO object key (or local path in dev); `checksum_sha256` deduplicates uploads; `access_token` is a random token for unauthenticated download links (portal, emails) and is rotated on demand. `res_field` is set when the attachment backs a `Binary`/`Image` field. Composite index on `(model, res_id)`. |

---

## 4. Chatter, activities, notifications, mail (`chatter`, `core`)

```mermaid
erDiagram
    chatter_message ||--o{ chatter_message : "parent_id (thread)"
    chatter_message }o--o| chatter_subtype : "subtype_id"
    chatter_message }o--|| contacts_partner : "author_id"
    chatter_message ||--o{ chatter_message_attachment : "attachments"
    core_attachment ||--o{ chatter_message_attachment : ""
    chatter_message ||--o{ chatter_tracking_value : "tracking"
    chatter_message ||--o{ chatter_reaction : "reactions"
    chatter_follower }o--|| contacts_partner : "partner_id"
    chatter_follower }o--o{ chatter_subtype : "subtype_ids"
    chatter_activity }o--|| chatter_activity_type : "activity_type_id"
    chatter_activity }o--|| core_user : "user_id (assignee)"
    chatter_message ||--o{ core_notification : "notifications"
    core_notification }o--|| core_user : "user_id"
    core_user ||--o{ core_push_subscription : "devices"

    chatter_message {
        text model
        bigint res_id
        text message_type
        bigint subtype_id FK
        bigint author_id FK
        text email_from
        text subject
        text body
        bigint parent_id FK
        text message_id_header
        jsonb partner_ids
        boolean is_internal
        boolean ai_drafted
        bigint ai_run_id FK
        timestamptz date
    }
    chatter_subtype {
        text name
        text model
        boolean default_follow
        boolean internal
        text parent_field
    }
    chatter_message_attachment {
        bigint message_id PK
        bigint attachment_id PK
    }
    chatter_tracking_value {
        bigint message_id FK
        bigint field_id FK
        text field_type
        jsonb old_value
        jsonb new_value
        text old_display
        text new_display
    }
    chatter_reaction {
        bigint message_id FK
        bigint partner_id FK
        text emoji
    }
    chatter_follower {
        text model
        bigint res_id
        bigint partner_id FK
        jsonb subtype_ids
    }
    chatter_activity_type {
        jsonb name
        text category
        int delay_count
        text delay_unit
        text default_summary
        text icon
        bigint next_type_id FK
        text model
        boolean active
    }
    chatter_activity {
        text model
        bigint res_id
        bigint activity_type_id FK
        text summary
        text note
        date date_deadline
        bigint user_id FK
        text state
        bigint calendar_event_id FK
        numeric ai_rank
        jsonb ai_rank_factors
        timestamptz done_at
        text feedback
    }
    core_notification {
        bigint user_id FK
        bigint message_id FK
        text kind
        text title
        text body
        text link
        boolean is_read
        timestamptz read_at
        text email_status
        text push_status
    }
    core_push_subscription {
        bigint user_id FK
        text endpoint
        text p256dh
        text auth
        text user_agent
        timestamptz last_success_at
    }
```

| Table.column | Notes |
|---|---|
| `chatter_message.message_type` | `comment / note / notification / email / activity_done / system`; `is_internal` hides notes from portal and email recipients. Composite index `(model, res_id, date desc)` serves every chatter load. |
| `chatter_message.message_id_header` | RFC 5322 `Message-ID` for threading inbound replies; unique partial index where not null. |
| `chatter_message.ai_drafted`, `ai_run_id` | AI-drafted content keeps the "AI-drafted" label until a human edits or explicitly reviews it; `ai_run_id` FK → `ai_core_run` for audit. |
| `chatter_tracking_value` | written on `write` for `tracking=True` fields; `field_id` FK → `core_model_field`. Values are JSONB so any field type (incl. many2one as `{"id":…, "display_name":…}`) is captured. |
| `chatter_follower` | `UNIQUE (model, res_id, partner_id)`; `subtype_ids` controls which message subtypes notify this follower. |
| `chatter_activity.state` | computed `overdue / today / planned / done` relative to the assignee's timezone; stored and refreshed nightly so it can be grouped and indexed. `ai_rank` and `ai_rank_factors` back the AI daily to-do ranking (explainable). |
| `chatter_activity.calendar_event_id` | meeting activities link to `calendar_event`. |
| `core_notification` | in-app inbox; `email_status` / `push_status` track delivery per channel per user's `notification_mode`. |

### 4.1 Mail servers, aliases, queue

```mermaid
erDiagram
    core_company ||--o{ core_mail_server : "outgoing"
    core_company ||--o{ core_mail_alias : "inbound"
    core_mail_alias }o--|| core_model : "target model"
    core_mail_queue }o--o| core_mail_server : "mail_server_id"
    core_mail_queue }o--o| chatter_message : "message_id"
    core_mail_queue }o--|| core_job : "job_id"

    core_mail_server {
        text name
        bigint company_id FK
        text smtp_host
        int smtp_port
        text smtp_encryption
        text smtp_user
        text smtp_password_ref
        text from_filter
        int sequence
        boolean active
    }
    core_mail_alias {
        text alias_name UK
        text alias_domain
        bigint company_id FK
        text target_model
        bigint target_res_id
        jsonb defaults
        text contact_policy
        bigint owner_user_id FK
    }
    core_mail_queue {
        bigint message_id FK
        bigint mail_server_id FK
        text email_from
        jsonb email_to
        jsonb email_cc
        text subject
        text body_html
        jsonb headers
        text state
        int attempts
        timestamptz next_attempt_at
        timestamptz scheduled_at
        text provider_message_id
        text failure_reason
        bigint job_id FK
    }
```

`core_mail_alias.target_res_id` is `NULL` for "create a new record" aliases (e.g. `crm@` → new `crm.lead`) and set for per-thread reply aliases (`<thread-token>@`), which call `message_update` on the existing record. `contact_policy` (`everyone / followers / partners`) gates who may write in. `core_mail_queue.state` is `outgoing / sent / bounced / failed / cancelled`; the row is the durable side of the Celery mail task (Architecture §4.11).

---

## 5. Workflow, automation, jobs, audit, import, reports, Studio

```mermaid
erDiagram
    workflow_definition }o--|| core_model : "model_id"
    workflow_definition ||--o{ workflow_state : "states"
    workflow_definition ||--o{ workflow_transition : "transitions"
    workflow_transition }o--|| workflow_state : "from_state_id"
    workflow_transition }o--|| workflow_state : "to_state_id"
    workflow_transition }o--o{ core_group : "group_ids"
    workflow_automation }o--|| core_model : "model_id"
    workflow_automation ||--o{ workflow_automation_action : "actions"
    workflow_automation }o--o| workflow_transition : "trigger transition"
    core_scheduled_job ||--o{ core_job : "enqueues"
    core_webhook ||--o{ core_webhook_delivery : "deliveries"
    core_webhook }o--|| core_model : "model_id"

    workflow_definition {
        bigint model_id FK
        text state_field
        text name
        text origin_module
        boolean active
    }
    workflow_state {
        bigint definition_id FK
        text code
        jsonb name
        int sequence
        boolean is_initial
        boolean is_final
        text kind
    }
    workflow_transition {
        bigint definition_id FK
        text code
        jsonb label
        bigint from_state_id FK
        bigint to_state_id FK
        jsonb guard_domain
        text guard_method
        jsonb side_effect_methods
        boolean requires_confirmation
        text button_style
        int sequence
    }
    workflow_automation {
        text name
        bigint model_id FK
        text trigger
        jsonb watched_fields
        text time_field
        int time_offset
        text time_unit
        jsonb filter_pre
        jsonb filter_post
        bigint transition_id FK
        bigint company_id FK
        text origin
        boolean active
    }
    workflow_automation_action {
        bigint automation_id FK
        int sequence
        text action_kind
        jsonb config
    }
    core_job {
        uuid uuid UK
        text task_name
        text queue
        jsonb args_ref
        text state
        int attempts
        int max_attempts
        timestamptz next_retry_at
        text idempotency_key
        bigint user_id FK
        bigint company_id FK
        text db_name
        text error
        text result_ref
        int progress
        bigint scheduled_job_id FK
        timestamptz started_at
        timestamptz finished_at
    }
    core_scheduled_job {
        text name
        text task_name
        text cron_expression
        int interval_seconds
        timestamptz next_run_at
        timestamptz last_run_at
        text lock_token
        timestamptz lock_until
        bigint user_id FK
        bigint company_id FK
        jsonb args
        text origin
        boolean active
    }
    core_webhook {
        text name
        bigint model_id FK
        jsonb events
        jsonb filter_domain
        text url
        text secret_ref
        jsonb headers
        int max_attempts
        bigint company_id FK
        boolean active
    }
    core_webhook_delivery {
        bigint webhook_id FK
        text event
        text model
        bigint res_id
        jsonb payload
        int attempts
        int response_status
        text response_body
        text state
        timestamptz next_attempt_at
        timestamptz delivered_at
    }
```

| Table.column | Notes |
|---|---|
| `workflow_state.kind` | `normal / won / lost / cancelled / done` hints for kanban progress bars and reports; `is_final` blocks further transitions unless a transition explicitly leaves the state. |
| `workflow_transition.guard_domain` / `guard_method` | both must pass; `side_effect_methods` lists whitelisted model methods called after the state write. `requires_confirmation` shows a dialog in the client and is mandatory for transitions AI proposals may trigger. |
| `workflow_automation.trigger` | `on_create / on_write / on_delete / on_time / on_transition / on_webhook`; `time_field` + `time_offset` + `time_unit` for `on_time` (e.g. 3 days after `date_deadline`). `origin` is `module / studio / ai_proposal`. |
| `workflow_automation_action.action_kind` | `set_values / create_record / send_email / send_sms / add_followers / schedule_activity / call_webhook / server_action / ai_feature`; `config` is validated against a per-kind JSON Schema. `ai_feature` always ends in a proposal, never a direct write. |
| `core_job` | the ledger mirror of every Celery task (D-008). `idempotency_key` has a unique partial index so duplicate enqueues collapse. `db_name` supports database-per-tenant routing. |
| `core_scheduled_job.lock_token` / `lock_until` | DB-side fencing in addition to the Redis lock, so a scheduler restart cannot double-fire. |
| `core_webhook.secret_ref` | encrypted HMAC secret; deliveries are signed `X-Tessera-Signature: sha256=…`. `core_webhook_delivery` is purged after 30 days by a scheduled job. |

### 5.1 Audit, PII log, import, reports, Studio

```mermaid
erDiagram
    core_audit_log }o--|| core_user : "user_id"
    core_pii_access_log }o--|| core_user : "user_id"
    core_pii_access_log }o--|| core_model_field : "field_id"
    core_import_job }o--|| core_model : "model_id"
    core_import_job }o--o| core_import_mapping : "mapping_id"
    core_import_job }o--|| core_attachment : "file_id"
    core_import_job }o--o| core_job : "job_id"
    reports_template }o--|| core_model : "model_id"
    reports_template ||--o{ reports_template : "previous_version_id"
    reports_layout }o--|| core_company : "company_id"
    studio_change }o--|| core_user : "create_uid"
    studio_change ||--o{ studio_change : "reverts_id"

    core_audit_log {
        bigint user_id FK
        text model
        bigint res_id
        text operation
        jsonb changes
        text request_id
        inet ip
        text origin
        bigint ai_run_id FK
        bigint company_id FK
        timestamptz timestamp
    }
    core_pii_access_log {
        bigint user_id FK
        text model
        bigint field_id FK
        int record_count
        text request_id
        text purpose
        timestamptz timestamp
    }
    core_import_job {
        bigint model_id FK
        bigint file_id FK
        text file_format
        bigint mapping_id FK
        jsonb options
        text state
        boolean dry_run
        int row_count
        int ok_count
        int error_count
        jsonb errors
        bigint job_id FK
        bigint company_id FK
    }
    core_import_mapping {
        text name
        bigint model_id FK
        jsonb column_map
        jsonb options
        bigint user_id FK
        boolean is_shared
    }
    reports_template {
        text name
        text technical_name
        bigint model_id FK
        text html
        text css
        jsonb blocks
        text paper_format
        text version
        bigint previous_version_id FK
        text origin_module
        boolean active
    }
    reports_layout {
        bigint company_id FK
        text header_html
        text footer_html
        bigint logo_attachment_id FK
        text primary_color
        text secondary_color
        text font
        text paper_format
        jsonb margins
    }
    studio_change {
        text change_kind
        text target_model
        text target_ref
        jsonb before
        jsonb after
        text status
        bigint reverts_id FK
        text batch_id
        bigint company_id FK
    }
```

| Table.column | Notes |
|---|---|
| `core_audit_log.changes` | `{"field": {"old": …, "new": …}}` for `write`; full values for `create`/`unlink`. `origin` is `ui / api / import / job / ai_tool / automation`; `ai_run_id` links AI-accepted proposals to the run that produced them. Partitioned by month from Phase 2 when volumes justify it. |
| `core_pii_access_log` | one row per `(request, model, field)` with a `record_count`, not per row — Architecture §4.5. |
| `core_import_job.errors` | `[{"row": 12, "column": "email", "message": …}]` from the dry run; a dry run writes nothing but keeps this report. |
| `reports_template.blocks` | the visual editor's block tree; `html`/`css` is what the engine renders, regenerated from `blocks` when the editor is used, hand-edited otherwise. Editing creates a new version row and links `previous_version_id`. |
| `studio_change` | every Studio action (field added, view op, automation, menu) stores `before`/`after` metadata; rollback inserts a new row with `reverts_id` and replays `before`. `batch_id` groups the changes of one Studio session. |

---

## 6. Contacts

```mermaid
erDiagram
    contacts_partner ||--o{ contacts_partner : "parent_id (company / addresses)"
    contacts_partner }o--o| core_user : "user_id (salesperson)"
    contacts_partner }o--o| contacts_industry : "industry_id"
    contacts_partner }o--o| contacts_title : "title_id"
    contacts_partner }o--o{ contacts_tag : "tag_ids"
    contacts_partner }o--o| core_country : "country_id"
    contacts_partner }o--o| core_country_state : "state_id"
    contacts_partner ||--o{ contacts_bank_account : "bank_ids"
    contacts_partner ||--o{ contacts_merge_log : "kept partner"
    contacts_partner ||--o{ core_user : "users (portal / internal)"

    contacts_partner {
        text name
        text display_name
        boolean is_company
        bigint parent_id FK
        text type
        text email
        text email_normalized
        text phone
        text mobile
        text website
        text vat
        text ref
        text lang
        text tz
        bigint user_id FK
        bigint industry_id FK
        bigint title_id FK
        text function
        text street
        text street2
        text city
        text zip
        bigint state_id FK
        bigint country_id FK
        numeric latitude
        numeric longitude
        text comment
        bigint image_attachment_id FK
        bigint company_id FK
        boolean has_portal_access
        int color
        boolean active
    }
    contacts_tag {
        jsonb name
        int color
        bigint parent_id FK
        boolean active
    }
    contacts_industry {
        jsonb name
        text full_name
        boolean active
    }
    contacts_title {
        jsonb name
        text shortcut
    }
    contacts_bank_account {
        bigint partner_id FK
        text acc_number
        text sanitized_acc_number
        text bank_name
        text bic
        bigint currency_id FK
        bigint company_id FK
        boolean active
    }
    contacts_merge_log {
        bigint kept_partner_id FK
        jsonb merged_partner_ids
        jsonb field_choices
        jsonb relinked
        text method
        bigint ai_run_id FK
    }
```

| Table.column | Notes |
|---|---|
| `contacts_partner.type` | `contact / invoice / delivery / other`; addresses are child partners of a company partner with `is_company = false`. `display_name` is a stored compute (`"Company, Person"`) with a trigram index — it is the column list views and `name_search` hit. |
| `contacts_partner.email_normalized` | lower-cased, trimmed, used for duplicate detection and inbound mail matching; btree index. |
| `contacts_partner.company_id` | nullable: `NULL` means shared across companies (the default for contacts); a company-specific contact is visible only to that company. |
| `contacts_partner.has_portal_access` | stored compute from `portal_access`; the portal rule for partners is `id = $user.partner_id or parent_id = $user.partner_id`. |
| `contacts_bank_account.sanitized_acc_number` | `UNIQUE (sanitized_acc_number, company_id)`; the accounting module (Phase 2) adds `journal_id`. |
| `contacts_merge_log` | the merge wizard's record: which partner survived, which were archived/removed, which field value was chosen per field, and every `(model, field)` re-pointed in `relinked`. `method` is `manual / rule / ai_suggested`; AI-suggested merges are proposals first. |

---

## 7. CRM

### 7.1 Leads, opportunities and their lookups

```mermaid
erDiagram
    crm_lead }o--o| contacts_partner : "partner_id"
    crm_lead }o--|| crm_stage : "stage_id"
    crm_lead }o--o| crm_team : "team_id"
    crm_lead }o--o| core_user : "user_id"
    crm_lead }o--o| crm_lost_reason : "lost_reason_id"
    crm_lead }o--o{ crm_tag : "tag_ids"
    crm_lead }o--o| crm_source : "source_id"
    crm_lead }o--o| crm_medium : "medium_id"
    crm_lead }o--o| crm_campaign : "campaign_id"
    crm_lead ||--o{ sale_quotation : "quotations"
    crm_scoring_rule }o--o| core_company : "company_id"

    crm_lead {
        text type
        text name
        bigint partner_id FK
        text partner_name
        text contact_name
        text email_from
        text email_normalized
        text phone
        text mobile
        text website
        text function
        jsonb address
        bigint team_id FK
        bigint user_id FK
        bigint stage_id FK
        numeric probability
        numeric automated_probability
        numeric expected_revenue
        numeric recurring_revenue
        text recurring_plan
        bigint currency_id FK
        date date_deadline
        timestamptz date_closed
        timestamptz date_conversion
        timestamptz date_last_stage_update
        boolean won
        boolean lost
        bigint lost_reason_id FK
        text lost_feedback
        int priority
        bigint source_id FK
        bigint medium_id FK
        bigint campaign_id FK
        int color
        text description
        numeric score
        jsonb score_factors
        text risk_level
        jsonb risk_factors
        jsonb next_best_action
        text activity_state
        date activity_date_deadline
        text activity_summary
        bigint company_id FK
        boolean active
    }
    crm_stage {
        jsonb name
        int sequence
        boolean is_won
        boolean is_lost
        numeric probability
        boolean fold
        text requirements
        boolean active
    }
    crm_lost_reason {
        jsonb name
        boolean active
    }
    crm_tag {
        jsonb name
        int color
    }
    crm_source {
        text name
    }
    crm_medium {
        text name
    }
    crm_campaign {
        text name
        bigint company_id FK
    }
    crm_scoring_rule {
        text name
        jsonb domain
        int points
        int sequence
        bigint company_id FK
        boolean active
    }
```

### 7.2 Teams, assignment, forecasts, intake

```mermaid
erDiagram
    crm_team }o--o| core_user : "user_id (leader)"
    crm_team ||--o{ crm_team_member : "members"
    crm_team_member }o--|| core_user : "user_id"
    crm_team }o--o{ crm_stage : "team_ids (stage visible to)"
    crm_team }o--o| core_mail_alias : "alias_id"
    crm_team ||--o{ crm_assignment_rule : "rules"
    crm_team ||--o{ crm_forecast_snapshot : "forecasts"
    crm_team ||--o{ crm_lead : "team_id"
    crm_lead ||--o{ crm_lead_intake : "intake log"
    crm_lead_intake }o--o| chatter_message : "mail_message_id"

    crm_team {
        text name
        bigint user_id FK
        bigint alias_id FK
        text assignment_mode
        int assignment_max_per_day
        numeric invoiced_target
        text color
        bigint company_id FK
        boolean active
    }
    crm_team_member {
        bigint team_id FK
        bigint user_id FK
        int assignment_weight
        int assignment_max
        jsonb assignment_domain
        timestamptz last_assigned_at
        boolean active
    }
    crm_assignment_rule {
        bigint team_id FK
        text name
        jsonb domain
        bigint assign_user_id FK
        bigint assign_team_id FK
        int sequence
        boolean active
    }
    crm_forecast_snapshot {
        bigint team_id FK
        bigint user_id FK
        date period_start
        text period_kind
        bigint currency_id FK
        numeric pipeline_total
        numeric committed
        numeric best_case
        numeric ai_forecast
        numeric ai_forecast_low
        numeric ai_forecast_high
        jsonb ai_factors
        bigint ai_run_id FK
        numeric actual_won
        bigint company_id FK
    }
    crm_lead_intake {
        bigint lead_id FK
        text channel
        text source_ref
        jsonb raw_payload_ref
        jsonb extracted_fields
        numeric extraction_confidence
        bigint ai_run_id FK
        bigint mail_message_id FK
        inet ip
    }
```

| Table.column | Notes |
|---|---|
| `crm_lead.type` | `lead / opportunity`; conversion sets `type = opportunity`, `date_conversion`, links/creates `partner_id`, keeps the captured contact fields for history. |
| `crm_lead.probability` vs `automated_probability` | `probability` is what the user sees and can override; `automated_probability` is the current predictive score. If the user has not overridden, `probability` follows the stage default (rule-based, Phase 1) or the model (predictive, Phase 1.6). |
| `crm_lead.score`, `score_factors` | rule-based points from `crm_scoring_rule` plus the predictive component; `score_factors` is `[{"factor": "Replied within 24h", "weight": +12}, …]` for the explainability requirement. |
| `crm_lead.risk_level`, `risk_factors`, `next_best_action` | AI outputs stored on the record (advisory, labelled); refreshed by the deal-risk job; no write to any other field without a proposal. |
| `crm_lead.activity_*` | denormalised from `chatter_activity` for list/kanban sorting ("my next activity"); refreshed on activity write. |
| `crm_lead.won` / `lost` | derived from the stage's `is_won`/`is_lost` at the time of the move and stored so historical reports survive stage edits. `lost` leads are archived (`active = false`) and keep `lost_reason_id`. |
| Indexes | `(team_id, stage_id)` for kanban columns, `(user_id, activity_date_deadline)` for the activity dashboard, trigram on `name` and `partner_name`, btree on `email_normalized` and `date_deadline`, partial on `active`. |
| `crm_team.assignment_mode` | `manual / round_robin / rules`; `crm_team_member.assignment_weight` and `assignment_max` drive round-robin, `assignment_domain` restricts which leads a member can receive; `crm_assignment_rule` routes by domain to a user or a team. AI lead routing (Phase 1.6) proposes an assignee; acceptance writes `user_id`. |
| `crm_stage` | stages are global by default; `team_ids` restricts a stage to given teams. `requirements` is free text shown in the kanban column tooltip. |
| `crm_forecast_snapshot` | one row per `(team or user, period)`; snapshots are frozen weekly so forecast accuracy can be measured against `actual_won` later. `period_kind` is `month / quarter`. |
| `crm_lead_intake.channel` | `manual / web_form / email / api / import`; `raw_payload_ref` points at an attachment (the form post or the raw email), `extracted_fields` and `extraction_confidence` record what AI extraction proposed. |

---

## 8. Calendar and Discuss

```mermaid
erDiagram
    calendar_event }o--o| calendar_recurrence : "recurrence_id"
    calendar_event }o--|| core_user : "user_id (organizer)"
    calendar_event ||--o{ calendar_attendee : "attendee_ids"
    calendar_attendee }o--|| contacts_partner : "partner_id"
    calendar_event }o--o{ calendar_alarm : "alarm_ids"
    calendar_event }o--o| calendar_room : "room_id"
    calendar_event ||--o{ calendar_external_link : "synced as"
    calendar_external_account ||--o{ calendar_external_link : "links"
    calendar_external_account }o--|| core_user : "user_id"
    discuss_channel ||--o{ discuss_channel_member : "members"
    discuss_channel_member }o--|| contacts_partner : "partner_id"
    discuss_channel ||--o{ chatter_message : "messages (model = discuss.channel)"
    discuss_channel }o--o| core_company : "company_id"

    calendar_event {
        text name
        timestamptz start
        timestamptz stop
        boolean allday
        text event_tz
        numeric duration
        bigint recurrence_id FK
        boolean is_recurrence_exception
        bigint user_id FK
        text description
        text location
        bigint room_id FK
        text videocall_url
        text res_model
        bigint res_id
        text privacy
        text show_as
        bigint ai_brief_run_id FK
        boolean active
    }
    calendar_recurrence {
        text rrule
        text freq
        int interval
        jsonb byweekday
        int bymonthday
        text end_type
        int count
        date until
        bigint base_event_id FK
        text event_tz
    }
    calendar_attendee {
        bigint event_id FK
        bigint partner_id FK
        text state
        text access_token
        boolean is_optional
        boolean notified
    }
    calendar_alarm {
        jsonb name
        text alarm_type
        int duration
        text interval
    }
    calendar_room {
        text name
        int capacity
        text location
        jsonb equipment
        bigint company_id FK
        boolean active
    }
    calendar_external_account {
        bigint user_id FK
        text provider
        text account_email
        text credentials_ref
        text calendar_external_id
        text sync_cursor
        timestamptz last_sync_at
        text last_error
        boolean active
    }
    calendar_external_link {
        bigint account_id FK
        bigint event_id FK
        text external_id
        text external_etag
        timestamptz external_updated_at
        text sync_state
    }
    discuss_channel {
        text name
        text channel_type
        text description
        text privacy
        jsonb group_ids
        bigint company_id FK
        bigint image_attachment_id FK
        text uuid
        bigint last_message_id FK
        boolean active
    }
    discuss_channel_member {
        bigint channel_id FK
        bigint partner_id FK
        text custom_name
        bigint seen_message_id FK
        bigint fetched_message_id FK
        boolean is_pinned
        timestamptz mute_until
        text notification_level
        timestamptz last_interest_at
    }
```

| Table.column | Notes |
|---|---|
| `calendar_event.start` / `stop` | always `timestamptz`; `allday` events store midnight in `event_tz` and are rendered as dates. Indexes on `(start, stop)` and on `(user_id, start)`; attendee lookups go through `calendar_attendee (partner_id, event_id)`. |
| `calendar_event.recurrence_id`, `is_recurrence_exception` | occurrences are materialised rows (one per occurrence within a rolling horizon, extended by a job) so they can be searched, synced and linked; an edited occurrence is flagged as an exception and detached from future bulk edits. |
| `calendar_event.res_model` / `res_id` | polymorphic link to the business record (opportunity, ticket, applicant) — the source of AI meeting briefs; `ai_brief_run_id` points at the last generated brief. |
| `calendar_attendee.state` | `needs_action / accepted / declined / tentative`; `access_token` lets external attendees respond from an email link. |
| `calendar_room` | room booking is a `room_id` on the event; the overlap constraint is enforced in the ORM (`EXCLUDE USING gist` on `(room_id, tstzrange(start, stop))` is added as a DB-level guard). |
| `calendar_external_account.provider` | `google / microsoft / caldav`; `credentials_ref` → envelope-encrypted OAuth tokens or CalDAV credentials; `sync_cursor` is the provider's incremental token. `calendar_external_link` has `UNIQUE (account_id, external_id)` and `sync_state` (`synced / pending_push / pending_pull / conflict`). |
| `discuss_channel.channel_type` | `channel / group / dm`; `privacy` is `public / groups / invite`. DMs have exactly two members and a deterministic `uuid`. Messages are `chatter_message` rows with `model = 'discuss.channel'`, so mentions, reactions, attachments and threads are the same code path as record chatter. |
| `discuss_channel_member.seen_message_id` / `fetched_message_id` | unread counters and read receipts; `UNIQUE (channel_id, partner_id)`. |

---

## 9. Product, quotations, portal (Phase 1 subset — D-011)

```mermaid
erDiagram
    product_template ||--o{ product_product : "variants (1 in Phase 1)"
    product_template }o--|| product_category : "categ_id"
    product_template }o--o{ product_tax : "taxes_id"
    product_template }o--|| core_currency : "currency_id"
    sale_quotation }o--|| contacts_partner : "partner_id"
    sale_quotation }o--o| crm_lead : "opportunity_id"
    sale_quotation }o--|| core_user : "user_id"
    sale_quotation }o--o| crm_team : "team_id"
    sale_quotation }o--|| core_currency : "currency_id"
    sale_quotation ||--o{ sale_quotation_line : "line_ids"
    sale_quotation_line }o--|| product_product : "product_id"
    sale_quotation_line }o--o{ product_tax : "tax_ids"
    portal_access }o--|| contacts_partner : "partner_id"
    portal_access }o--o| core_user : "user_id"

    product_template {
        jsonb name
        text default_code
        text product_type
        bigint categ_id FK
        numeric list_price
        numeric standard_price
        bigint currency_id FK
        text uom_name
        jsonb description_sale
        bigint image_attachment_id FK
        boolean sale_ok
        bigint company_id FK
        boolean active
    }
    product_product {
        bigint template_id FK
        text default_code
        text barcode
        jsonb attribute_values
        boolean active
    }
    product_category {
        jsonb name
        bigint parent_id FK
        text complete_name
    }
    product_tax {
        jsonb name
        numeric amount
        text amount_type
        boolean price_include
        bigint company_id FK
        boolean active
    }
    sale_quotation {
        text name
        bigint partner_id FK
        bigint opportunity_id FK
        bigint user_id FK
        bigint team_id FK
        text state
        date date_quotation
        date validity_date
        bigint currency_id FK
        numeric amount_untaxed
        numeric amount_tax
        numeric amount_total
        jsonb terms
        text note
        text access_token
        timestamptz sent_at
        timestamptz accepted_at
        text accepted_by_name
        text accepted_by_email
        bigint signature_attachment_id FK
        inet accepted_ip
        text rejected_reason
        bigint ai_draft_run_id FK
        bigint company_id FK
    }
    sale_quotation_line {
        bigint quotation_id FK
        int sequence
        bigint product_id FK
        jsonb name
        numeric quantity
        text uom_name
        numeric price_unit
        numeric discount
        numeric price_subtotal
        numeric price_tax
        numeric price_total
        text display_type
        boolean is_optional
    }
    portal_access {
        bigint partner_id FK
        bigint user_id FK
        text state
        text invitation_token_hash
        timestamptz invited_at
        timestamptz accepted_at
        bigint invited_by FK
        bigint company_id FK
    }
```

| Table.column | Notes |
|---|---|
| `product_template` / `product_product` | kept minimal for Phase 1 (no attributes/variants, `uom_name` is free text, `product_tax` is a stub). Phase 2 adds attribute lines, UoM, pricelists, and replaces `product_tax` with `account.tax`; the FKs are named so that migration is a rename, not a re-link. |
| `sale_quotation.name` | from sequence `sale.quotation` per company (`QT/2026/00001`). `state` is a `workflow_definition`: `draft → sent → accepted / rejected / expired`; expiry is an `on_time` automation on `validity_date`. |
| `sale_quotation.access_token` | random token for the portal link; the portal rule is `partner_id = $user.partner_id or partner_id.parent_id = $user.partner_id`, and token links are rate-limited. Acceptance captures name, email, IP, and an optional drawn signature as an attachment (the Phase 2b Sign module replaces this with an envelope). |
| `sale_quotation.ai_draft_run_id` | set when the quotation was drafted from an AI proposal (lines generated from an opportunity); the "AI-drafted" label clears on first human edit. |
| `sale_quotation_line.display_type` | `NULL / section / note` for layout lines; `is_optional` lines are shown as upsells in the portal and excluded from totals until accepted. |
| `portal_access.state` | `invited / active / revoked`; `invitation_token_hash` is single-use. Creating portal access creates a `core_user` with only `portal.group_portal`. |

---

## 10. AI platform (`ai_core`)

### 10.1 Configuration, routing, prompts, features

```mermaid
erDiagram
    core_company ||--o{ ai_core_provider_config : "providers"
    core_company ||--o{ ai_core_route : "routing table"
    ai_core_route }o--|| ai_core_provider_config : "primary_provider_id"
    ai_core_route }o--o| ai_core_provider_config : "fallback_provider_id"
    ai_core_feature ||--o{ ai_core_feature_flag : "per-company flags"
    ai_core_feature }o--|| core_module : "module"
    ai_core_feature ||--o{ ai_core_prompt : "prompts"
    ai_core_prompt ||--o{ ai_core_prompt_version : "versions"
    ai_core_feature }o--o{ ai_core_tool : "allowed tools"
    ai_core_tool }o--o| core_model : "model scope"
    core_company ||--o{ ai_core_retention_policy : "retention"
    core_company ||--o{ ai_core_budget : "budgets"
    core_user ||--o{ ai_core_budget : "user quotas"

    ai_core_provider_config {
        bigint company_id FK
        text provider
        text display_name
        text region
        text residency_region
        text endpoint_url
        text credential_ref
        jsonb model_catalog
        jsonb options
        boolean allow_training
        boolean enabled
    }
    ai_core_route {
        bigint company_id FK
        text task_class
        bigint primary_provider_id FK
        text primary_model
        bigint fallback_provider_id FK
        text fallback_model
        jsonb params
        int timeout_ms
        boolean cache_enabled
        boolean active
    }
    ai_core_feature {
        text technical_name UK
        text module
        jsonb name
        text description
        text task_class
        text risk_class
        boolean employment_related
        boolean requires_confirmation
        boolean writes_records
        boolean sends_outbound
        numeric release_threshold
        text threshold_metric
        boolean requires_bias_test
        text compliance_notes
        boolean active
    }
    ai_core_feature_flag {
        bigint feature_id FK
        bigint company_id FK
        boolean enabled
        int rollout_percent
        boolean kill_switch
        bigint prompt_version_id FK
        text reason
    }
    ai_core_prompt {
        text technical_name UK
        bigint feature_id FK
        text module
        text description
        bigint active_version_id FK
    }
    ai_core_prompt_version {
        bigint prompt_id FK
        int version
        text system_template
        text user_template
        jsonb input_schema
        jsonb output_schema
        jsonb model_hints
        jsonb redaction_policy
        text status
        text changelog
        bigint eval_run_id FK
        text content_hash
    }
    ai_core_tool {
        text name UK
        text module
        text description
        jsonb input_schema
        jsonb output_schema
        boolean is_write
        boolean is_outbound
        text model_scope
        jsonb allowed_operations
        boolean active
    }
    ai_core_retention_policy {
        bigint company_id FK
        int prompt_retention_days
        int output_retention_days
        int embedding_retention_days
        text residency_region
        boolean redact_pii_external
        boolean store_raw_io
    }
    ai_core_budget {
        text scope
        bigint company_id FK
        bigint user_id FK
        bigint feature_id FK
        text period
        bigint token_limit
        numeric spend_limit
        bigint tokens_used
        numeric spend_used
        timestamptz period_start
        text on_exceed
    }
```

| Table.column | Notes |
|---|---|
| `ai_core_provider_config.provider` | `anthropic / openai / azure_openai / google / vllm / ollama / mock`; `region` is the provider endpoint region (e.g. Anthropic `inference_geo`, Azure resource region, Vertex location), `residency_region` is the company's declared residency requirement — the gateway refuses a route whose provider region is outside it. `credential_ref` → envelope-encrypted key; `allow_training` defaults to `false` and is informational (providers' no-training terms are configured on the provider side). |
| `ai_core_route` | `UNIQUE (company_id, task_class)`; `task_class` ∈ `classify / extract / embed / summarize / draft / reason / translate / vision / transcribe`. `params` holds effort/temperature equivalents per provider; the gateway enforces `timeout_ms` and falls back on timeout, 5xx or refusal. |
| `ai_core_feature.risk_class` | EU AI Act classification `minimal / limited / high`; `employment_related = true` forces `risk_class = high`, `requires_bias_test = true` and `requires_confirmation = true`. `release_threshold` + `threshold_metric` (e.g. `accuracy >= 0.85`) gate activation in CI and at runtime: a feature with no passing `ai_core_eval_run` for the active prompt version cannot be enabled. |
| `ai_core_feature_flag` | `UNIQUE (feature_id, company_id)`; `company_id NULL` is the platform default. `kill_switch = true` overrides everything, including rollout. `prompt_version_id` pins a company to a version during staged rollout. |
| `ai_core_prompt_version.status` | `draft / testing / active / retired`; only one `active` per prompt (partial unique index). `content_hash` is part of the response-cache key. `redaction_policy` lists which input fields are PII and whether the feature needs them. |
| `ai_core_tool` | the typed tool catalogue the model may call. `is_write` / `is_outbound` tools always produce proposals; `model_scope` + `allowed_operations` (`search / read / read_group / propose_write / propose_create / propose_message`) bound what the tool handler may do — the handler itself still runs through the user's `Environment`. |
| `ai_core_budget.scope` | `company / user / feature`; `period` ∈ `day / month`; `on_exceed` ∈ `block / degrade_to_small_model / notify`. Counters are incremented from the usage ledger by the gateway. |

### 10.2 Runs, tool calls, proposals, citations, feedback, evaluation, embeddings

```mermaid
erDiagram
    ai_core_run }o--|| core_user : "user_id"
    ai_core_run }o--|| ai_core_feature : "feature_id"
    ai_core_run }o--|| ai_core_prompt_version : "prompt_version_id"
    ai_core_run ||--o{ ai_core_tool_call : "tool calls"
    ai_core_tool_call }o--|| ai_core_tool : "tool_id"
    ai_core_run ||--o{ ai_core_proposal : "proposals"
    ai_core_run ||--o{ ai_core_citation : "citations"
    ai_core_run ||--o{ ai_core_feedback : "feedback"
    ai_core_run ||--o{ ai_core_usage : "usage lines"
    ai_core_feedback ||--o| ai_core_eval_case : "promoted to"
    ai_core_eval_set ||--o{ ai_core_eval_case : "cases"
    ai_core_eval_set }o--|| ai_core_feature : "feature_id"
    ai_core_eval_set ||--o{ ai_core_eval_run : "runs"
    ai_core_eval_run }o--|| ai_core_prompt_version : "prompt_version_id"

    ai_core_run {
        uuid uuid UK
        bigint user_id FK
        bigint company_id FK
        bigint feature_id FK
        bigint prompt_version_id FK
        text provider
        text model
        text fallback_from_model
        text context_model
        bigint context_res_id
        text input_ref
        text output_ref
        text input_hash
        boolean redaction_applied
        jsonb redaction_summary
        int tokens_in
        int tokens_out
        int tokens_cached
        numeric cost
        int latency_ms
        boolean cache_hit
        text status
        text stop_reason
        text error
        text decision
        bigint decision_uid FK
        timestamptz decision_at
        text request_id
        text trace_id
        timestamptz expires_at
    }
    ai_core_tool_call {
        bigint run_id FK
        bigint tool_id FK
        int sequence
        jsonb input
        text output_ref
        boolean allowed
        text denial_reason
        int record_count
        int duration_ms
    }
    ai_core_proposal {
        bigint run_id FK
        text kind
        text model
        bigint res_id
        jsonb payload
        text preview_text
        jsonb explanation
        text state
        bigint decided_by FK
        timestamptz decided_at
        jsonb edited_payload
        bigint applied_audit_id FK
        timestamptz expires_at
    }
    ai_core_citation {
        bigint run_id FK
        text model
        bigint res_id
        text source_type
        text snippet
        numeric score
        int rank
    }
    ai_core_feedback {
        bigint run_id FK
        bigint user_id FK
        text rating
        text correction
        jsonb tags
        text comment
        bigint promoted_case_id FK
    }
    ai_core_eval_set {
        text technical_name UK
        bigint feature_id FK
        text module
        text description
        text grading
        jsonb thresholds
        boolean is_bias_set
        text source_path
    }
    ai_core_eval_case {
        bigint eval_set_id FK
        text external_id
        jsonb input
        jsonb expected
        jsonb rubric
        jsonb protected_attribute_variants
        text origin
        boolean active
    }
    ai_core_eval_run {
        bigint eval_set_id FK
        bigint prompt_version_id FK
        text provider
        text model
        text mode
        jsonb metrics
        jsonb bias_metrics
        boolean passed
        int case_count
        numeric total_cost
        text git_sha
        text report_ref
    }
    ai_core_embedding {
        text model
        bigint res_id
        int chunk_index
        text source_type
        text field_name
        text text
        vector embedding
        text embedding_model
        int dim
        bigint company_id FK
        text lang
        text content_hash
        timestamptz updated_at
    }
    ai_core_usage {
        bigint run_id FK
        bigint company_id FK
        bigint user_id FK
        bigint feature_id FK
        text provider
        text model
        text usage_kind
        int tokens
        numeric unit_cost
        numeric cost
        timestamptz timestamp
    }
```

| Table.column | Notes |
|---|---|
| `ai_core_run` | the audit record required by the spec: who, which feature, which prompt version, which model (and `fallback_from_model` when the primary failed), input/output references (object-storage keys, subject to `ai_core_retention_policy` — `expires_at` is set from it and a purge job deletes the blobs while keeping the row), redaction evidence, tokens/cost/latency, and the human `decision` (`none / accepted / edited / rejected / expired`). `context_model`/`context_res_id` is the record the user was looking at. Indexes on `(company_id, feature_id, create_date)` and `(user_id, create_date)` feed the admin cost and quality dashboards. |
| `ai_core_tool_call.allowed` | `false` with `denial_reason` when the tool handler refused (ACL, record rule, field group, kill switch); denied calls are still logged — they are the evidence for the tool-permission tests. `record_count` is what the tool returned after record-rule filtering. |
| `ai_core_proposal.kind` | `create / write / message / action / transition`; `payload` is a diff (`{"field": {"old": …, "new": …}}`) or the outbound message; `preview_text` is what the UI shows; `explanation` carries the top factors. Accepting applies the payload (or `edited_payload`) through the user's `Environment` and records `applied_audit_id` → `core_audit_log` (which in turn carries `ai_run_id`). Pending proposals expire (`expires_at`) so stale suggestions never apply. |
| `ai_core_citation` | one row per source the answer used; the UI renders them as links. `source_type` ∈ `record / chatter / document / knowledge`. |
| `ai_core_feedback.rating` | `up / down`; a correction can be promoted to an `ai_core_eval_case` (`promoted_case_id`), closing the feedback → evaluation loop. |
| `ai_core_eval_set` / `_case` / `_run` | golden sets live in the module (`ai/evals/*.jsonl`, `source_path`) and are loaded into these tables for the harness; `grading` ∈ `exact / schema / rubric_llm / numeric_tolerance`; `is_bias_set` marks the sets with `protected_attribute_variants` required for employment-related features. `ai_core_eval_run.mode` is `replay / live` (D-017); `passed` is what the release gate reads. |
| `ai_core_embedding` | `UNIQUE (model, res_id, chunk_index, field_name, embedding_model)`; `vector` dimension is fixed per `embedding_model` (one HNSW index per dimension, see §11). `content_hash` skips re-embedding unchanged text. Rows are deleted with their source record (polymorphic cleanup, §11.1) and purged by `embedding_retention_days`. Retrieval joins on `(model, res_id)` against the caller's record-rule-filtered id set *before* ordering by distance. |
| `ai_core_usage.usage_kind` | `input / output / cached_input / embedding / audio_seconds / image`; the gateway writes these lines and aggregates them into `ai_core_budget` counters and the cost dashboard. |

---

## 11. Cross-cutting design notes

### 11.1 Polymorphic references
Chatter, followers, activities, attachments, audit log, notifications, search index, embeddings, calendar event links, CRM intake and AI runs all point at "any record" with the pair `(model varchar, res_id bigint)` rather than a hard FK. This keeps those tables module-agnostic and lets new modules get chatter, audit and AI for free.

- Every such table has a composite btree index `(model, res_id)` (plus `date desc` where a timeline is read).
- There is no database FK, so referential cleanup is the ORM's job: `unlink` (and hard-delete after the trash period) calls the registered polymorphic cleaners — messages, followers, activities, attachments, embeddings, search-index rows, notifications, proposals (marked `expired`) and external calendar links are removed or detached in the same transaction. `core_audit_log` and `ai_core_run` rows are kept (they are the evidence) with `res_id` left as-is.
- A nightly `core.polymorphic_integrity` scheduled job scans for orphans (a sampled anti-join per model) and reports counts; it never deletes silently.
- Model renames write a `core_module_migration_log` step that updates the `model` column across all polymorphic tables in one migration helper.

### 11.2 Indexing strategy for 100k-row lists
The performance target (list views < 300 ms at 100k records) is met with declared indexes, not query-time heroics:

| Pattern | Index |
|---|---|
| Every many2one column | btree (`index=True` is implied for FKs; the schema sync creates them) |
| `active` on archivable models | partial index `WHERE active` on the default sort column (`crm_lead (team_id, stage_id) WHERE active`) |
| `name`, `display_name`, `email`, `ref` searched with `ilike` | GIN `gin_trgm_ops` (pg_trgm) |
| Translatable JSONB fields that are sorted or searched | expression index per active language, `((name ->> 'en_US'))`, created lazily by the sync when a language is activated |
| Date range scans (`date_deadline`, `start/stop`, `create_date`) | btree, composite with the usual filter column (`(user_id, date_deadline)`) |
| Composite "kanban column" queries | `(team_id, stage_id)`, `(stage_id, user_id)` |
| Unique business keys | unique btree, often partial (`WHERE deleted_at IS NULL`) |
| Polymorphic tables | `(model, res_id[, date desc])` |
| Full-text / command palette | GIN on `tsvector` + trigram on summary |
| Embeddings | HNSW on `vector` with `vector_cosine_ops`, one index per `(embedding_model)` partition; `m = 16`, `ef_construction = 64`; queries set `hnsw.ef_search` per request |

`read_group` aggregates (`sum`, `count`, `avg`) run as SQL `GROUP BY` with the same indexes; list views request only displayed fields and the ORM prefetches related `display_name`s in one query per relation. Keyset pagination (`order + id` cursor) is available for infinite-scroll lists; offset pagination is kept for the API.

### 11.3 Row-level security
Row-level security is implemented by the ORM compiling record rules into every query (`WHERE … AND (<rule domain SQL>)`), not with PostgreSQL `ROW LEVEL SECURITY` policies. Reasons:

1. **Per-request company switching.** A user can select any subset of their allowed companies per request (`$companies`); expressing that with Postgres session settings per connection in a pooled, async server is fragile and defeats statement caching.
2. **Domain expressiveness.** Rules reference relation paths (`team_id.member_ids.user_id`), hierarchical operators (`child_of`), and user attributes (`$user.partner_id`); the domain compiler already produces the joins and CTEs these need, and the same domain is evaluated in Python on `write`/`unlink` re-checks.
3. **One enforcement point.** The API, imports, reports, automations and AI tools all go through the ORM, so there is no path that bypasses rules; Postgres RLS would add a second, divergent policy language to keep in sync.
4. **Testability.** Record-rule tests assert on compiled SQL and on result sets per user, including cross-company leak tests, without needing database roles per user.

The database roles are therefore few (`app`, `migrate`, `readonly_reporting`), and the `readonly_reporting` role is the only one that reads tables directly — for BI tooling with its own company filtering.

---

## 12. Deferred to later phases
Each of these gets its own ERD document at the start of its phase, extending the models above (e.g. `contacts_partner` gains receivable/payable accounts; `product_tax` is replaced by `account.tax`):

- **Phase 2:** `sale` (orders, templates, pricelists, variants, UoM), `sale_subscription`, `sale_rental`, `sale_commission`, `account` (chart of accounts, journals, moves, payments, bank statements, taxes, fiscal positions, analytic, budgets, assets, tax reports, e-invoicing), `payment`.
- **Phase 2b:** `sign` (templates, template versions, envelopes, roles, fields, signers, events hash chain, certificates, verification).
- **Phase 3:** `purchase`, `inventory` (warehouses, locations, moves, quants, lots, valuation), `inventory_barcode`, `shipping`, `manufacturing`, `quality`, `maintenance`, `fleet` (`uom` arrives in Phase 2 with `account`).
- **Phase 4:** `project`, `timesheet`, `helpdesk`, `field_service`, `planning`, `appointment`.
- **Phase 5:** `hr` and its satellites (`recruitment`, `time_off`, `attendance`, `expense`, `appraisal`, `payroll`) with role-restricted private data tables.
- **Phase 6–8:** marketing and engagement, website/eCommerce/POS, documents/knowledge/approvals/dashboards/spreadsheet/marketplace/IoT.
