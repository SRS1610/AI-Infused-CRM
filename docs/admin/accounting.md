# Accounting (`account`)

Invoicing and the general ledger: chart of accounts, journals, invoices and bills, taxes, payments,
reconciliation and multi-currency. Data model: [ERD_PHASE_2.md §3](../architecture/ERD_PHASE_2.md);
decisions D-072, D-073 and D-077.

## Setting up a company

Installing `account` gives every existing company a **chart of accounts** that matches its country, and
every company created afterwards gets one too:

| Template | Used for | Taxes | Fiscal positions |
|---|---|---|---|
| `us` | United States | state 6 %, local 2.5 %, combined 8.5 % (group), exempt | resale certificate; export |
| `eu` | EU member states | VAT 20 / 10 / 5.5 / 0 %, price-included 20 %, reverse charge on EU purchases | intra-EU business (reverse charge, needs a tax ID); outside the EU |
| `generic` | everyone else | sales and purchase tax 10 %, exempt | export |

A template creates accounts, tax groups, taxes with their tax-report grids, fiscal positions, journals
(*Customer invoices* `INV`, *Vendor bills* `BILL`, *Bank* `BNK1`, *Cash* `CSH1`, *Miscellaneous* `MISC`,
*Exchange differences* `EXCH`) and the company's default accounts. From the command line:

```bash
uv run tessera account charts
```

```bash
uv run tessera account install-chart "My Company (Europe)" --chart eu
```

Records created by a chart are found in code as `env.ref("account.<company id>_<template id>")`, for
example `account.2_sale_vat_20`.

## Invoices, bills and journal entries

*Accounting → Customers → Invoices* (and *Credit notes*), *Vendors → Bills* (and *Refunds*),
*Accounting → Journal entries*.

- Each line has a product, an account (from the product, else the journal), a quantity, a price, a
  discount and taxes. Tax lines and the receivable / payable installments are rebuilt automatically from
  the lines whenever a draft changes, so a saved invoice is always a balanced entry.
- **Confirm** posts the invoice: it gets its number (`INV/2026/00001`, credit notes `RINV/2026/00001`)
  from a gapless sequence per journal and fiscal year. Drafts that are deleted never use a number.
- A posted entry cannot be edited or deleted (only its reference, notes and reconciliation change).
  **Reset to draft** reopens it unless its period is locked or its journal chains hashes; **Cancel** keeps
  its number; **Credit note** creates a draft credit note, or a posted one matched with the invoice, or that
  plus a new draft to correct.
- **Send by email** attaches the PDF and a link the customer opens without an account
  (`/portal/invoices/<id>?token=…`). Portal users see their company's posted invoices.

## Taxes and fiscal positions

A tax is a percentage, a fixed amount per unit, a "percentage of the tax-included price" or a group of
taxes. It can be included in the price and can increase the base of the taxes after it. Its
*Distribution* lists, for invoices and for credit notes, where the base and the tax go (account and
tax-report grid); reverse charge is a +100 % line on deductible tax and a -100 % line on tax due.

A fiscal position replaces taxes and accounts for some partners. It is chosen from the partner's
*Fiscal position* field, otherwise automatically from the partner's country and tax ID (positions marked
*Detect automatically*, in sequence order; *Except countries* excludes countries).

## Payment terms

Installments as a percentage, a fixed amount or the balance, due a number of days after the invoice date,
after the end of the month, or on a given day of the next month. **Early-payment discount**: paid within N
days, the customer may pay X % less; with *On early payment (taxes adjusted)* the tax is reduced in
proportion.

## Payments and reconciliation

**Register payment** on an invoice (or several selected invoices: one payment per partner) asks for an
amount (empty = everything due) and a date. Less than the amount due leaves the invoice *Partially paid*;
a write-off account closes a small difference.

A journal with *Payments wait for bank matching* books payments on the outstanding receipts / payments
accounts; the invoice shows *In payment* until a bank statement line matches the payment (bank module).
Otherwise payments go straight to the bank account and the invoice is *Paid*.

