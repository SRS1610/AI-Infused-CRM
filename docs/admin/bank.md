# Bank statements and reconciliation (`account_bank`)

Imports bank statements, books each transaction, and matches it with invoices, bills, payments or
write-offs — by hand on the reconciliation screen or automatically with reconciliation rules. Data model:
[ERD_PHASE_2.md §5](../architecture/ERD_PHASE_2.md); accounting behaviour: [accounting.md](accounting.md).

## How a transaction is booked

A statement belongs to a bank or cash journal. **Posting** a statement (or a single transaction) creates
one journal entry per transaction in that journal:

| Item | Account | Amount |
|---|---|---|
| bank | the journal's bank / cash account | the transaction amount |
| suspense | the journal's suspense account (else the company's *Bank suspense account*) | the opposite |

**Matching** replaces the suspense item with counterparts and reconciles them:

- an **open item** (customer invoice, vendor bill, credit note, or a payment waiting on the *Outstanding
  receipts / payments* account) gets a counterpart on its own account, partner and currency, and the two
  are matched (`reconcile_lines`). When the transaction is smaller than the item, the item stays partly
  open; when it is larger, the rest stays on the suspense account until it is explained;
- a **write-off** books the remaining (or a given) amount on an account, optionally with a tax
  (the amount is tax-included: base and tax items are created);
- items in another currency are matched in that currency; the rate difference becomes a realised
  exchange difference (2-c).

Matching rebuilds the transaction's entry through the ledger: the entry is reset to draft (lock dates
apply), its items are replaced and it is posted again (balance check; the entry keeps its number). **Undo**
removes the matches and restores the single suspense item. Adding matches to a partly matched transaction
re-applies its earlier matches with the new ones. Posted transactions cannot be edited or deleted;
statements whose transactions are posted cannot be deleted.

### Payments and bank matching

Registered payments are booked straight on the bank account unless the journal has **Payments wait for
bank matching** (D-077). Turn it on for every bank journal whose statements you import: payments then go to
the outstanding receipts / payments accounts, invoices show *In payment*, and matching the statement line
with the payment marks the payment *matched* and the invoice *Paid*. A payment booked straight on the bank
account is not a candidate for matching (it already moved the bank balance). The flag is not switched on
automatically, so existing journals keep their behaviour (proposed decision D-078).

## Importing statements

*Accounting → Bank → Imports → New*: choose the journal and the file, then **Import**. The reconciliation
screen has an *Import a statement* button, and the API takes an uploaded attachment:

```bash
# upload the file, then import it
curl -b cookies -H "X-CSRF-Token: $CSRF" -F file=@march.xml https://erp.example/api/v1/attachments
curl -b cookies -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
     -d '{"journal_id": 7, "attachment_id": 1234}' https://erp.example/api/v1/account_bank/import
```

From the command line:

```bash
uv run tessera account import-statement march.xml --journal BNK1 --company "My Company (Europe)"
```

| Format | Notes |
|---|---|
| **CAMT.053** (`camt.053.001.02` … `.08`) | Booked entries only (pending ones are skipped). A batched entry with several transaction details becomes one line per transaction. Keeps end-to-end id, servicer reference, structured creditor reference, counterparty name and IBAN, bank transaction code and the instructed amount in another currency. |
| **OFX** 1.x (SGML) and 2.x (XML) | Bank and credit-card statements; `FITID` is the unique id; the opening balance is derived from `LEDGERBAL`. |
| **MT940** | Several statements per file (optionally in SWIFT blocks); `:86:` read in German `?NN`, SWIFT `/KEY/value` or free-text layouts; `RC`/`RD` reversals. |
| **CSV** | Header row detected (English, French, German, Dutch, Spanish column names); one amount column or debit + credit; date and decimal formats detected; a running balance gives the opening and closing balances. |

Every import checks that the statement's account number matches the journal's bank account (when set),
that its currency is the journal's, and that **opening balance + transactions = closing balance**. A
transaction already imported in the journal (same `unique_import_id`) is skipped, so importing the same file
twice adds nothing; a partly imported file creates a statement with the remaining transactions only. A file
without balances continues from the journal's last closing balance. The partner is found from the
counterparty IBAN (partner bank accounts) or from an exact, unique name.

