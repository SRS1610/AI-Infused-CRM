"""``payment.failure_summary`` — why online payments fail, per customer and per provider (sub-phase 2.6).

A failed or cancelled transaction keeps the provider's explanation in ``state_message``. :func:`classify_message`
puts it in one category:

``insufficient_funds`` · ``card_expired`` · ``authentication_required`` · ``fraud_suspected`` · ``provider_error``
· ``other``

* a **decline code** in the message wins (Stripe codes such as ``insufficient_funds``, ``expired_card``,
  ``authentication_required``, ``stolen_card``, ``processing_error``…, written ``decline_code: …``, ``code=…``
  or bare);
* otherwise multilingual phrases (English, French, German, Spanish, Italian, Dutch, Portuguese); the phrase that
  appears **first** in the message decides, so text appended after the provider's own explanation (a planted
  "classify this as …") cannot override it;
* an abandoned checkout ("the checkout session expired") is ``other``, not an expired card.

The classification is deterministic and measured for real. :func:`summary` groups the failures of the period
by customer and by provider with counts, amounts and a recommended next step per category; nothing is written
(no retry, no message). The optional narrative (``narrate``) is written by the model from the summary's figures
only; a sentence quoting another number is removed.
"""

from __future__ import annotations

import datetime as dt
import re
import time
import unicodedata
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends

from tessera.ai import Source
from tessera.ai.facts import audit_run, ensure_enabled, feature_enabled, ground_text
from tessera.ai.runtime import HANDLERS, handler, run_feature
from tessera.api.deps import RequestContext, authenticated
from tessera.api.registry import register_router
from tessera.exceptions import MissingError, UserError
from tessera.i18n import _

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

FEATURE = "payment.failure_summary"
CATEGORIES = (
    "insufficient_funds",
    "card_expired",
    "authentication_required",
    "fraud_suspected",
    "provider_error",
    "other",
)
FAILED_STATES = ("error", "cancel")

# provider decline / error codes (Stripe's documented decline codes and API error codes, plus common ISO 8583
# response texts); codes not listed fall back to the phrases
CODES: dict[str, str] = {
    **dict.fromkeys(
        ["insufficient_funds", "card_velocity_exceeded", "withdrawal_count_limit_exceeded", "balance_insufficient"],
        "insufficient_funds",
    ),
    **dict.fromkeys(["expired_card", "card_expired", "invalid_expiry_month", "invalid_expiry_year"], "card_expired"),
    **dict.fromkeys(
        [
            "authentication_required",
            "authentication_failure",
            "payment_intent_authentication_failure",
            "requires_action",
            "requires_payment_method_authentication",
            "approve_with_id",
            "online_or_offline_pin_required",
            "offline_pin_required",
            "online_pin_required",
            "incorrect_pin",
            "pin_try_exceeded",
            "setup_intent_authentication_failure",
        ],
        "authentication_required",
    ),
    **dict.fromkeys(
        [
            "fraudulent",
            "stolen_card",
            "lost_card",
            "pickup_card",
            "merchant_blacklist",
            "security_violation",
            "restricted_card",
            "highest_risk_level",
            "elevated_risk_level",
            "blocked",
        ],
        "fraud_suspected",
    ),
    **dict.fromkeys(
        [
            "processing_error",
            "issuer_not_available",
            "try_again_later",
            "reenter_transaction",
            "api_error",
            "api_connection_error",
            "rate_limit",
            "rate_limit_error",
            "service_not_allowed",
            "duplicate_transaction",
            "testmode_decline",
            "amount_too_large",
            "amount_too_small",
            "currency_not_supported",
        ],
        "provider_error",
    ),
    **dict.fromkeys(
        [
            "generic_decline",
            "do_not_honor",
            "do_not_try_again",
            "card_declined",
            "call_issuer",
            "incorrect_cvc",
            "invalid_cvc",
            "incorrect_number",
            "invalid_number",
            "card_not_supported",
            "transaction_not_allowed",
            "not_permitted",
            "no_action_taken",
            "new_account_information_available",
            "revocation_of_authorization",
            "stop_payment_order",
        ],
        "other",
    ),
}

