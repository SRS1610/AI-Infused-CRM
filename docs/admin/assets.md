# Fixed assets (`account_asset`)

Fixed assets and their depreciation boards: straight-line and declining methods, prorata, value changes,
pause, disposal and sale. Data model: [ERD_PHASE_2.md §6](../architecture/ERD_PHASE_2.md); acceptance
criterion 2-g (golden table in `backend/modules/account_asset/tests/test_golden.py`).

## Assets and models

*Accounting → Assets → Assets*. An asset has an original value, a salvage value (left at the end of the
life), an acquisition date and a **depreciation start** (prorata date, the acquisition date by default),
a method, a number of depreciations, a period (months or years), the asset, accumulated depreciation and
depreciation expense accounts and a journal. Without explicit accounts the chart's *Property, plant and
equipment*, *Accumulated depreciation*, *Depreciation* accounts and the *Miscellaneous* journal are used.

*Asset models* (*Accounting → Assets → Asset models*) hold reusable settings; choosing a model on an asset
copies them. States: *Draft* → *Running* (*Confirm*) → *Paused* ⇄ *Running* → *Closed* (disposed or
sold). A running asset without posted depreciation can go back to draft.

### From vendor bills

On an account (*Configuration → Chart of accounts*), set **Create assets** (*Create in draft* or
*Create and confirm*) and an **asset model**: posting a vendor bill line on that account creates an asset
for it (original value = the line's amount without tax, acquisition date = the bill date, linked to the
line). Otherwise, *Create assets* on a posted bill creates draft assets for its lines on fixed-asset
accounts, and `account.move_line.action_create_asset()` does it for any posted purchase item.

## Depreciation board

*Confirm* (or *Compute depreciation* on a draft, as a preview) plans the whole life as **draft
depreciation entries** (debit depreciation expense, credit accumulated depreciation), each dated at the end
of the span it covers. The *Post due depreciation entries* scheduled job (`account_asset.post_depreciation`,
daily) posts the drafts that are due; *Post due entries* does it for one asset. An entry dated on or before
the lock date is posted on the first open day instead. Posted depreciation entries cannot be reset or
cancelled: change the asset with *Modify*.

### Prorata

| Prorata | Schedule |
|---|---|
| No prorata | starts on the first day of the period containing the depreciation start; full periods |
| Constant periods | first period prorated on a 30/360 basis (every month counts 30 days); one more, partial entry at the end completes the life |
| Based on days per period | amounts proportional to actual days; the life ends the day before the start's anniversary |

### Methods

With `R` = book value − salvage value still to depreciate:

| Method | Amount of a period |
|---|---|
| Straight line | `R × period share / remaining life`, rounded on the cumulative amount so the entries add up exactly |
| Declining | `R × factor × months / 12 × period fraction`; the last period takes what is left |
| Declining then straight line | the larger of the declining amount and the straight-line amount over the remaining life |

Examples (golden table): 10 000 over 5 years straight line = 2 000 a year; declining 30 % = 3 000, 2 100,
1 470, 1 029, 2 401; declining 40 % then straight line = 4 000, 2 400, 1 440, 1 080, 1 080; 12 000 over 4
years from 1 April with constant periods = 2 250, 3 000, 3 000, 3 000, 750.

## Changing an asset

- **Modify** (new book value, date, reason): depreciation up to the date is posted (prorated for the
  current period). A **decrease** is posted as an extra depreciation entry (impairment) and the remaining
  life depreciates the lower value. An **increase** creates a *gross increase* child asset for the
  difference, posted against the revaluation counterpart account (the chart's *Other income* account unless
  the API caller gives `counterpart_account_id`), depreciated over the parent's remaining life with the same
  method. The new value cannot be lower than the salvage value.
- **Pause** posts the depreciation up to the pause date (prorated) and stops; **Resume** plans the remaining
  life from the resume date, so the end of life moves by the paused time.
- Values and method of an asset with posted depreciation can only change through *Modify*; on a draft asset
  any change recomputes the preview.

## Disposal and sale

- **Dispose** (date, reason) and **Sell** (date, price) post the prorated depreciation up to the date, then
  one closing entry for the asset and its gross increases: credit the asset account with the gross value,
  debit accumulated depreciation, and book the difference between the proceeds and the book value on
  *Gain on disposal of assets* (`asset_gain`) or *Loss on disposal of assets* (`asset_loss`).
- A sale given a customer invoice line (`action_sell(date, invoice_line_id=…)`) debits that line's account
  with its amount (the proceeds move from the income account into the disposal result). A sale for a cash
  amount debits the company's outstanding receipts account, which the bank statement then matches.

## Data for other features

`modules.account_asset.services`: `asset_profile(asset)` (method, period, values, accounts and the whole
board), `board_checks(asset)` (deterministic consistency checks: total, negative amounts, overdue drafts,
order, book value below salvage) and `asset_model_candidates(env, company)` (the models a bill line can be
classified into) — the inputs of the sub-phase 2.6 asset classification and depreciation anomaly features.

## Access

Accountants manage assets; read-only users and billing users can read them. Assets belong to one company.

## Limits

- No reversal of a disposal: post a reversal of the closing entry and create the asset again if needed.
- Deferred expenses use the same mechanics (choose the prepaid account as asset account); deferred revenue
  is not covered.
- Imported assets with depreciation already booked elsewhere are not supported yet.