**CSV options** (import record *CSV options*, or remembered per journal in *CSV import options*):

```json
{"delimiter": ";", "skip_rows": 2, "date_format": "%d/%m/%Y", "decimal_separator": ",", "thousands_separator": ".",
 "columns": {"date": "Booking date", "payment_ref": "Details", "debit": "Debit", "credit": "Credit",
             "partner_name": "Counterparty", "account_number": "IBAN", "ref": "Reference", "balance": "Balance"}}
```

Columns can also be given by 0-based position. Sample files of every format are in
`backend/modules/account_bank/data/samples/`.

By default an import posts the statements and runs the automatic rules.

## Reconciliation rules

*Accounting → Configuration → Reconciliation rules*. Every company gets three rules:

| Rule | Type | Automatic | Effect |
|---|---|---|---|
| Invoices, bills and payments | invoice matching | yes | finds open items by reference, partner and amount |
| Bank fees | write-off suggestion | yes | money paid whose label mentions fees / charges / commission → *Bank fees* |
| Bank interest | write-off suggestion | yes | money received whose label mentions interest → *Other income* |

Conditions: journals, direction (received / paid), amount (lower / greater / between), label (contains /
does not contain / regular expression), partner set or among a list. Invoice matching also has: look for
references in the label, match the amount with the partner's open items, a payment **tolerance**
(percentage or fixed amount; with counterpart lines the difference is written off and the invoice closed),
and how far back to look. Write-off counterparts take a percentage of the remaining amount, a fixed amount,
or an amount read from the label with a regular expression (`FEE (\d+[.,]\d+)`), with optional taxes.

**Suggestions** (`modules.account_bank.rules.suggest_matches(line, limit=5)`) rank candidates with a 0–1
score and reason codes: `reference` (an invoice number, payment reference, vendor reference or payment
memo appears in the label), `partner` / `partner_name`, `amount_exact`, `amount_tolerance`,
`amount_partial`, `group` (several invoices paid together, found by their references or because together
they make the amount), and for write-off rules `label` / `rule_conditions`.

**Automatic reconciliation** applies the rules marked *Reconcile automatically*, in sequence, to posted
transactions: an invoice-matching rule applies only when the best candidate is unambiguous and has a
reference (a partial payment of a referenced invoice is accepted) or the same partner and amount; a
write-off rule applies when its conditions match. It runs after each import, from *Auto-reconcile* on a
statement or on the reconciliation screen, and every hour (*Bank: apply automatic reconciliation rules*).

## Reconciliation screen

*Accounting → Bank → Reconcile*: transactions on the left (filter by journal, to reconcile / reconciled,
search); for the selected transaction, its suggestions with score and reasons (**Accept**), a search of
open items by number, reference, partner or amount with an optional partial amount per item (**Match
selected items**), a write-off to any account with an optional tax (**Write off the rest**), and **Undo** for
matched transactions.

## Access

| Group | Can |
|---|---|
| Accounting / Read-only | see statements, transactions, imports and rules |
| Accounting / Accountant | import, post, match and undo; run the rules |
| Accounting / Adviser | also configure reconciliation rules |

Statements, transactions, imports and rules are per company.

## Demo

`tessera seed demo --phase 2` switches each demo company's main bank journal to *Payments wait for bank
matching*, registers two customer payments, then imports a CAMT.053 statement built from the company's open
demo documents (customer payments quoting invoice numbers, a batched transfer for the two payments, vendor
bill payments, three bank fees, bank interest and one unknown transfer). The shipped rules reconcile all but
the unknown transfer (≥ 90 %, acceptance criterion 2-d, tested in `test_rules.py`).

## Limits

- Matching rebuilds the transaction's entry: transactions dated in a locked period cannot be matched or
  undone; journals locked with a hash chain cannot be used for statements.
- A payment booked straight on the bank account (journal without bank matching) cannot be matched with the
  statement line that reports it.
- Journals in another currency are supported (the bank items are kept in the journal currency); items in a
  third currency are converted at the transaction date.
