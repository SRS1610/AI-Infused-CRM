# Phase 2 demo: sales and accounting

A walkthrough of everything Phase 2 delivers, in the order of the build plan (§3). Like the Phase 1 demo it
runs against the demo scenario with the deterministic **Local test provider** standing in for a model
provider, so it needs no API keys; with a real provider configured (**Settings → AI → Providers /
Routing**) the AI steps show real model output.

## Prepare

```bash
docker compose -f deploy/compose/docker-compose.yml up -d
```

```bash
docker compose -f deploy/compose/docker-compose.yml exec api tessera seed demo --phase 2
```

Without Docker: start PostgreSQL 16 with the `vector`, `pg_trgm`, `unaccent` and `btree_gist` extensions,
then from `backend/` run `TESSERA_AI_GATEWAY_URL=inprocess tessera seed demo --phase 2` and `tessera
serve`, and from `frontend/` run `npm run dev`.

`tessera seed demo --phase 2` installs every Phase 1 and Phase 2 module and, in each company (**My
Company**, USD, US chart; **My Company (Europe)**, EUR, EU chart): customer invoices, vendor bills,
payments and a credit note; sales orders in several invoicing states; a bank statement to reconcile;
analytic plans, a budget and fixed assets; a tax return; subscriptions with history; rental orders;
a commission plan with two salespeople (`hugo`, `nadia`); a duplicate vendor bill for the anomaly check.

Users: `admin` / `admin1234`, `demo` / `demo1234`, `marie` / `demo1234` (French, European company).

## 1. Catalogue and prices

1. **Sales → Products → Products**: open *T-shirt*. The **Variants** tab shows 9 colour × size variants;
   *L* sizes cost 2.00 more. Shared fields (name, taxes, accounts) are read-only on a variant.
2. **Sales → Products → Pricelists**: *Wholesale* gives 10 % from 10 units and 15 % from 50; *Europe (EUR)*
   applies to French and German customers.
3. **Sales → Configuration → Discount bands**: approved discount ranges per category and quantity.

## 2. Quotation to sales order to invoice

1. **Sales → Orders → Quotations → New**: customer *Acme Robotics*, add *Collaborative robot arm* × 2. The price
   comes from the customer's pricelist; taxes come from the product and the fiscal position.
2. **Apply template** (button) to start from a quotation template, then **Apply promotions** with a code.
3. AI panels on the quotation: **Suggested products** (bought together), **Discount suggestions** (always
   within the band), **Pricing check** (flags lines below cost + margin), **Lines from text** — paste an
   RFP; unmatched items are flagged, never invented. Accepting a proposal adds the lines.
4. **Confirm** → the quotation becomes a *Sales order*. **Create invoice** → *Down payment (percentage)*
   30 %; then **Create invoice** → *Regular invoice*: the down payment is deducted.
5. Record a return quantity on a line and **Credit note for returns**.

## 3. Invoices, payments and the ledger

1. **Accounting → Customers → Invoices**: open the invoice, **Confirm**. It is numbered `INV/2026/…` and
   becomes read-only. **Journal items** shows the double entry.
2. **Register payment** → amount 400 → *Partially paid*; register the rest → *Paid*.
3. **Credit note** → *Full refund* on another invoice → *Reversed*.
4. **Send by email**: the customer opens the link and **Pay now** with the test provider; the invoice is
   paid and the payment reconciled.
5. Run `tessera account integrity`: every hash chain verifies.

## 4. Bank reconciliation

1. **Accounting → Bank → Statements**: the demo CAMT.053 statement is imported; 11 of 12 lines are already
   matched by the reconciliation rules.
2. **Accounting → Bank → Reconcile**: the remaining line shows AI-ranked suggestions; match it or write it off.
3. Import one of `backend/modules/account_bank/data/samples/*` (CSV, OFX, MT940) into the bank journal.

## 5. Vendor bills with AI

1. **Accounting → Vendors → Bills → Read from documents**: upload a bill PDF (or a scan). The draft
   proposal shows the vendor, lines, accounts and taxes it chose; accept it to create the draft bill.
2. **Accounting → Vendors → Anomaly flags** lists the bill the demo entered twice; its form shows the
   flag with an explanation.

## 6. Reporting, tax and planning

1. **Accounting → Reporting**: trial balance (sums to zero), profit and loss, balance sheet (current-year
   earnings = net income), cash flow, aged receivable, partner ledger; **Consolidated** across companies.
   Ask a question in words ("profit and loss last quarter vs same quarter last year") to fill the filters.
2. **Cash-flow forecast**: the weekly cash position for the next 8 weeks with a narrative.
3. **Accounting → Accounting → Tax returns**: the EU return's grids tie to the tax accounts; **Close period** posts the closing
   entry and locks the period.
4. **Accounting → Analytic** (analytic report, **Budgets**, **Budget vs actual**, **Explain variance**) and
   **Accounting → Assets** (depreciation boards; **Sell** posts the gain or loss).

## 7. Reminders and e-invoices

1. **Accounting → Customers → Payment reminders → Run**: overdue customers get a reminder at their level;
   **Draft personalised message** proposes a reworded text with the same amounts.
2. Open a posted invoice of the European company: **Send via Peppol** validates the UBL document against
   the official rules and sends it to the test access point.

## 8. Subscriptions, rentals and commissions

1. **Sales → Subscriptions → Subscriptions**: MRR, churn risk (AI) and history per subscription; **Upsell** mid-period
   invoices the prorated difference; **Draft renewal** proposes a renewal e-mail.
2. **Sales → Rentals → Rental orders**: confirm two orders for the same unit and dates — the second is refused. The
   availability calendar shows booked units; the demand forecast panel shows the next weeks.
3. **Sales → Commissions → Statements**: *hugo*'s statement for the demo month (51,816.25 base, ×1.2 accelerator,
   1,964.87 commission) matches `backend/modules/sale_commission/tests/data/commission_crosscheck.csv`;
   **Explain** cites only the statement's numbers.

## 9. Sign-in with SAML

**Settings → Users & Companies → SAML providers**: add an identity provider (metadata URL or certificate);
its button appears on the sign-in page.
