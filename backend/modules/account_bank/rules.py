"""Reconciliation rules engine: candidate search, scoring, suggestions and auto-reconciliation.

``suggest_matches(statement_line, limit)`` is the service the reconciliation screen (and the sub-phase 2.6
AI re-ranking) uses: it returns :class:`Candidate` objects ranked by a 0-1 score with machine-readable
reason codes. Candidates are single open items, groups of items (several invoices paid at once, found by
their references or because together they make the amount) and write-off suggestions from the rules.

``auto_reconcile(lines)`` applies the rules marked *Reconcile automatically* in sequence; a rule applies
only when it explains the whole transaction (an invoice found by reference may be paid partly).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from dateutil.relativedelta import relativedelta

from tessera.i18n import _
from tessera.logging import get_logger
from tessera.orm import Model

log = get_logger(__name__)

SCORES = {
    "reference": 0.55,
    "amount_exact": 0.25,
    "amount_tolerance": 0.15,
    "amount_partial": 0.05,
    "partner": 0.2,
    "partner_name": 0.1,
    "group": 0.0,
    "label": 0.6,
    "rule_conditions": 0.0,
    "partner_rule": 0.05,
}
MAX_ITEMS = 2000


def reason_message(code: str) -> str:
    messages = {
        "reference": _("The label mentions the document's reference"),
        "amount_exact": _("Same amount"),
        "amount_tolerance": _("Amount within the rule's tolerance"),
        "amount_partial": _("Partial payment of the item"),
        "partner": _("Same partner"),
        "partner_name": _("The counterparty name matches the partner"),
        "group": _("Several items together"),
        "label": _("The label matches the rule"),
        "rule_conditions": _("The rule's conditions are met"),
        "partner_rule": _("Partner condition of the rule"),
    }
    return str(messages.get(code, code))


@dataclass
class Candidate:
    """A proposed match for a bank transaction."""

    kind: str  # "items" | "writeoff"
    items: Model | None = None
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    model: Model | None = None
    writeoffs: list[dict[str, Any]] = field(default_factory=list)
    amount: float = 0.0  # open amount of the items, in the transaction's currency

    def add(self, code: str) -> None:
        if code not in self.reasons:
            self.reasons.append(code)
            self.score = min(1.0, round(self.score + SCORES.get(code, 0.0), 4))

    @property
    def line_ids(self) -> list[int]:
        return self.items.ids if self.items is not None else []

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "line_ids": self.line_ids,
            "items": [item_payload(i) for i in self.items] if self.items is not None else [],
            "amount": self.amount,
            "score": self.score,
            "reasons": [{"code": c, "message": reason_message(c)} for c in self.reasons],
            "model": {"id": self.model.ids[0], "name": self.model.name} if self.model else None,
            "writeoffs": self.writeoffs,
        }


def _norm(text: str | None) -> str:
    return re.sub(r"[^0-9A-Z]", "", (text or "").upper())


def _text_of(st_line: Model) -> str:
    return " ".join(p for p in (st_line.payment_ref, st_line.ref, st_line.narration) if p)


def _references(item: Model) -> list[str]:
    move = item.move_id
    refs = [move.name if move.name != "/" else None, move.payment_reference, move.ref]
    payment = item.payment_id
    if payment:
        refs.extend([payment.name, payment.ref])
    return [r for r in refs if r]


def reference_found(ref: str, text: str, norm_text: str) -> bool:
    clean = _norm(ref)
    if len(clean) >= 6 and clean in norm_text:
        return True
    if len(clean) >= 3 and any(ch.isdigit() for ch in clean):
        pattern = r"(?<![0-9A-Za-z])" + re.escape(ref.strip()) + r"(?![0-9A-Za-z])"
        return bool(re.search(pattern, text, flags=re.IGNORECASE))
    return False


def item_payload(item: Model) -> dict[str, Any]:
    move = item.move_id
    kind = (
        "invoice"
        if move.move_type in ("out_invoice", "in_refund")
        else "bill"
        if move.move_type in ("in_invoice", "out_refund")
        else "payment"
        if item.payment_id
        else "entry"
    )
    if move.move_type in ("out_refund", "in_refund"):
        kind = "credit_note"
    currency = item.currency_id or item.company_id.currency_id
    return {
        "id": item.ids[0],
        "move_id": move.ids[0],
        "move_name": move.name,
        "kind": kind,
        "ref": move.ref or (item.payment_id.ref if item.payment_id else None),
        "date": item.date.isoformat() if item.date else None,
        "date_maturity": item.date_maturity.isoformat() if item.date_maturity else None,
        "partner": item.partner_id.display_name if item.partner_id else None,
        "partner_id": item.partner_id.ids[0] if item.partner_id else None,
        "account": item.account_id.display_name,
        "label": item.name,
        "amount_residual": float(item.amount_residual),
        "amount_residual_currency": float(item.amount_residual_currency),
        "currency": currency.name,
    }


def _open_item_domain(st_line: Model, months: int = 18) -> list[Any]:
    company = st_line.company_id.elevated()
    suspense = st_line.journal_id._bank_suspense_account()
    accounts = (
        st_line.env["account.account"]
        .elevated()
        .search(
            [
                ("company_id", "=", company.ids[0]),
                ("reconcile", "=", True),
                {
                    "any": [
                        ("account_type", "in", ["asset_receivable", "liability_payable"]),
                        (
                            "id",
                            "in",
                            [
                                *company.account_outstanding_receipts_id.ids,
                                *company.account_outstanding_payments_id.ids,
                            ],
                        ),
                    ]
                },
            ]
        )
    )
    domain: list[Any] = [
        ("company_id", "=", company.ids[0]),
        ("parent_state", "=", "posted"),
        ("reconciled", "=", False),
        ("account_id", "in", [a for a in accounts.ids if a not in suspense.ids]),
        {"any": [("amount_residual", "!=", 0), ("amount_residual_currency", "!=", 0)]},
    ]
    if st_line.move_id:
        domain.append(("move_id", "!=", st_line.move_id.ids[0]))
    if months:
        domain.append(("date", ">=", (st_line.date or st_line.env.today()) - relativedelta(months=months)))
    return domain


def _open_items(st_line: Model, months: int = 18) -> Model:
    return (
        st_line.env["account.move_line"]
        .elevated("bank matching")
        .search(_open_item_domain(st_line, months), order="date_maturity, date, id", limit=MAX_ITEMS)
    )


def _in_line_currency(st_line: Model, item: Model) -> float:
    jcur = st_line.journal_id._bank_currency()
    ccur = st_line.company_id.elevated().currency_id
    tcur = item.currency_id or ccur
    residual = float(item.amount_residual_currency if tcur.ids != ccur.ids else item.amount_residual)
    if tcur.ids == jcur.ids:
        return residual
    return float(tcur.convert(residual, jcur, st_line.company_id.elevated(), st_line.date))


def _open_amount(st_line: Model) -> float:
    """What is left to explain, in the transaction's currency (the whole amount when not matched yet)."""
    return float(st_line.amount_residual if st_line.state != "draft" else st_line.amount)


