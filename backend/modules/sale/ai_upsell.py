"""``sale.upsell`` — upsell and cross-sell suggestions on a quotation.

Purely statistical (AI architecture note §4.7): co-purchase association rules learnt from the company's
confirmed orders. For each product not on the quotation, the confidence of every rule *product on the
quotation → candidate* (orders with both / orders with the antecedent) is combined with a noisy-or, so a
candidate bought with several of the quotation's products ranks higher; the catalogue's optional products and
accessories (``sale.quotation._suggested_products``) add a prior, and popular products fill the list when the
history says nothing. Every reason shown is a count from the history, never model text.

Suggestions are computed as the requesting user: only products they can read and sell appear, and the
quotation must be readable. Adding a suggestion is a write proposal (a new line) that the user accepts.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Hashable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from modules.sale import ai_common as AC
from tessera.exceptions import UserError
from tessera.i18n import _

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

FEATURE = "sale.upsell"
MIN_SUPPORT = 2  # an antecedent must appear in at least this many orders
MIN_PAIR = 2  # a rule needs at least this many orders with both products
PRIOR_CATALOGUE = 0.3  # weight of "optional product / accessory of a product on the quotation"
POPULARITY_WEIGHT = 0.05
DEFAULT_LIMIT = 5


@dataclass
class Evidence:
    antecedent: Hashable
    both: int
    antecedent_orders: int

    @property
    def confidence(self) -> float:
        return self.both / self.antecedent_orders if self.antecedent_orders else 0.0


@dataclass
class Ranked:
    key: Hashable
    score: float
    evidence: list[Evidence] = field(default_factory=list)
    prior: str | None = None
    popular: bool = False


@dataclass
class CoPurchase:
    """Item counts and pair counts over a set of baskets (each basket counts an item once)."""

    orders: int = 0
    count: dict[Hashable, int] = field(default_factory=dict)
    pair: dict[Hashable, dict[Hashable, int]] = field(default_factory=dict)

    @classmethod
    def fit(cls, baskets: Iterable[Iterable[Hashable]]) -> CoPurchase:
        model = cls()
        count: dict[Hashable, int] = defaultdict(int)
        pair: dict[Hashable, dict[Hashable, int]] = defaultdict(lambda: defaultdict(int))
        for basket in baskets:
            items = list(dict.fromkeys(basket))
            if not items:
                continue
            model.orders += 1
            for a in items:
                count[a] += 1
                for b in items:
                    if a != b:
                        pair[a][b] += 1
        model.count = dict(count)
        model.pair = {a: dict(row) for a, row in pair.items()}
        return model

    def rank(
        self,
        basket: Iterable[Hashable],
        *,
        exclude: Iterable[Hashable] = (),
        prior: dict[Hashable, str] | None = None,
        allowed: set[Hashable] | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[Ranked]:
        """Candidates ranked by noisy-or of rule confidences, catalogue prior and popularity."""
        basket = list(dict.fromkeys(basket))
        skip = set(basket) | set(exclude)
        evidence: dict[Hashable, list[Evidence]] = defaultdict(list)
        for a in basket:
            n_a = self.count.get(a, 0)
            if n_a < MIN_SUPPORT:
                continue
            for c, both in self.pair.get(a, {}).items():
                if both >= MIN_PAIR and c not in skip:
                    evidence[c].append(Evidence(a, both, n_a))
        candidates = set(evidence) | set(prior or {})
        if len(candidates) < limit:  # popular products fill the list
            candidates |= {c for c in self.count if c not in skip}
        out: list[Ranked] = []
        for c in candidates:
            if c in skip or (allowed is not None and c not in allowed):
                continue
            miss = 1.0
            for ev in evidence.get(c, []):
                miss *= 1.0 - ev.confidence
            note = (prior or {}).get(c)
            if note:
                miss *= 1.0 - PRIOR_CATALOGUE
            score = 1.0 - miss
            popular = False
            if not evidence.get(c) and not note:
                score = POPULARITY_WEIGHT * self.count.get(c, 0) / max(1, self.orders)
                popular = True
            evs = sorted(evidence.get(c, []), key=lambda e: (-e.confidence, -e.both, str(e.antecedent)))
            out.append(Ranked(c, score, evs, note, popular))
        out.sort(key=lambda r: (-r.score, -self.count.get(r.key, 0), str(r.key)))
        return out[:limit]


# ----------------------------------------------------------------------------- production path

_CACHE: dict[tuple[Any, ...], CoPurchase] = {}


def _signature(env: Environment, company_id: int) -> tuple[Any, ...]:
    lines = env["sale.quotation_line"].elevated("sales statistics")
    domain = [("company_id", "=", company_id), ("quotation_id.state", "=", "accepted")]
    last = lines.search(domain, order="id desc", limit=1)
    changed = lines.search(domain, order="write_date desc", limit=1)
    return (
        id(env.registry),
        company_id,
        lines.search_count(domain),
        last.ids[0] if last else 0,
        str(changed.write_date) if changed else "",
    )


def company_model(env: Environment, company_id: int) -> CoPurchase:
    """Co-purchase statistics of the company's confirmed orders (cached until an order line changes)."""
    key = _signature(env, company_id)
    model = _CACHE.get(key)
    if model is None:
        baskets: dict[int, list[int]] = defaultdict(list)
        for row in AC.history_lines(env, company_id, AC.WON, fields=["quotation_id", "product_id"]):
            order, product = AC.m2o_id(row["quotation_id"]), AC.m2o_id(row["product_id"])
            if order and product:
                baskets[order].append(product)
        model = CoPurchase.fit(baskets.values())
        if len(_CACHE) > 64:
            _CACHE.clear()
        _CACHE[key] = model
    return model


