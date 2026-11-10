# Financial reports (`account_reports`)

`account_reports` computes the financial statements live from journal items: general ledger, trial
balance, profit and loss, balance sheet, cash flow statement, aged receivable and payable, partner ledger,
and their consolidated multi-company versions. Every figure is aggregated in PostgreSQL (one grouped query
per company, period and kind of figure), never by loading journal items one by one.

Menu: **Accounting → Reporting** (users with *Accounting / Read-only* or more). Definitions:
**Accounting → Configuration → Financial reports** (advisers).

## Filters

Every report takes the same *report parameters* (`ReportParams`, `modules/account_reports/params.py`):

| Parameter | Meaning |
|---|---|
| `report` | report code: `balance_sheet`, `profit_loss`, `cash_flow`, `trial_balance`, `general_ledger`, `partner_ledger`, `aged_receivable`, `aged_payable`, `tax` (see [Tax returns](tax-return.md)) |
| `date_from`, `date_to` | period (range reports) or as-of date (balance sheet, aged balances). `date_to` defaults to today, `date_from` to the start of the fiscal year of `date_to` (`account.fiscal_year.year_of`; calendar year when no fiscal year is defined) |
| `comparison`, `periods` | `previous_period` or `previous_year`, 1 to 12 extra columns. A period made of whole months shifts by that many months, any other period by its number of days; an as-of date moves back one month (or year) per column. Line reports and the trial balance only |
| `journal_ids`, `partner_ids` | restrict to these journals / partners (journal item level) |
| `company_ids`, `currency` | one company, or several for a consolidation in `currency` (default: the first company's) |
| `include_draft` | count draft entries too (default: posted only) |
| `account_type` | partner ledger: `receivable`, `payable` or `all` |
| `unfold`, `unfold_all`, `expand`, `offset`, `limit` | line ids to unfold; `expand` + `offset` return the next page of items of one unfolded line (`limit` items per line, 80 by default) |
| `hide_zero` | hide account rows that are zero in every column (default on) |

The 2.6 natural-language reports feature only produces these parameters (`ReportParams.model_json_schema()`,
`GET /api/v1/account/reports/schema`); `run_report(env, params)` computes the numbers.

## Reports

**Balance sheet** (as of a date). Assets are shown with their balance, liabilities and equity with the
opposite sign. There is no closing entry (D-077): equity has two computed lines —

* *Current year earnings*: the result of the profit and loss accounts (and any posting on the
  *current year earnings* account) from the start of the fiscal year of the report date to that date;
* *Retained earnings*: the balance of the equity accounts plus the result of every earlier fiscal year.

So assets = liabilities + equity at any date, and *current year earnings* equals the profit and loss's
*net income* for the fiscal year to date (acceptance criterion 2-a).

**Profit and loss** (range): revenue, cost of revenue, gross profit, operating expenses, depreciation,
operating income, other income and expenses, net income.

**Cash flow statement** (range) — *direct method*. For every entry that touches a bank or cash account
(type *Bank and cash*), the other items of the entry are the counterpart of the money movement: their
balance, sign reversed, is classified by account type — receipts from customers (receivable, income),
payments to suppliers and employees (payable, expenses), other operating flows (other current assets and
liabilities such as taxes, prepayments, credit cards, other income), investing (fixed and non-current
assets), financing (non-current liabilities, equity) and the effect of exchange rate changes (the company's
exchange gain / loss accounts, e.g. the revaluation of a foreign-currency bank account). Transfers between
bank accounts net to zero. By construction, cash at the beginning + net change = cash at the end.

**Trial balance** (range): per account, the opening balance, the period's debit and credit and the closing
balance. Profit and loss accounts start at zero at the beginning of the fiscal year of the period's start;
the results of earlier years appear on *Undistributed earnings of previous years*, so every column sums to
zero (debits equal credits).

**General ledger** (range): per account, debit, credit and closing balance; unfolding an account lists its
initial balance and its items with a running balance, 80 at a time (*Load more…*).

**Partner ledger** (range): the same per partner, over receivable and / or payable accounts.

**Aged receivable / payable** (as of a date): open items per partner by days past due (due date, else
the item's date): not due, 1-30, 31-60, 61-90, 91-120, older. The residual is computed *as of the date*:
an item's balance minus its matches whose latest matched item is dated on or before the date (the
`max_date` of the partial reconciliation), so an invoice paid after the date counts as open. Approximation:
a match that involves an exchange difference entry dated after the date counts from that entry's date.
Payables are shown positive.

## Definitions

Reports are data (`data/reports.json`): an `account.report` with an engine (`lines`, or one of the built-in
ledgers above) and, for line reports, a tree of `account.report_line`. A line has a `code`, an
`expression`, a date scope, a sign, and flags (unfold by account, hide if zero, consolidation only, style).
Expressions are sums of signed terms:

| Term | Value |
|---|---|
| `type(asset_cash, …)` | balance of the items on accounts of these types |
| `code(10, 11)` | balance of the items on accounts whose code starts with these prefixes |
| `tag(Name, …)` | balance of the items on accounts with these account tags |
| `grid(01, …)` | tax-report grid amount (see [Tax returns](tax-return.md)) |
| `cash(type, …)` | cash-flow counterpart amounts (see above); `cash(exchange)` for the exchange accounts |
| `line(CODE)` | another line of the report |
| `children` | the sum of the line's children |

A term can override the line's date scope with `@scope`: `period` (the column's dates), `as_of`
(everything up to the column's end), `opening` (before its start), `fy_to_date` (from the start of the
fiscal year of the column's end), `before_fy` (before that start). Example — retained earnings:
`type(equity) + type(income, income_other, expense, expense_direct_cost, expense_depreciation,
equity_unaffected)@before_fy` with sign *reversed*.

A country module adds its own report as data; `variant_of` + `chart_template` make it the version of a
generic code (`tax`) for the companies using that chart. *Check the definition* reports `line()` references
that do not resolve; a reference cycle is refused when the report runs.

## Consolidation

With several companies, each company is computed in its own currency and converted into the report
currency with the shared rate table (rates without a company):

* balance-sheet amounts at the **closing rate** of the column's end (opening balances: the day before the
  period);