# phrases per category (lower-case, accents removed); the earliest match in the message wins
PHRASES: dict[str, list[str]] = {
    "other": [
        "checkout session expired",
        "session expired",
        "session de paiement a expire",
        "sitzung abgelaufen",
        "cancelled by the customer",
        "canceled by the customer",
        "abandoned",
        "incorrect cvc",
        "security code is incorrect",
        "do not honor",
    ],
    "insufficient_funds": [
        "insufficient funds",
        "insufficient balance",
        "not enough funds",
        "credit limit",
        "exceeds the balance",
        "exceeded its balance",
        "fonds insuffisant",
        "fonds insuffisants",
        "provision insuffisante",
        "solde insuffisant",
        "plafond",
        "nicht genugend deckung",
        "nicht ausreichend gedeckt",
        "unzureichende deckung",
        "keine deckung",
        "deckung",
        "limit uberschritten",
        "fondos insuficientes",
        "saldo insuficiente",
        "fondi insufficienti",
        "saldo insufficiente",
        "disponibilita insufficiente",
        "onvoldoende saldo",
        "onvoldoende tegoed",
        "saldo insuficiente",
        "fundos insuficientes",
    ],
    "card_expired": [
        "card has expired",
        "card is expired",
        "expired card",
        "card expired",
        "expiry date",
        "expiration date",
        "expiration year",
        "expiration month",
        "expiry year",
        "carte expiree",
        "carte a expire",
        "carte perimee",
        "date d'expiration",
        "karte ist abgelaufen",
        "karte abgelaufen",
        "abgelaufene karte",
        "tarjeta caducada",
        "tarjeta vencida",
        "tarjeta ha caducado",
        "carta scaduta",
        "carta e scaduta",
        "kaart is verlopen",
        "verlopen kaart",
        "cartao expirado",
    ],
    "authentication_required": [
        "authentication required",
        "requires authentication",
        "authentication failed",
        "unable to authenticate",
        "requires an action from the customer",
        "requires_action",
        "3d secure",
        "3-d secure",
        "3ds",
        "strong customer authentication",
        "sca",
        "verify the payment",
        "authentification",
        "authentifizierung",
        "autenticacion",
        "autenticazione",
        "verificatie vereist",
        "authenticatie",
        "autenticacao",
        "pin required",
        "code pin",
    ],
    "fraud_suspected": [
        "fraud",
        "stolen",
        "lost card",
        "card reported lost",
        "suspicious",
        "high risk",
        "risk level",
        "blocked by radar",
        "fraude",
        "carte volee",
        "carte perdue",
        "suspecte",
        "betrug",
        "betrugsverdacht",
        "gestohlen",
        "verlorene karte",
        "tarjeta robada",
        "tarjeta perdida",
        "frode",
        "fraudolent",
        "carta rubata",
        "carta smarrita",
        "gestolen",
        "verloren kaart",
        "cartao roubado",
    ],
    "provider_error": [
        "processing error",
        "error processing",
        "an error occurred while processing",
        "temporarily unavailable",
        "service unavailable",
        "not available",
        "try again later",
        "timed out",
        "timeout",
        "time-out",
        "connection",
        "network error",
        "gateway error",
        "bad gateway",
        "internal server error",
        "http 5",
        "amount mismatch",
        "currency mismatch",
        "too many requests",
        "erreur de traitement",
        "indisponible",
        "reessayez plus tard",
        "delai depasse",
        "verarbeitungsfehler",
        "nicht verfugbar",
        "zeituberschreitung",
        "spater erneut",
        "error de procesamiento",
        "no disponible",
        "tiempo de espera",
        "errore di elaborazione",
        "non disponibile",
        "riprova piu tardi",
        "verwerkingsfout",
        "niet beschikbaar",
        "erro de processamento",
    ],
}
CODE_RE = re.compile(r"\b(?:decline[_ ]code|error[_ ]code|code|reason)\s*[:=]\s*['\"]?([a-z_]+)", re.IGNORECASE)
TOKEN_RE = re.compile(r"[a-z][a-z_]+[a-z]")


