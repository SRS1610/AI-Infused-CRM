# Subscriptions — guide

The `sale_subscription` module (depends on `sale` and `payment`) bills customers every week, month or year:
recurring invoices charged on a saved card, upgrades and downgrades in the middle of a period with
proration, fixed terms with renewal quotations, pause, closing and reactivation, and MRR / churn analytics.

A subscription is a **sales order** (`sale.quotation`) with a **plan** and at least one **subscription
product**: there is no separate document, so pricelists, taxes, fiscal positions, payment terms, the
quotation email and the customer's online acceptance all work as for any order ([Sales guide](sale.md)).

| Menu | What you find |
|---|---|
| Sales → Subscriptions → Subscriptions | every subscription (list, kanban by status, form, pivot, graph) |
| Sales → Subscriptions → Fixed-term subscriptions | running subscriptions with an end date |
| Sales → Reporting → Subscription analysis | MRR movements by month and type (pivot, graph, list) |
| Sales → Reporting → MRR by plan | running subscriptions' MRR by plan and status |
| Sales → Configuration → Subscription plans / Subscription close reasons | sales administrators |

## Setting up

- **Products.** Tick *Subscription product* on the product form (*Sales* tab). Its price is the price **per
  billing period** of the plan it is sold with. Other products on a subscription (a set-up fee…) are
  invoiced once, with the first period.
- **Plans** (*Configuration → Subscription plans*): *Bill every* N weeks / months / years; *Close after
  (days unpaid)* (0 = never); *Retry a failed payment every (days)* (0 = no retry); *Customers can close*
  (from the portal); *Renewal quotation (days before the end)* for fixed terms (0 = none); company (empty =
  shared). *Monthly*, *Quarterly* and *Yearly* are installed.
- **Close reasons**: *Too expensive*, *Not satisfied with the quality*, *Moved to a competitor*, *No longer
  needed* (offered to customers on the portal), *Payment failure* and *Term ended without renewal* (set by
  the system).
- **Saved cards** come from the `payment` module (a customer who pays an invoice online can save their
  card); choose one in *Saved payment method* on the subscription. It must belong to the customer and to
  the subscription's company.

## Life cycle

| Status | Meaning |
|---|---|
| Not started | a quotation, or a confirmed order whose start date is in the future (e.g. a renewal) |
| In progress | invoiced every period |
| Paused | not invoiced; the periods that start while paused are skipped |
| Renewed | ended and replaced by its accepted renewal (not a churn) |
| Churned | closed for any other reason: the **churn flag** (`is_churned`) |

- **Start.** Create a quotation with a plan and subscription products (from *Subscriptions*, or from
  *Quotations* with the *Subscription plan* field) and confirm it (**Start**), or let the customer accept it
  online. The start date defaults to the confirmation date; a later start date starts the subscription that
  day (daily job).
- **Recurring invoices.** Every day the job *Subscriptions: invoices, charges, renewals* (03:17, also
  **Invoice due periods** on the form and `action_run_recurring` for administrators) invoices each period
  that has started: one posted invoice per period, dated on its first day, with the order lines'
  quantities, prices, discounts and taxes and the period in each description. It is then charged on the
  saved card. *Next invoice* moves to the next period. A period is never invoiced twice (a unique key per
  subscription and period); cancelling a period invoice allows invoicing it again. Several periods missed
  (the job did not run) are caught up in one run. Recurring lines are never invoiced by the regular
  *Create invoice*, and a subscription never locks.
- **Failed payments.** A declined charge leaves the invoice unpaid, counts a *Failed payment* and posts a
  message; it is retried every *Retry* days. When an invoice is still unpaid *Close after* days after its
  due date, the subscription is churned with the reason *Payment failure* on that day, without credit
  (collect the debt with payment reminders). This also applies to customers who pay by transfer.
- **Upsell / downgrade (mid-period change).** **Upsell** prepares a change order with the current lines.
  Change quantities, prices or discounts, add lines, set a quantity to 0 to stop a product (deleting a line
  from the change order leaves it unchanged), then confirm it (or send it for online acceptance). The
  subscription's lines become those of the change order for the next periods, and for the period already
  invoiced each changed line is billed `(new − old line amount) × remaining days / period days` — on one
  invoice when the total is positive (charged on the saved card), on one credit note when it is negative
  (matched with the open period invoice). Lines of the change order that are not subscription products
  are invoiced from the change order as usual.
- **Pause / Resume.** Pausing keeps the period already invoiced (no credit). Resuming continues on the
  original billing dates: the next invoice is the first period starting on or after the resume day.
- **Close** (reason and comment asked): *At the end of the invoiced period* sets the end date to the
  period's last day; nothing is credited and the subscription churns the next day. *Now*: the service
  stops today and today until the end of the period is credited (current lines × remaining days / period
  days) on a credit note matched with the period's invoice when it is still open. A paused subscription,
  or one whose current period is not invoiced, closes without credit.
