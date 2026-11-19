# Analytic accounting (`account_analytic`)

Cost centres, projects, departments: analytic plans and accounts, analytic distributions on invoice lines
and journal items, and the analytic items posting creates. Data model:
[ERD_PHASE_2.md §6](../architecture/ERD_PHASE_2.md). Budgets built on it: [budgets.md](budgets.md).

## Plans and analytic accounts

*Accounting → Analytic → Analytic plans* and *Analytic accounts*.

- A **plan** groups analytic accounts of one dimension (*Departments*, *Projects*, *Cost centres*…). A
  plan without company is shared by every company; its accounts can still belong to one company.
- An **analytic account** has a reference (code, unique per plan and company), a name, an optional
  customer and company, and shows its **revenue**, **costs** and **balance** computed from its analytic
  items. An account with analytic items cannot be deleted: archive it.

## Distributions

A journal item's **analytic distribution** is a JSON object mapping analytic account ids to percentages,
for example `{"12": 60, "14": 40, "21": 100}`: 60 % to account 12 and 40 % to account 14 of the
*Departments* plan, and 100 % to account 21 of the *Projects* plan.

- Each percentage is greater than 0 and at most 100; the percentages of one plan add up to at most 100
  (a line may be only partly allocated on a plan). Accounts must exist and belong to the line's company
  (or to no company).
- The distribution is a column of the invoice lines (*Invoices* and *Bills* forms), of the journal items
  of an entry, and an optional column of *Journal items*.
- It stays editable after posting (D-073 allows analytic data on posted items): the analytic items of the
  line are then regenerated. Every other field of a posted item stays read-only.
- Credit notes and reversals keep the distribution of the lines they reverse.

## Analytic items

Posting an entry creates one **analytic item** per (journal item, analytic account): date, label,
amount, quantity share, analytic account and plan, financial account, partner, product, journal item and
entry. **Sign convention: `amount = -balance × percent / 100` in company currency**, so revenue is
positive and costs are negative — a 1 000.00 sale with 60 % on *Sales* gives +600.00, a 300.00 bill on
*Marketing* gives -300.00. Resetting an entry to draft or cancelling it deletes its analytic items;
posting it again recreates them.

Analytic items can also be entered by hand (an accountant recording internal costs, for instance).

*Accounting → Analytic → Analytic items* lists them; *Analytic report* opens the pivot by analytic account
and month (and a graph). Accountants see every menu; read-only users (auditors) see the report and the
analytic accounts.

## Distribution models

*Accounting → Analytic → Distribution models* prefill distributions. When an invoice, bill or journal
entry line is created **without** a distribution, the models matching it propose one:

| Criterion | Matches |
|---|---|
| Partner | the document's partner (or its commercial entity) |
| Product | the line's product |
| Product category | the product's category or one of its parents |
| Account code prefix | the line's account (for invoices: the account the line will get) |
| Company | the document's company (empty = every company) |

Every criterion a model sets must match. The most specific model (most criteria) wins, then the lower
sequence; a less specific model still fills the plans the better ones leave empty. A line created with an
explicit distribution — even an empty one — is left alone.

For other modules and the AI features: `modules.account_analytic.services.distribution_for(env,
line_vals)` returns the proposed distribution (`line_vals` with `partner_id`, `product_id`, `account_id`
or `account_code`, `company_id`), `allocation_context(env, company)` lists the plans and accounts a
suggestion may use, and `validate_distribution(env, distribution, company)` checks a proposal.

## Mandatory plans

A plan's **applicability** is *optional* or *mandatory*. The plan's default applies unless a rule on the
*Applicability* tab matches the journal item; the most specific matching rule wins (account code prefix
counts more than account type or journal). Example: *Departments* mandatory for account type
*Expenses*.

When a plan is mandatory for a product line (or a journal entry item), posting is refused until the line's
distribution covers that plan at 100 %, and a posted line's distribution cannot be emptied.

## AI: allocation suggestions (`account_analytic.allocation_suggest`)

Sub-phase 2.6. On an invoice or a bill, *Analytic suggestions (AI)* → **Suggest analytic distributions** proposes a
distribution for each line:

1. the **distribution models** that apply (`distribution_for`) keep the plans they cover — they are the company's
   policy;
2. for the other plans, the nine most similar **posted invoice and bill lines of the last two years that the user can
   read** vote for the distribution they used. Similarity: same product 3, same partner 2, same account 1.5, label
   words 2 × Jaccard; lines older than a year count 0.7; each vote weighs the cube of the similarity, so a few close
   lines outweigh many loose ones. The winner needs at least two lines behind it and 40 % of the votes.

Each suggestion is a **proposal** on the line's `analytic_distribution`: nothing changes before *Accept*, and
accepting goes through the ledger's checks (mandatory plans, analytic items of posted lines regenerated). Read-only
accountants see suggestions without proposals. The feature has a per-company switch (*AI → Features*): switched
off, the panel disappears and `POST /api/v1/account_analytic/ai/moves/{id}/suggest` (or `/lines/{id}/suggest`)
answers `404 feature_disabled`. Every request is an `ai_core.run`.

**Metric.** The evaluation counts a suggestion as right only when the whole distribution — every analytic account
and every percentage — equals the expected one (*exact-distribution accuracy*, threshold 0.8): a split that is only
partly right still needs a manual correction. The per-plan accuracy is reported for information.

Limits: a vendor or customer never seen gets no suggestion beyond the models (cold start); a new kind of purchase
from a known vendor gets the vendor's usual allocation.

## Access

| Group | Plans, accounts, models | Analytic items |
|---|---|---|
| Billing | read (to set distributions on invoices and bills) | — |
| Read-only | read | read |
| Accountant | create, edit, delete | create, edit, delete |

Plans, accounts, models and items with a company are visible only to users of that company.

## Limits

- Distributions are not on sales order lines yet; invoices created from orders get the distribution
  models' proposal.
- Analytic amounts are in company currency only.
- The distribution is edited as JSON in the generic client (a dedicated widget comes with the AI
  allocation suggestions in sub-phase 2.6).
