# OWASP ASVS 4.0.3 Level 2 — Phase 1 review

Acceptance criterion **U-9** (`docs/architecture/BUILD_PLAN.md`): *OWASP ASVS L2 checklist delta for the phase reviewed; no high-severity finding open.* Phase 1 is the first reviewed phase, so the delta is the whole application: backend (`backend/`), AI gateway (`gateway/`), client (`frontend/`) and the Docker Compose deployment (`deploy/`).

Reviewed revision: the Phase 1 branch at the time of this document, plus the fixes listed below. Paths are relative to the repository root; line numbers refer to this revision. Regression tests named `test_sec*` are in `backend/tessera/api/tests/test_security_asvs.py`.

## Summary

| Chapter | Applicable rows | Met | Partially met | Not met | Not applicable |
|---|---:|---:|---:|---:|---:|
| V1 Architecture, design and threat modelling | 28 | 21 | 7 | 0 | 0 |
| V2 Authentication | 30 | 20 | 4 | 6 | 8 |
| V3 Session management | 18 | 13 | 3 | 2 | 0 |
| V4 Access control | 9 | 6 | 2 | 1 | 0 |
| V5 Validation, sanitisation and encoding | 25 | 24 | 1 | 0 | 1 |
| V6 Stored cryptography | 14 | 9 | 5 | 0 | 1 |
| V7 Error handling and logging | 10 | 9 | 0 | 1 | 2 |
| V8 Data protection | 14 | 7 | 5 | 2 | 1 |
| V9 Communications | 5 | 3 | 2 | 0 | 3 |
| V10 Malicious code | 3 | 3 | 0 | 0 | 2 |
| V11 Business logic | 8 | 5 | 1 | 2 | 0 |
| V12 Files and resources | 15 | 10 | 2 | 3 | 0 |
| V13 API and web service | 9 | 8 | 1 | 0 | 2 |
| V14 Configuration | 21 | 15 | 5 | 1 | 1 |
| **Total** | **209** | **153** | **38** | **18** | **21** |

| Findings | High | Medium | Low | Total |
|---|---:|---:|---:|---:|
| Fixed in this review | 7 | 4 | 6 | 17 |
| **Open** | **0** | 6 | 16 | 22 |

**U-9 status: no high-severity finding is open.** 7 high-severity findings were found and fixed, each with a regression test that fails on the previous code; the open findings are medium or low and are scheduled (section 2.2).

