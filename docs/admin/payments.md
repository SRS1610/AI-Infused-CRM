# Online payments (`payment`)

Customers pay invoices online from the invoice link in their email or from the customer portal; the
payment is booked and matched with the invoice automatically. Data model:
[ERD_PHASE_2.md §5](../architecture/ERD_PHASE_2.md).

## Providers

*Accounting → Configuration → Payment providers*. Every company gets two providers, both **disabled**:

| Provider | Use |
|---|---|
| **Stripe** | real card payments through Stripe Checkout (REST API, no SDK) |
| **Test payments** | settles immediately without any service; for demos and end-to-end tests |

A provider needs a **payment journal** (bank journal of its company) and a state: *Disabled*, *Test mode*
(shown with a "no real payment" notice) or *Enabled*. Customers see the enabled and test providers of the
invoice's company, by sequence. *Save payment methods* offers customers to keep their card for later
charges (subscriptions).

### Stripe setup

1. In the Stripe dashboard, copy the secret key (`sk_test_…` / `sk_live_…`) and the publishable key into the
   provider.
2. Add a webhook endpoint `https://<your server>/api/v1/payment/webhook/<provider id>` for the events
   `checkout.session.completed`, `checkout.session.async_payment_succeeded`,
   `checkout.session.async_payment_failed`, `checkout.session.expired`, `payment_intent.succeeded`,
   `payment_intent.payment_failed`, and copy its **signing secret** (`whsec_…`) into *Webhook signing secret*.
3. Set the state to *Test mode* (test keys) or *Enabled* (live keys).

The secret key, the webhook secret and saved-card references are stored **encrypted** with the platform
master key (`TESSERA_MASTER_KEY`, like SSO client secrets and AI provider keys) and only advisers and
administrators can read them.

Webhooks are authenticated by the `Stripe-Signature` header (`t=…,v1=…`: HMAC-SHA256 of `"<t>.<raw body>"`
with the signing secret, compared in constant time; signatures older than five minutes are refused).

## How a payment flows

1. The invoice page (`/portal/invoices/<id>?token=…`, or the portal for signed-in customers) shows **Pay
   now** when the invoice has an amount due and a provider is available.
2. `POST /api/v1/payment/invoices/<id>/pay` (the link's token, or the portal session) creates a
   **transaction** for the amount due and returns the provider's checkout URL; the customer pays there.
3. The provider's notification (webhook) — or, when the customer comes back first, a status check —
   reports the result. A confirmed transaction creates an **`account.payment`** in the provider's journal and
   matches it with the invoice (`register_payments`): the invoice ends *Paid*, or *In payment* when the
   journal waits for bank matching.

Every notification is recorded in *Provider notifications* with `UNIQUE (provider, event id)`: a
notification delivered again is a **no-op** (2-j), and a transaction never leaves a final state (late or
out-of-order notifications are ignored). An amount or currency that does not match the transaction puts it
in error instead of booking it. When the invoice was paid another way meanwhile, the payment is kept as a
customer credit (unmatched) for an accountant to allocate or refund.

**Failed and cancelled** transactions keep the provider's explanation in *Provider message* (e.g. "Your
card has insufficient funds."); the customer can try again.

## Saved methods, charges and refunds

- A saved method (`payment.token`, shown as "Visa •••• 4242") can be charged without the customer:
  `modules.payment.service.charge_token(token, amount, reference=…, invoices=…)` (used by subscriptions).
- **Refund** on a confirmed transaction (all or part) asks the provider to pay back and books an outbound
  payment to the customer.

## Endpoints

| Endpoint | Access | Purpose |
|---|---|---|
| `GET /api/v1/payment/invoices/{id}?token=` | link token or session | can it be paid, providers, recent attempts |
| `POST /api/v1/payment/invoices/{id}/pay` | link token or session | start a payment; returns `redirect_url` |
| `GET /api/v1/payment/invoices/{id}/status?reference=&token=` | link token or session | state of an attempt (checks the provider while pending) |
| `POST /api/v1/payment/webhook/{provider id}` | provider signature | notifications |

## AI: payment failure summary (`payment.failure_summary`)

Sub-phase 2.6. A failed or cancelled transaction keeps the provider's explanation (*Provider message*). The
panel *Why did this payment fail? (AI)* of such a transaction puts it in one category — **insufficient funds,
card expired, authentication required, fraud suspected, provider error, other** — with the next step to take
(update the card, send the link again for the bank's verification, never retry a suspected fraud, retry later
after a provider error…). The panel *Payment failures (AI)* of a provider lists the failures of the last 90
days per customer (count, amounts, main reason); **Summarise all failures** asks the model for a short
narrative whose figures must all be the summary's (a sentence with another number is removed).

How the category is chosen (deterministic, no model): a decline code in the message wins (Stripe's documented
codes such as `insufficient_funds`, `expired_card`, `authentication_required`, `stolen_card`,
`processing_error`, written `decline_code: …`, `code=…` or bare); otherwise phrases in English, French, German,
Spanish, Italian, Dutch and Portuguese, the **earliest** phrase in the message deciding, so text appended after
the provider's own words cannot change the category. An abandoned checkout ("the checkout session expired") is
*other*. Nothing is written, retried or sent; every request is an `ai_core.run`. The feature has a per-company
switch (*AI → Features*); switched off, the panels disappear and the endpoints answer `404 feature_disabled`.

| Endpoint | |
|---|---|
| `GET /api/v1/payment/ai/transactions/{id}/failure` | category, label, next step of one failed transaction |
| `GET /api/v1/payment/ai/failures?days=&provider_id=&partner_id=` | failures by category, customer and provider (current company, transactions the user can read) |
| `POST /api/v1/payment/ai/failures/narrative?days=` | the model's narrative (figures checked) |

Limits: messages worded unlike any known phrase fall back to *other* (5 of the 6 hold-out messages of the
evaluation set); a model fallback for those is a possible later step.

## Access

Billing users see providers (not their credentials), transactions and saved methods; advisers configure
providers and see notifications. Everything is per company. Transactions change only through the provider's
reports (nobody edits them by hand); a saved method can be renamed or archived, not moved to another
customer. Refunds and charges of a saved method need write access (billing users), not read-only access.
In production the test provider is refused and never offered to customers
([security review](../security/asvs-phase-2.md), SEC-53/SEC-54).

## Demo

The demo switches the test provider on (test mode, saving methods allowed) in every company and pays one
demo invoice (`DEMO-ONLINE`) online.

## Limits

- One provider adapter per service: Stripe and test. Others plug in with
  `modules.payment.providers.register_adapter(code, Adapter)` (see `providers/base.py`).
- Payments are captured immediately (no separate authorisation and capture step).
