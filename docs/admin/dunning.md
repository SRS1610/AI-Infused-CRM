# Payment reminders (`account_dunning`)

Reminds customers of overdue invoices with escalating levels: emails with the list of overdue invoices
and links to pay them online, printed letters and reminder fees. Data model:
[ERD_PHASE_2.md §5](../architecture/ERD_PHASE_2.md).

## Levels

*Accounting → Configuration → Dunning levels* (per company). A level has the **days overdue** at which it is
reached, whether it sends an **email** and/or prints a **letter**, an optional **fee** (amount and product,
charged on a fee invoice), and the email **subject** and **message**. Messages use the placeholders
`{partner}`, `{company}`, `{amount_due}`, `{level}`, `{max_days}` and `{count}`; the table of overdue
invoices (number, dates, days late, amount due, *View and pay* link) is always added below the message and is
computed from the ledger.

The demo creates three levels: *First reminder* (7 days), *Second reminder* (30 days) and *Final notice*
(60 days, letter and a 15.00 fee).

## Runs

*Accounting → Customers → Payment reminders*:

1. **New**, then **Find customers to remind**: for each customer (commercial entity) the run lists the
   overdue receivable items of posted customer invoices, takes the highest level reached by the oldest one,
   and renders the reminder. Excluded:
   - items marked *No follow-up* (disputed),
   - invoices *In payment*,
   - customers with *No payment reminders* (on the contact form),
   - balances at or below the company's *Minimum overdue amount for reminders*,
   - customers who already got this level (or a higher one) within the level's delay — a level is never
     sent twice within its delay; a higher level is sent as soon as it is reached.
2. Review the reminders (subject and message can be edited), then **Send reminders**: fee invoices are
   created and posted, emails are queued through the mail queue (`core.mail_queue`), and the letters are
   printed into one PDF attached to the run (*Letters (PDF)*). Customers without an email address are
   reminded by letter when the level prints one.

The daily job *Payment reminders (daily)* (`account_dunning.run`) prepares and sends a run for every company
with levels. Sent runs are kept as a record (*Reminders sent* lists every reminder).

## For developers

`modules.account_dunning.dunning`:

- `prepare_run(company, day=None)` / `send_run(run)`;
- `overdue_by_partner(company, day)` returns the items per partner;
- message rendering goes through a replaceable function: `set_renderer(fn)` where
  `fn(ReminderContext) -> (subject, html)`; the context has the partner, company, level, items and the
  computed `amount_due` / `max_days`. The personalised-message AI feature (sub-phase 2.6) proposes a message
  for a prepared line and keeps `invoice_table(ctx)` for the amounts.

## AI: personalised reminders (`account_dunning.message_draft`)

Sub-phase 2.6. On a prepared reminder (still to send), *Personalised message (AI)* → **Draft personalised message**
asks the mid-tier model for the reminder's text: tone by level (friendly first reminder, firm second, formal final
notice) and by the customer's history (earlier reminders, how late they usually pay, disputed invoices). The facts
it sees are computed from the ledger; **every number in the draft must be one of them** — a sentence with another
figure (a wrong total, an invented discount or deadline) is removed and listed — and the invoice table with the
amounts and pay links is always the ledger's (`invoice_table`), never the model's.

The draft is a **write proposal** on the reminder (subject and message): nothing changes before *Accept*, and
accepting only replaces the text — no mail is queued until the run is sent as before. Read-only users get the
draft without a proposal. The customer's name is sent to the model (`redaction: minimal`), so the feature is only
routed to providers approved for personal data. Per-company switch: switched off, the panel disappears and the
endpoint answers `404 feature_disabled` without calling the gateway.

## Access

Billing users run and send reminders; advisers configure levels. Levels and runs are per company.