def _partner_tokens(partner: Model) -> set[str]:
    stop = {"SA", "SAS", "SARL", "GMBH", "AG", "BV", "NV", "LTD", "LLC", "INC", "CORP", "CO", "THE", "AND", "DE", "LA"}
    return {t for t in re.findall(r"[A-Z0-9]{2,}", (partner.name or "").upper()) if t not in stop}


def _item_candidates(st_line: Model, model: Model | None = None, items: Model | None = None) -> list[Candidate]:
    open_amount = _open_amount(st_line)
    if not open_amount:
        return []
    tolerance = model._tolerance(open_amount) if model else 0.0
    months = int(model.past_months_limit or 18) if model else 18
    items = items if items is not None else _open_items(st_line, months)
    text = _text_of(st_line)
    norm_text = _norm(text)
    name_text = f"{st_line.partner_name or ''} {st_line.payment_ref or ''}".upper()
    st_partner = st_line.partner_id.elevated()
    commercial = (st_partner.commercial_partner_id or st_partner) if st_partner else None
    use_refs = model.match_text_location if model else True
    singles: list[Candidate] = []
    by_reference = items.browse([])
    by_partner = items.browse([])
    for item in items:
        residual = _in_line_currency(st_line, item)
        if not residual:
            continue
        cand = Candidate(kind="items", items=item, amount=residual)
        if use_refs and any(reference_found(r, text, norm_text) for r in _references(item)):
            cand.add("reference")
            by_reference |= item
        partner = item.partner_id.elevated()
        item_commercial = (partner.commercial_partner_id or partner) if partner else None
        if commercial and item_commercial and commercial.ids == item_commercial.ids:
            cand.add("partner")
            by_partner |= item
        elif not commercial and item_commercial:
            tokens = _partner_tokens(item_commercial)
            if tokens and all(t in name_text for t in tokens):
                cand.add("partner_name")
                by_partner |= item
        same_direction = (residual > 0) == (open_amount > 0)
        if same_direction:
            diff = abs(open_amount) - abs(residual)
            if abs(diff) < 0.005:
                cand.add("amount_exact")
            elif tolerance and abs(diff) <= tolerance + 1e-9:
                cand.add("amount_tolerance")
            elif diff < 0 and "reference" in cand.reasons:
                cand.add("amount_partial")
        if cand.reasons and same_direction:
            singles.append(cand)
    groups: list[Candidate] = []
    for group, base in ((by_reference, "reference"), (by_partner, "partner")):
        if len(group) < 2 or len(group) > 12:
            continue
        total = sum(_in_line_currency(st_line, i) for i in group)
        if not total or (total > 0) != (open_amount > 0):
            continue
        diff = abs(open_amount) - abs(total)
        if base == "partner" and abs(diff) >= 0.005 and not (tolerance and abs(diff) <= tolerance):
            continue
        cand = Candidate(kind="items", items=group, amount=total)
        cand.add(base)
        cand.add("group")
        if base == "reference" and (by_partner & group) == group:
            cand.add("partner")
        if abs(diff) < 0.005:
            cand.add("amount_exact")
        elif tolerance and abs(diff) <= tolerance:
            cand.add("amount_tolerance")
        groups.append(cand)
    return groups + singles


