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
(*Configuration → Fiscal years → Close the year*) sets the lock date to its last day. Fiscal years may run
across calendar years: a July-June year ending in 2027 numbers entries `…/2027/…`.

The database itself refuses to commit a posted entry that does not balance. Entries in the sales and
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