Journal items on receivable, payable and other reconcilable accounts are matched oldest first. Items in
the same foreign currency are matched in that currency; the difference in company currency is posted as a
**realised exchange gain or loss** in the *Exchange differences* journal.

## Multi-currency

Invoices can be in any active currency; amounts are converted at the company's rate on the invoice date
(company-specific rates override shared ones: *Settings → Currencies*). On the first day of each month the
scheduled job *Unrealised exchange differences* revalues open foreign-currency receivables, payables and
foreign bank accounts at the month-end rate, and reverses the entry the next day.

## Locking and integrity

*Settings → Companies → Accounting*:

| Lock date | Effect |
|---|---|
| Lock date for everyone | nothing can be posted, reset or cancelled on or before it |
| Lock date for non-advisers | the same, except for *Accounting / Adviser* users |
| Tax lock date | entries with taxes cannot be posted on or before it |

Only advisers change lock dates; every change is in the audit log. Closing a fiscal year
(*Configuration → Fiscal years → Close the year*, advisers only) sets the lock date to its last day. Fiscal years may run
across calendar years: a July-June year ending in 2027 numbers entries `…/2027/…`.

The database itself refuses to commit a posted entry that does not balance. The fields the ledger manages
(state, number, payment status, amounts and residuals, matching, hash) are set by the posting, payment and
reconciliation actions only: the API refuses them in a user's values (security review SEC-50,
[asvs-phase-2.md](../security/asvs-phase-2.md)). Entries in the sales and
purchase journals are chained with SHA-256; to verify every chain:

```bash
uv run tessera account integrity
```

## Access

| Group | Can |
|---|---|
| Accounting / Billing | create, post, send and collect invoices and bills; register payments |
| Accounting / Read-only | see every accounting document (auditors) |
| Accounting / Accountant | billing + journal entries and journal items |
| Accounting / Adviser | everything, configuration, lock dates |

Every internal user can read taxes, accounts, journals, payment terms and fiscal positions (sales people
choose taxes on quotations). Records are separated by company.

## AI features

Three AI features ship with `account` (sub-phase 2.6). Each has a per-company switch (*AI → Features*);
switched off, its buttons and panels disappear and its API answers `404 feature_disabled` without calling
the AI gateway. Everything they read is read **as the user** (access rights, record rules, companies).
Their evaluations are described in [docs/ai/governance.md](../ai/governance.md#accounting-account).

### Reading bills and invoices from documents (`account.bill_ocr`)

*Vendors → Read from documents* (and *Customers → Read from documents*), the **Read a document** button in
the search bar of invoice and bill lists, or **Read from a document** on an empty draft: upload a PDF or a
photo / scan (PNG, JPEG, WebP; 15 MB at most).

1. **Reading.** A PDF's text layer is read without a model, in English, French, German and Spanish
   (labels, number formats such as `1 234,56` / `1.234,56` / `1,234.56`, dates such as `14/03/2026`,
   `14. März 2026`, `March 14, 2026`). A line is only accepted when quantity × unit price equals its amount.
   When the reading is incomplete or does not add up, the text is sent to the model; a scan or an image-only
   PDF is sent as an image to the **vision** route (task class `vision`). Images cannot be redacted, so the
   feature declares `redaction: minimal`: it is only routed to providers approved for personal data.
2. **Partner.** Matched among the partners the user can read: VAT ID, then IBAN (bank accounts on file),
   then name. An unknown issuer is proposed for creation (with its IBAN) together with the bill. A document
   issued by the company itself is read as a customer invoice (the partner is the customer); a credit note
   as a refund.
3. **Coding.** Each line gets an account and taxes, in this order of trust: a product whose reference or
   name appears on the line; the partner's **posted** documents (the most similar past line, else the
   partner's usual account); keywords of the chart in four languages (rent, software, travel, bank fees,
   commissions, goods, general expenses; German compound words included); the company's default account.
   The printed rate must exist among the company's taxes for that use; a 0 % line on a document that
   mentions reverse charge gets the company's self-assessed tax; VAT charged by a vendor of another country
   is not the company's deductible VAT, so the line gets no tax and the reading is marked for review.
