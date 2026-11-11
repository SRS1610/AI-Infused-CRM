# Tax returns (`account_tax`)

`account_tax` adds the tax return report of each chart of accounts, the tax closing entry with the tax
lock date, tax return records and pre-filing checks. It depends on [`account_reports`](reports.md).

Menus: **Accounting → Reporting → Tax report** (the report of the company's chart), **Accounting →
Accounting → Tax returns** (one record per period).

## Grids and sign convention

The chart templates (`modules/account/data/charts/*.json`) put tax-report grids on the taxes' distribution
lines: `+NN` tags on invoices and `-NN` tags (with *tax negate*) on credit notes, for the base and for the
tax amount. Journal items carry these tags (`tax_tag_ids`).

A grid's amount is the sum, over the period's posted items carrying `+NN` or `-NN`, of

```
balance × direction × (−1 for a -NN tag)
```

where *direction* puts the item in its document's terms: −1 on customer invoices and vendor credit notes
(whose product lines are credits), +1 on vendor bills and customer credit notes; for journal entries, −1
when the item's tax is a sales tax. Result: invoices and bills count positively, credit notes reduce the
grid, and a discount line on an invoice reduces it too. Reverse charge uses a −100 % distribution line
(*VAT due*), whose amounts come out negative; the EU report shows grid 08 with its sign reversed.

## Reports shipped

One definition per chart template (`data/tax_reports.json`, variants of the code `tax`):

| Chart | Report | Lines |
|---|---|---|
| EU-style (`eu`) | VAT return | 01 taxable sales, 02 output VAT, 03 intra-EU supplies, 04 exports, 05 taxable purchases, 06 deductible VAT, 07 intra-EU acquisitions (reverse charge), 08 VAT due on them, 09 VAT deductible on them, 10 imports |
| US (`us`) | Sales tax return | taxable sales, exempt sales, state and local sales tax collected, purchases |
| Generic (`generic`) | Tax return | 01 taxable sales, 02 tax collected, 03 exempt sales, 04 taxable purchases, 05 deductible tax, 06 exempt purchases |

Every tax report has the lines `TAX_DUE`, `TAX_DEDUCTIBLE` and `NET` (= due − deductible). A country adds
its own return as data with `variant_of: "tax"` and its `chart_template`.

## Tie to the ledger

`NET` equals minus the balance movement of the tax accounts (the accounts of the taxes' tax distribution
lines) over the period, tax closing entries excluded (`modules.account_tax.closing.ledger_check`). It holds
as long as every tax item carries a grid; the pre-filing checks report items and taxes without grids and
any difference.

## Tax closing

*Close the period* on a tax return (or `account.tax_return.action_close_period(date_from, date_to,
company_id=None, force=False)`, advisers only):

1. refuses when taxes are already locked on or after the period's start, or a closed return overlaps it;
2. refuses when the report does not tie to the ledger, unless `force`;
3. posts one entry dated on the period's last day in the company's *tax closing journal* (default: the
   first miscellaneous journal): for each tax group, the balance of every tax account is cleared and the
   group's net goes to its *tax payable account* (tax owed) or *tax receivable account* (credit) — the
   accounts of `account.tax_group`; distribution lines with *use in tax closing* off are left alone;
4. sets the company's tax lock date to the period's end (taxed documents can no longer be posted, reset or
   cancelled in it — D-073), and stores the report snapshot, the totals, the closing entry, who closed and
   when on the return.

The periodicity (`account_tax_periodicity`: monthly or quarterly, calendar aligned) on the company gives
the default period of a new return: the one after the last return, else the last complete period.

## Pre-filing checks

*Run pre-filing checks* (or `modules.account_tax.prefiling.prefiling_checks(company, date_from, date_to)`)
returns findings with a stable `code`, a `severity`, a message, the records concerned (`model`,
`res_ids`) and details. The sub-phase 2.6 AI feature explains and ranks them; it does not compute them.

| Code | Severity | Finding |
|---|---|---|
| `tax_lines_without_grid` | error | posted tax items of the period without any grid |
| `reverse_charge_without_vat` | error | reverse-charge or intra-EU documents (−100 % distribution, or a fiscal position that requires a tax ID) whose partner has no tax ID |
| `report_ledger_mismatch` | error | the return does not tie to the tax accounts |
| `taxes_without_grids` | warning | taxes of the company whose base or tax distribution has no grid |
| `unusual_tax_for_country` | warning | customer invoice lines whose taxes are used by less than 20 % of the lines of the same customer country (5 lines at least) |
| `fiscal_position_mismatch` | warning | documents whose fiscal position differs from the one the partner's country and tax ID call for |
| `draft_entries` | warning | draft entries dated in the period (they would be locked out) |
| `period_locked` | info | taxes are already locked for the period |

### AI: ranked and explained pre-filing checks (`account_tax.prefiling`)

The *Pre-filing checks (AI)* panel of an open tax return (sub-phase 2.6) lists the findings above together with
two statistical outliers computed against the company's **previous four periods**:

| Code | Finding |
|---|---|
| `rate_outlier_for_country` | customer invoice lines whose tax rate was used by fewer than 10 % of the lines of customers of the same country and kind (with / without tax ID) in the previous periods (at least 4 such lines) |
| `grid_swing` | grids whose amount moved by more than 60 % of their median **and** 3.5 robust deviations (and at least 200), grids that appear from nothing or vanish; not computed for a company with fewer than two active previous periods |

Each item is ranked — severity first, then the amount at stake against the net tax (ledger difference, largest
grid change), then the number of documents — and comes with why it matters and what to do; the first five
documents are linked. **Brief me** asks the small-tier model for a five-sentence briefing from the ranked items;
a sentence quoting a figure that is not in them is removed. Nothing is written and closing is never blocked
(flags only). The feature has a per-company switch (*AI → Features*): switched off, the panel disappears and
`GET /api/v1/account_tax/ai/returns/{id}/prefiling` answers `404 feature_disabled`. Everything is read as the user
(tax returns: accountants of the return's company) and every review is an `ai_core.run`.

Limits: the rate check compares rates, not tax codes (two different 20 % taxes look alike); refunds count like
invoices; grid swings ignore seasonality beyond the four previous quarters (or months).

## Access

Tax returns: read for *Accounting / Read-only*, create and edit for accountants, delete for advisers;
closing a period is reserved to advisers. Returns belong to a company and follow multi-company rules.

## Limits

* Cash-basis taxes are not supported (`tax_cash_basis` is off in Phase 2).
* The closing entry does not create a payment or a vendor bill to the tax authority.
* No electronic filing; the snapshot is the record of what was declared.