Severity scale: **high** — an authenticated user of a lower role, or an anonymous user, can gain another user's or the administrator's rights, read or change data outside their permissions across a boundary that matters (other company, other user's records), or run script/read files on the server. **Medium** — weakens a control or needs a precondition (a stolen password, a privileged role, a specific deployment). **Low** — defence in depth, hardening, or a gap in a non-functional ASVS requirement.

## 1. How this was reviewed

- **Scope.** Every HTTP and WebSocket entry point (`backend/tessera/api/routers/*.py` and the routers registered by modules under `backend/modules/*/api.py`, `*_api.py`, `ai_assist.py`, `ai_todo.py`), the authentication library (`backend/tessera/security/`), the request pipeline (`backend/tessera/api/deps.py`, `middleware.py`, `errors.py`, `app.py`), the ORM access-control paths (`backend/tessera/orm/model.py`, `backend/modules/core/security.py`, record rules and ACLs in every `backend/modules/*/security/*.json`), the AI runtime, tools and retrieval (`backend/tessera/ai/`), the report and expression engines, Studio, workflow automations, integrations making outbound calls, storage, configuration, the gateway's authentication, the Compose file and the nginx image. The client was reviewed for XSS sinks (`dangerouslySetInnerHTML`) and browser storage.
- **Method.** Manual code review against each ASVS 4.0.3 L2 requirement, guided by targeted searches: every SQL string built with an f-string or `.format` (each interpolated value traced to a registry identifier — see section 4), every `elevated()` call on a request path, every `context` key the server honours, every anonymous (`request_context`) endpoint and its token comparison and rate limit, every outbound HTTP call, every template/expression evaluator, every place a response sets `Content-Disposition` or serves HTML. Suspected issues were reproduced with a test against the running application (FastAPI `TestClient` on PostgreSQL) before being classified.
- **Verification.** Each fixed high finding has a regression test; the tests were run against the code before the fixes and failed (e.g. SEC-02: JSON-RPC `write` re-pointed the API key and returned `true`), then pass after. On the fixed code ruff and mypy pass and the full backend suite passes (581 tests) except `modules/crm/tests/test_ai_assist.py::test_lost_opportunities_stay_in_the_retrieval_index`, which failed identically on the code before this review (the retrieval text was produced in French after the translation merge) and is unrelated to security; it was fixed on the branch afterwards (index text in one language), and the full suite passes (586 tests).
- **Out of scope / not reviewed.** Third-party library internals, the infrastructure that terminates TLS in production (not in the repository), denial-of-service by volume beyond the rate limits, and the AI providers themselves (covered by `docs/ai/governance.md`).
- **Statuses.** *Met* requires a pointer to code or a test; *partially met* names what is missing and links the finding; *not applicable* states why.

## 2. Findings

### 2.1 Fixed in this review

#### SEC-01 — A client-supplied `context` could widen the company scope and lift server-side guards (high)

- **Issue.** REST (`?context=`, `CallBody.context`) and JSON-RPC (`params.context`) passed every non-underscore key into the environment. `allowed_company_ids`/`company_id` feed the `$companies` variable of the multi-company record rule, so any internal user could read and write records of companies they do not belong to; `workflow_transition` lifted the strict-workflow guard (group-restricted transitions), `automation_disable`/`tracking_disable` silenced automations and tracking, `force_delete` bypassed the trash.
- **Fix.** Allow-list: only `lang`, `tz`, `active_test`, `include_deleted`, `default_*`, `search_default_*` reach the environment (`backend/tessera/api/routers/records.py:53-81`). The company scope comes only from `X-Tessera-Companies`, intersected with the user's companies.
- **Regression test.** `test_sec01_client_context_cannot_widen_company_scope`, `test_sec01_client_context_keeps_presentation_keys_only`

#### SEC-02 — Owner-scoped records could be handed to another user (API key → administrator) (high)

- **Issue.** Record rules were evaluated on the records before a write only. An internal user could create an API key (`POST /auth/api-keys`) and re-point it at the administrator with JSON-RPC `write {user_id: 2}` (which does not read the record back), then call the API as administrator. Same pattern for Web Push subscriptions (receive another user's notifications), imports, saved filters, calendar sync accounts and booking links, AI runs/proposals/feedback; and on every multi-company model a user could move a record to a company they do not belong to.
- **Fix.** `Model._rule_check_after_write` (`backend/tessera/orm/model.py:74-77,871-872`) re-checks the write rules after the write on the twelve owner-scoped models: `core.api_key`, `core.notification`, `core.push_subscription`, `core.import_job`, `core.import_mapping`, `core.export_template`, `calendar.booking_link`, `calendar.sync_account`, `ai_core.run`, `ai_core.proposal`, `ai_core.feedback`, `web.filter`. On every model, `company_id` may only be set to one of the caller's companies (`Model._check_company_target`, `backend/tessera/orm/model.py:877-891`). Business models otherwise keep the before-write semantics (SEC-20).
- **Regression test.** `test_sec02_api_key_cannot_be_repointed_at_another_user`, `test_sec02_record_cannot_be_moved_to_a_foreign_company`

#### SEC-03 — Attachments had no object-level access control outside the download endpoint (high)

- **Issue.** `core.attachment` grants read/write to every internal user and has no record rule; only `GET /attachments/{id}/content` checked the linked record. Through the generic API any internal user could read any attachment's content (`GET /models/core.attachment/{id}?fields=raw`), list them, set `public=true`, or mint an anonymous share link (`call/generate_access_token`, which elevated without a check). Two kernel gaps made this wider: (a) `default_<field>` context keys were treated as model defaults and skipped field-level write checks on create (e.g. `default_store_key` pointed a new attachment at any stored file); (b) reading only non-stored fields skipped the read rules, and exposed methods ran on records the caller could not read.
- **Fix.** Attachment access follows the linked record on every ORM path (`check_access_rule`, `read`, `search` overrides, `backend/modules/core/models/attachment.py:63-143`); unlinked uploads belong to their uploader; `generate_access_token` checks access. Kernel: context defaults are subject to field access (`backend/tessera/orm/model.py:795-799`); computed-only reads check the read rules (`:647-651`); exposed methods need read access (`backend/tessera/api/routers/records.py:284-286`).
- **Regression test.** `test_sec03_attachment_of_unreadable_record_is_hidden_from_the_generic_api`, `test_sec03_unlinked_upload_belongs_to_its_uploader`, `test_sec03_context_default_cannot_set_restricted_field`

#### SEC-04 — Uploaded files were rendered inline on the application origin (stored XSS) (high)

- **Issue.** Downloads used the uploader-supplied MIME type with `Content-Disposition: inline`, so an uploaded `text/html` or SVG file ran script in the application's origin (reads the CSRF token from `/auth/session`, acts as the victim). The filename was interpolated unescaped into the header and the share token compared with `!=`.
- **Fix.** Only passive types (images except SVG, PDF, plain text, CSV, audio/video) are shown inline; everything else is `attachment`. Non-PDF downloads carry `Content-Security-Policy: default-src 'none' …`; RFC 6266 header with sanitised fallback name and `filename*`; constant-time token check; upload read bounded to 50 MB (`backend/tessera/api/routers/attachments.py`, `backend/tessera/api/serializers.py:76-82`).
- **Regression test.** `test_sec04_active_content_is_downloaded_not_rendered`

#### SEC-05 — Studio designers could grant themselves administrator rights through automations (high)

- **Issue.** `studio.group_designer` is not an administrator group, but automations run with system rights and Studio accepted any `create_record` target and any Studio-editable model (including `core.user`). A designer could create an automation that creates an API key for the administrator with a known hash, or writes `group_ids` on users.
- **Fix.** Designers who are not administrators may only automate Studio-customisable models outside `core.*`, and only create records of such models (`backend/modules/studio/service.py:530-560`).
- **Regression test.** `test_sec05_designer_automation_cannot_target_platform_models`

#### SEC-06 — PDF rendering read local files and fetched URLs; templates could print secrets (high)

- **Issue.** WeasyPrint's default fetcher follows `file://`, `http(s)://` and `<a rel="attachment">`, so a report designer (non-admin group) could embed `/proc/self/environ` (master key, secret key, database URL) in a PDF, or make the server call internal addresses. Templates also receive elevated records (company, printing user, customer quotation): `{{ company.create_uid.password_hash }}` or `totp_secret` printed administrator-only fields.
- **Fix.** `render_pdf` uses a fetcher limited to `data:` URIs, no redirects (`backend/tessera/reports/engine.py:212-220`); `RecordProxy` refuses `core.group_system`-only fields on elevated records (`:46-53`).
- **Regression test.** `test_sec06_pdf_rendering_loads_data_uris_only`, `test_sec06_templates_cannot_print_administrator_only_fields`

#### SEC-07 — OIDC linked accounts by an unverified e-mail claim (account takeover) (high)

- **Issue.** When no account matched (provider, subject), the callback linked the first user whose login equals the `email` claim, whatever `email_verified` said, and could also auto-create accounts from unverified addresses. With a provider that lets users choose their e-mail (multi-tenant directories, social logins), anyone could sign in as an existing account, the administrator included. The id-token algorithm list was taken from the discovery document unfiltered.
- **Fix.** E-mail linking and provisioning require `email_verified: true` (`backend/tessera/security/auth.py:286-329`); returning users are matched by subject. Only asymmetric signature algorithms are accepted (`:40-42,268-272`). Decision: providers that never send `email_verified` need the administrator to pre-link accounts (`sso_provider_id` + `sso_subject`).
- **Regression test.** `test_sec07_oidc_unverified_email_does_not_take_over_an_account`; `test_oidc_login_provisions_user` now sends `email_verified`

#### SEC-08 — Second factor: failures not counted, enrolment could silently replace it (medium)

- **Issue.** Wrong TOTP codes did not count towards the lockout and a correct password reset the counter, so a password holder could keep guessing codes (bounded only by the per-IP login limit, itself spoofable, SEC-09). `POST /auth/totp/setup` overwrote an enabled secret and turned TOTP off without a code (a stolen session could remove the factor).
- **Fix.** Wrong codes call `record_failed_login`; for TOTP users the counter is cleared only after the code is accepted; a locked account is refused at the second step; setup is refused while TOTP is on (`backend/tessera/api/routers/auth.py:121-133,168-176`, `backend/modules/core/models/user.py:201-252`).
- **Regression test.** `test_sec08_totp_failures_lock_the_account_and_setup_cannot_replace_it`

#### SEC-09 — X-Forwarded-For was trusted from any client (medium)

- **Issue.** The first `X-Forwarded-For` entry was used as the client address for the login and API rate limits, the session record and the audit log. A client could rotate the header to bypass the login limit and forge its address in the audit trail.
- **Fix.** `client_ip()` believes the header only when the peer is a trusted proxy (`TESSERA_TRUSTED_PROXIES`, default loopback + RFC 1918) and reads it from the right (`backend/tessera/api/middleware.py:33-66`, `backend/tessera/config.py:58-66`).
- **Regression test.** `test_sec09_forwarded_for_is_only_trusted_from_proxies`, `test_sec09_spoofed_forwarded_for_does_not_reset_the_login_limit`

#### SEC-10 — Production start-up accepted development secrets and a wildcard CORS origin (medium)

- **Issue.** `Settings.check()` accepted the published development master key (it is 39 bytes, so the length test passed), the `.env.example` secret key placeholder, and `TESSERA_CORS_ORIGINS=*` (with credentials, Starlette then reflects any origin).
- **Fix.** All three are refused in production (`backend/tessera/config.py:141-157`).
- **Regression test.** `test_sec10_production_check_refuses_dev_master_key_and_wildcard_cors`

#### SEC-11 — Server-side request forgery through user-configured URLs (medium)

- **Issue.** Any internal user could point a CalDAV account at an internal address (and a CalDAV server could name absolute hrefs elsewhere); designers set automation webhook URLs; administrators set webhook URLs whose response bodies are stored. None was restricted to public addresses (cloud metadata, the AI gateway, Redis …).
- **Fix.** `tessera.net.check_outbound_url` (`backend/tessera/net.py`): http(s) only and, in production (or with `TESSERA_OUTBOUND_ALLOW_PRIVATE=false`), public addresses only. Applied to every CalDAV request (event hook), automation webhooks and webhook deliveries. Residual: no address pinning (SEC-28).
- **Regression test.** `test_sec11_outbound_url_policy`, `test_sec11_caldav_refuses_private_addresses_in_production`

#### SEC-12 — API-rendered HTML had no CSP; request ids were echoed unvalidated (low)

- **Issue.** HTML produced under `/api/` (report HTML, RSVP pages) had no Content-Security-Policy; `X-Request-Id` was copied into logs and responses as sent.
- **Fix.** `default-src 'none'` CSP on active content types under `/api/` except the API docs; request ids must match `[A-Za-z0-9._:-]{1,64}` (`backend/tessera/api/middleware.py:29,74,92-115`).
- **Regression test.** `test_sec12_api_html_gets_a_content_security_policy`, `test_sec12_request_id_header_is_sanitised`

#### SEC-13 — Merge undo spliced identifiers from a JSON field into SQL (low)

- **Issue.** `undo_merge` interpolated table/column names read from the administrator-writable `relinked` JSON.
- **Fix.** Identifiers validated (pattern + existing table) before use (`backend/modules/contacts/merge.py:222-244`).
- **Regression test.** `test_sec13_merge_undo_log_identifiers_are_checked`

#### SEC-14 — Unbounded JSON-RPC batches (low)

- **Issue.** One request could carry any number of calls in one transaction.
- **Fix.** At most 100 calls per batch (`backend/tessera/api/routers/jsonrpc.py`).
- **Regression test.** `test_sec14_jsonrpc_batch_is_capped`

#### SEC-15 — Login timing revealed unknown accounts; failed logins were not logged (low)

- **Issue.** An unknown login returned without an Argon2 verification; failures left no log line.
- **Fix.** Dummy verification for unknown logins; warnings for failed/locked logins with user id and client address, never the login string or password (`backend/modules/core/models/user.py:201-220,288-297`).
- **Regression test.** Not unit-tested (timing); covered by existing login tests

#### SEC-16 — OIDC callback echoed the provider's `error_description` (low)

- **Issue.** Attacker-controllable text from the URL was returned as the error message (and was not translatable).
- **Fix.** Generic translatable message; provider error code logged (`backend/tessera/api/routers/auth.py:274-278`).
- **Regression test.** `test_oidc_login_provisions_user`

#### SEC-17 — SPA shell served without framing/sniffing headers (low)

- **Issue.** The nginx image served `index.html` without `X-Frame-Options`, `nosniff` or `Referrer-Policy`.
- **Fix.** Added to the SPA location (`deploy/images/nginx.conf`).
- **Regression test.** Configuration only

### 2.2 Open

None is high severity. Each states why it is not high and when it is planned.

| ID | Severity | Planned | Finding | Why not high / decision |
|---|---|---|---|---|
| SEC-20 | medium | Phase 2 | **Business models check write rules before the write only.** On models such as `crm.lead`, `sale.quotation`, `calendar.event`, `chatter.activity` a user who may write a record can move it out of their own rule scope (e.g. reassign it to a colleague). No privilege is gained (the user already controls the record), moving it to a foreign company is refused since SEC-02, and reassignment is an intended flow on several of these models. | Decision: keep before-write semantics for business models in Phase 1; decide per model in Phase 2 using the existing `_rule_check_after_write` flag. |
| SEC-21 | medium | Phase 2 | **Designer automations run with system rights on business models.** Within Studio-customisable business models an automation's `update`/`call_method` bypasses field groups and record rules of those models. Platform models are out of reach since SEC-05, so this is a confidentiality/integrity issue inside business data the designer can already customise, not an escalation to administrator. | Phase 2: run automations as their author (or a dedicated technical user with explicit rights). |
| SEC-22 | medium | Phase 2 | **Default session lifetimes exceed ASVS L2 (3.3.2).** Defaults are 8 h idle / 7 days absolute; L2 asks for 12 h absolute or 30 min idle. | Configurable today (`TESSERA_SESSION_IDLE_MINUTES`, `TESSERA_SESSION_ABSOLUTE_HOURS`); deployments targeting L2 set 30 / 12. Changing the defaults is a product decision tied to a "remember me" option, planned for Phase 2. |
| SEC-23 | medium | Phase 2 | **Password policy below ASVS L2 (2.1.1, 2.1.2, 2.1.7, 2.1.8, 2.1.12).** Minimum 8 characters, up to 512 accepted, no breached-password check, no strength meter or reveal toggle. | Phase 2: minimum 12, maximum 128, offline breached-password list (k-anonymity range files), meter in the client. Raising the minimum now would invalidate seeded demo/test credentials mid-phase. |
| SEC-24 | medium | Phase 2 | **MFA cannot be required for administrators; no step-up.** Administrators may work without a second factor (4.3.1); API-key creation needs no re-authentication (3.7.1). | Phase 2: company policy "require two-factor for administrators" and step-up for API keys, user and security changes. |
| SEC-25 | low | Phase 2 | **TOTP codes can be replayed within their validity window (2.8.4/2.8.5).** A valid code can be used again within ~90 s. Exploitation needs the password and an observed code. | Phase 2: remember the last accepted time step per user. |
| SEC-26 | low | Phase 2 | **No self-service session list; no change notifications (3.3.4, 2.2.3).** Only administrators list sessions; password/TOTP changes send no notification. | Phase 2 (profile screen). |
| SEC-27 | low | Phase 2 | **Session cookie without `__Host-` prefix (3.4.4).** Cookie name `tessera_session`. | Phase 2: `__Host-tessera_session` when served over HTTPS (needs the dev setup on plain HTTP to keep working). |
| SEC-28 | low | Phase 2 | **Outbound policy is not pinned; administrator-set URLs unchecked.** `check_outbound_url` resolves once and the HTTP client resolves again (DNS rebinding). OIDC discovery/JWKS URLs and AI provider base URLs (administrator settings) are not checked. | Phase 2: one pinned outbound client (the pattern of `backend/modules/contacts/website.py`) for every integration. |
| SEC-29 | medium | Phase 2 | **No Content-Security-Policy on the SPA shell (14.4.3).** XSS defences rely on React escaping and nh3 sanitising; there is no CSP as a second layer for the client. | Phase 2: CSP for the shell after an inventory of inline styles, WebSocket and asset origins (Vite dev server differs). |
| SEC-30 | low | Phase 2 | **Secrets in URLs (3.1.1, 8.3.1, 13.1.3).** Emailed attachment/quotation links carry their token in the query string by design; the WebSocket accepts `?api_key=`. They can end up in proxy logs. | Phase 2: short-lived exchange tickets for the WebSocket; tokens moved to the path fragment for emailed links. |
| SEC-31 | low | Phase 3 | **Upload hygiene: no AV scan, no quota, no zip-bomb limit (12.1.2, 12.1.3, 12.2.1, 12.4.2).** Files are capped at 50 MB and never executed; content is not scanned. | Phase 3 (document management) with ClamAV hook and per-company quotas. |
| SEC-32 | low | Phase 2 | **Access-control failures not logged; no alerting (7.2.2, 8.1.4, 11.1.7, 11.1.8).** 403s are returned but not logged; there is no alerting on lockouts or rate-limit hits. | Phase 2 with the observability work (OpenTelemetry exporter already configurable). |
| SEC-33 | low | Phase 2 | **Version and API docs exposed in production (14.2.2, 14.3.3).** `/api/docs`, `/api/openapi.json` and `/healthz` (version) are public. | Phase 2: setting to disable the docs in production. |
| SEC-34 | low | Phase 2 | **Recovery codes hashed with unsalted SHA-256 (6.2.5).** Codes carry 48 bits; an attacker with the database could brute-force them offline. | Phase 2: longer codes and a keyed hash (HMAC with the secret key). |
| SEC-35 | low | Phase 2 | **Deployment hardening (1.7.2, 1.9.x, 9.1.1, 9.2.2, 14.1.3).** The dev compose file publishes PostgreSQL, Redis and the object store on all interfaces with fixed dev credentials (clearly marked dev-only) and internal traffic is plain TCP; TLS is the ingress's job and no production deployment guide exists yet. | Phase 2: production deployment guide (TLS, private network, secrets, log shipping). |
| SEC-36 | low | Phase 2 | **Key management (1.6.1, 2.10.1, 6.4.x).** Master key in the environment, no rotation command, no KMS. | Phase 2: `tessera secrets rotate` (re-encrypt with a new master key) and KMS-backed key provider. |
| SEC-37 | low | Phase 2 | **No dependency vulnerability scan or SBOM in CI (14.2.1, 14.2.5).** Lock files pin versions; nothing reports known CVEs. | Phase 2: `pip-audit`/`npm audit` and an SBOM in CI. |
| SEC-38 | low | Phase 2 | **Privacy features (1.8.2, 6.1.x, 8.3.2, 8.3.3, 8.3.5, 8.3.8).** No self-service export/erasure, no retention rules, PII access log not written on every read, PII not column-encrypted. | Phase 2 (GDPR tooling), plus deployment-level encryption at rest. |
| SEC-39 | low | Phase 2 | **Expression engine: nested powers can allocate large integers.** Each `**` is capped at 64 but nesting is not, so a designer's expression can consume CPU/memory. Only designers write expressions. | Phase 2: cap the magnitude of intermediate integers. |
| SEC-40 | low | Phase 2 | **AI prompt templates receive full ORM records.** Prompt templates (administrator-only) are rendered in a Jinja sandbox with elevated records rather than read-only proxies; a template could call record methods. Not reachable by non-administrators. | Phase 2: reuse the report `RecordProxy` for prompts. |
| SEC-41 | low | Phase 2 | **No consolidated threat model / abuse-case list (1.1.2, 11.1.5).** Threats are discussed per area (AI governance, this review) but not kept as one document. | Phase 2: threat model maintained with each phase review. |

## 3. Checklist

Requirements are ASVS 4.0.3 identifiers; Level-3-only requirements are omitted except where noted. *Finding* links the open or fixed finding related to the row.

### V1 Architecture, design and threat modelling

| # | Requirement | Status | Evidence | Finding |
|---|---|---|---|---|
| 1.1.2 | Threat modelling for design changes | partially met | `docs/architecture/ARCHITECTURE.md`, `docs/ai/governance.md` (AI threats); no formal threat model per feature | SEC-41 |
| 1.1.4 | Trust boundaries, components and data flows documented | met | `docs/architecture/ARCHITECTURE.md`, `docs/architecture/AI_ARCHITECTURE.md` (backend ↔ gateway ↔ providers) | — |
| 1.1.5 | High-level architecture and remote services defined | met | `docs/architecture/ARCHITECTURE.md`, `deploy/compose/docker-compose.yml` | — |
| 1.1.6 | Centralised, vetted security controls | met | Single security provider consulted by the ORM on every operation: `backend/modules/core/security.py:32`, `backend/tessera/orm/model.py:264-312` | — |
| 1.2.2 | Communication between components authenticated | met | Backend → gateway bearer service token, constant-time check: `gateway/gateway/app.py:100-102`; `gateway/tests/test_app.py::test_requires_service_token` | — |
| 1.2.3 | Single vetted authentication mechanism | met | `backend/tessera/api/deps.py:113-143` (API key → session cookie → public user, never the system user) | — |
| 1.4.1 | Access control enforced at a trusted service layer | met | ORM-level checks for REST, JSON-RPC, realtime and module routers: `backend/tessera/orm/model.py:264-312`; `backend/tessera/api/routers/records.py` | — |
| 1.4.4 | Single, well-vetted access control mechanism | met | ACL + record rules + field groups in `backend/modules/core/security.py`; tests `tessera/testing/modules/kernel_test_a/tests/test_security.py` | — |
| 1.4.5 | Attribute/feature-based access control (least privilege) | partially met | Field groups and record rules exist; owner-scoped models now re-check rules after write (SEC-02); business models still check rules before write only | SEC-20 |
| 1.5.2 | Serialisation not used with untrusted clients | met | JSON only; no pickle/yaml.load in `backend/` or `gateway/` (grep); ML artefacts stored as JSON `backend/modules/crm/models/ai_insights.py:24` | — |
| 1.5.3 | Input validation enforced on a trusted service layer | met | Pydantic bodies + ORM field validation (`backend/tessera/orm/fields.py`) | — |
| 1.5.4 | Output encoding near the interpreter | met | Jinja autoescape `backend/tessera/reports/engine.py:139`; React escaping; nh3 for rich text `backend/tessera/orm/fields.py:396` | — |
| 1.6.1 | Explicit policy for cryptographic keys | partially met | `TESSERA_MASTER_KEY` envelope encryption documented in `backend/tessera/security/crypto.py:1-4`; no rotation tooling | SEC-36 |
| 1.6.2 | Consumers of crypto services protect key material | met | Keys only from environment; refused defaults in production `backend/tessera/config.py:141-157` (SEC-10) | — |
| 1.6.3 | No hard-coded keys or passwords | met | Defaults are dev-only placeholders, refused in production (`backend/tessera/config.py:141-157`, `gateway/gateway/settings.py:47`); `backend/tessera/api/tests/test_security_asvs.py::test_sec10_production_check_refuses_dev_master_key_and_wildcard_cors` | — |
| 1.6.4 | Low-entropy secrets not used as keys | met | Fernet key derived from ≥32-byte master key `backend/tessera/security/crypto.py:20-25` | — |
| 1.7.1 | Common logging format across the system | met | structlog JSON in production `backend/tessera/logging.py:19-50` | — |
| 1.7.2 | Logs sent to a remote system | partially met | stdout JSON for the platform's collector; no shipping configured in compose | SEC-35 |
| 1.8.1 | Sensitive data identified and classified | met | `pii=True` field attribute (`backend/modules/core/models/user.py`, metadata `pii` in `backend/modules/core/hooks.py`), `core.pii_access_log` | — |
| 1.8.2 | Protection levels per classification | partially met | Secrets encrypted, PII redacted before AI providers (`gateway/gateway/redaction.py`); no column encryption of regulated PII | SEC-38 |
| 1.9.1 | Communications to components encrypted | partially met | Production expects TLS at the ingress + HSTS (`backend/tessera/api/middleware.py:100`); internal hops in compose are plain | SEC-35 |
| 1.9.2 | Components verify the other side | partially met | httpx verifies TLS by default for outbound calls; internal DB/Redis/gateway hops are unauthenticated TLS-wise in compose | SEC-35 |
| 1.10.1 | Source control with check-ins linked to changes | met | Git; CI `.github/workflows/ci.yml` | — |
| 1.11.1 | Business logic flows documented | met | `docs/user/*.md`, `docs/dev/workflows.md` | — |
| 1.11.2 | High-value flows not shared unsynchronised state | met | Row locks for jobs `backend/tessera/jobs/api.py:95`, lockout counter `backend/modules/core/models/user.py:299` | — |
| 1.12.2 | Uploaded files served as downloads or from a separate domain | met | Active types forced to `attachment` + CSP (SEC-04) `backend/tessera/api/routers/attachments.py:22-104` | — |
| 1.14.1 | Segregation of components of differing trust | met | AI gateway is a separate service holding provider keys (`gateway/`) | — |
| 1.14.6 | No unsupported/insecure client-side technologies | met | React SPA only | — |

### V2 Authentication

| # | Requirement | Status | Evidence | Finding |
|---|---|---|---|---|
| 2.1.1 | Passwords of at least 12 characters | not met | Minimum is 8: `backend/modules/core/models/user.py:20` (`MIN_PASSWORD_LENGTH`) | SEC-23 |
| 2.1.2 | Passwords of 64+ allowed, >128 denied | partially met | Up to 512 accepted `backend/tessera/api/routers/auth.py` (`LoginRequest`, `PasswordChangeRequest`) | SEC-23 |
| 2.1.3 | No truncation of passwords | met | Argon2 hashes the full value `backend/modules/core/models/user.py:174-184` | — |
| 2.1.4 | Any printable Unicode allowed | met | No character restriction (`_hash_password`) | — |
| 2.1.5 | Users can change their password | met | `POST /auth/password` `backend/tessera/api/routers/auth.py:149`; `test_password_change_revokes_other_sessions` | — |
| 2.1.6 | Change requires current and new password | met | `backend/tessera/api/routers/auth.py:150-155` | — |
| 2.1.7 | Passwords checked against breached-password lists | not met | No check | SEC-23 |
| 2.1.8 | Password strength meter | not met | No meter in the client | SEC-23 |
| 2.1.9 | No composition rules | met | Length only | — |
| 2.1.10 | No periodic rotation / password history | met | None imposed | — |
| 2.1.11 | Paste and password managers allowed | met | Plain `<input type=password>` in the client | — |
| 2.1.12 | User can view the masked password | partially met | Not offered in the login form | SEC-23 |
| 2.2.1 | Anti-automation (rate limit, lockout) | met | Per-IP login limit `backend/tessera/api/middleware.py:158-184`, account lockout `backend/modules/core/models/user.py:201-228`, TOTP failures now count (SEC-08), client address no longer spoofable (SEC-09); `test_login_rate_limit`, `test_account_lockout_after_five_failures`, `backend/tessera/api/tests/test_security_asvs.py::test_sec08_totp_failures_lock_the_account_and_setup_cannot_replace_it` | — |
| 2.2.2 | Weak authenticators (SMS/email) restricted | not applicable | No SMS/email OTP | — |
| 2.2.3 | Notifications after authentication-detail changes | not met | No notification on password/TOTP change | SEC-26 |
| 2.2.4 | Phishing-resistant MFA (L3) | not applicable | L3 only | — |
| 2.3.1 | System-generated initial passwords random and short-lived | met | Portal invitations: random token, hashed, expiring `backend/modules/portal/models/access.py:128-142`; admin password from `TESSERA_ADMIN_PASSWORD`, none in production by default `backend/modules/core/hooks.py:233-245` | — |
| 2.3.2 | Enrolment of user-provided authenticators | met | TOTP enrolment `backend/tessera/api/routers/auth.py:167-186` | — |
| 2.4.1 | Passwords stored with an approved KDF | met | Argon2id (`argon2-cffi` defaults) `backend/modules/core/models/user.py:18` | — |
| 2.4.2 | Salt ≥32 bits, unique | met | Argon2 per-hash random salt | — |
| 2.4.4 | bcrypt/PBKDF2 parameters | not applicable | Argon2id used | — |
| 2.5.1 | Recovery secrets not sent in clear | not applicable | No self-service password reset in Phase 1 | — |
| 2.5.2 | No password hints / secret questions | met | None | — |
| 2.5.3 | No password reveal on recovery | not applicable | No recovery flow | — |
| 2.5.4 | No shared/default accounts | partially met | `admin` account seeded; password required via env in production; `system` user cannot log in (no password) | SEC-23 |
| 2.5.6 | Secure recovery for lost MFA | met | Single-use recovery codes `backend/modules/core/models/user.py:254-273`; `test_totp_recovery_codes` | — |
| 2.5.7 | Lost OTP does not bypass MFA | met | Recovery codes or administrator reset only | — |
| 2.7.x | Out-of-band verifiers | not applicable | Not used | — |
| 2.8.1 | Time-based OTP has a defined lifetime | met | 30 s step, ±1 window `backend/tessera/security/auth.py:153-156` | — |
| 2.8.3 | Approved algorithms for OTP | met | RFC 6238 via pyotp | — |
| 2.8.4 | OTP usable only once in its validity period | not met | A code can be replayed within the 90 s window | SEC-25 |
| 2.8.5 | Re-use attempt logged and notified | not met | See 2.8.4 | SEC-25 |
| 2.8.6 | Physical OTP revocable | not applicable | Soft TOTP only | — |
| 2.9.x | Cryptographic verifiers | not applicable | Not used | — |
| 2.10.1 | Intra-service secrets not static/unchangeable | partially met | Service token and keys from environment, rotatable by redeploy; no rotation automation | SEC-36 |
| 2.10.2 | No default credentials for services | met | Production refuses dev defaults `backend/tessera/config.py:141-157`; `gateway/gateway/settings.py:47` | — |
| 2.10.3 | Credentials protected (not in source) | met | Environment variables; stored secrets encrypted `backend/tessera/security/crypto.py:28-40` | — |
| 2.10.4 | Secrets/API keys not in source code | met | No secrets in repository other than marked dev-only placeholders (`.env.example`, compose) | — |

### V3 Session management

| # | Requirement | Status | Evidence | Finding |
|---|---|---|---|---|
| 3.1.1 | Session token never in URL parameters | partially met | Cookie for sessions; WebSocket accepts an API key in `?api_key=` `backend/tessera/api/routers/realtime.py:99` | SEC-30 |
| 3.2.1 | New session token on authentication | met | `backend/tessera/security/auth.py:55-75` (fresh token per login, no fixation) | — |
| 3.2.2 | Session tokens ≥64 bits of entropy | met | 256-bit `secrets.token_urlsafe(32)` `backend/tessera/security/auth.py:60` | — |
| 3.2.3 | Tokens stored in secure browser storage | met | HttpOnly cookie; CSRF token in memory `backend/tessera/api/routers/auth.py:52-63` | — |
| 3.2.4 | Tokens generated with approved crypto | met | `secrets` module `backend/tessera/security/crypto.py:52` | — |
| 3.3.1 | Logout and expiry invalidate the session | met | Server-side revocation `backend/tessera/api/routers/auth.py:136-141`, `backend/tessera/security/auth.py:78-102`; `test_login_logout_and_csrf` | — |
| 3.3.2 | Re-authentication after 12 h or 30 min idle (L2) | partially met | Configurable; defaults 8 h idle / 7 days absolute `backend/tessera/config.py:51-52` | SEC-22 |
| 3.3.3 | Option to end all other sessions after password change | met | Done automatically `backend/tessera/api/routers/auth.py:156-160`; `test_password_change_revokes_other_sessions` | — |
| 3.3.4 | Users can view and log out of their sessions | not met | Only administrators list sessions `backend/tessera/api/routers/auth.py:288` | SEC-26 |
| 3.4.1 | Cookie Secure attribute | met | Secure in production `backend/tessera/config.py:127`, `backend/tessera/api/routers/auth.py:58` | — |
| 3.4.2 | Cookie HttpOnly | met | `backend/tessera/api/routers/auth.py:57` | — |
| 3.4.3 | Cookie SameSite | met | `SameSite=Lax` + CSRF token on unsafe methods `backend/tessera/api/deps.py:128-132` | — |
| 3.4.4 | Cookie `__Host-` prefix | not met | Cookie is `tessera_session` | SEC-27 |
| 3.4.5 | Cookie path set narrowly | met | Single application path `/`; OIDC state cookie scoped to `/api/v1/auth/oidc/` | — |
| 3.5.1 | Users can revoke OAuth/API tokens | met | `DELETE /auth/api-keys/{id}` `backend/tessera/api/routers/auth.py:217`; calendar OAuth tokens removed by deleting one's own `calendar.sync_account` (rule `user_id = $uid`, `backend/modules/calendar/security/`) | — |
| 3.5.2 | Session tokens rather than static secrets (except APIs) | met | API keys only for programmatic access, hashed at rest `backend/tessera/security/auth.py:109-130` | — |
| 3.5.3 | Stateless tokens signed and validated | met | OIDC id_token: signature (asymmetric algorithms only, SEC-07), iss, aud, nonce, exp `backend/tessera/security/auth.py:263-283` | — |
| 3.7.1 | Re-authentication before sensitive changes | partially met | Password change needs the current password; turning TOTP off needs a code; TOTP setup refused while enabled (SEC-08); API-key creation needs no re-authentication | SEC-24 |

### V4 Access control

| # | Requirement | Status | Evidence | Finding |
|---|---|---|---|---|
| 4.1.1 | Access control enforced server-side | met | ORM checks `backend/tessera/orm/model.py:264-312`; `test_rest_permissions` | — |
| 4.1.2 | Attributes used by access control cannot be manipulated | met | Client context restricted to presentation keys (SEC-01) `backend/tessera/api/routers/records.py:53-81`; context defaults subject to field access (SEC-03) `backend/tessera/orm/model.py:798`; `backend/tessera/api/tests/test_security_asvs.py::test_sec01_client_context_cannot_widen_company_scope` | — |
| 4.1.3 | Least privilege | partially met | Group-based ACLs per model; Studio designers' automations run with system rights on business models | SEC-21 |
| 4.1.5 | Access controls fail securely | met | Exceptions deny; anonymous gets uid 0 without rights `backend/tessera/api/deps.py:140-143` | — |
| 4.2.1 | Protection against IDOR / record-level access | met | Record rules in SQL on read `backend/tessera/orm/model.py:544`, rule check for computed-only reads `:651`, after-write check on owner-scoped models `:871` (SEC-02), attachments follow their record (SEC-03) `backend/modules/core/models/attachment.py:63-143`, exposed methods require read access `backend/tessera/api/routers/records.py:284-286`; `backend/tessera/api/tests/test_security_asvs.py::test_sec02_api_key_cannot_be_repointed_at_another_user`, `test_sec03_*`, `test_realtime_denies_unreadable_records` | SEC-20 |
| 4.2.2 | CSRF protection | met | Per-session CSRF token for cookie-authenticated unsafe requests `backend/tessera/api/deps.py:128-134`; `test_login_logout_and_csrf` | — |
| 4.3.1 | Administrative interfaces use MFA | not met | Administrators may work without TOTP | SEC-24 |
| 4.3.2 | Directory browsing disabled | met | No static file serving in the API; nginx SPA `try_files` without autoindex `deploy/images/nginx.conf` | — |
| 4.3.3 | Additional authorisation for high-value operations | partially met | Admin-only dependencies (`backend/tessera/api/deps.py:188`), proposals need the requester's acceptance `backend/tessera/ai/proposals.py:87-101`; no step-up | SEC-24 |

### V5 Validation, sanitisation and encoding

| # | Requirement | Status | Evidence | Finding |
|---|---|---|---|---|
| 5.1.1 | HTTP parameter pollution defences | met | Typed FastAPI parameters; single-valued | — |
| 5.1.2 | Mass assignment protection | met | Server-managed fields dropped `backend/tessera/api/serializers.py:15-44`; field groups on write `backend/tessera/orm/model.py` (`_split_vals`); context defaults checked (SEC-03); `backend/tessera/api/tests/test_security_asvs.py::test_sec03_context_default_cannot_set_restricted_field` | — |
| 5.1.3 | Input validation (positive) | met | Pydantic models, ORM field types, selection values | — |
| 5.1.4 | Structured data strongly typed | met | Pydantic + field validation | — |
| 5.1.5 | Redirects only to allow-listed destinations | met | OIDC callback redirects to `/` only `backend/tessera/api/routers/auth.py:265-286` | — |
| 5.2.1 | Untrusted HTML sanitised | met | nh3 on every `Html` field `backend/tessera/orm/fields.py:379-397`; `tessera/orm/tests/test_fields.py::test_html_sanitized` | — |
| 5.2.2 | Unstructured data sanitised | met | Webform text escaped `backend/modules/crm/api.py:80-90` | — |
| 5.2.3 | Email injection (SMTP/IMAP) | met | Messages built with `email.message` APIs (`backend/modules/core/models/mail.py`) | — |
| 5.2.4 | No eval / dynamic code execution | met | Whitelisted AST evaluator `backend/tessera/expr.py:127-160`; `tessera/tests/test_expr.py` | SEC-39 |
| 5.2.5 | Template injection | met | Sandboxed Jinja with autoescape for reports `backend/tessera/reports/engine.py:74-146`, record proxies only, admin-only fields hidden on elevated records (SEC-06); `modules/reports/tests/test_reports.py::test_sandbox_blocks_escapes` | SEC-40 |
| 5.2.6 | SSRF protection | partially met | Public-address policy for webhooks, automation webhooks, CalDAV (SEC-11) `backend/tessera/net.py:43-70`; enrichment fetch pins addresses `backend/modules/contacts/website.py:104-188`; PDF renderer loads `data:` only (SEC-06); DNS rebinding and admin-set URLs open | SEC-28 |
| 5.2.7 | SVG scriptable content sanitised | met | SVG and other active types served as downloads with CSP (SEC-04) | — |
| 5.2.8 | Scriptable template languages sanitised | met | See 5.2.5; AI prompt templates are administrator-only | SEC-40 |
| 5.3.1 | Context-aware output encoding | met | React, Jinja autoescape, `html.escape` in server-rendered pages (`backend/modules/calendar/api.py:73-95`) | — |
| 5.3.3 | XSS protection | met | As 5.3.1 + nh3 + CSP on API HTML (SEC-12) `backend/tessera/api/middleware.py:101-116` | SEC-29 |
| 5.3.4 | Parameterised queries | met | SQLAlchemy core with bound parameters; every f-string SQL interpolates registry identifiers only (reviewed, see §4); merge-undo identifiers now validated (SEC-13) `backend/modules/contacts/merge.py:222-244` | — |
| 5.3.5 | Database-specific escaping where parameters impossible | met | Identifiers quoted from the registry (`backend/tessera/orm/schema.py`) | — |
| 5.3.6 | JSON/JS injection | met | `JSONResponse`; embed script uses `json.dumps` `backend/modules/crm/api.py:93-100` | — |
| 5.3.7 | LDAP injection | not applicable | No LDAP | — |
| 5.3.8 | OS command injection | met | No subprocess/shell use in `backend/` or `gateway/` (grep) | — |
| 5.3.9 | Local/remote file inclusion | met | Storage path guard `backend/tessera/storage.py:31-35`; PDF renderer `data:` only (SEC-06) `backend/tessera/reports/engine.py:212-220`; `backend/tessera/api/tests/test_security_asvs.py::test_sec06_pdf_rendering_loads_data_uris_only` | — |
| 5.3.10 | XPath / XML injection | met | No XPath; CalDAV XML parsed with the standard library (no external entities) | — |
| 5.5.1 | Serialised objects integrity-checked | met | AI proposals HMAC-signed `backend/tessera/ai/proposals.py:42-44,98` | — |
| 5.5.2 | XML parsers restrict XXE | met | `xml.etree` does not resolve external entities | — |
| 5.5.3 | No deserialisation of untrusted data | met | JSON only | — |
| 5.5.4 | Safe JSON parsing in the client | met | `JSON.parse` / `fetch().json()` only | — |

### V6 Stored cryptography

| # | Requirement | Status | Evidence | Finding |
|---|---|---|---|---|
| 6.1.1 | Regulated private data encrypted at rest | partially met | Secrets encrypted; PII relies on database/volume encryption of the deployment | SEC-38 |
| 6.1.2 | Regulated health data encrypted | not applicable | No health data | — |
| 6.1.3 | Regulated financial data encrypted | partially met | As 6.1.1 | SEC-38 |
| 6.2.1 | Crypto modules fail securely | met | Decryption failure raises `ConfigurationError` `backend/tessera/security/crypto.py:32-40` | — |
| 6.2.2 | Industry-proven algorithms | met | Fernet (AES-CBC + HMAC-SHA256), Argon2id, HMAC-SHA256, SHA-256 for 256-bit tokens | — |
| 6.2.3 | Approved modes / padding | met | Fernet | — |
| 6.2.4 | Algorithms replaceable | met | Versioned prefix `enc:v1:` `backend/tessera/security/crypto.py:17` | — |
| 6.2.5 | No insecure modes/hashes | partially met | Recovery codes (48 bits) hashed with unsalted SHA-256 `backend/modules/core/models/user.py:258` | SEC-34 |
| 6.2.6 | Nonces/IVs not reused | met | Fernet random IV | — |
| 6.2.7 | Encrypted data authenticated | met | Fernet HMAC | — |
| 6.2.8 | Constant-time comparisons | met | `hmac.compare_digest` for CSRF, tokens, signatures (`backend/tessera/security/crypto.py:56`, `backend/tessera/api/routers/attachments.py:86` — fixed in SEC-04) | — |
| 6.3.1 | CSPRNG for random values | met | `secrets` everywhere tokens are minted | — |
| 6.3.2 | GUIDs v4 from CSPRNG | met | `uuid4` for AI stream channels | — |
| 6.4.1 | Secrets management solution | partially met | Environment variables; no vault/KMS integration | SEC-36 |
| 6.4.2 | Key material not exposed to the application | partially met | Master key held in process memory (no HSM/KMS) | SEC-36 |

### V7 Error handling and logging

| # | Requirement | Status | Evidence | Finding |
|---|---|---|---|---|
| 7.1.1 | No credentials or session tokens logged | met | Tokens/passwords never passed to loggers (reviewed `backend/tessera/security`, `backend/tessera/api`); API keys/sessions stored hashed | — |
| 7.1.2 | No other sensitive data logged | met | Logs carry ids, not record content; AI gateway redacts PII before providers `gateway/gateway/redaction.py`; `gateway/tests/test_units.py::test_redaction_kinds_and_stable_placeholders` | — |
| 7.1.3 | Security-relevant events logged | met | Failed/locked logins and counted second-factor failures now logged with user id and client address (SEC-15) `backend/modules/core/models/user.py:210-220`; record changes in `core.audit_log` `backend/modules/core/hooks.py:313-393` | — |
| 7.1.4 | Log events carry information for investigation | met | Request id, path, uid bound per request `backend/tessera/api/middleware.py:69-89`, `backend/tessera/api/deps.py:155` | — |
| 7.2.1 | Authentication decisions logged | met | As 7.1.3; sessions recorded in `core.session` with IP/user agent | — |
| 7.2.2 | Access-control failures logged | not met | 403 responses are not logged | SEC-32 |
| 7.3.1 | Log injection prevented | met | Structured JSON logs; request id header validated (SEC-12) `backend/tessera/api/middleware.py:29,74`; `backend/tessera/api/tests/test_security_asvs.py::test_sec12_request_id_header_is_sanitised` | — |
| 7.3.3 | Logs protected from modification | not applicable | Deployment concern (log collector) | — |
| 7.3.4 | Time synchronised | not applicable | Deployment concern | — |
| 7.4.1 | Generic error message with an id | met | `backend/tessera/api/errors.py:20-63` (500 → "Internal error" + request id) | — |
| 7.4.2 | Exception handling across the code base | met | Central handlers `backend/tessera/api/errors.py:40-63`; JSON-RPC hides internals `backend/tessera/api/routers/jsonrpc.py:85-117` | — |
| 7.4.3 | Last-resort error handler | met | `backend/tessera/api/errors.py:61` | — |

### V8 Data protection

| # | Requirement | Status | Evidence | Finding |
|---|---|---|---|---|
| 8.1.1 | Sensitive data not cached server-side | met | `Cache-Control: no-store` on the API `backend/tessera/api/middleware.py:103` | — |
| 8.1.2 | Cached copies protected | met | Attachments `private` unless public `backend/tessera/api/routers/attachments.py:99` | — |
| 8.1.3 | Minimal parameters | met | Typed request models | — |
| 8.1.4 | Abnormal request volumes detected | partially met | Rate limits per credential and address `backend/tessera/api/middleware.py:161-184`; no alerting | SEC-32 |
| 8.2.1 | Anti-caching headers | met | As 8.1.1 | — |
| 8.2.2 | No sensitive data in browser storage | met | localStorage holds theme, columns, company choice only (`frontend/src/kernel/theme.ts`, `frontend/src/kernel/session.tsx:21`) | — |
| 8.2.3 | Client data cleared after session end | partially met | Session state cleared on logout; local preferences kept | SEC-30 |
| 8.3.1 | Sensitive data in body/headers, not URL | partially met | Attachment and quotation links carry tokens in the query string by design (emailed links); WebSocket `?api_key=` | SEC-30 |
| 8.3.2 | Users can export/delete their data | not met | No self-service export/erasure | SEC-38 |
| 8.3.3 | Clear language on data use | partially met | AI disclosure per feature (`ai_core.feature.disclosure`); no privacy notice yet | SEC-38 |
| 8.3.4 | Sensitive data identified, policy exists | met | `pii` flags, `docs/ai/governance.md` | — |
| 8.3.5 | Access to sensitive data audited | partially met | `core.pii_access_log` model `backend/modules/core/models/audit.py:39`; not written on every PII read | SEC-38 |
| 8.3.6 | Sensitive data cleared from memory | not applicable | Not achievable in Python; L2 recommendation only | — |
| 8.3.7 | Sensitive data encrypted with approved algorithms | met | Stored secrets (SSO, SMTP, webhook, calendar OAuth, provider keys) Fernet-encrypted `backend/tessera/security/crypto.py`; `test_webhook_delivery_with_signature` asserts `enc:v1:` | — |
| 8.3.8 | Retention classification | not met | No retention rules | SEC-38 |

### V9 Communications

| # | Requirement | Status | Evidence | Finding |
|---|---|---|---|---|
| 9.1.1 | TLS for all client connectivity | partially met | HSTS in production `backend/tessera/api/middleware.py:100`; TLS terminates at the ingress (not in the repo); the single-image nginx listens on :80 | SEC-35 |
| 9.1.2 | Strong cipher suites | not applicable | Ingress configuration (outside the repo) | — |
| 9.1.3 | Only TLS 1.2+ | not applicable | Ingress configuration | — |
| 9.2.1 | Outbound connections use trusted certificates | met | httpx default verification (no `verify=False` in `backend/`, `gateway/`) | — |
| 9.2.2 | Encrypted connections to internal services | partially met | PostgreSQL/Redis/gateway over plain TCP in compose | SEC-35 |
| 9.2.3 | Outbound connections authenticated | met | Gateway service token; provider API keys in the gateway | — |
| 9.2.4 | Certificate revocation checking | not applicable | Library defaults | — |
| 9.2.5 | Backend TLS failures logged | met | httpx errors surface as logged job/delivery failures (`backend/modules/core/models/webhook.py:146-153`) | — |

### V10 Malicious code

| # | Requirement | Status | Evidence | Finding |
|---|---|---|---|---|
| 10.2.1 | No unauthorised phone-home / data collection | met | Outbound calls limited to configured integrations (grep of httpx usage) | — |
| 10.2.2 | No unnecessary permissions requested | met | Permissions-Policy limits camera/microphone/geolocation to self `backend/tessera/api/middleware.py:98` | — |
| 10.3.1 | Auto-update integrity | not applicable | No auto-update | — |
| 10.3.2 | Integrity of dependencies | met | Lock files `backend/uv.lock`, `gateway/uv.lock`, `frontend/package-lock.json` | SEC-37 |
| 10.3.3 | Subdomain takeover | not applicable | Deployment DNS | — |

### V11 Business logic

| # | Requirement | Status | Evidence | Finding |
|---|---|---|---|---|
| 11.1.1 | Flows processed in order | met | Strict workflows guard state fields (`backend/modules/workflow/hooks.py:32`); the `workflow_transition` context bypass is closed (SEC-01) | — |
| 11.1.2 | Realistic human-time limits | met | Rate limits on public forms/links: `backend/modules/crm/api.py:60`, `backend/modules/sale/api.py:28`, `backend/modules/portal/api.py:29`, `backend/modules/calendar/api.py:76,137` | — |
| 11.1.3 | Business limits enforced | met | AI budgets/kill switches (`backend/tessera/ai/runtime.py:97-104`, gateway budgets); JSON-RPC batch cap (SEC-14) | — |
| 11.1.4 | Anti-automation for excessive calls | met | As 11.1.2 and `backend/tessera/api/middleware.py:161-184` | — |
| 11.1.5 | Business logic limits documented | partially met | `docs/ai/governance.md`; no consolidated abuse-case list | SEC-41 |
| 11.1.6 | No TOCTOU on sensitive operations | met | Proposals signed + single decision `backend/tessera/ai/proposals.py:87-101`; job claim `FOR UPDATE SKIP LOCKED` `backend/tessera/jobs/api.py:95` | — |
| 11.1.7 | Monitoring of unusual events | not met | No alerting | SEC-32 |
| 11.1.8 | Configurable alerting | not met | No alerting | SEC-32 |

### V12 Files and resources

| # | Requirement | Status | Evidence | Finding |
|---|---|---|---|---|
| 12.1.1 | Large files refused | met | 50 MB cap, read bounded `backend/tessera/api/routers/attachments.py:55-59`; imports `backend/tessera/api/routers/dataio.py:61`; inbound mail 25 MB | — |
| 12.1.2 | Compressed files checked (zip bombs) | not met | XLSX imports are not checked for expansion ratio | SEC-31 |
| 12.1.3 | Per-user file quota | not met | None | SEC-31 |
| 12.2.1 | Files from untrusted sources validated | partially met | Type not verified against content; active types neutralised on download (SEC-04) | SEC-31 |
| 12.3.1 | User filenames not used for filesystem paths | met | Content-addressed keys `backend/tessera/storage.py:21-24` | — |
| 12.3.2 | Path traversal prevented | met | `backend/tessera/storage.py:31-35`; `tessera/testing/modules/kernel_test_a/tests/test_attachments.py::test_filesystem_storage` | — |
| 12.3.3 | No remote file inclusion / SSRF via files | met | PDF renderer `data:` only (SEC-06) | — |
| 12.3.4 | Reflective file download prevented | met | RFC 6266 `Content-Disposition` with sanitised fallback and `filename*` `backend/tessera/api/serializers.py:76-82` (SEC-04) | — |
| 12.3.5 | No OS command with file metadata | met | None | — |
| 12.3.6 | No untrusted code inclusion | met | Modules loaded from configured paths only | — |
| 12.4.1 | Files stored outside web root | met | Object storage / `var/storage` | — |
| 12.4.2 | Anti-virus scanning | not met | None | SEC-31 |
| 12.5.1 | Web tier serves only expected file types | met | nginx serves the built SPA only | — |
| 12.5.2 | Uploaded files never executed as HTML/JS | met | Active types forced to `attachment` + `default-src 'none'` CSP + nosniff `backend/tessera/api/routers/attachments.py:20-104`; `backend/tessera/api/tests/test_security_asvs.py::test_sec04_active_content_is_downloaded_not_rendered` | — |
| 12.6.1 | Outbound requests allow-listed | partially met | Private-range policy rather than allow-list (SEC-11) | SEC-28 |

### V13 API and web service

| # | Requirement | Status | Evidence | Finding |
|---|---|---|---|---|
| 13.1.1 | Same encoding/parsers across components | met | JSON UTF-8 everywhere | — |
| 13.1.3 | No sensitive data in API URLs | partially met | Token links by design (8.3.1) | SEC-30 |
| 13.1.4 | Authorisation at URI and resource level | met | Router dependencies + ORM record checks; exposed methods require read access (SEC-03) `backend/tessera/api/routers/records.py:279-292` | — |
| 13.1.5 | Requests with unexpected content types rejected | met | FastAPI JSON bodies require `application/json` | — |
| 13.2.1 | Only intended HTTP methods enabled | met | Explicit routes | — |
| 13.2.2 | JSON schema validation | met | Pydantic request models; generic values validated by ORM fields | — |
| 13.2.3 | CSRF for cookie-based REST | met | As 4.2.2 | — |
| 13.2.5 | Content-Type checked | met | As 13.1.5 | — |
| 13.2.6 | Message headers not trusted for security decisions | met | `X-Tessera-Companies` intersected with the user's companies `backend/tessera/api/deps.py:89-102`; X-Forwarded-For only from trusted proxies (SEC-09) `backend/tessera/api/middleware.py:51-66` | — |
| 13.3.x | SOAP | not applicable | Not used | — |
| 13.4.x | GraphQL | not applicable | Not used (JSON-RPC batches capped, SEC-14) | — |

### V14 Configuration

| # | Requirement | Status | Evidence | Finding |
|---|---|---|---|---|
| 14.1.1 | Repeatable, secure build/deploy | met | Dockerfiles `deploy/images/`, lock files, CI `.github/workflows/ci.yml` | — |
| 14.1.3 | Server configuration hardened | partially met | App-level checks `backend/tessera/config.py:141-157`; no production deployment guide yet | SEC-35 |
| 14.1.5 | Integrity of security-relevant configuration | not applicable | Deployment concern | — |
| 14.2.1 | Components up to date | partially met | Pinned and locked; no automated vulnerability scan in CI | SEC-37 |
| 14.2.2 | Unneeded features removed | partially met | OpenAPI/Swagger UI served in production (`backend/tessera/api/app.py:46-48`) | SEC-33 |
| 14.2.3 | SRI for third-party assets | met | Client bundles its dependencies; no CDN scripts in `frontend/index.html` | — |
| 14.2.4 | Third-party components from trusted repositories | met | PyPI / npm via lock files | — |
| 14.2.5 | SBOM maintained | not met | None | SEC-37 |
| 14.2.6 | Attack surface reduced by sandboxing | met | Report/expression sandboxes; AI gateway isolation | — |
| 14.3.2 | Debug modes disabled in production | met | No debug flags; `--reload` only on request `backend/tessera/cli/run.py:14` | — |
| 14.3.3 | No version disclosure in headers | partially met | `/healthz` returns the version; uvicorn `Server` header | SEC-33 |
| 14.4.1 | Content-Type with charset | met | Starlette JSON/HTML responses | — |
| 14.4.2 | Content-Disposition on API file responses | met | Attachments, exports, reports set it (`backend/tessera/api/routers/attachments.py:98`, `backend/tessera/api/routers/dataio.py:173,203`) | — |
| 14.4.3 | Content-Security-Policy | partially met | API HTML and downloads carry a restrictive CSP (SEC-04, SEC-12); the SPA shell has none | SEC-29 |
| 14.4.4 | X-Content-Type-Options: nosniff | met | `backend/tessera/api/middleware.py:95`; SPA shell `deploy/images/nginx.conf` (SEC-17) | — |
| 14.4.5 | HSTS | met | Production `backend/tessera/api/middleware.py:100` | — |
| 14.4.6 | Referrer-Policy | met | `backend/tessera/api/middleware.py:97`; nginx shell (SEC-17) | — |
| 14.4.7 | Framing restricted | met | `X-Frame-Options: DENY` API `backend/tessera/api/middleware.py:96`, SPA shell (SEC-17) | — |
| 14.5.1 | Only required HTTP methods | met | Explicit routes | — |
| 14.5.2 | Origin header not used for authentication | met | Not used | — |
| 14.5.3 | CORS allow-list | met | Configured origins, `*` refused in production (SEC-10) `backend/tessera/api/app.py:54-60`, `backend/tessera/config.py:154-155` | — |
| 14.5.4 | Proxy-added headers authenticated | met | `TESSERA_TRUSTED_PROXIES` (SEC-09) `backend/tessera/config.py:58-66` | — |

## 4. Raw SQL review

Every `execute`/`fetchall`/`fetchone`/`scalar` call whose SQL is built with an f-string was traced. All interpolated values are identifiers coming from the model registry or from constants, never from request data:

| Location | Interpolated values | Source |
|---|---|---|
| `backend/tessera/orm/schema.py` (CREATE/ALTER/INDEX) | table, column, index, SQL type, FK action, language code | registry (`cls._table`, field definitions); Studio field names validated by `^x_[a-z][a-z0-9_]{0,50}$` (`backend/modules/studio/service.py:36`) |
| `backend/tessera/modules/installer.py:377-490` | table/column/index names, extension names | registry and module manifests |
| `backend/tessera/i18n/apply.py:64-82` | table, column list | registry |
| `backend/tessera/ai/client.py:279-320` | column list, `WHERE` fragments | constants `COLUMNS`; filter values are bound parameters |
| `backend/tessera/ai/evals.py:360`, `backend/tessera/orm/sandbox.py:79` | table | registry |
| `backend/tessera/orm/registry.py:626-638` | stamp table | constant |
| `backend/tessera/db/engine.py:59` | statement timeout | integer setting |
| `backend/modules/core/models/sequence.py:63-103` | sequence name, start, step | `_pg_sequence_name()` from the record id; integers cast with `int()` |
| `backend/modules/contacts/merge.py:108-251` | table, column, relation names | registry (`_references`); undo path validated since SEC-13 |
| `backend/modules/crm/services.py:169-172` | table | constant list `GENERIC_TABLES` |
| `backend/modules/crm/ai_insights.py:497`, `backend/modules/contacts/duplicates.py:287`, `backend/modules/calendar/ai_features.py:553` | `sql_filter(...)` fragment | record-rule SQL compiled by the ORM from the caller's rules (values bound) |
| `backend/modules/web/search.py:105` | `WHERE` fragment | compiled from the registry; query text bound |
| `backend/modules/studio/service.py:751-753` | table/column for removal | registry + validated Studio names |
| `backend/modules/studio/hooks.py:94` | column list | constant `COLUMNS` |
| `gateway/gateway/ledger.py` | column list | constants (`gateway/tests/test_sql.py`) |

Domains from clients are parsed into a node tree and compiled with bound parameters (`backend/tessera/orm/domain.py`, `backend/tessera/orm/sql.py`); field names in domains and `order` are checked against the model's fields.

## 5. Changelog paragraph and decisions

The changelog entry and the decision records (D-numbers) for this review are handed to the coordinator with the review report rather than written here (CHANGELOG.md and DECISIONS.md are maintained centrally). New settings introduced: `TESSERA_TRUSTED_PROXIES` (default: loopback and RFC 1918 ranges) and `TESSERA_OUTBOUND_ALLOW_PRIVATE` (default: allowed outside production only).