def _rank(cands: list[Candidate], open_amount: float) -> list[Candidate]:
    def key(c: Candidate) -> tuple[Any, ...]:
        first = c.items[0] if c.items else None
        return (
            -c.score,
            abs(abs(open_amount) - abs(c.amount)),
            first.date_maturity if first and first.date_maturity else dt.date.max,
        )

    return sorted(cands, key=key)


def _models(st_line: Model, rule_type: str | None = None, auto: bool | None = None) -> Model:
    domain: list[Any] = [("company_id", "=", st_line.company_id.ids[0])]
    if rule_type:
        domain.append(("rule_type", "=", rule_type))
    if auto is not None:
        domain.append(("auto_reconcile", "=", auto))
    return st_line.env["account.reconcile_model"].elevated().search(domain, order="sequence, id")


def suggest_matches(st_line: Model, limit: int = 5) -> list[Candidate]:
    """Ranked match candidates (score 0-1 with reason codes) for a bank transaction."""
    st_line = st_line.elevated("bank suggestions")
    open_amount = _open_amount(st_line)
    if not open_amount:
        return []
    models = _models(st_line)
    invoice_model = next((m for m in models if m.rule_type == "invoice_matching" and m._matches_line(st_line)), None)
    candidates = _item_candidates(st_line, invoice_model)
    for cand in candidates:
        cand.model = invoice_model
        if "amount_tolerance" in cand.reasons and invoice_model is not None and invoice_model.line_ids:
            diff = abs(open_amount) - abs(cand.amount)
            cand.writeoffs = invoice_model._writeoff_values(st_line, -diff if open_amount > 0 else diff)
    for model in models.filtered(lambda m: m.rule_type == "writeoff_suggestion"):
        reasons = model._matches_line(st_line)
        if not reasons or not model.line_ids:
            continue
        cand = Candidate(kind="writeoff", model=model, amount=open_amount)
        for code in reasons:
            cand.add(code)
        cand.score = max(cand.score, 0.3)
        cand.writeoffs = model._writeoff_values(st_line, -_company_open(st_line))
        candidates.append(cand)
    return _rank(candidates, open_amount)[: max(int(limit), 0)]


def _company_open(st_line: Model) -> float:
    """Open amount in company currency (the suspense balance, sign of the transaction)."""
    if not st_line.move_id:
        return float(st_line.amount)
    suspense = st_line.journal_id._bank_suspense_account()
    lines = st_line.move_id.line_ids.filtered(lambda r: r.account_id.ids == suspense.ids)
    return -sum(float(r.balance) for r in lines)


def search_open_items(st_line: Model, query: str = "", limit: int = 20) -> list[dict[str, Any]]:
    """Manual search of open items (number, reference, partner, label or amount)."""
    st_line = st_line.elevated("bank search")
    domain = _open_item_domain(st_line, months=0)
    query = (query or "").strip()
    if query:
        alternatives: list[Any] = [
            ("move_id.name", "ilike", query),
            ("move_id.ref", "ilike", query),
            ("move_id.payment_reference", "ilike", query),
            ("partner_id.name", "ilike", query),
            ("name", "ilike", query),
        ]
        try:
            number = float(query.replace(",", "."))
            alternatives += [("amount_residual", "=", number), ("amount_residual", "=", -number)]
            alternatives += [("amount_residual_currency", "=", number), ("amount_residual_currency", "=", -number)]
        except ValueError:
            pass
        domain.append({"any": alternatives})
    elif st_line.partner_id:
        partner = st_line.partner_id.commercial_partner_id or st_line.partner_id
        domain.append(("partner_id.commercial_partner_id", "=", partner.ids[0]))
    items = st_line.env["account.move_line"].search(domain, order="date_maturity, id", limit=min(int(limit), 100))
    return [item_payload(i) for i in items]


def _qualifies(cand: Candidate, model: Model) -> bool:
    reasons = set(cand.reasons)
    if "reference" in reasons:
        return True
    if model.match_same_amount and ("partner" in reasons or "partner_name" in reasons):
        return bool({"amount_exact", "amount_tolerance"} & reasons)
    return False