def _catalogue_prior(order: Model) -> dict[int, str]:
    """Optional products and accessories of the order's products, with the reason shown."""
    out: dict[int, str] = {}
    for line in AC.product_lines(order.elevated()):
        product = line.product_id
        for cand in product.accessory_product_ids:
            out.setdefault(cand.ids[0], _("Accessory of {product}", product=product.display_name))
        for cand in product.optional_product_ids:
            out.setdefault(cand.ids[0], _("Optional product of {product}", product=product.display_name))
    suggested = set(order._suggested_products().ids)
    return {k: v for k, v in out.items() if k in suggested}


def _sellable(env: Environment, order: Model, ids: Iterable[int]) -> Model:
    """The products among ``ids`` the user can read and sell on ``order`` (its company or shared)."""
    ids = list(ids)
    if not ids:
        return env["product.product"]
    company = order.elevated().company_id.ids[0]
    return env["product.product"].search(
        [
            ("id", "in", ids),
            ("is_sellable", "=", True),
            {"any": [("company_id", "is_not_set"), ("company_id", "=", company)]},
        ]
    )


def suggest_products(order: Model, limit: int = DEFAULT_LIMIT, *, audit: bool = True) -> dict[str, Any]:
    """Ranked product suggestions for ``order`` (read as ``order.env``'s user)."""
    env = order.env
    AC.ensure_enabled(env, FEATURE)
    order.ensure_one()
    order.check_access("read")
    started = time.monotonic()
    rec = order.elevated()
    model = company_model(env, rec.company_id.ids[0])
    basket = list(AC.product_lines(rec).mapped("product_id").ids)
    present = set(rec.line_ids.mapped("product_id").ids)
    prior = _catalogue_prior(order)
    pool = set(prior) | {c for a in basket for c in model.pair.get(a, {})} | set(model.count)
    allowed = set(_sellable(env, order, pool - present).ids)
    ranked = model.rank(basket, exclude=present, prior=prior, allowed=allowed, limit=limit)
    products = env["product.product"].browse([r.key for r in ranked])
    by_id = {p.ids[0]: p for p in products}
    rows: list[dict[str, Any]] = []
    for r in ranked:
        p = by_id[int(r.key)]  # type: ignore[call-overload]
        reasons: list[str] = []
        for ev in r.evidence[:2]:
            name = env["product.product"].browse([int(ev.antecedent)]).elevated().display_name  # type: ignore[call-overload]
            reasons.append(
                _(
                    "Bought with {product} in {both} of {orders} orders",
                    product=name,
                    both=ev.both,
                    orders=ev.antecedent_orders,
                )
            )
        if r.prior:
            reasons.append(r.prior)
        if r.popular:
            reasons.append(
                _("Popular: in {count} of {orders} orders", count=model.count.get(r.key, 0), orders=model.orders)
            )
        rows.append(
            {
                "product_id": p.ids[0],
                "display_name": p.display_name,
                "score": round(r.score, 4),
                "price_unit": rec._price_for(p, 1.0),
                "currency": rec.currency_id.name,
                "source": "history" if r.evidence else ("catalogue" if r.prior else "popular"),
                "reasons": reasons,
            }
        )
    out: dict[str, Any] = {"suggestions": rows, "orders_learnt": model.orders, "ai_generated": True}
    if audit:
        run = AC.audit_run(
            env,
            FEATURE,
            record=order,
            inputs={"limit": limit},
            output=[{k: r[k] for k in ("product_id", "score", "source")} for r in rows],
            model="co-purchase association rules",
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        out["run_id"] = run.ids[0]
    return out


def propose_line(order: Model, product_id: int, quantity: float = 1.0, run_id: int | None = None) -> Model:
    """A write proposal adding ``product_id`` to ``order``: nothing changes until the user accepts it."""
    from tessera.ai import proposals as P
    from tessera.ai.tools import PendingProposal

    env = order.env
    AC.ensure_enabled(env, FEATURE)
    order.ensure_one()
    order.check_access("write")
    rec = order.elevated()
    if rec.state not in ("draft", "sent") or rec.locked:
        raise UserError(_("Only draft or sent quotations can get new lines"))
    if not (0 < float(quantity) <= 1e6):
        raise UserError(_("The quantity must be positive"))
    product = _sellable(env, order, [int(product_id)])
    if not product:
        raise UserError(_("This product cannot be sold on this quotation"))
    if product.ids[0] in set(rec.line_ids.mapped("product_id").ids):
        raise UserError(_("{product} is already on the quotation", product=product.display_name))
    run = env["ai_core.run"].elevated().browse([run_id]).exists() if run_id else None
    if not run or run.user_id.ids[:1] != [env.uid] or run.feature != FEATURE or run.res_id != rec.ids[0]:
        run = AC.audit_run(env, FEATURE, record=order, inputs={"product_id": product.ids[0], "quantity": quantity})
    payload = {"line_ids": [["create", {"product_id": product.ids[0], "quantity": float(quantity)}]]}
    title = _("Add {product} to {order}", product=product.display_name, order=rec.name)
    proposal = P.create(env, run, FEATURE, PendingProposal("write", "sale.quotation", rec.ids[0], payload, title))
    run.elevated().write({"decision": "pending"})
    return proposal


# ----------------------------------------------------------------------------- optional explanation


def explain_context(env: Environment, record: Model, inputs: dict[str, Any]) -> dict[str, Any]:
    """Sources for the optional one-line explanation of each suggestion (the ranking never comes from it)."""
    from tessera.ai import Source

    if record is None or record._name != "sale.quotation":
        raise UserError(_("Product suggestions work on a quotation"))
    found = suggest_products(record, audit=False)
    rec = record.elevated()
    lines = "\n".join(f"- {line.product_id.display_name} x {line.quantity:g}" for line in AC.product_lines(rec))
    text = f"Quotation {rec.name}\nLines:\n{lines}\nSuggestions:\n" + "\n".join(
        f"- {s['display_name']}: {'; '.join(s['reasons'])}" for s in found["suggestions"]
    )
    return {"sources": [Source(f"sale.quotation:{rec.ids[0]}", rec.name, text)]}


def _register() -> None:
    from tessera.ai.runtime import handler

    handler(FEATURE)(explain_context)


_register()
