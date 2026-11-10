"""Payment flows: test provider, Stripe webhooks (2-j: replay is a no-op), tokens, refunds, access."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from modules.account.tests.common import eu_env, invoice, partner
from modules.payment import service
from modules.payment.providers import ProviderEvent
from modules.payment.providers import stripe as stripe_mod
from modules.payment.providers.stripe import sign
from tessera.exceptions import AccessError, UserError

pytestmark = pytest.mark.db
WHSEC = "whsec_flow_test"


def _provider(env: Any, code: str = "test", **vals: Any) -> Any:
    """Switch one provider of the current company on (and the others off, so the choice is deterministic)."""
    providers = env["payment.provider"].search([("company_id", "=", env.company.ids[0])])
    provider = env.ref(f"payment.{env.company.ids[0]}_provider_{code}")
    (providers - provider).write({"state": "disabled"})
    provider.write({"state": "test", **vals})
    return provider


def test_providers_installed_per_company_and_secrets_encrypted(env):
    e = eu_env(env)
    for company in (env.company, e.company):
        codes = set(env["payment.provider"].elevated().search([("company_id", "=", company.ids[0])]).mapped("code"))
        assert {"stripe", "test"} <= codes
    stripe = _provider(env, "stripe", secret_key="sk_test_secret", webhook_secret=WHSEC)
    raw = env.cr.fetchone("SELECT secret_key, webhook_secret FROM payment_provider WHERE id = :i", {"i": stripe.ids[0]})
    assert raw[0].startswith("enc:v1:") and "sk_test_secret" not in raw[0]
    assert stripe._secret("secret_key") == "sk_test_secret" and stripe._secret("webhook_secret") == WHSEC
    clerk = env.with_user(
        env["core.user"]
        .create(
            {
                "name": "pay clerk",
                "login": "pay-clerk@test",
                "group_ids": [("set", [env.ref("account.group_account_invoice").ids[0]])],
            }
        )
        .ids[0]
    )
    assert clerk["payment.provider"].browse(stripe.ids).read(["name"])[0]["name"] == "Stripe"
    with pytest.raises(AccessError):
        clerk["payment.provider"].browse(stripe.ids).read(["secret_key"])  # credentials: advisers only
    with pytest.raises(AccessError):
        clerk["payment.provider"].browse(stripe.ids).write({"state": "enabled"})


def test_test_provider_pays_invoice_immediately(env):
    provider = _provider(env)
    inv = invoice(env, partner(env, "Online Buyer"), [{"price_unit": 99.0}])
    tx = service.create_invoice_transaction(inv)
    assert tx.provider_id == provider and tx.amount == pytest.approx(99.0) and tx.state == "draft"
    url = service.start_checkout(tx, return_url="https://app.test/return")
    assert url == "https://app.test/return"
    assert tx.state == "done" and tx.payment_id.state == "posted"
    assert inv.payment_state == "paid"  # booked straight on the bank account (journal without bank matching)
    assert tx.payment_id.journal_id == provider.journal_id
    with pytest.raises(UserError):
        service.create_invoice_transaction(inv)  # nothing left to pay
    with pytest.raises(UserError):
        service.start_checkout(tx, "x")


def test_outstanding_journal_leaves_invoice_in_payment(env):
    provider = _provider(env)
    provider.journal_id.write({"payment_through_outstanding": True})
    inv = invoice(env, partner(env, "Outstanding Buyer"), [{"price_unit": 10.0}])
    service.start_checkout(service.create_invoice_transaction(inv), "https://app.test/r")
    assert inv.payment_state == "in_payment"


def _stripe(env: Any, monkeypatch: Any) -> tuple[Any, list[httpx.Request]]:
    provider = _provider(env, "stripe", secret_key="sk_test_flow", webhook_secret=WHSEC)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/v1/checkout/sessions":
            return httpx.Response(
                200, json={"id": f"cs_{uuid.uuid4().hex[:8]}", "url": "https://checkout.stripe.test/s"}
            )
        return httpx.Response(404, json={"error": {"message": "nope"}})

    monkeypatch.setattr(stripe_mod, "transport_factory", lambda: httpx.MockTransport(handler))
    return provider, calls


def _completed(tx: Any, event_id: str, amount: float | None = None) -> bytes:
    session = {
        "id": tx.provider_reference,
        "client_reference_id": tx.reference,
        "payment_status": "paid",
        "amount_total": round((amount if amount is not None else tx.amount) * 100),
        "currency": tx.currency_id.name.lower(),
        "payment_intent": f"pi_{event_id}",
    }
    return json.dumps({"id": event_id, "type": "checkout.session.completed", "data": {"object": session}}).encode()


def test_stripe_webhook_settles_once(env, monkeypatch):  # type: ignore[no-untyped-def]
    provider, calls = _stripe(env, monkeypatch)
    inv = invoice(env, partner(env, "Stripe Buyer"), [{"price_unit": 200.0}])
    tx = service.create_invoice_transaction(inv, provider)
    url = service.start_checkout(tx, "https://app.test/return")
    assert url == "https://checkout.stripe.test/s" and tx.state == "pending" and len(calls) == 1
    body = _completed(tx, "evt_settle_1")
    assert service.handle_webhook(provider, {"Stripe-Signature": sign(body, WHSEC)}, body) == ["processed"]
    assert tx.state == "done" and inv.payment_state == "paid"
    payments = env["account.payment"].search([("ref", "=", tx.reference)])
    assert len(payments) == 1
    # 2-j: the same notification again changes nothing
    assert service.handle_webhook(provider, {"Stripe-Signature": sign(body, WHSEC)}, body) == ["duplicate"]
    # a different notification for the same payment is ignored too (the transaction is final)
    other = _completed(tx, "evt_settle_2")
    assert service.handle_webhook(provider, {"Stripe-Signature": sign(other, WHSEC)}, other) == ["ignored"]
    assert env["account.payment"].search_count([("ref", "=", tx.reference)]) == 1
    assert env["payment.event"].search_count([("transaction_id", "=", tx.ids[0])]) == 2


def test_failed_payment_keeps_message(env, monkeypatch):  # type: ignore[no-untyped-def]
    provider, _calls = _stripe(env, monkeypatch)
    inv = invoice(env, partner(env, "Declined Buyer"), [{"price_unit": 50.0}])
    tx = service.create_invoice_transaction(inv, provider)
    service.start_checkout(tx, "https://app.test/return")
    failed = json.dumps(
        {
            "id": "evt_fail_1",
            "type": "payment_intent.payment_failed",
            "data": {
                "object": {
                    "id": "pi_x",
                    "metadata": {"reference": tx.reference},
                    "last_payment_error": {"message": "Card expired"},
                }
            },
        }
    ).encode()
    service.handle_webhook(provider, {"Stripe-Signature": sign(failed, WHSEC)}, failed)
    assert tx.state == "error" and tx.state_message == "Card expired"
    assert inv.payment_state == "not_paid" and not tx.payment_id
    # a new attempt is possible after a failure
    assert service.create_invoice_transaction(inv, provider).state == "draft"


def test_amount_mismatch_and_unknown_events(env, monkeypatch):  # type: ignore[no-untyped-def]
    provider, _calls = _stripe(env, monkeypatch)
    inv = invoice(env, partner(env, "Mismatch Buyer"), [{"price_unit": 80.0}])
    tx = service.create_invoice_transaction(inv, provider)
    service.start_checkout(tx, "https://app.test/return")
    body = _completed(tx, "evt_mismatch", amount=1.0)
    service.handle_webhook(provider, {"Stripe-Signature": sign(body, WHSEC)}, body)
    assert tx.state == "error" and "mismatch" in tx.state_message
    unknown = ProviderEvent(event_id="evt_orphan", event_type="x", state="done", reference="NOPE")
    assert service.process_event(provider, unknown) == "unknown"
    assert service.process_event(provider, unknown) == "duplicate"


def test_invoice_paid_meanwhile_keeps_customer_credit(env):
    provider = _provider(env)
    inv = invoice(env, partner(env, "Twice Buyer"), [{"price_unit": 40.0}])
    tx = service.create_invoice_transaction(inv, provider)
    inv.action_register_payment()
    tx.write({"provider_reference": "manual"})
    tx._set_state("pending")
    event = ProviderEvent(event_id="late-1", event_type="test", state="done", reference=tx.reference, amount=40.0)
    assert service.process_event(provider, event) == "processed"
    assert tx.state == "done" and tx.payment_id.state == "posted" and not tx.payment_id.reconciled_invoice_ids


def test_tokens_charge_and_refund(env):
    provider = _provider(env, allow_tokenization=True)
    customer = partner(env, "Token Buyer")
    inv = invoice(env, customer, [{"price_unit": 60.0}])
    tx = service.create_invoice_transaction(inv, tokenize=True)
    service.start_checkout(tx, "https://app.test/r")
    token = tx.token_id
    assert token.display.endswith("4242") and token.partner_id == customer.commercial_partner_id
    raw = env.cr.fetchone("SELECT provider_ref FROM payment_token WHERE id = :i", {"i": token.ids[0]})
    assert raw[0].startswith("enc:v1:")
    inv2 = invoice(env, customer, [{"price_unit": 25.0}])
    charged = service.charge_token(token, 25.0, reference="SUB-1", invoices=inv2)
    assert charged.state == "done" and inv2.payment_state == "paid" and charged.operation == "offline"
    result = token.action_charge(5.0)
    assert result["state"] == "done"
    bad = env["payment.token"].create(
        {
            "provider_id": provider.ids[0],
            "partner_id": customer.ids[0],
            "provider_ref": "fail-card",
            "display": "•••• 0002",
        }
    )
    declined = service.charge_token(bad, 10.0, reference="SUB-2")
    assert declined.state == "error" and "declined" in declined.state_message
    refund = tx.action_refund(20.0)
    assert refund["state"] == "done"
    refund_tx = env["payment.transaction"].search([("reference", "=", refund["reference"])])
    assert refund_tx.payment_id.payment_type == "outbound" and refund_tx.source_transaction_id == tx
    with pytest.raises(UserError):
        tx.action_refund(100.0)  # more than what is left
    with pytest.raises(UserError):
        refund_tx.action_refund()
    provider.write({"state": "disabled"})
    with pytest.raises(UserError):
        service.charge_token(token, 1.0)


def test_test_provider_webhook(env):
    provider = _provider(env)
    inv = invoice(env, partner(env, "Hook Buyer"), [{"price_unit": 12.0}])
    tx = service.create_invoice_transaction(inv)
    tx._set_state("pending")
    body = json.dumps(
        {
            "id": "test-evt-1",
            "type": "test.cancel",
            "state": "cancel",
            "reference": tx.reference,
            "message": "Customer left",
        }
    ).encode()
    from modules.payment.providers.testing import DEFAULT_SECRET

    assert service.handle_webhook(provider, {"Test-Signature": sign(body, DEFAULT_SECRET)}, body) == ["processed"]
    assert tx.state == "cancel" and tx.state_message == "Customer left"
    assert service.sync_transaction(tx).state == "cancel"


def test_company_isolation_and_portal_access(env):
    e = eu_env(env)
    _provider(e)
    eu_inv = invoice(e, partner(e, "EU Buyer"), [{"price_unit": 10.0}])
    eu_tx = service.create_invoice_transaction(eu_inv)
    user = env["core.user"].create(
        {
            "name": "US accountant",
            "login": "pay-us@test",
            "company_id": env.ref("core.main_company").ids[0],
            "company_ids": [("set", env.ref("core.main_company").ids)],
            "group_ids": [("set", [env.ref("account.group_account_user").ids[0]])],
        }
    )
    us = env.with_user(user.ids[0])
    assert not us["payment.transaction"].search([("id", "=", eu_tx.ids[0])])
    assert not us["payment.provider"].search([("company_id", "=", e.company.ids[0])])
    assert not us["payment.event"].search([("company_id", "=", e.company.ids[0])])
    clerk = env["core.user"].create(
        {
            "name": "pay billing",
            "login": "pay-billing@test",
            "group_ids": [("set", [env.ref("account.group_account_invoice").ids[0]])],
        }
    )
    with pytest.raises(AccessError):
        env.with_user(clerk.ids[0])["payment.event"].search([])  # notifications: accountants and advisers
    with pytest.raises(UserError):
        service.create_invoice_transaction(eu_inv, env.ref(f"payment.{env.company.ids[0]}_provider_test"))


def test_demo_payment(env, system_env):
    from modules.payment.demo import seed_company

    tx = seed_company(system_env, env.ref("core.main_company"))
    assert tx is not None and tx.state == "done" and tx.token_id
    assert tx.invoice_ids.payment_state in ("paid", "in_payment")
    assert seed_company(system_env, env.ref("core.main_company")) is None


def test_new_company_gets_providers_and_bank_rules(env):
    eur = env["core.currency"].search([("name", "=", "EUR")], limit=1)
    company = env["core.company"].create({"name": "Fresh Co", "currency_id": eur.ids[0]})
    assert company.chart_template
    providers = env["payment.provider"].elevated().search([("company_id", "=", company.ids[0])])
    assert set(providers.mapped("code")) == {"stripe", "test"} and set(providers.mapped("state")) == {"disabled"}
    rules = env["account.reconcile_model"].elevated().search([("company_id", "=", company.ids[0])])
    assert len(rules) == 3
