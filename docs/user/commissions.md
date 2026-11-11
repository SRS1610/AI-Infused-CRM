# Commissions — guide

The **sale_commission** module pays salespeople a commission on what they invoice (or on what their customers
pay): **plans** define the rates, targets and accelerators; **statements** list, per salesperson and period, every
invoice and credit note line that counted and what it earned; a confirmed statement is paid with a **vendor bill**
to the salesperson. Install it with `tessera module install sale_commission` (it depends on `sale`, `account` and
`crm`, whose sales teams can be assigned to a plan).

| Menu | Who | What |
|---|---|---|
| Sales → Commissions → My commissions | every salesperson | their own statements, line by line |
| Sales → Commissions → Statements | sales administrators, billing | every statement: check, confirm, pay |
| Sales → Configuration → Commission plans | sales administrators | plans, rates, accelerators, salespeople |

Salespeople see only their own statements and lines (record rules); sales administrators and users with
*Accounting / Billing* see all of them. Plans and statements belong to a company and are only seen in it.

## Plans

- **Period** — monthly or quarterly. **Basis** — *Invoiced*: invoices and credit notes count in the period they are
  posted; *Paid*: an invoice counts in the period it becomes fully paid (see below).
- **Target per period** — the untaxed amount each salesperson should reach (0: no accelerators). An assignment can
  give some salespeople their **own target**.
- **Rates** — each rule applies to *all products*, a *product category* (and its sub-categories) or a *product*,
  with a **rate %** of the untaxed amount and an optional **fixed amount per unit**.
- **Accelerators** — bands *from achievement %* → *multiplier*, e.g. from 0 % × 0.8, from 80 % × 1, from 100 % × 1.2.
- **Salespeople** — a salesperson or a sales team (all its members), with optional *from* / *until* dates. A
  salesperson is in at most one active plan of a company at a time.
- **Commission expense account** — the account of the commission bills (default: *Sales commissions*, 6400).

## How a statement is computed

For a salesperson, a plan and a period:

1. **What counts.** The product lines of posted customer invoices and credit notes of the plan's company whose
   **salesperson** is that person — the invoice's *Salesperson* field (filled from the sales order's salesperson,
   and still editable on a posted invoice to re-attribute it); for a credit note made from an invoice, the original
   invoice's salesperson. The line counts on its **date**:
   - *Invoiced* basis: the invoice's or credit note's accounting date — a credit note reduces the commission of the
     period it is posted in, even if the invoice was in an earlier period;
   - *Paid* basis: an invoice on the day it became **fully paid** (the latest payment matched with it; partly paid
     invoices wait); a credit note on its own date when the invoice it credits was fully paid by then (the commission
     had been earned), or when it credits no invoice; otherwise the credit note is ignored (its invoice never earned
     anything).

   The date must be in the period and the salesperson assigned to the plan on that date. Drafts and cancelled
   documents never count.
2. **Base** — the line's untaxed amount after discount, in company currency (negative for credit notes).
3. **Rate** — the most specific rule: the product's, else its category's or the nearest parent category's, else
   *all products* (equal rules: the lowest sequence). A line without a rule earns nothing but still counts toward the
   target. **Before accelerator** = base × rate / 100 + quantity × fixed amount (minus for credit notes).
4. **Achievement** = total base of the period / target × 100. The accelerator is the band with the highest *from
   achievement %* that is not above it — a band includes its threshold, so exactly 100 % gets the 100 % band — and it
   applies to the **whole** period's commission (retroactive). No band or no target: × 1.
5. **Commission** of each line = before accelerator × multiplier, rounded to the cent (half away from zero, exact
   decimal arithmetic); the statement's commission is the sum of its lines; **Total** = commission + manual
   **adjustments**.

Example (the demo plan, 50,000 target): Hugo invoices 51,816.25 net of a credit note → 103.63 % → × 1.2; a gripper
line of 3,806.25 at 3 % earns 114.1875 → × 1.2 = 137.025 → **137.03**.

## Statements

- **Compute statements** on a plan (choose a day of the period; empty: the last complete period) creates the
  statements of the period for every salesperson with something counted, or recomputes the drafts. **Recompute** on a
  draft statement gives the same result as long as the ledger is unchanged, and picks up new invoices otherwise.
- **Adjustments** (with a reason) can be entered on drafts.
- **Confirm** freezes the statement (*Back to draft* is possible until it has a bill).
- **Create the bill** (needs *Accounting / Billing*) creates a draft vendor bill to the salesperson's contact — one
  bill per salesperson, one line per statement, on the commission expense account, without tax. A statement whose
  total is zero or negative is not billed: carry it to the next period as an adjustment.
- The statement becomes **Paid** when its bill is paid; it goes back to *Confirmed* if the payment is undone, and
  loses its bill if the bill is cancelled.

`statement_facts(statement)` (in `modules.sale_commission.engine`) returns the statement's numbers — base per
category, rates, target, achievement, band and multiplier, credit notes, totals — as structured data: the AI
explanation of a later release may only cite these numbers.

## Cross-check (acceptance criterion 2-k)

The demo plan (*Sales commissions (demo)*) pays Hugo Lambert and Nadia Keller (logins `hugo` and `nadia`, password
`demo1234`). `tessera seed demo --phase 2` creates last month's invoices — two of them from sales orders —, a credit
note made by reversing an invoice, a separate credit note, and documents that must not count (an invoice of the
month before, a draft, another salesperson's invoice), then computes the statements. The expected statements are in
`backend/modules/sale_commission/tests/data/commission_crosscheck.csv`, produced by a separate script
(`make_crosscheck.py`, plain decimal arithmetic from the agreed quantities, prices and discounts, without the
module); the test suite checks the module's statements against it line by line and in total.

## Limits

- Commissions are in the company currency; invoices in other currencies count at their booked rate.
- One salesperson per invoice (no splits); team leaders get no override commission.
- Statements are not created automatically on a schedule: compute them from the plan.