- **Fixed terms and renewals.** With an *End date*, the last period is invoiced up to that day (prorated:
  covered days / period days). *Renewal quotation* days before the end, the job creates the renewal
  quotation (same lines and prices, the next term's dates — same number of months or years, else days —
  valid until the end date) and emails it with the online acceptance link; **Renew** does it by hand. You
  can still change its prices. Once accepted it starts the day after the end date (no gap) and the old
  subscription ends as *Renewed*. Not renewed (declined, expired or ignored): the subscription churns the
  day after its end date with *Term ended without renewal*. One automatic renewal quotation is sent per
  term. A renewal confirmed after the end counts as a reactivation.
- **Reactivate** a churned subscription: it is open-ended again and a new billing period starts today
  (the billing day becomes today).
- **Portal.** Customers see their subscriptions on *My documents*: status, next invoice, amount per
  period, saved card, lines and recent invoices (with their payment links). When the plan allows it they
  close their subscription themselves, with a reason, at the end of the invoiced period. The page also
  opens without signing in with the order's access token (`/portal/subscriptions/<id>?token=…`).

### Day-count rule

Billing periods are computed from the billing anchor (the start date): period *k* starts on *anchor + k
periods*, so a monthly subscription started on the 31st bills on the 31st, or on the last day of shorter
months (Jan 31, Feb 28, Mar 31, Apr 30…). A period runs from its start to the day before the next start.
Prorations count calendar days, both ends included: a change or closure effective on day *C* covers *C …
period end*, i.e. `remaining = period end − C + 1` out of the period's length (28–31 days for a month,
365/366 for a year, 7 for a week). Each prorated line is rounded to the cent.

## MRR and churn

- **MRR** (*recurring_monthly*) is the untaxed recurring amount per month: monthly plans ÷ months,
  yearly ÷ 12 per year, weekly × 52 / 12 per week; 0 once renewed or churned. *MRR (company currency)*
  converts it at today's rate.
- **MRR history** (*MRR history* tab, `sale.subscription_log`): every movement with its date, type and
  amount, in the order currency and in the company currency at the event date — *New* (start), *Upsell*
  / *Downsell* (changes), *Churn* (closing, with the reason), *Reactivation*, *Renewal* (the old
  subscription −its MRR, the renewal +its MRR on the same day). A subscription's movements add up to its
  MRR. Pausing does not change the MRR.
- **Subscription analysis** shows the movements by month and type (company currency).
- **KPIs** — `modules.sale_subscription.metrics.subscription_metrics(company, date_from, date_to)` (also
  `sale.subscription_log.subscription_metrics(date_from, date_to)` through the API): MRR and active
  subscriptions at both dates, ARR, new, expansion (upsells and net positive renewals), contraction,
  churn, reactivation, net change, customer churn rate (churned ÷ active at the start), revenue churn rate,
  net revenue retention, and the same per calendar month.
- **Churn features** — `churn_features(subscription, day)` returns tenure, MRR, plan and billing period,
  line, product and discount figures, fixed term and days to the end, renewals, upsell / downsell /
  reactivation counts and the MRR change over 90 days, pauses, failed payments, saved card, invoice /
  credit note / unpaid counts, overdue amount, days since the last paid invoice and customer emails. The
  churn prediction of sub-phase 2.6 uses them; no score is computed here.

## Access

Salespeople (*Sales / Own documents*) manage their own subscriptions and see their history; *All documents*
see every subscription; sales administrators configure plans and close reasons. Recurring, change and
credit documents are created by the subscription engine with system rights (the user needs write access
to the subscription). Everything is per company.

## Acceptance 2-f: the 20-scenario fixture

`backend/modules/sale_subscription/tests/test_scenarios.py` runs these scenarios through the real actions and
the daily job (monthly plans and a 100.00 product unless stated; no taxes unless stated) and asserts every
document (type, date, untaxed, total, period), the final status, the dates, the MRR and the MRR history.

