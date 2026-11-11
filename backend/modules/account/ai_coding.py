"""Partner matching and auto-coding of document lines (``account.bill_ocr``).

Everything reads **as the requesting user**: partners, bank accounts, products, accounts, taxes and the
posted documents used as history are searched with the user's environment, so record rules and the
multi-company rule decide what can be matched. Nothing is written.

Coding a line, in order of trust:

1. a **product** whose internal reference (or exact name) appears in the description → its expense /
   income account and vendor / customer taxes;
2. the partner's **history** of posted documents: the past line with the most similar label gives its
   account (and the taxes used with it); with no similar line, the partner's usual account when one
   account dominates its history;
3. the **chart**: multilingual keywords mapped to chart template accounts (rent, software, travel,
   bank fees…), else an account whose name shares a word with the description;
4. the company's **default** expense / income account.

Taxes: the rate printed on the line must exist among the company's taxes for that use (``rate must exist``);
a 0 % line on a reverse-charge document maps to the company's self-assessed (reverse-charge) tax. A rate
with no tax leaves the line without tax and the proposal is marked for review.
"""

from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

from modules.account import ai_ocr as O
from modules.account.chart import chart_ref
from tessera.exceptions import AccessError
from tessera.i18n import _

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

PURCHASE = ("in_invoice", "in_refund")
LEGAL_WORDS = {
    "sarl", "sas", "sasu", "sa", "selarl", "scp", "snc", "eurl", "gmbh", "ag", "kg", "ohg", "ug", "co", "sl", "slu",
    "ltd", "limited", "llc", "llp", "inc", "corp", "plc", "bv", "nv", "srl", "spa", "the", "de", "la", "le", "les",
}  # fmt: skip
STOP = {
    "de", "du", "des", "la", "le", "les", "et", "the", "of", "for", "and", "a", "an", "to", "der", "die", "das", "und",
    "fur", "el", "los", "las", "del", "y", "en", "pour", "par", "mit", "von", "con", "per", "por",
}  # fmt: skip
KEYWORDS: dict[str, list[str]] = {
    "rent": ["rent", "loyer", "miete", "alquiler", "charges locatives", "nebenkosten", "service charges",
             "gastos de comunidad", "lease", "bail"],
    "software": ["software", "logiciel", "licence", "license", "lizenz", "licencia", "saas", "subscription",
                 "abonnement", "abo", "suscripcion", "cloud", "hosting", "hebergement", "alojamiento", "antivirus"],
    "travel": ["hotel", "nuitee", "ubernachtung", "habitacion", "train", "bahn", "tren", "flight", "vol", "flug",
               "vuelo", "taxi", "travel", "voyage", "reise", "viaje", "billet", "ticket", "billete"],
    "bank_fees": ["bank fee", "maintenance fee", "transfer fee", "frais bancaires", "tenue de compte", "virement",
                  "kontofuhrung", "uberweisungsgebuhr", "comision", "commission sur virement"],
    "commissions": ["commission", "provision", "comision de agente", "comision sobre ventas"],
    "cogs": ["aluminium", "aluminio", "steel", "inox", "edelstahl", "bolts", "vis", "schrauben", "tornillos",
             "circuit", "cartes electroniques", "leiterplatten", "placas", "profiles", "profiles", "profile",
             "perfiles", "aluminiumprofile", "packaging", "emballage", "versandkartons", "embalaje", "components",
             "composants", "raw material", "matiere premiere"],
    "expense": ["paper", "papier", "papel", "toner", "coffee", "cafe", "kaffee", "office", "bureau", "buro",
                "oficina", "legal", "juridique", "rechtsberatung", "juridica", "accounting", "comptable",
                "steuerberatung", "contabilidad", "consulting", "conseil", "beratung", "consultoria", "recruitment",
                "recrutement", "cleaning", "nettoyage", "reinigung", "limpieza", "fournitures", "material"],
}  # fmt: skip
SALE_KEYWORDS: dict[str, list[str]] = {
    "income_services": ["installation", "maintenance", "service", "commissioning", "mise en service", "support",
                        "consulting", "conseil", "training", "formation", "contrat"],
    "income_subscriptions": ["subscription", "abonnement"],
    "income": [],
}  # fmt: skip
# the commission keywords must not catch "commission sur virement" (a bank fee): checked in this order
KEYWORD_ORDER = ["bank_fees", "rent", "software", "travel", "commissions", "cogs", "expense"]


