# Products — quick guide

Products and services you quote: name, internal reference, barcode, category, unit (unit, hour,
day, kg …), sales price, cost (visible to product managers only) and customer taxes (with sales and
accounting installed; the Phase 1 *Tax %* placeholder is converted to taxes on upgrade). The *Sales* tab
also holds the invoicing policy and the optional products and accessories offered with the product. Archive products you no longer sell instead of deleting them.

## Products, variants and pricelists

Salespeople find products, variants and pricelists under **Sales → Products** (attributes, categories and
units under *Sales → Configuration*); product managers also have the **Catalogue** menu.

**Units of measure.** Each category (unit, working time, weight, length, volume) has one *reference*
unit; the others are *bigger* (a dozen = 12 units, a day = 8 hours) or *smaller* (a gram = 0.001 kg).
Quantities convert only within a category and are rounded to the target unit's *rounding precision*.

**Variants.** Give a product attributes (*Attributes & variants* tab: e.g. Colour = Red, Blue, Green and
Size = S, M, L) and press **Generate variants**: one variant is created per combination, with its own
internal reference (`TSHIRT-RED-L`), barcode and extra price (the values' *extra prices* are added to the
sales price). The product with attributes becomes a *template*: it is not sold itself. Name, type,
category, unit, description, prices, cost, currency, tax, *Can be sold* and company are edited on the
template and copied to every variant; they are read-only on variants. Removing a value and generating
again archives the variants that no longer exist (they are never deleted, documents keep pointing at
them); adding it back reactivates them. Variants appear as `[TSHIRT-RED-L] T-shirt (Red, L)`.

**Pricelists.** A pricelist has a currency, a company (or none: shared), optional countries and price
rules. A rule applies to all products, a category (and its sub-categories), a product (for a template:
all its variants) or one variant, from a minimum quantity and between optional dates. Its price is:

- *Fixed price* — for a template, the variants' extra prices are still added;
- *Discount* — a percentage off the base price;
- *Formula* — base price minus a discount %, rounded to a multiple of *Round to*, plus a *surcharge*
  (e.g. round to 1 and add −0.01 for prices ending in .99), kept between cost + *minimum margin* and
  cost + *maximum margin* when they are set.

The base price is the sales price, the cost, or the price from another pricelist (a pricelist can never
be based on itself, even through others); prices in another currency are converted at the day's rate.
When several rules apply, the most specific wins: variant, then product, then the deepest category, then
all products; then the higher minimum quantity, then the rule order. Without a rule the sales price
applies.

**Which pricelist a customer gets:** their own pricelist when the customer has one (set on the contact
once Sales 2.0 is installed) and it belongs to the company or is shared; otherwise the first pricelist
whose countries include the customer's country; otherwise the company's default — its first pricelist
(or a shared one) without countries, in sequence order.

The demo data has a T-shirt in three colours and three sizes (L costs 2.00 more), a *Wholesale* list
(10 % off from 10 units, 15 % from 50) and a *Europe (EUR)* list for France and Germany.

**For developers.** `pricelist._get_product_price(product, quantity, uom=None, date=None, currency=None)`
returns the unit price and `_get_product_rule(...)` the rule used; `env["product.pricelist"]._for_partner(
partner, company)` picks the pricelist. Templates must not be put on sales lines: choose line products
with the stored flag `is_sellable` (`[("is_sellable", "=", True)]`, i.e. *Can be sold* and not a
template). `name_search` and the default product searches are unchanged and still list templates.

## AI: sales description drafts

The **Sales description draft** panel on the product form (`product.description_draft`, hidden when the
feature is switched off) writes two to four sentences from what the catalogue says about the product: its
name, reference, type, category, unit, attributes and their values, variants and current sales description.
It writes in your language (a French user gets French; accepting writes the French translation of the
description and leaves the other languages alone). Before anything is proposed, Tessera checks the draft
against those facts: a sentence that states a number (size, weight, duration, rating such as IP67) or a
specification claim (waterproof, warranty, certified, materials, battery, recycled…) the catalogue does not
contain is removed, and the panel shows what was removed and why. Product managers get the result as a
proposal on the product (on its template for a variant); other users see the text only. Nothing is written
until you accept.

Limits: the check knows numbers and a list of common specification claims in English, French and German; a
new kind of claim the list does not know (say, a brand name of a material) is not caught, so read the draft
before accepting it.