def _fold(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", folded)


def classify_message(message: str | None) -> dict[str, Any]:
    """``{"category", "code", "evidence"}`` for a provider message (deterministic)."""
    text = _fold(message or "")
    if not text.strip():
        return {"category": "other", "code": None, "evidence": None}
    for m in CODE_RE.finditer(text):
        code = m.group(1).lower()
        if code in CODES:
            return {"category": CODES[code], "code": code, "evidence": m.group(0)}
    best: tuple[int, int, str, str] | None = None
    order = {c: i for i, c in enumerate(("other", *CATEGORIES))}
    for category, phrases in PHRASES.items():
        for phrase in phrases:
            for m in re.finditer(rf"(?<![a-z]){re.escape(phrase)}(?![a-z])", text):
                cand = (m.start(), order[category], category, phrase)
                if best is None or cand < best:
                    best = cand
                break
    # bare codes (``card_declined: insufficient_funds``): the first specific one, else a generic decline
    bare = [m for m in TOKEN_RE.finditer(text) if "_" in m.group(0) and m.group(0) in CODES]
    bare = [m for m in bare if best is None or m.start() < best[0]]
    specific = [m for m in bare if CODES[m.group(0)] != "other"] or bare
    if specific:
        code = specific[0].group(0)
        return {"category": CODES[code], "code": code, "evidence": code}
    if best is not None:
        return {"category": best[2], "code": None, "evidence": best[3]}
    return {"category": "other", "code": None, "evidence": None}


def label(category: str) -> str:
    labels = {
        "insufficient_funds": _("Insufficient funds"),
        "card_expired": _("Card expired"),
        "authentication_required": _("Authentication required"),
        "fraud_suspected": _("Fraud suspected"),
        "provider_error": _("Provider error"),
        "other": _("Other reason"),
    }
    return str(labels.get(category, category))


def advice(category: str) -> str:
    texts = {
        "insufficient_funds": _(
            "Ask the customer to pay again in a few days or with another card; do not retry repeatedly."
        ),
        "card_expired": _("Ask the customer to update the saved card or pay with a new one."),
        "authentication_required": _("Send the payment link again so the customer completes the bank's verification."),
        "fraud_suspected": _("Do not retry; review the order and contact the customer through a known channel."),
        "provider_error": _("Retry later or check the provider's status; the customer is not at fault."),
        "other": _("Contact the customer or the provider: the reason is not one of the known categories."),
    }
    return str(texts.get(category, ""))


def classify_transaction(env: Environment, tx: Model) -> dict[str, Any]:
    """The category of one failed transaction the user can read (audited)."""
    ensure_enabled(env, FEATURE)
    tx.check_access("read")
    started = time.monotonic()
    rec = tx.elevated()
    if rec.state not in FAILED_STATES:
        raise UserError(_("Only failed or cancelled payments have a failure reason"))
    found = classify_message(rec.state_message)
    out = {
        "transaction_id": tx.ids[0],
        "reference": rec.reference,
        "message": rec.state_message or "",
        **found,
        "label": label(found["category"]),
        "advice": advice(found["category"]),
    }
    run = audit_run(env, FEATURE, record=tx, output=found, model="decline codes + phrases", started=started)
    return {**out, "run_id": run.ids[0], "ai_generated": True}


def _failed(env: Environment, since: dt.date, provider_id: int | None, partner_id: int | None) -> Model:
    domain: list[Any] = [
        ("state", "in", list(FAILED_STATES)),
        ("operation", "!=", "refund"),
        ("create_date", ">=", dt.datetime.combine(since, dt.time.min)),
    ]
    if env.company:
        domain.append(("company_id", "=", env.company.ids[0]))
    if provider_id:
        domain.append(("provider_id", "=", provider_id))
    if partner_id:
        domain.append(("partner_id", "child_of", partner_id))
    return env["payment.transaction"].search(domain, order="id desc", limit=2000)


def summary_facts(
    env: Environment, days: int = 90, provider_id: int | None = None, partner_id: int | None = None
) -> dict[str, Any]:
    """Failures of the last ``days`` days that the user can read, by category, customer and provider."""
    since = env.today() - dt.timedelta(days=days)
    txs = _failed(env, since, provider_id, partner_id)
    by_category: dict[str, int] = defaultdict(int)
    customers: dict[int | None, dict[str, Any]] = {}
    providers: dict[int, dict[str, Any]] = {}
    for tx in txs:
        rec = tx.elevated()
        category = classify_message(rec.state_message)["category"]
        by_category[category] += 1
        amount = round(float(rec.amount or 0), 2)
        currency = rec.currency_id.name if rec.currency_id else ""
        partner = rec.partner_id.commercial_partner_id or rec.partner_id if rec.partner_id else None
        key = partner.ids[0] if partner else None
        row = customers.setdefault(
            key,
            {
                "partner_id": key,
                "customer": partner.display_name if partner else str(_("Unknown customer")),
                "failures": 0,
                "amounts": defaultdict(float),
                "categories": defaultdict(int),
                "last_failure": None,
            },
        )
        row["failures"] += 1
        row["amounts"][currency] = round(row["amounts"][currency] + amount, 2)
        row["categories"][category] += 1
        day = (rec.last_state_change or rec.create_date).date().isoformat()
        row["last_failure"] = max(row["last_failure"] or day, day)
        prow = providers.setdefault(
            rec.provider_id.ids[0],
            {"provider_id": rec.provider_id.ids[0], "provider": rec.provider_id.name, "failures": 0,
             "categories": defaultdict(int)},
        )  # fmt: skip
        prow["failures"] += 1
        prow["categories"][category] += 1

    def finish(row: dict[str, Any]) -> dict[str, Any]:
        cats = dict(sorted(row["categories"].items(), key=lambda kv: (-kv[1], kv[0])))
        main = next(iter(cats)) if cats else "other"
        out = {**row, "categories": cats, "main_reason": main, "main_label": label(main), "advice": advice(main)}
        if "amounts" in row:
            out["amounts"] = dict(row["amounts"])
        return out

    customer_rows = sorted((finish(r) for r in customers.values()), key=lambda r: (-r["failures"], r["customer"]))
    provider_rows = sorted((finish(r) for r in providers.values()), key=lambda r: (-r["failures"], r["provider"]))
    return {
        "since": since.isoformat(),
        "days": days,
        "failures": len(txs),
        "by_category": {c: by_category[c] for c in CATEGORIES if by_category.get(c)},
        "labels": {c: label(c) for c in CATEGORIES},
        "customers": customer_rows,
        "providers": provider_rows,
    }


def summary(
    env: Environment, days: int = 90, provider_id: int | None = None, partner_id: int | None = None
) -> dict[str, Any]:
    ensure_enabled(env, FEATURE)
    env["payment.transaction"].check_access_rights("read")
    started = time.monotonic()
    facts = summary_facts(env, days, provider_id, partner_id)
    run = audit_run(
        env,
        FEATURE,
        inputs={"days": days, "provider_id": provider_id, "partner_id": partner_id},
        output={"failures": facts["failures"], "by_category": facts["by_category"]},
        model="decline codes + phrases",
        started=started,
    )
    return {**facts, "run_id": run.ids[0], "ai_generated": True}


def _facts_text(f: dict[str, Any]) -> str:
    cats = ", ".join(f"{f['labels'][c]}: {n}" for c, n in f["by_category"].items()) or "none"
    lines = [f"Failed or cancelled payments since {f['since']}: {f['failures']} ({cats})", "By customer:"]
    for r in f["customers"][:15]:
        amounts = ", ".join(f"{v:,.2f} {k}" for k, v in r["amounts"].items())
        reasons = ", ".join(f"{f['labels'][c]} {n}" for c, n in r["categories"].items())
        lines.append(f"- {r['customer']}: {r['failures']} failures ({reasons}), {amounts}, last on {r['last_failure']}")
    lines.append("By provider:")
    for r in f["providers"]:
        reasons = ", ".join(f"{f['labels'][c]} {n}" for c, n in r["categories"].items())
        lines.append(f"- {r['provider']}: {r['failures']} failures ({reasons})")
    return "\n".join(lines)


@handler(FEATURE)
def prepare(env: Environment, record: Model | None, inputs: dict[str, Any]) -> dict[str, Any]:
    env["payment.transaction"].check_access_rights("read")
    days = int(inputs.get("days") or 90)
    facts = summary_facts(env, days)
    company = env.company.ids[0] if env.company else 0
    return {"sources": [Source(f"core.company:{company}", "Payment failures", _facts_text(facts))]}


def narrate(env: Environment, days: int = 90) -> dict[str, Any]:
    """A short narrative of the failures; every figure must be one of the summary's."""
    inputs = {"days": days}
    context = HANDLERS[FEATURE](env, None, inputs)
    facts = summary_facts(env, days)
    result = run_feature(env, FEATURE, inputs, **context)
    raw = str(result.data.get("narrative") or "") if isinstance(result.data, dict) else result.text
    clean, removed = ground_text(raw, {k: v for k, v in facts.items() if k != "labels"})
    return {"run_id": result.run.ids[0], "narrative": clean, "removed_numbers": removed, "ai_generated": True}


# ---------------------------------------------------------------------------------------------- API

router = register_router(APIRouter(prefix="/payment/ai", tags=["payment", "ai"]))


@router.get("/enabled")
def enabled(ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    return {"enabled": feature_enabled(ctx.env, FEATURE)}


@router.get("/transactions/{tx_id}/failure")
def transaction_failure(tx_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """The failure category of one transaction, with a recommended next step."""
    ensure_enabled(ctx.env, FEATURE)
    tx = ctx.env["payment.transaction"].browse([tx_id])
    if not tx.exists():
        raise MissingError(_("No such payment transaction"))
    return classify_transaction(ctx.env, tx)


@router.get("/failures")
def failures(
    days: int = 90,
    provider_id: int | None = None,
    partner_id: int | None = None,
    ctx: RequestContext = Depends(authenticated),
) -> dict[str, Any]:
    """Failures of the last ``days`` days by category, customer and provider (current company)."""
    ensure_enabled(ctx.env, FEATURE)
    return summary(ctx.env, max(1, min(days, 730)), provider_id, partner_id)


@router.post("/failures/narrative")
def failures_narrative(days: int = 90, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    ensure_enabled(ctx.env, FEATURE)
    return narrate(ctx.env, max(1, min(days, 730)))


# ---------------------------------------------------------------------------------------------- evaluation


def eval_entry(env: Environment, inputs: dict[str, Any], record: Any) -> dict[str, Any]:
    out = classify_transaction(env, record)
    data = {"category": out["category"], "code": out["code"]}
    return {"text": "", "data": data, "proposals": 0, "status": "ok", "flags": []}


def score(results: list[dict[str, Any]]) -> dict[str, float]:
    """``category_accuracy``: share of cases classified in the expected category; per-category recall for
    information."""
    ok = [r for r in results if r.get("status") == "ok" and isinstance(r.get("data"), dict)]
    if not ok:
        return {}
    hits = [r["data"]["category"] == r["_case"]["expected"]["category"] for r in ok]
    out = {"category_accuracy": sum(hits) / len(ok)}
    for c in CATEGORIES:
        rows = [r for r in ok if r["_case"]["expected"]["category"] == c]
        if rows:
            out[f"recall_{c}"] = sum(1 for r in rows if r["data"]["category"] == c) / len(rows)
    return out