* profit and loss amounts and flows (cash flow, tax grids) at the **average rate** of their period — the
  daily rate averaged over the days of the period;
* equity and earlier years' results at the **historical rate, approximated by the closing rate**.

The difference goes to *Currency translation adjustment* (balance sheet, trial balance) and *Currency
translation difference* (cash flow), lines that are only computed in a consolidation, so the consolidated
balance sheet balances and the consolidated trial balance sums to zero. Accounts are merged by code across
companies. The general ledger, partner ledger and aged balances convert every amount at the closing rate
of the report date. Consolidation does not eliminate intercompany balances.

## API and exports

* `GET /api/v1/account/reports` — the reports available (the `tax` variant of the current company's chart).
* `GET /api/v1/account/reports/{code}?date_to=2026-03-31&comparison=previous_year&company_ids=1,2&unfold=line:12`
  — lists are comma-separated. The answer has `columns`, `lines` (flat, with `parent_id` and `level`),
  `domains` per value (the journal items behind the figure, as a domain for
  `/api/v1/models/account.move_line`), `move_id` on items, `next_offset` on *Load more* lines.
* `POST /api/v1/account/reports/run` with a `ReportParams` body.
* `GET /api/v1/account/reports/{code}/export?format=xlsx|csv|pdf` — every line unfolded, up to 20,000
  items per ledger line (a warning says when more exist). PDFs use the company's report layout
  (`reports` module) and switch to landscape for wide reports.

The web client's report page (client action `account_reports.viewer`, `context: {"report": code}`) has
the filters bar, comparison columns, expandable lines, drill-down to journal items with links to their
entries, and export buttons.

## Access

Running a report needs *Accounting / Read-only* (accountants and advisers have it) and is limited to the
user's allowed companies. Report definitions are readable by read-only users and maintained by advisers.

## Performance

`tessera perf reports` fills a company up to 100,000 posted journal items and times the main reports
through the API; see [Performance](performance.md). No index beyond the `account` module's
(`account_id, date`, `company_id, date`, partner, reconciliation) was needed.

## Limits

* Cash flow by the direct method only; no indirect presentation.
* Consolidation converts and adds; no intercompany eliminations, minority interests or ownership
  percentages.
* Analytic and budget filters come with `account_analytic` / `account_budget`.
