# OWASP ASVS 4.0.3 Level 2 — Phase 2 delta review

Acceptance criterion **U-9** (`docs/architecture/BUILD_PLAN.md`): *OWASP ASVS L2 checklist delta for the phase reviewed; no high-severity finding open.* The Phase 1 review (`docs/security/asvs-phase-1.md`) covered the whole application; this review covers what Phase 2 added or changed since that review (`git log --stat 1a1fada..HEAD`): the modules `account`, `account_bank`, `payment`, `account_dunning`, `account_einvoice`, `account_analytic`, `account_budget`, `account_asset`, `account_reports`, `account_tax`, `sale` 2.0, `product` 2.0, `sale_subscription`, `sale_rental`, `sale_commission`, `auth_saml`, the Phase 2 AI features and their endpoints, and the kernel changes (`tessera/api/deps.py` login-path registration, `tessera/api/middleware.py`, `tessera/ai/runtime.py` attachments, `tessera/ai/facts.py`, `tessera/cli/account.py`). The BUILD_PLAN §11 Phase 2 security track — **payment adapters, SAML, accounting lock integrity**, and authorisation tests for every new model — is the focus.

Reviewed revision: the Phase 2 branch at the time of this document, plus the fixes listed below. Paths are relative to the repository root; line numbers refer to this revision. Regression tests named `test_sec5x_*` / `test_sec_phase2_*` are in `backend/tessera/api/tests/test_security_asvs.py` (after the Phase 1 tests).

## Summary

Delta rows only: requirements whose evidence changed with Phase 2 (new endpoints, models, file parsers, outbound calls, cryptography). Rows that Phase 1 assessed and Phase 2 does not touch keep their Phase 1 status.

| Chapter | Rows reviewed | Met | Partially met | Not met | Not applicable |
|---|---:|---:|---:|---:|---:|
| V1 Architecture, design and threat modelling | 6 | 5 | 1 | 0 | 0 |
| V2 Authentication (SAML) | 5 | 4 | 1 | 0 | 0 |
| V3 Session management (SAML, token links) | 3 | 2 | 1 | 0 | 0 |
| V4 Access control | 8 | 7 | 1 | 0 | 0 |
| V5 Validation, sanitisation and encoding | 12 | 12 | 0 | 0 | 0 |
| V6 Stored cryptography | 4 | 4 | 0 | 0 | 0 |
| V7 Error handling and logging | 3 | 3 | 0 | 0 | 0 |
| V8 Data protection | 4 | 3 | 1 | 0 | 0 |
| V9 Communications | 2 | 2 | 0 | 0 | 0 |
| V11 Business logic | 7 | 6 | 1 | 0 | 0 |
| V12 Files and resources | 7 | 6 | 1 | 0 | 0 |
| V13 API and web service | 4 | 4 | 0 | 0 | 0 |
| V14 Configuration | 3 | 3 | 0 | 0 | 0 |
| **Total** | **68** | **61** | **7** | **0** | **0** |

| Findings | High | Medium | Low | Total |
|---|---:|---:|---:|---:|
| Fixed in this review | 3 | 3 | 1 | 7 |
| **Open** | **0** | 0 | 6 | 6 |

**U-9 status: no high-severity finding is open.** Three high-severity findings were found and fixed, each with a regression test that fails on the code before the fix; the medium findings were small and were fixed too; the open findings are low and scheduled (section 2.2). None of the Phase 1 open findings (SEC-20 … SEC-41) was closed by Phase 2; their status is restated in section 2.3.