def _auto_plan(st_line: Model, model: Model) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """(items, write-offs) when ``model`` explains the whole transaction unambiguously."""
    open_amount = _open_amount(st_line)
    if model.rule_type == "writeoff_suggestion":
        if not model.line_ids:
            return None
        writeoffs = model._writeoff_values(st_line, -_company_open(st_line))
        return ([], writeoffs) if writeoffs else None
    ranked = _rank([c for c in _item_candidates(st_line, model) if _qualifies(c, model)], open_amount)
    if not ranked:
        return None
    best = ranked[0]
    runner_up = set(ranked[1].line_ids) if len(ranked) > 1 else set()
    if len(ranked) > 1 and ranked[1].score == best.score and not runner_up <= set(best.line_ids):
        return None  # ambiguous: leave it for review
    diff = abs(open_amount) - abs(best.amount)
    items = [{"line_id": i} for i in best.line_ids]
    if abs(diff) < 0.005:
        return items, []
    tolerance = model._tolerance(open_amount)
    if tolerance and abs(diff) <= tolerance + 1e-9 and model.line_ids:
        # pay the items in full and write the difference off
        items = [
            {"line_id": i.ids[0], "amount": abs(float(i.amount_residual_currency or i.amount_residual))}
            for i in best.items
        ]
        sign = 1 if open_amount > 0 else -1
        return items, model._writeoff_values(st_line, -sign * diff)
    if diff < 0 and "reference" in best.reasons:
        return items, []  # partial payment of a referenced item: the transaction is fully explained
    return None


def auto_reconcile(st_lines: Model) -> dict[str, Any]:
    """Apply the automatic rules to posted, unreconciled transactions."""
    from modules.account_bank.matching import match_line

    done = 0
    lines = st_lines.elevated("auto reconciliation").filtered(lambda r: r.state == "posted")
    for st_line in lines.sorted(lambda r: (r.date, r.ids[0])):
        for model in _models(st_line, auto=True):
            if not model._matches_line(st_line):
                continue
            plan = _auto_plan(st_line, model)
            if plan is None:
                continue
            items, writeoffs = plan
            try:
                with st_line.env.cr.savepoint():
                    match_line(st_line, items, writeoffs, model=model)
            except Exception as exc:  # one bad line must not stop the run
                log.warning("auto reconciliation skipped", line=st_line.ids[0], error=str(exc))
                continue
            done += 1
            break
    return {"lines": len(lines), "reconciled": done}


def install_default_models(company: Model) -> Model:
    """The shipped rules for a company (idempotent): invoices and payments, bank fees, bank interest."""
    from modules.account.chart import chart_ref

    env = company.env
    company = company.elevated()
    models = env["account.reconcile_model"].elevated()
    cid = company.ids[0]
    created = models.browse([])

    def ensure(key: str, vals: dict[str, Any]) -> None:
        nonlocal created
        xmlid = f"account_bank.{cid}_{key}"
        if env.ref(xmlid, raise_if_not_found=False):
            return
        record = models.create({**vals, "company_id": cid})
        env.set_ref(xmlid, record, noupdate=True)
        created |= record

    ensure(
        "rule_invoices",
        {
            "name": "Invoices, bills and payments",
            "sequence": 10,
            "rule_type": "invoice_matching",
            "auto_reconcile": True,
            "match_text_location": True,
            "match_same_amount": True,
        },
    )
    fees = chart_ref(company, "bank_fees")
    if fees is not None:
        ensure(
            "rule_bank_fees",
            {
                "name": "Bank fees",
                "sequence": 20,
                "rule_type": "writeoff_suggestion",
                "auto_reconcile": True,
                "match_nature": "amount_paid",
                "match_label": "match_regex",
                "match_label_param": r"\b(bank fees?|fees?|service charge|charges|commission|frais|entgelt|geb[uü]hr)",
                "line_ids": [
                    (
                        "create",
                        {"account_id": fees.ids[0], "label": "Bank fees", "amount_type": "percentage", "amount": 100},
                    )
                ],
            },
        )
    interest = chart_ref(company, "income_other")
    if interest is not None:
        ensure(
            "rule_interest",
            {
                "name": "Bank interest",
                "sequence": 30,
                "rule_type": "writeoff_suggestion",
                "auto_reconcile": True,
                "match_nature": "amount_received",
                "match_label": "match_regex",
                "match_label_param": r"\b(interest|int[ée]r[eê]ts?|zinsen|rente)\b",
                "line_ids": [
                    (
                        "create",
                        {
                            "account_id": interest.ids[0],
                            "label": "Bank interest",
                            "amount_type": "percentage",
                            "amount": 100,
                        },
                    )
                ],
            },
        )
    return created