def tokens(text: str) -> set[str]:
    return {w for w in O.words(text) if len(w) > 2 and w not in STOP and not w.isdigit()}


def name_key(name: str) -> str:
    return " ".join(w for w in O.words(name) if w not in LEGAL_WORDS)


def similarity(a: str, b: str) -> float:
    """Word overlap (Jaccard) of two labels; near-identical spellings count as the same label."""
    ta, tb = tokens(a), tokens(b)
    jac = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    seq = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return max(jac, seq) if seq >= 0.8 else jac


def keyword_in(kw: str, words_: list[str], joined: str) -> bool:
    """A keyword as a word or a phrase, or ending (≥ 5 letters) / starting (≥ 6) a compound word."""
    if " " in kw:
        return f" {kw} " in joined
    return any(w == kw or (len(kw) >= 5 and w.endswith(kw)) or (len(kw) >= 6 and w.startswith(kw)) for w in words_)


# ----------------------------------------------------------------------------- partners


def match_partner(env: Environment, ex: O.Extraction) -> dict[str, Any]:
    """The partner of a document among those the user can read: by VAT ID, then IBAN, then name."""
    partners = env["contacts.partner"]
    vat = O.norm_vat(ex.partner_vat)
    if vat:
        digits = re.sub(r"\D", "", vat)[-8:] or vat
        for p in partners.search([("vat", "ilike", digits)], limit=20):
            if O.norm_vat(p.vat) == vat:
                return _match(p, "vat", 1.0)
    if ex.iban:
        banks = env["account.bank_account"].search([("sanitized_acc_number", "=", O.norm_iban(ex.iban))], limit=5)
        for bank in banks:
            try:
                partner = bank.partner_id
                partner.check_access("read")
            except AccessError:
                continue
            if partner:
                return _match(partner.commercial_partner_id or partner, "iban", 0.95)
    if ex.partner_name:
        key = name_key(ex.partner_name)
        words_ = sorted((w for w in key.split() if len(w) > 2), key=len, reverse=True)[:2]
        if words_:
            domain: list[Any] = [("name", "ilike", words_[0])]
            best: tuple[float, Model] | None = None
            for p in partners.search(domain, limit=50):
                score = SequenceMatcher(None, key, name_key(p.name or "")).ratio()
                if score >= 0.86 and (best is None or score > best[0] or (score == best[0] and p.is_company)):
                    best = (score, p)
            if best:
                return _match(best[1].commercial_partner_id or best[1], "name", round(best[0], 2))
    return {"id": None, "display_name": None, "method": "new", "confidence": 0.0}


def _match(p: Model, method: str, confidence: float) -> dict[str, Any]:
    return {"id": p.ids[0], "display_name": p.display_name, "method": method, "confidence": confidence}


# ----------------------------------------------------------------------------- context


def _company(env: Environment) -> Model:
    return env.company or env.ref("core.main_company")


def history(env: Environment, partner_id: int | None, move_type: str, limit: int = 200) -> list[dict[str, Any]]:
    """Product lines of the partner's posted documents of the same direction, newest first."""
    if not partner_id:
        return []
    types = list(PURCHASE) if move_type in PURCHASE else ["out_invoice", "out_refund"]
    company = _company(env)
    lines = env["account.move_line"].search(
        [
            ("partner_id", "child_of", partner_id),
            ("move_type", "in", types),
            ("parent_state", "=", "posted"),
            ("display_type", "=", "product"),
            ("company_id", "=", company.ids[0]),
        ],
        order="date desc, id desc",
        limit=limit,
    )
    return [
        {
            "name": ln.name or "",
            "account_id": ln.account_id.ids[0] if ln.account_id else None,
            "tax_ids": list(ln.tax_ids.ids),
            "product_id": ln.product_id.ids[0] if ln.product_id else None,
            "move": ln.move_id.display_name,
        }
        for ln in lines
    ]