| # | Scenario | Documents | Final state / MRR history |
|---|---|---|---|
| 1 | monthly, start Jan 31 | invoices Jan 31–Feb 27, Feb 28–Mar 30, Mar 31–Apr 29, Apr 30–May 30 (job run twice: no duplicate) | in progress, next May 31; new +100 |
| 2 | yearly 1,200 | 2026-03-01–2027-02-28, 2027-03-01–2028-02-29 | MRR 100; new +100 |
| 3 | weekly 30, job skipped a week | three weekly invoices, two in one run | MRR 130; new +130 |
| 4 | upsell 1 → 2 on Mar 16 | invoice 100; change invoice 51.61 (16/31); April 200 | upsell +100 |
| 5 | downgrade 3 → 1 on Apr 21 | invoice 300; credit note 66.67 (10/30), matched (233.33 left); May 100 | downsell −200 |
| 6 | add a 50 product May 11, price 100 → 80 May 21 | 100; +33.87 (21/31); credit 7.10 (11/31); June 130 | upsell +50, downsell −20 |
| 7 | pause Jan 20, resume Mar 10 | January and April only (Feb, Mar skipped) | in progress, next May 1 |
| 8 | close now Jun 21 (too expensive) | June 100; credit note 33.33 (10/30) | churned, end Jun 20; churn −100 |
| 9 | close at period end (Jul 10) | July 100 | churned Aug 1, end Jul 31 |
| 10 | Jan–Mar term, renewal 15 days before, price 110, accepted | 3 × 100; renewal: April 110 | old *Renewed* (renewal −100 Apr 1); renewal Apr–Jun, +110 |
| 11 | Feb–Apr term, renewal declined | 3 × 100 | churned May 1, *Term ended without renewal* |
| 12 | saved card | two invoices, paid | no failure |
| 13 | card declined, new card, retry after 3 days | one invoice, paid on Sep 4 | 1 failed payment |
| 14 | card always declined, close after 10 days | one unpaid invoice | 4 failures; churned Oct 11, *Payment failure* |
| 15 | close now Jan 11, reactivate Feb 15 | 100; credit 67.74 (21/31); Feb 15–Mar 14 100 | new, churn, reactivation |
| 16 | EUR pricelist, 90 EUR, rate 0.9 | one EUR invoice 90 | MRR 90 EUR, 100 USD in the company currency |
| 17 | 20 % tax included, 120, upsell 1 → 2 May 16 | 100/120; change 51.62/61.94; June 200/240 | upsell +100 |
| 18 | yearly 5 × 1,200, 8 from Jul 2 | 6,000; change 1,804.93 (183/365); 2027 9,600 | MRR 500 → 800 |
| 19 | 2 × 100 −25 %, then −10 % on Feb 15 | 150 (discount 25 % on the line); change 15.00 (14/28); March 180 | upsell +30 |
| 20 | end date Mar 15 | Jan, Feb 100; Mar 1–15 48.39 (15/31) | churned Mar 16 |

## AI: churn prediction and renewal outreach

Both features are switched per company in *Settings > AI* (on by default) and disappear from the form when off.

**Churn prediction** (`sale_subscription.churn_predict`). Every running subscription gets a churn score (0–100), a risk
level (*Low*, *Medium* from 35 %, *High* from 60 %) and its five main factors, computed by the daily job *Subscriptions:
churn prediction (AI, daily)* or with **Score now** in the *Churn and renewal (AI)* panel of the subscription form.
**Why this score?** lists the factors that raise or lower the risk, with the subscription's own values (*4 failed
payments*, *Customer for 30 months*…). The *At risk (AI)* filter of the subscription list shows medium and high risks;
the *Churn risk* column can be added to the list.

- *Model.* A logistic regression on the churn features above (tenure, MRR and its 90-day change, products, discount,
  fixed term, renewals, upgrades and downgrades, pauses, failed payments, saved card, unpaid and overdue invoices,
  customer e-mails, reactivations). A factor's weight is its exact contribution to the log-odds against the
  average subscription. The subscription's state, close reason and the customer's identity are never inputs.
- *Training.* Each night the company's own model is trained on its churned subscriptions and on those that were
  renewed or ran for at least 180 days, once there are 40 of them with at least 10 of each kind (the hold-out AUC is
  stored with the model in *sale.subscription_churn_model*). Until then documented starting weights are used (the
  panel says which).
- *Quality.* On a held-out synthetic history of 120 subscriptions (created as records and scored through the
  production extraction) the AUC is 0.86, with explanations for every score.
- *What it writes.* Only the score fields of the subscription. When the feature is switched off the job clears them.

**Personalised renewal outreach** (`sale_subscription.renewal_draft`). **Draft renewal** (running subscriptions) asks the
model for a renewal e-mail built from the subscription's plan, products, tenure, MRR history, end date, renewal
quotation and churn risk: appreciative when the risk is low, reassuring when medium, retention-minded when high.
Every number of the draft must be one of the records' (amounts, dates, counts): a sentence with another figure — a
recomputed yearly total, an invented discount, anything a customer name tries to slip in — is removed and listed
under the draft. The draft is a **message proposal**: nothing is posted or e-mailed until you edit it if needed and
click **Send**; only the person who asked can send it. The run is in the AI audit trail.

## Limits

- One plan per subscription; its billing period cannot change once started (close and create another).
- Invoices are dated on the period's first day; a period starting before a lock date cannot be invoiced
  (the job reports it and goes on with the other subscriptions).
- Editing the end date by hand does not credit days already invoiced (use *Close → Now*).
- Pausing keeps the MRR and the current period; there is no credit for a pause.
- Usage-based (metered) billing and plan-level price changes for all subscribers are not covered.