4. **Checks.** Lines must add up to the untaxed amount, the taxes computed by the tax engine on the coded
   lines to the printed tax, and both to the total. Any difference, a missing field, an unknown rate or an
   amount the model read that does not appear in the text layer marks the reading **Needs review**, with
   each difference; the draft is then created *To check*.
5. **Anomalies.** The anomaly checks below run on the reading before the bill exists (possible duplicate,
   changed bank account…) and are shown as warnings.
6. **Proposal.** All of it — partner, header and **every auto-coded line** — is one proposal. Nothing is
   written until the user clicks **Create the draft** (accounts can be changed per line first: *Accept
   edited*); then the draft is created as that user, the document is attached to it and its text is kept
   for duplicate detection. Rejecting leaves nothing behind but the audit trail. The draft is posted the
   usual way.

Every reading is audited in `ai_core.run`, including those done without a model (provider `tessera`,
model `bill-parser`).

**Limits.** Multi-page tables are read page by page; descriptions printed on two lines keep their first
line; the layout heuristics were tuned on the synthetic evaluation corpus (four layouts) and real documents
will more often need the model or a correction. Credit notes are recognised by their title. A document
with several VAT IDs of other companies (factoring, agents) may be matched to the wrong one: check the
partner.

### Anomaly flags on bills (`account.anomaly`)

After a vendor bill is created or posted, a background job checks it (also **Check again** in the
*Anomaly checks* panel of the bill):

| Flag | When |
|---|---|
| Same invoice number | another bill of the vendor has the same number, also written differently (`INV-0042` / `inv 42`) or with characters a scan confuses (`O`/`0`, `I`/`1`…) |
| Same amount and date | same amount within 7 days |
| Nearly identical document | the document texts are ≥ 85 % identical (word shingles) with the same amount, within 10 days or with the same number |
| Unusual amount | robust z-score ≥ 3.5 against the vendor's last bills (median / MAD, at least 4 bills) and at least 50 % away from the median; or under a fifth of the median with z ≤ −2 |
| First bill of a new vendor, large amount | no earlier bill and an amount above the 90th percentile of the company's bills of the last year (5,000 while the company has fewer than 10 bills) |
| Bank account changed recently | a bank account of the vendor was added or changed in the last 30 days although it was already billing, or the bill shows an IBAN that is not on file |

Each flag carries its explanation (with the similar document) and the bill's responsible user is notified.
Flags **never block posting**. *Not an anomaly* dismisses a flag (it is not raised again for the same
document); *Confirm the problem* keeps it for the audit trail. **Explain** asks the model for a plain-words
summary of the open flags; an answer quoting a number that is not in the flags is replaced by the flags
themselves. *Vendors → Anomaly flags* lists the open flags of the company.

### Cash-flow forecast (`account.cashflow_forecast`)

*Accounting → Reporting → Cash-flow forecast* shows the expected cash position for the next 4, 8, 13 or 26
weeks, as a chart (with a range) and a table:

- today's balance of the bank and cash accounts (and of the outstanding receipts / payments accounts, where
  registered payments wait for their bank statement);
- open invoices and bills by due date, shifted by each partner's usual payment delay (median of its last
  six payments; the company's median otherwise); receivables more than 90 days overdue count for half;
- invoices and bills **not issued yet**: the daily amount issued over the last 13 weeks, spread with the
  observed issue-to-payment delays;
- recurring bank movements not linked to an invoice (salaries, rent, subscriptions: monthly, stable amount
  and day) on their usual day; the remaining movements as their weekly average, whose variability gives the
  range.

**Explain the forecast** asks the model for a short commentary of these figures. Every number in it is
checked against the forecast: a commentary with another number is replaced by a summary of the figures
(labelled *From the figures*). The forecast does not see bank statement lines that are not yet booked,
taxes not yet invoiced, or loans; it assumes the company pays its own bills when they are due as before.