Severity scale (unchanged from Phase 1): **high** — an authenticated user of a lower role, or an anonymous user, can gain another user's or the administrator's rights, read or change data outside their permissions across a boundary that matters (other company, other user's records, an accounting invariant of D-073), or run script/read files on the server. **Medium** — weakens a control or needs a precondition (a stolen password, a privileged role, a specific deployment). **Low** — defence in depth, hardening, or a gap in a non-functional ASVS requirement.

## 1. How this was reviewed

- **Scope.** Every HTTP entry point added in Phase 2 (`backend/modules/{account,account_bank,account_reports,payment,sale,sale_subscription,sale_rental,auth_saml}/api.py`, `backend/modules/{account,sale,sale_subscription,sale_rental}/ai_api.py`); every method exposed to the generic API (`@api.expose`, 95 methods in the Phase 2 modules, listed with an AST scan for those that elevate or lack a write check); the access rights and record rules of every new model (`backend/modules/*/security/*.json`); the write paths of the ledger (`account.move`, `account.move_line`, `account.partial_reconcile`, `account.full_reconcile`, `account.payment`) through REST, JSON-RPC, x2many commands and `default_*` context keys; the payment adapters (Stripe, test) and webhook processing; the SAML implementation against D-076; the file parsers (CAMT.053, OFX, MT940, CSV statements; inbound UBL; OCR uploads; pypdf); report exports (CSV, XLSX, PDF); e-mails built in Phase 2 code; the AI features' endpoints and prompts (spot checks); raw SQL added in Phase 2 (section 4).
- **Method.** Manual review against the ASVS rows below, plus exploratory tests run as low-privilege users (billing clerk, read-only auditor, plain internal user, user of another company) on the test database, through the ORM and the HTTP API. Every finding was reproduced before being classified (e.g. SEC-50: a billing clerk's `PATCH /api/v1/models/account.move/{id}` with `{"payment_state": "paid"}` returned 200 and the invoice showed *Paid*; SEC-52: a user without any accounting group closed a fiscal year).
- **Verification.** Each fixed finding has a regression test; the seven `test_sec5x_*` tests were run against the code before the fixes and all failed, then pass. On the fixed code ruff and mypy pass and the full backend suite passes on a freshly built database (1311 tests). Two existing tests needed attention: `modules/account/tests/test_payments.py::test_credit_note_reversal_modes` revealed that copying journal items copied their matches (fixed, see SEC-50), and a subscription scenario that rewrote a saved card's reference as a user now does it as the provider would (elevated).
- **Out of scope / not reviewed.** Third-party library internals (signxml, lxml, pypdf, openpyxl, WeasyPrint), Stripe's own security, the Peppol access point (mocked in Phase 2), infrastructure outside the repository, and AI evaluation quality (covered by `docs/ai/governance.md` and the eval suites).
- **Statuses.** *Met* requires a pointer to code or a test; *partially met* names what is missing and links the finding.

## 2. Findings

### 2.1 Fixed in this review

#### SEC-50 — The ledger's own fields could be set through the generic API (high)

- **Issue.** The ledger refused `state`/`name` writes on entries, but every other field it manages was open to anyone with write access to journal entries (*Accounting / Billing*): `readonly=True` is a client hint only. A billing clerk could (a) create an entry already `posted`, or a draft with a `name` of their choice that posting then kept — bypassing gapless numbering, lock dates and the hash chain (D-073); (b) write `payment_state: "paid"` and `amount_residual: 0` on a posted invoice (both were in `POSTED_WRITABLE` for the engine's sake) — hiding a receivable without a payment; (c) write `reconciled`, `amount_residual*`, `full_reconcile_id` or `parent_state: "draft"` on posted journal items (also in `POSTED_LINE_WRITABLE`), taking items out of reports and aged balances; (d) create, change or delete `account.partial_reconcile` / `account.full_reconcile` records directly (billing has full rights on them); (e) the same values passed as `default_<field>` context keys (allowed by D-067's allow-list) reached `create`. Same pattern on `account.payment` (`state`, `name`, `is_matched`, `is_reconciled`, `reconciled_invoice_ids`).
- **Fix.** Server-managed fields are listed (`SERVER_FIELDS`, `SERVER_AMOUNTS`, `SERVER_LINE_FIELDS` in `backend/modules/account/models/move.py:80-129`; `PAYMENT_SERVER_FIELDS` in `backend/modules/account/models/payment.py:25-32`). Outside the ledger's own calls (`INTERNAL` context, which a client cannot send) and elevated server code, `create` refuses them unless they hold their initial value — explicit or through a `default_*` context key (`_changed_server_fields`) — and `write` refuses them outright (`move.py:263,304,936-962`, `payment.py:247-258`). Amounts and residuals are recomputed after a create, so creation drops them; `copy()` keeps working. Matches are made by the reconciliation engine only (`backend/modules/account/models/reconcile.py:12-72`). The guard also exposed an integrity bug: copying journal items copied their matches (`matched_debit_ids`, `matched_credit_ids`, `full_reconcile_id` were copyable), so the draft copy made by a *modify* reversal carried duplicated partial matches of the original; these fields are now `copy=False` (`move.py:902-907`).
- **Regression test.** `test_sec50_ledger_fields_cannot_be_set_through_the_api` (HTTP: REST create/patch, JSON-RPC create with context defaults and write, journal items, a hand-made match), `test_sec51_payment_cannot_cancel_another_entry` (payment fields)

#### SEC-51 — A payment could cancel any journal entry, of any company (high)

- **Issue.** `account.payment.move_id` was writable by billing users and `action_cancel` cancelled `pay.move_id` **elevated**. A clerk created a draft payment with `move_id` = any posted entry id — of their company or another one — and called `action_cancel`: the entry was cancelled (reproduced on an invoice of the EUR company by a user of the US company).
- **Fix.** `move_id` is a server field (SEC-50), and `action_cancel` refuses an entry the payment did not book (its items must carry the payment, `backend/modules/account/models/payment.py:235-239`).
- **Regression test.** `test_sec51_payment_cannot_cancel_another_entry`

#### SEC-52 — Exposed actions ran privileged writes for users who could only read the record (high)

- **Issue.** The generic API requires read access before an exposed method runs (SEC-03); methods that then write **elevated** must check more themselves. Five did not: `account.fiscal_year.action_close` (every internal user can read fiscal years; closing set the company's lock date through an elevated write that skips the advisers-only lock-date guard — any user could lock the books), `payment.token.action_charge` (an auditor with *Accounting / Read-only* could charge a customer's saved card any amount), `account.bank_statement.action_auto_reconcile` and `account.bank_statement_line.action_auto_reconcile_all` (an auditor could match bank transactions, which re-posts entries and pays invoices), `account.move.action_send` (an auditor could e-mail any invoice and its access link to any address; stores a PDF and a chatter message elevated), `account.move.action_generate_ubl` and `account.edi_document.action_retry` (an auditor could create e-invoice documents and resend invoices through the access point).
- **Fix.** Closing a fiscal year needs *Accounting / Adviser* (`backend/modules/account/models/journal.py:151-157`); the other methods check write access on their records (`backend/modules/payment/models/provider.py:147`, `backend/modules/account_bank/models/statement.py:149,391-393`, `backend/modules/account/models/move.py:841`, `backend/modules/account_einvoice/models/edi.py:111,158`). Every other exposed method of the Phase 2 modules was checked: it writes as the caller (the ORM's rights apply), checks write access or a group itself, or only reads.
- **Regression test.** `test_sec52_privileged_actions_need_the_right_role`

#### SEC-53 — Billing users could rewrite payment transactions and saved payment methods (medium)

- **Issue.** *Accounting / Billing* has write access to `payment.transaction` and `payment.token` (for refunds and archiving). Every transaction field (amount, state, invoices, provider reference, payment) and the token's customer could be rewritten: a clerk could mark a transaction *Confirmed*, change the amount the provider's report is checked against, point the provider reference at another checkout session (which `action_sync` then fetches from Stripe), or move a customer's saved card to another customer. Medium: needs the billing role, and the money stays with the company.
- **Fix.** Transactions are written by `modules.payment.service` only (elevated); users' writes are refused (`backend/modules/payment/models/transaction.py:87-92`). Users may only rename or archive a saved method (`backend/modules/payment/models/provider.py:124-131`).
- **Regression test.** `test_sec53_payment_records_are_provider_managed`

#### SEC-54 — The test payment provider worked in production (medium)

- **Issue.** The *test* provider settles every checkout immediately without money, and its webhooks are signed with a published default secret when none is configured (`backend/modules/payment/providers/testing.py:23`). An adviser switching it on in production (the demo switches it on in every company) let any customer mark their invoices paid, and anyone forge its webhooks. Medium: needs a configuration mistake.
- **Fix.** In production the adapter refuses the test provider and it is never offered to customers (`backend/modules/payment/providers/__init__.py:38-45`, `backend/modules/payment/models/provider.py:92-106`).
- **Regression test.** `test_sec54_test_payment_provider_is_refused_in_production`

#### SEC-55 — Report exports handed formulas to spreadsheets (medium)

- **Issue.** CSV and XLSX exports of the financial reports wrote line labels (partner names, journal item labels, references — from users, imported bank statements, received e-invoices, OCR) as they were; openpyxl stores a string starting with `=` as a formula. A partner named `=HYPERLINK(…)` became a live formula in the accountant's spreadsheet (CSV injection). The generic export already neutralised formulas (`backend/tessera/dataio/exporter.py:90`).
- **Fix.** Text starting with `=`, `+`, `-`, `@`, tab or CR is prefixed with `'` in both formats (`backend/modules/account_reports/export.py:51-56`); numbers are unchanged.
- **Regression test.** `test_sec55_report_exports_neutralise_formulas`

#### SEC-56 — Customer names were inserted as HTML into invoice and quotation e-mails (low)

- **Issue.** `send_invoice` and `sale.quotation.action_send` built the e-mail body with f-strings (`mail_queue.body_html` is not sanitised: "rendered by trusted templates"); a partner name containing markup (a link) went into the e-mail sent to customers. Dunning letters already escaped their values.
- **Fix.** Names, numbers and links are escaped (`backend/modules/account/sending.py:52-56,72`, `backend/modules/sale/models/quotation.py:413-417,441`).
- **Regression test.** `test_sec56_invoice_email_escapes_names`

### 2.2 Open

None is high or medium severity. Each states why it is low and when it is planned.

| ID | Severity | Planned | Finding | Why low / decision |
|---|---|---|---|---|
| SEC-57 | low | Phase 3 | **Refunds and saved-card charges are open to every billing user.** `payment.transaction.action_refund` and `payment.token.action_charge` need write access only, which *Accounting / Billing* has; there is no second approval and no per-user limit (11.1.3, 4.3.3). | A refund goes back to the card that paid and cannot exceed what is left of the payment; a charge pays the company. Billing users are trusted accounting staff. Decision for Phase 3: refunds and off-session charges for accountants, with an optional approval above an amount. |
| SEC-58 | low | Phase 3 | **Stripe providers in *Test mode* are offered to customers in production.** A customer paying with a Stripe test card gets the invoice marked *Paid* (test keys move no money) (14.1.3). | Needs an adviser to configure test keys in production; the payment page shows "no real payment". Phase 3: in production, test-mode providers are shown to internal users only. |
| SEC-59 | low | Phase 3 | **Statement files sent as `data_file` through the generic API are not size-capped.** Uploads through `/attachments` are capped at 50 MB, but a JSON create of `account.bank_statement_import` with a base64 file is bounded only by the request body (12.1.1). | Accountants only; the parsers stream line by line (CSV, MT940) or use libxml2's limits (CAMT, OFX: no entities, amplification refused). Extends Phase 1 SEC-31 (upload hygiene, Phase 3). |
| SEC-60 | low | Phase 3 | **A few configuration children have no company.** `account.fiscal_position_tax`, `account.fiscal_position_account`, `account.payment_term_line` and `account.full_reconcile` carry no `company_id`, so their rows (tax and account mappings, installment rules, matching numbers) of other companies are readable by any internal user with a direct query (4.2.1). | No amounts or partners; the parents and the matched items are company-scoped. `test_sec_phase2_every_company_model_is_isolated` lists them explicitly so a new company-specific model without `company_id` fails. Phase 3: related stored `company_id` on these children. |
| SEC-61 | low | Phase 3 | **Customer links never expire and billing users can set the token.** Invoice, quotation and subscription access tokens (192 bits, constant-time, rate-limited) are valid until the document is cancelled; `account.move.access_token` stays writable on posted invoices (3.3.1, 8.3.1). | Same design as Phase 1 (SEC-30); a weak token can only be set by someone who can already read the invoice. Phase 3 with SEC-30: expiry and *regenerate link*, token not writable. |
| SEC-62 | low | Phase 3 | **Internal users with read access can close a customer's subscription from the customer endpoint.** `POST /sale/subscriptions/{id}/close` checks read access for internal users (meant for portal users and token links); the back-office `action_close` checks write (4.2.1). | Groups that read subscriptions without write access are rare (sales users have write on their own); the closing is audited and reversible (`action_reactivate`). Phase 3: write check for internal callers. |

Also noted, not findings: SAML and OIDC sign-ins do not ask the local second factor (the identity provider is trusted for authentication strength, D-076, `backend/modules/auth_saml/api.py:101-104`); `trust_email` linking can link any existing account, administrators included — it is off by default and its help text says to enable it only for one's own directory.

### 2.3 Phase 1 open findings

None was closed in Phase 2: SEC-20 … SEC-41 (`docs/security/asvs-phase-1.md` §2.2) keep their status and are carried to Phase 3, except SEC-31 (already Phase 3). SEC-20 (business models check record rules before a write only) is narrowed for the ledger: SEC-50 makes the engine-managed fields of entries, items, matches and payments unwritable by users whatever their record rules.

## 3. Checklist (delta)

Requirements are ASVS 4.0.3 identifiers. *Finding* links the open or fixed finding related to the row.

### V1 Architecture, design and threat modelling

| # | Requirement | Status | Evidence | Finding |
|---|---|---|---|---|
| 1.1.2 | Threat modelling for design changes | partially met | Ledger invariants and their threats in D-073 and `docs/admin/accounting.md`; payment and SAML threats in `docs/admin/payments.md`, `docs/admin/saml.md`, D-076; no consolidated threat model (Phase 1 SEC-41) | SEC-41 |
| 1.4.1 | Access control enforced at a trusted service layer | met | Phase 2 routers check ORM access before elevating: `backend/modules/account/api.py:22-41`, `backend/modules/sale/api.py:22-41`, `backend/modules/sale_subscription/api.py:28-48`, `backend/modules/payment/api.py:51-120`, `backend/modules/account/ai_api.py:38-43`, `backend/modules/sale/ai_api.py:26-29`, `backend/modules/account_bank/api.py:27-35`, `backend/modules/account_reports/engine.py:374-394` | SEC-52 |
| 1.4.4 | Single access control mechanism | met | Phase 2 models use ACLs, record rules and the automatic multi-company rule (stored `company_id`); `test_sec_phase2_every_company_model_is_isolated` | SEC-60 |
| 1.5.3 | Input validation on a trusted layer | met | Server-managed ledger fields refused in the ORM, not in the client (SEC-50) `backend/modules/account/models/move.py:80-129` | SEC-50 |
| 1.6.2 | Consumers of crypto services protect key material | met | Payment secrets and SAML SP key via the platform encryption (`backend/modules/payment/models/provider.py:22-28`, `backend/modules/auth_saml/models/provider.py:86-91`) | — |
| 1.14.1 | Segregation of components of differing trust | met | Payment provider calls only from the server with the encrypted key (`backend/modules/payment/providers/stripe.py:89-99`); IdP certificates configured, never taken from the message (`backend/modules/auth_saml/saml.py:370-401`) | — |

### V2 Authentication (SAML)

| # | Requirement | Status | Evidence | Finding |
|---|---|---|---|---|
| 2.2.1 | Anti-automation | met | SAML consumer shares the per-address login limit (`register_login_path`, `backend/modules/auth_saml/api.py:33`, `backend/tessera/api/middleware.py:174-181`) | — |
| 2.5.4 | No shared or default accounts | met | SAML sign-up creates internal users with `core.group_user` only, off by default (`backend/modules/auth_saml/service.py:182-203`) | — |
| 2.10.3 | Service credentials protected | met | Stripe secret key and webhook secret encrypted, readable by advisers only (`backend/modules/payment/models/provider.py:60-61`); `modules/payment/tests/test_flow.py::test_providers_installed_per_company_and_secrets_encrypted` | — |
| 2.10.4 | No secrets in source | met | `git grep` for live/test Stripe keys, webhook secrets, private keys and cloud keys: none (the test provider's documented default secret is refused in production, SEC-54) | SEC-54 |
| 2.7.x / federation | Federated assertions validated | partially met | Signature against configured certificates, SHA-1/HMAC/DSA refused, exactly one assertion, only the signed element read, issuer, audience, recipient, destination, InResponseTo bound to a browser cookie, single-use requests, replay cache, no IdP-initiated login (`backend/modules/auth_saml/saml.py:370-587`, `backend/modules/auth_saml/service.py:73-128`; `modules/auth_saml/tests/`); the local second factor is not asked after a federated login (D-076) | — |

### V3 Session management (SAML, token links)

| # | Requirement | Status | Evidence | Finding |
|---|---|---|---|---|
| 3.2.1 | New session on authentication | met | SAML login creates a fresh session like password and OIDC logins (`_finish_login`, `backend/modules/auth_saml/api.py:101-104`) | — |
| 3.4.x | Cookie attributes | met | SAML binding cookie HttpOnly, path-scoped, `SameSite=None; Secure` only with secure cookies, 10 minutes (`backend/modules/auth_saml/api.py:48-58`) | — |
| 3.3.1 | Tokens invalidated / expire | partially met | Customer access tokens do not expire (Phase 1 SEC-30) | SEC-61 |

### V4 Access control

| # | Requirement | Status | Evidence | Finding |
|---|---|---|---|---|
| 4.1.1 | Access control enforced server-side | met | Exposed methods that elevate check write access or a group (SEC-52); AST scan of the 95 exposed Phase 2 methods (section 1) | SEC-52 |
| 4.1.2 | Attributes used by access control not manipulable | met | Client context allow-list (D-067) keeps `INTERNAL`-style keys (`_account_internal`, `_sale_internal`, `_bank_internal`, `commission_internal`, `asset_internal`) out (`backend/tessera/api/routers/records.py:53-71`); `default_*` keys cannot set ledger fields (SEC-50) | SEC-50 |
| 4.1.3 | Least privilege | met | Read-only accounting users cannot charge cards, match bank lines, send or resend invoices (SEC-52); billing users cannot rewrite transactions or tokens (SEC-53); lock dates and year closing for advisers (`backend/modules/account/models/journal.py:151-157,211-216`) | SEC-57 |
| 4.1.5 | Access controls fail securely | met | Exposed-method checks raise before any write; API requests roll back on error | — |
| 4.2.1 | IDOR / record-level access | partially met | Multi-company rule on every company-specific Phase 2 model (`test_sec_phase2_every_company_model_is_isolated`); cross-company references refused: journal / partner on entries (`backend/modules/account/models/move.py:336-352`), accounts at posting (`:588-597`), bank matching items and write-off accounts (`backend/modules/account_bank/matching.py:53-75`), reports' companies (`backend/modules/account_reports/engine.py:382-394`), payment provider journal (`backend/modules/payment/models/provider.py:67-74`); payment journal of another company refused (`register_payments` → record rule); a payment cannot cancel another entry (SEC-51); portal rules: posted invoices and sent quotations of the user's commercial partner (`backend/modules/account/security/access.json`, `backend/modules/sale/security/`); config children without company (SEC-60) | SEC-51, SEC-60, SEC-62 |
| 4.2.2 | CSRF | met | SAML consumer exempt by exact path pattern only and never uses an existing session (`backend/tessera/api/deps.py:32-46`, `backend/modules/auth_saml/api.py:34`) | — |
| 4.3.2 | Directory browsing | met | No static serving added | — |
| 4.3.3 | Additional authorisation for high-value operations | met | Fiscal-year closing and lock dates for advisers; tax closing (`action_close_period`) for advisers (`backend/modules/account_tax/models/tax_return.py:173-180`); discount approvals by band approvers (`backend/modules/sale/models/discount_band.py:148-163`); subscription job for sales administrators | SEC-57 |

### V5 Validation, sanitisation and encoding

| # | Requirement | Status | Evidence | Finding |
|---|---|---|---|---|
| 5.1.2 | Mass assignment protection | met | Ledger and payment server fields (SEC-50), transactions and tokens (SEC-53); quotation `state` guarded by the strict workflow (`backend/modules/sale/data/workflow.json`), locked orders (`backend/modules/sale/models/quotation.py:270-306`); pricing flags read-only for sales users; AI bill acceptance filters fields (`MOVE_FIELDS`/`LINE_FIELDS`, `backend/modules/account/ai_bill.py:49-60,344-372`) | SEC-50, SEC-53 |
| 5.1.3 | Positive input validation | met | Pydantic bodies on Phase 2 routers (e.g. `ImportIn` format pattern `backend/modules/account_bank/api.py:18-23`, `ReportParams`), ORM field validation | — |
| 5.1.5 | Redirects to allow-listed destinations | met | Payment return URLs built from `TESSERA_PUBLIC_URL` (`backend/modules/payment/api.py:93-97`); Stripe returns its own checkout URL; SAML RelayState is a random value bound to the request, the consumer redirects to `/` only (`backend/modules/auth_saml/api.py:103`) | — |
| 5.2.1 | Untrusted HTML sanitised | met | Dunning messages escape values (`backend/modules/account_dunning/dunning.py:75-109`); invoice / quotation e-mails escape names (SEC-56) | SEC-56 |
| 5.2.3 | E-mail injection | met | `EmailMessage` refuses CR/LF in headers (`backend/modules/core/models/mail.py:129-150`); recipients are passed as values | — |
| 5.2.6 | SSRF | met | Stripe base URL constant (`backend/modules/payment/providers/stripe.py:31`); Peppol access point mocked; no user-supplied URL fetched by Phase 2 code | — |
| 5.3.1 | Context-aware output encoding | met | Report CSV/XLSX formula neutralisation (SEC-55); PDF templates autoescaped (Phase 1) | SEC-55 |
| 5.3.4 | Parameterised queries | met | Section 4 | — |
| 5.3.10 | XML injection | met | Outgoing UBL built with lxml element APIs (`backend/modules/account_einvoice/ubl.py:62-70`); SAML requests and metadata built with lxml (`backend/modules/auth_saml/saml.py:237-326`) | — |
| 5.5.1 | Serialised objects integrity-checked | met | AI proposals stay HMAC-signed (Phase 1); Stripe webhooks HMAC-SHA256 with a 5-minute tolerance, constant-time (`backend/modules/payment/providers/stripe.py:38-60`; `modules/payment/tests/test_stripe.py`) | — |
| 5.5.2 | XML parsers restrict XXE | met | Statements `safe_xml` (`backend/modules/account_bank/importers/base.py:136-142`), UBL import (`backend/modules/account_einvoice/ubl_import.py:41-46`), validator (`backend/modules/account_einvoice/validator.py:52-56`), SAML (`backend/modules/auth_saml/saml.py:329-356`, DTDs refused outright): no entity resolution, no network; checked on this revision that an external entity is not expanded and a billion-laughs document is refused ("Maximum entity amplification factor exceeded") | — |
| 5.5.3 | No deserialisation of untrusted data | met | JSON only; ML artefacts (churn, demand) stored as JSON | — |

### V6 Stored cryptography

| # | Requirement | Status | Evidence | Finding |
|---|---|---|---|---|
| 6.2.2 | Industry-proven algorithms | met | SHA-256 hash chain (`backend/modules/account/models/move.py:816-826`), HMAC-SHA256 webhooks, RSA-3072 SP key, SHA-2 only for SAML | — |
| 6.2.8 | Constant-time comparisons | met | Access tokens `hmac.compare_digest` (`backend/modules/account/api.py:31`, `backend/modules/sale/api.py:31`, `backend/modules/sale_subscription/api.py:36`), webhook signatures (`stripe.py:56`), SAML relay hash (`backend/modules/auth_saml/service.py:104`) | — |
| 6.3.1 | CSPRNG | met | `secrets.token_urlsafe(24)` access tokens (`move.py:606`, `quotation.py:389`), SAML ids 160 bits (`saml.py:224-226`), transaction references | — |
| 8.3.7 / 6.4 | Secrets encrypted at rest | met | Stripe keys, webhook secrets, saved-method references, SAML SP key (`enc:v1:`, `backend/modules/payment/models/provider.py:22-28,116-121`) | — |

### V7 Error handling and logging

| # | Requirement | Status | Evidence | Finding |
|---|---|---|---|---|
| 7.1.1 | No credentials logged | met | Webhook refusals log the provider id and reason only (`backend/modules/payment/api.py:134-136`); SAML logs reason codes, never assertions (`backend/modules/auth_saml/api.py:86-112`) | — |
| 7.1.3 | Security-relevant events logged | met | SAML successes and refusals with reason and address; lock-date changes audited (`backend/modules/account/models/journal.py:211-216`, companies are `_audit`); payment events recorded (`payment.event`) | — |
| 7.4.1 | Generic error messages | met | Provider errors shown as translated messages; webhook signature failures return `Invalid signature` | — |

### V8 Data protection

| # | Requirement | Status | Evidence | Finding |
|---|---|---|---|---|
| 8.1.1 | Sensitive data not cached | met | API `Cache-Control: no-store` (Phase 1) covers the new routers | — |
| 8.3.1 | Sensitive data not in URLs | partially met | Customer links carry their token in the query string (Phase 1 SEC-30) | SEC-61 |
| 8.3.4 | Sensitive data identified | met | Cost prices used for computation only, never in AI prompts or messages (`backend/modules/sale/ai_pricing.py:14-16,271`, `backend/modules/sale/ai_discount.py:127-136`) | — |
| 8.3.5 / AI | No data of other users or companies in prompts | met | AI endpoints check record access before running (`backend/modules/account/ai_api.py:38-43`, `backend/modules/sale/ai_api.py:26-29`, `backend/modules/sale_subscription/ai_api.py:28-31`); reports' AI uses `run_report` with checked companies; proposals still require the requester's acceptance before any write (bill reading: `backend/modules/account/ai_bill.py:375-418`) | — |

### V9 Communications

| # | Requirement | Status | Evidence | Finding |
|---|---|---|---|---|
| 9.2.1 | Outbound TLS verified | met | Stripe calls with httpx defaults (`backend/modules/payment/providers/stripe.py:89-99`) | — |
| 9.2.3 | Outbound connections authenticated | met | Stripe basic auth with the encrypted key; idempotency keys on checkout, charges and refunds | — |

### V11 Business logic

| # | Requirement | Status | Evidence | Finding |
|---|---|---|---|---|
| 11.1.1 | Flows in order | met | Posting, reset, cancel only through actions (SEC-50); payments booked only from provider events (`backend/modules/payment/service.py:126-198`); transactions never leave a final state | SEC-50 |
| 11.1.2 | Human-time limits on public links | met | Token links rate-limited per address (60/min: invoices, quotes, subscriptions; 20/min: payment start) | — |
| 11.1.3 | Business limits enforced | partially met | Refund ≤ what is left of the payment (`backend/modules/payment/service.py:302-312`); amount and currency of provider reports checked against the transaction (`:168-175`); refunds and charges not limited per role | SEC-57 |
| 11.1.6 | No TOCTOU on sensitive operations | met | Gapless numbering with `SELECT … FOR UPDATE` (`backend/modules/core/models/sequence.py:62-66`); hash chain under an advisory lock per journal (`move.py:816-820`); webhook idempotency `UNIQUE (provider_id, event_id)` with a savepoint (`backend/modules/payment/service.py:126-152`); SAML requests used once (`UPDATE … WHERE used IS NOT TRUE`) and assertions in a unique replay cache (`backend/modules/auth_saml/service.py:105-124`) | — |
| ledger | Posted entries immutable, balanced, locked periods | met | Read-only posted entries except narrative and engine fields; engine fields engine-only (SEC-50); deferred balance trigger and `CHECK` constraints (D-073, `backend/modules/account/hooks.py`); lock dates on post / reset / cancel (`move.py:534-547`); `tessera account integrity` (`backend/tessera/cli/account.py`) | SEC-50 |
| amount tampering | Amount charged comes from the server | met | Transaction amount = sum of the invoices' residuals (`backend/modules/payment/service.py:46-90`); Stripe checkout uses it (`stripe.py:128-155`); users cannot change it (SEC-53) | SEC-53 |
| test mode | Test settlement not reachable in production | met | SEC-54 | SEC-54 |

### V12 Files and resources

| # | Requirement | Status | Evidence | Finding |
|---|---|---|---|---|
| 12.1.1 | Large files refused | partially met | OCR uploads 15 MB (`backend/modules/account/ai_api.py:79-81`), SAML responses 512 KB (`saml.py:79`), attachments 50 MB (Phase 1); statement `data_file` through JSON not capped | SEC-59 |
| 12.1.2 | Compressed content / decompression bombs | met | pypdf 7 output caps (75 MB per stream) and 20 pages read (`backend/modules/account/ai_ocr.py:25,51-56`); images are not decoded on the server (sent to the vision model); XML entity amplification refused | — |
| 12.2.1 | File type from content | met | OCR type from magic bytes, not the browser's type (`backend/modules/account/ai_bill.py:65-75`) | — |
| 12.3.1 | User filenames not used as paths | met | Content-addressed storage (Phase 1); statement and e-invoice attachments named from the upload or transmission id only | — |
| 12.3.4 | Reflective file download | met | Report exports: fixed `<code>_<date>.<ext>` names (`backend/modules/account_reports/export.py:71-75`) | — |
| 12.5.2 | Uploaded files never executed | met | Imported XML, UBL and OCR sources stored as attachments, served as downloads (D-067) | — |
| 12.6.1 | Outbound requests allow-listed | met | Stripe API host only | — |

### V13 API and web service

| # | Requirement | Status | Evidence | Finding |
|---|---|---|---|---|
| 13.1.4 | Authorisation at URI and resource level | met | Every new router depends on `authenticated` (internal), `authenticated_any` (portal list) or checks the token; exposed methods checked (SEC-52) | SEC-52 |
| 13.1.5 | Unexpected content types rejected | met | JSON bodies; SAML consumer form fields with length limits (`backend/modules/auth_saml/api.py:77-83`); webhook body parsed after the signature check | — |
| 13.2.1 | Only intended methods | met | Explicit routes | — |
| 13.2.6 | Headers not trusted for security decisions | met | Webhooks authenticated by signature, not by origin | — |

### V14 Configuration

| # | Requirement | Status | Evidence | Finding |
|---|---|---|---|---|
| 14.1.3 | Server configuration hardened | met | Test payment provider refused in production (SEC-54); `/metrics` needs `TESSERA_METRICS_TOKEN` in production (D-068, `backend/tessera/api/routers/health.py:47-60`) | SEC-54, SEC-58 |
| 14.2.1 | Components up to date | met | New dependencies pinned in `backend/uv.lock` (signxml, lxml, pypdf, openpyxl, zxing-cpp) | — |
| 14.4.2 | Content-Disposition on file responses | met | Invoice and quotation PDFs, report exports (`backend/modules/account/api.py:86-89`, `backend/modules/sale/api.py:89-94`, `backend/modules/account_reports/api.py:115-125`) | — |

## 4. Raw SQL review (Phase 2 delta)

Every Phase 2 SQL string built with an f-string was traced; interpolated values are identifiers or fragments from constants or the registry, never request data:

| Location | Interpolated values | Source |
|---|---|---|
| `backend/modules/account_reports/engine.py:241-250,288,300-360` | `WHERE` fragments, date conditions | constant column names with alias `l`; values (`states`, `journal_ids`, `partner_ids`, dates, company ids) bound |
| `backend/modules/account_reports/ledgers.py:40-56,183-199,312-345,443-510` | grouping key, join, `extra`, currency factor, bucket `CASE` | constants chosen by `group`; factors `int()`/`float()`-cast; partner table from the registry |
| `backend/modules/account_tax/prefiling.py:95-128` | partner table | registry (`_table`) |
| `backend/modules/product/models/product.py:377` | table and column assignments | registry (variant shared fields) |
| `backend/modules/sale/taxmap.py:132-141` | line table | constant (migration) |
| `backend/modules/account/ai_eval.py:50` | table | fixed list (evaluation fixtures) |
| `backend/modules/account/models/move.py:816-822`, `backend/modules/auth_saml/service.py:105-124` | — | static SQL, bound parameters |

## 5. Changelog paragraph and decisions

The changelog bullet is under *Sub-phase 2.7 — Closing* in `CHANGELOG.md`; the decision record for this review is handed to the coordinator with the review report (DECISIONS.md is maintained centrally). No new setting.
