# Rentals — guide

The **sale_rental** module rents products out on ordinary sales orders: a rental line has a pickup date and a
return date, its price comes from the product's rental prices for that duration, confirming the order reserves
the units (and refuses to double-book them), and the pickup, the return, late fees and the security deposit are
tracked on the order. Install it with `tessera module install sale_rental` (it depends on `sale`); the database
needs the `btree_gist` extension, which the installer creates with the other extensions.

| Menu | What you find |
|---|---|
| Sales → Rentals → Rental orders | orders with at least one rental line |
| Sales → Rentals → Pickups and returns | confirmed rental lines: to pick up, out with customers, late, returned |
| Sales → Rentals → Availability calendar | every reserved unit, day by day (calendar, list, pivot) |
| Sales → Rentals → Rental products | products that can be rented |
| Sales → Configuration → Rental prices | every rental price (sales administrators) |

## Setting up a rental product

On the product form, **Rental** tab:

- **Can be rented** — offers the product for rent.
- **Rentable units** — how many units can be out at the same time. Serial numbers come with inventory (Phase 3);
  until then units are numbered 1, 2, 3… and the reservations say which number a customer has.
- **Rental prices** — rules of the form *duration × unit → price*: e.g. 4 hours = 40, 1 day = 90, 1 week = 450,
  1 month = 1,500. A month is 30 days. A rule can be limited to a **pricelist** (for orders using it) and to a
  **company**; its price is in its currency (the pricelist's when it has one) and is converted to the order's.
  Without any rule, the product's sales price is used as a price per day.
- **Late fee per hour / per day** and **Security deposit** (per unit).

The demo products *Event projector* (3 units) and *Robot arm demo unit* (1 unit) show complete set-ups.

## Renting on an order

Add a line with a rentable product, tick **Rental** (giving the pickup and return dates is enough) and set the
**pickup date** and **return date**. The line's description shows the period and its unit price is the best price
for the duration:

- **Duration.** The time between pickup and return on the wall clock of the salesperson's time zone, a started hour
  counting as an hour. From 10:00 Saturday to 10:00 Sunday is one day even when the clocks change that night.
- **Best price.** The cheapest combination of rules, each usable several times, that covers the duration. With
  1 day = 100 and 1 week = 500: 9 days = 1 week + 2 days = 700; 12 days = 2 weeks = 1,000 (cheaper than 1 week +
  5 days); 6 days = 1 week = 500; 3 hours = 1 day. *Rental price made of* shows the combination. Ties go to the
  combination with fewer blocks.
- Changing the dates, the quantity or the pricelist updates the price, unless you typed a price yourself (clear it to
  go back to the rental price). Rentals are by whole units.
- When the product asks for a **security deposit**, a *Security deposit* line follows the rental line
  (quantity = units, no tax).

## Availability and reservations

Quotations reserve nothing. **Confirming** the order reserves the units for the period, and is **refused** when not
enough units are free: *Event projector is not available from 2026-12-02 09:00 to 2026-12-05 09:00: 1 of 3 units
free, 2 needed*. Changing the dates or quantity of a confirmed rental checks again; adding a rental line to a
confirmed order reserves at once. Cancelling the order or putting it back to draft frees the units.

- A customer keeps the **same unit** from pickup to return: a unit is free only if nothing overlaps the whole
  period. When several are free, the one whose previous rental ends closest to the start is taken, so long free gaps
  stay available.
- Periods **touching** are fine: a unit returned at 12:00 can be rented again from 12:00.
- Units are physical: reservations made in another company of the database count too.
- **No double booking, guaranteed by the database.** Each reserved unit is a row (product, unit number, period)
  under an exclusion constraint: two reservations of the same unit can never overlap, whatever writes them, and two
  people confirming at the same moment are served one after the other (the second gets the "not available"
  message).

The **availability calendar** shows each reserved unit on every day it covers (*Event projector #2 · SO0042 ·
Acme*). For a product's free units per day or hour, call `sale.rental_allocation.availability(product_id, start,
end, slot)` through the API (`slot` = `day` or `hour`, days in your time zone; also returns the units free for the
whole period). `sale.rental_allocation.rental_history(product_id, date_from, date_to)` returns the daily
utilisation (units booked, unit-hours booked, utilisation 0–1, rentals started, late returns) used by the
demand forecasting of a later release.

## Pickup and return

On the order (**Pick up** / **Return** buttons, for every rental of the order) or on a rental line (from *Pickups and
returns*):

- **Pick up** — status *Picked up*, time recorded, delivered quantity set. Only the return date of a picked-up
  rental can still change (an extension keeps the same units and is refused if they are booked by someone else).
  An order cannot be cancelled while something is out with the customer.
- **Return** — status *Returned*, time recorded. Returned early: the units are free from that moment. Returned
  **late**: a *Late return* line is added to the order with the fee, per unit: the daily fee per started day when only
  a daily fee is set, the hourly fee per started hour when only an hourly fee is set, and with both the daily fee per
  full day plus the hourly fee for the remaining hours, those costing at most one daily fee (26 hours late at
  10/hour and 60/day = 60 + 20 = 80). Lateness is counted in started hours on the salesperson's wall clock. A fully
  invoiced order is unlocked so the fee can be invoiced.
- **Late.** Every hour, rentals not back by their return date become *Late*; the salesperson is notified once and a
  note is posted on the order.

## Invoicing

Rental lines are invoiced on the **ordered** quantity (whatever the product's invoicing policy), with the order's
*Create invoice* button: rental lines and late fees on the **Rental revenue** account (unless the product has its own
income account), the deposit on **Customer deposits**. After the return the deposit line counts as returned:
*Credit note for returns* refunds it. Late fees are invoiced with *Create invoice* after the return.

## Limits

- Units are counted, not tracked by serial number, and no stock moves are made (Phase 3).
- A late rental keeps its planned reservation: if the next customer's period starts before the unit comes back, the
  late notification is the warning.
- Pickup before the planned start does not move the reservation.
- Durations longer than five years are refused.
