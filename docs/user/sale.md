# Sales — quick guide

Quotations and sales orders are the same document (D-011, D-074): a quotation becomes a **sales order**
when the customer accepts it online or when you confirm it. Everything lives under the **Sales** menu:

| Menu | What you find |
|---|---|
| Orders → Quotations | every quotation and order (list, kanban, pivot, graph) |
| Orders → Orders | confirmed sales orders |
| Orders → Customers | your customers (their pricelist is on the *Sales* tab) |
| To invoice → Orders to invoice | orders with something to invoice (or returns to credit) |
| Products → Products, Product variants, Pricelists, Promotions | the catalogue you sell from |
| Reporting → Sales analysis | confirmed orders by customer and month (pivot and graph) |
| Configuration | quotation templates, product categories, attributes, units (sales administrators) |

Product managers who do not sell still find products and pricelists under **Catalogue**.

## Quotations

- **Create** a quotation from Sales → Quotations or from an opportunity (*New quotation*). Choosing the
  customer fills the **pricelist** (the customer's own, else the one of their country, else the company's),
  the **currency** (the pricelist's), the **fiscal position** (the customer's, or the one detected from their
  country and tax ID) and the **payment terms** (the customer's, else none). Change any of them on the
  quotation; changing the customer refills them.
- **Lines.** Pick a product that can be sold (templates with variants cannot: pick a variant). The
  description, unit, **unit price** and **taxes** come from the product: the price from the pricelist for
  the quantity (*Wholesale*: −10 % from 10 units), the taxes from the product's customer taxes in the
  quotation's company, mapped by the fiscal position. The price follows the quantity and the pricelist
  until you type one yourself (*Price typed*); clear the price to go back to the pricelist. Totals use the
  accounting tax engine (tax-included prices, several taxes, discounts).
- **Sections, notes and optional lines.** Structure the quotation with *sections* and *notes*. *Optional*
  lines are offered to the customer and stay out of the totals until they add them. When you add a
  product that has **optional products**, they are offered as optional lines automatically (once).
  Accessories are kept as upsell suggestions.
- **Templates.** Choose a *Quotation template* when creating the quotation (or press **Apply template**,
  which replaces the lines): its lines, optional products, validity (days), terms and payment terms are
  copied.
- **Promotions.** Press **Apply promotions** (optionally with a code). Automatic promotions and the codes
  entered on the order add *reward lines*; pressing again updates them and removes those no longer earned
  (see *Promotions* below).
- **Send by email.** Attaches the PDF and a personal link where the customer can view, download, add an
  optional product, accept (by typing their name) or decline — no account needed. The quotation moves to
  *Sent*. The PDF shows each line's taxes by name and the payment terms.
- **Validity.** After the *Valid until* date, sent quotations expire automatically.
- **States.** Draft → Sent → Sales order / Declined / Expired; *Cancel* and *Back to draft* when needed.
  States change only through these buttons, so the history stays reliable. **Confirm** turns a draft or
  sent quotation into a sales order without the customer's signature; an accepted or confirmed quotation
  wins its CRM opportunity.
- **Print** from the form (*Print → Quotation*) using your company's layout.

## Sales orders and invoicing

Each product says how it is invoiced (*Sales* tab of the product):

- **Invoicing policy** — *Ordered quantities* (invoice what was ordered, e.g. subscriptions, services sold
  up front) or *Delivered quantities* (invoice what was delivered).
- **Service delivery** — *Manually* or *By milestones*: for services, how deliveries are tracked. Until
  stock management arrives (Phase 3) you enter the **Delivered** quantity on the order line in both cases.

On a confirmed order each line shows **Delivered**, **Invoiced** (draft and posted invoices count,
cancelled ones do not, credit notes subtract) and **Returned**; *To invoice* is the ordered or delivered
quantity minus returns minus what is already invoiced. The order's **Invoicing** status is *Nothing to
invoice*, *To invoice*, *Fully invoiced* or *Upselling opportunity* (more delivered than ordered on a line
invoiced on ordered quantities).

**Create invoice** asks what to invoice:

- *Regular invoice* — a draft customer invoice with everything that can be invoiced now. It takes the
  order's customer, currency, fiscal position and payment terms, and the order number as *source document*.
  Earlier down payments are deducted with negative lines. Invoicing several orders of the same customer
  and currency together (`action_invoice_orders`) puts them on one invoice, one section per order.
- *Down payment: percentage of the total* or *fixed amount, tax included* — a draft invoice of the
  *Down payment* product booked on the **customer deposits** account, with the same taxes as the order
  lines (one line per set of taxes). The order keeps a *down payment* line (quantity 0) so the final
  invoice deducts it.

The invoices appear on the order's **Invoices** tab. When an invoice that makes the order fully invoiced is
posted, the order is **locked**: its customer, prices and lines can no longer change (deliveries and
returns can still be recorded). **Unlock** it if you must change it. Cancelling an order with invoices is
refused: cancel or credit the invoices first. Creating invoices needs the *Accounting / Billing* right.

**Returns.** Enter the **Returned** quantity on the line, then press **Credit note for returns**: a draft
credit note credits the returned quantities that were already invoiced (stock returns come in Phase 3).
Returned quantities that were not invoiced yet are simply not invoiced. Reversing an invoice from
accounting keeps the link to the order, so the order goes back to *To invoice*.

## Promotions

Sales administrators create promotions under Sales → Products → Promotions:

- **Applies** *automatically* to every eligible order, or *with a code* the customer gives (not case
  sensitive); optional **dates**, **company** and **maximum uses** (confirmed orders).
- **Conditions** — the order is eligible when one condition holds: a minimum untaxed amount, a minimum
  quantity of some products or categories (sub-categories count), or both. No condition: every order.
- **Rewards** — a percentage on the order, a percentage on the cheapest line, a fixed amount, or a free
  product. Discounts keep the taxes of the lines they discount (one reward line per set of taxes; a fixed
  amount is split in proportion), a free product is added at a 100 % discount, and the rewards never take
  an order below zero.

Promotions apply when you press **Apply promotions**; press it again after changing the lines. Rewards
already invoiced cannot be recomputed.

## Customers and the portal

The customer's link (or their portal account) shows the quotation with the taxes of each line, the
payment terms and an **Add to my order** button on optional lines while the quotation waits for an answer.

## Upgrading from Phase 1

`tessera db migrate` installs accounting if needed and upgrades sales to 2.0.0: every Phase 1 tax
percentage (on quotation lines and products) becomes a sales tax of that rate in the company — an existing
one when the chart has it, else a new *Sales tax N%* on the tax-due account — and the totals are
recomputed from the taxes; 0 % means no tax. Accepted quotations become sales orders (unlocked, with
their prices kept as typed). The products' *Tax %* field remains as a placeholder for databases without
accounting and is hidden on the product form.
