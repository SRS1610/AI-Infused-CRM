# Budgets (`account_budget`)

Budgets per period on analytic accounts or financial accounts, with budget-vs-actual. Depends on
[analytic accounting](analytic.md). Data model: [ERD_PHASE_2.md §6](../architecture/ERD_PHASE_2.md).

## Budgets and lines

*Accounting → Analytic → Budgets*. A budget has a name, a period, a responsible, a company and a state:
*Draft* → *Confirmed* (needs lines) → *Done* (closed), or *Cancelled* (and back to draft). Only draft and
cancelled budgets can be deleted; only draft and confirmed budgets can be changed.

Each **line** has a label, a **type** (*Expense* or *Revenue*), a period within the budget's (the budget's
by default), a **planned amount**, and what it measures:

| Line target | Actual amount |
|---|---|
| Analytic account | its analytic items in the period (optionally only those on the line's financial accounts or account type) |
| Financial accounts and/or an account type, no analytic account | posted journal items on those accounts in the period |

## Amounts and signs

Planned and actual amounts are **positive for the line's nature**: an expense line's actual is the costs
booked (debit - credit, or minus the analytic amount), a revenue line's actual is the income booked.

| Column | Meaning |
|---|---|
| Actual | as above, in company currency |
| Theoretical | planned × share of the line's period elapsed (0 before it starts, the planned amount once over) |
| Achievement % | actual / planned × 100 |
| Variance | actual - planned: positive is over budget for an expense line, above target for a revenue line |

These amounts are stored so the *Budget vs actual* list, pivot (analytic account × month) and graph can
group them. They are refreshed when a line changes, when analytic items or posted journal items they cover
are created, changed or removed (whoever posts them), with *Refresh actuals*, and every night by the
*Budget actuals and theoretical amounts* scheduled job (`account_budget.refresh`), which moves the
theoretical amounts with the date. Cancelled and done budgets are no longer refreshed.

## Monthly split

*Split per month* (on a budget, or on one line) replaces each line spanning several months by one line
per month. The planned amount is shared pro rata of the days of each month, rounded on the cumulative
amount so the months add up exactly: 1 000.00 from 16 January to 15 April gives 177.78, 311.11, 344.44
and 166.67.

## Variance report

`modules.account_budget.services.variance_report(budget, as_of=None)` returns the structured
budget-vs-actual of a budget, recomputed live: per line the planned, actual, theoretical amounts, variance,
variance %, achievement %, elapsed share and whether it is favourable; totals per line type and the net
result. The plain-language variance explanations of sub-phase 2.6 must use only these numbers.

## AI: variance explanations (`account_budget.variance_explain`)

Sub-phase 2.6. *Explain variance (AI)* beside a budget → **Explain variance** (with an optional focus such as
"travel") asks the mid-tier model for at most five sentences: revenue and costs separately, the lines that matter
and whether each is ahead of or behind the time elapsed. The model only sees `variance_report` (recomputed live,
as the user); **every figure it writes must be one of the report's** — a sentence with another number is removed
and listed. Nothing is written. Per-company switch: switched off, the panel disappears and the endpoint answers
`404 feature_disabled` without calling the gateway.

## Access

Accountants manage budgets; read-only users see them. Budgets belong to one company and are visible only to
its users.

## Limits

- One currency (the company's); no multi-company consolidated budgets.
- A line targets one account type at most (several accounts are possible).