def is_reverse_charge(tax: Model) -> bool:
    """A self-assessed tax: a positive rate whose invoice distribution nets to zero."""
    reps = tax.repartition_line_ids.filtered(lambda r: r.document_type == "invoice" and r.repartition_type == "tax")
    return float(tax.amount or 0) > 0 and len(reps) >= 2 and abs(sum(float(r.factor_percent or 0) for r in reps)) < 1e-6


class Coder:
    """Codes the lines of one document for one company (see the module docstring)."""

    def __init__(
        self,
        env: Environment,
        move_type: str,
        partner_id: int | None,
        *,
        reverse_charge: bool = False,
        partner_country: str | None = None,
    ):
        self.env = env
        self.company = _company(env)
        self.purchase = move_type in PURCHASE
        self.reverse_charge = reverse_charge
        own = self.company.elevated()
        country = own.account_fiscal_country_id or own.country_id
        own_code = country.code if country else None
        # a vendor from another country charging VAT charges *its* VAT, not the company's deductible tax
        self.foreign = bool(self.purchase and partner_country and own_code and partner_country != own_code)
        use = "purchase" if self.purchase else "sale"
        cid = self.company.ids[0]
        self.taxes = list(
            env["account.tax"].search([("company_id", "=", cid), ("type_tax_use", "=", use)], order="sequence, id")
        )
        groups = ["expense", "asset"] if self.purchase else ["income"]  # internal groups the lines may be coded to
        self.accounts = {
            a.ids[0]: a
            for a in env["account.account"].search(
                [
                    ("company_id", "=", cid),
                    ("internal_group", "in", groups),
                    ("deprecated", "=", False),
                    ("account_type", "not_in", ["asset_cash", "asset_receivable", "liability_payable"]),
                ]
            )
        }
        self.history = history(env, partner_id, move_type)
        self.default_tax = self.company.elevated()[
            "default_purchase_tax_id" if self.purchase else "default_sale_tax_id"
        ]
        field = "default_expense_account_id" if self.purchase else "default_income_account_id"
        self.default_account = self.company.elevated()[field]

    # -- accounts --------------------------------------------------------------------------------------

    def _template(self, tid: str) -> Model | None:
        rec = chart_ref(self.company, tid)
        return rec if rec is not None and rec.ids and rec.ids[0] in self.accounts else None

    def _keyword_account(self, text: str) -> tuple[Model | None, str]:
        ws = O.words(text)
        f = " " + " ".join(ws) + " "
        table = KEYWORDS if self.purchase else SALE_KEYWORDS
        order = KEYWORD_ORDER if self.purchase else list(SALE_KEYWORDS)
        for tid in order:
            for kw in table[tid]:
                if keyword_in(kw, ws, f):
                    acc = self._template(tid)
                    if acc is not None:
                        return acc, kw
        words_ = tokens(text)
        for acc in self.accounts.values():
            if acc.internal_group == ("expense" if self.purchase else "income") and words_ & tokens(acc.name or ""):
                return acc, acc.name or ""
        return None, ""

    def _product(self, text: str) -> Model | None:
        codes = re.findall(r"\b[A-Z]{1,4}-?\d{1,5}[A-Z]?\b", text)
        products = self.env["product.product"]
        for code in codes:
            for found in products.search([("default_code", "ilike", code)], limit=10):
                if (found.default_code or "").upper() == code.upper():
                    return found
        name = text.strip()
        if len(name) >= 6:
            for found in products.search([("name", "ilike", name)], limit=10):
                if O.fold(found.name or "") == O.fold(name):
                    return found
        return None

    # -- taxes -----------------------------------------------------------------------------------------

    def _taxes_for_rate(self, rate: float | None, hint: list[int]) -> tuple[list[int], str, str | None]:
        """(tax ids, how they were chosen, issue) for a printed rate."""
        if rate is None or rate < 0:
            if hint:
                return hint, "history", None
            return (self.default_tax.ids if self.default_tax else []), "default", None
        cands = [t for t in self.taxes if t.amount_type == "percent" and not t.children_tax_ids]
        if rate > 0 and self.foreign and not self.reverse_charge:
            return (
                [],
                "foreign",
                _("Foreign VAT at {rate}%: not deductible here, the line has no tax", rate=f"{rate:g}"),
            )
        if rate == 0 and self.reverse_charge and self.purchase:
            rc = [t for t in cands if is_reverse_charge(t)]
            if rc:
                chosen = next((t for t in rc if t.ids[0] in hint), max(rc, key=lambda t: float(t.amount or 0)))
                return chosen.ids, "reverse_charge", None
        same = [t for t in cands if abs(float(t.amount or 0) - rate) < 1e-6 and not is_reverse_charge(t)]
        if not same:
            return [], "none", _("No {rate}% tax in this company", rate=f"{rate:g}")
        preferred = [t for t in same if t.ids[0] in hint] or [t for t in same if not t.price_include] or same
        return preferred[0].ids, "rate", None

    # -- lines -----------------------------------------------------------------------------------------

    def code(self, line: O.Line) -> dict[str, Any]:
        account: Model | None = None
        source = "default"
        reason = ""
        hint_taxes: list[int] = []
        product = self._product(line.description)
        if product is not None:
            prod = product.elevated()
            acc = prod.property_account_expense_id if self.purchase else prod.property_account_income_id
            if acc and acc.ids[0] in self.accounts:
                account, source, reason = acc, "product", _("Product {name}", name=product.display_name)
            taxes = prod._taxes_for(self.company, "purchase" if self.purchase else "sale")
            hint_taxes = list(taxes.ids)
        if account is None and self.history:
            scored = sorted(
                ((similarity(line.description, h["name"]), i, h) for i, h in enumerate(self.history)),
                key=lambda x: (-x[0], x[1]),
            )
            best_sim, _i, best = scored[0]
            if best_sim >= 0.34 and best["account_id"] in self.accounts:
                account, source = self.accounts[best["account_id"]], "history"
                reason = _('Like "{label}" on {move}', label=best["name"], move=best["move"])
                hint_taxes = hint_taxes or best["tax_ids"]
            else:
                counts = Counter(h["account_id"] for h in self.history if h["account_id"] in self.accounts)
                if counts:
                    top, n = counts.most_common(1)[0]
                    kw_acc, _kw = self._keyword_account(line.description)
                    if kw_acc is not None and kw_acc.ids[0] in counts:
                        account, source = kw_acc, "history"
                    elif n / sum(counts.values()) >= 0.6:
                        account, source = self.accounts[top], "history"
                    if account is not None:
                        reason = _("Usual account of this partner")
                        hint_taxes = hint_taxes or next(
                            (h["tax_ids"] for h in self.history if h["account_id"] == account.ids[0]), []
                        )
        if account is None:
            kw_acc, kw = self._keyword_account(line.description)
            if kw_acc is not None:
                account, source, reason = kw_acc, "chart", _('Keyword "{word}"', word=kw)
        if account is None and self.default_account:
            account, source, reason = self.default_account, "default", _("Company default account")
        tax_ids, tax_source, issue = self._taxes_for_rate(line.tax_rate, hint_taxes)
        taxes = self.env["account.tax"].browse(tax_ids)
        confidence = {"product": 0.95, "history": 0.9, "chart": 0.7, "default": 0.4}[source]
        if tax_source in ("default", "none", "foreign"):
            confidence = min(confidence, 0.5)
        return {
            "description": line.description,
            "quantity": line.quantity,
            "unit_price": line.unit_price,
            "amount": line.amount,
            "tax_rate": line.tax_rate,
            "product_id": product.ids[0] if product is not None else None,
            "account_id": account.ids[0] if account is not None else None,
            "account": account.display_name if account is not None else None,
            "account_source": source,
            "tax_ids": tax_ids,
            "taxes": [t.name for t in taxes],
            "tax_source": tax_source,
            "issue": issue,
            "confidence": confidence,
            "reason": reason,
        }
