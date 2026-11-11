"""``product.description_draft`` — a sales description drafted from what the catalogue knows about a product.

The facts are the product's name, internal reference, type, category, unit, attributes and their values (the
variants it comes in) and the current sales description, read as the requesting user. The model writes a
short description in the user's language. A deterministic **spec check** then compares the draft with the
facts: a number (dimension, weight, duration, percentage, rating such as IP67) or a specification claim
(waterproof, warranty, certified, materials, battery…) that the facts do not contain is an invented spec,
and the sentence carrying it is removed before anything is proposed. The result is a write proposal of
``description_sale`` on the product (on its template for a variant), in the user's language.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING, Any

from tessera.ai import Source
from tessera.ai.runtime import handler, run_feature
from tessera.ai.tools import PendingProposal, ToolContext
from tessera.exceptions import AccessError, UserError
from tessera.i18n import _

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

FEATURE = "product.description_draft"
MAX_VARIANTS_LISTED = 30
NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
# specification claims a description may only make when the facts state them (folded): each group holds the
# words of one claim in the supported languages, so a French draft may say "coton biologique" for "Organic cotton"
CLAIM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("waterproof", "water resistant", "water-resistant", "etanche", "wasserdicht"),
    ("warranty", "guarantee", "garantie", "garantia"),
    ("certified", "certification", "certifie", "certifiee", "zertifiziert"),
    ("iso",), ("ce marked", "marquage ce"), ("ul listed",), ("fda",),
    ("recycled", "recycle", "recyclee", "recycelt"),
    ("stainless", "inox", "inoxydable", "edelstahl"),
    ("aluminium", "aluminum"), ("titanium", "titane"), ("carbon fibre", "carbon fiber", "fibre de carbone"),
    ("leather", "cuir", "leder"),
    ("wireless", "sans fil", "kabellos"), ("bluetooth",), ("wifi", "wi-fi"),
    ("rechargeable", "battery", "batterie", "akku"),
    ("lifetime", "a vie"), ("vegan",), ("hypoallergenic", "hypoallergenique"),
    ("fireproof", "flame retardant", "ignifuge"), ("medical grade", "qualite medicale"),
    ("patented", "brevete"), ("handmade", "fait main", "handgemacht"),
    ("made in", "fabrique en", "fabrique au", "hergestellt in"),
    ("dishwasher safe", "lave-vaisselle"), ("uv resistant", "anti-uv"),
    ("antibacterial", "anti-bacterial", "antibacterien"),
    ("organic", "biologique", "bio"), ("fsc",), ("gots",), ("oeko-tex",), ("energy star",),
    ("ip",),
)  # fmt: skip
LANGUAGES = {"en": "English", "fr": "French", "de": "German", "es": "Spanish", "it": "Italian", "nl": "Dutch"}


def fold(text: str) -> str:
    return unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()


def language_name(lang: str | None) -> str:
    return LANGUAGES.get((lang or "en_US").split("_")[0].lower(), "English")


def facts_text(product: Model) -> str:
    """What the catalogue states about ``product`` (its template for a variant), as plain text."""
    tmpl = (product.template_id or product).elevated()
    lines = [f"Name: {tmpl.name}"]
    if tmpl.default_code:
        lines.append(f"Internal reference: {tmpl.default_code}")
    type_field: Any = tmpl._fields["type"]
    kinds = dict(type_field.get_selection(tmpl))
    lines.append(f"Type: {kinds.get(tmpl.type, tmpl.type)}")
    if tmpl.category_id:
        lines.append(f"Category: {tmpl.category_id.complete_name}")
    if tmpl.uom_id:
        lines.append(f"Sold per: {tmpl.uom_id.name}")
    for line in tmpl.attribute_line_ids.sorted(lambda r: (r.sequence or 0, r.ids[0])):
        lines.append(f"{line.attribute_id.name}: {', '.join(v.name for v in line.value_ids)}")
    variants = tmpl.variant_ids.filtered(lambda v: v.active)
    if variants:
        lines.append(f"Variants: {len(variants)}")
        for v in variants[:MAX_VARIANTS_LISTED]:
            lines.append(f"- {', '.join(x.name for x in v.attribute_value_ids)}")
    if product.template_id and product.attribute_value_ids:
        lines.append(f"This variant: {', '.join(v.name for v in product.elevated().attribute_value_ids)}")
    if tmpl.description_sale:
        lines.append(f"Current sales description: {tmpl.description_sale.strip()[:1500]}")
    return "\n".join(lines)


def _number(raw: str) -> str:
    """``"1,50"`` and ``"1.5"`` are the same number."""
    value = raw.replace(",", ".")
    return value.rstrip("0").rstrip(".") if "." in value else value


def _numbers(text: str) -> set[str]:
    return {_number(n) for n in NUMBER.findall(text)}


def unsupported_claims(sentence: str, facts: str) -> list[str]:
    """Numbers and specification claims in ``sentence`` that ``facts`` do not contain."""
    folded, known = fold(sentence), fold(facts)
    out = [n for n in sorted(_numbers(folded)) if n not in _numbers(known)]
    for group in CLAIM_GROUPS:
        patterns = [rf"\b{re.escape(c)}s?\b" if c != "ip" else r"\bip\s?\d{2}\b" for c in group]
        hits = [m.group(0) for p in patterns if (m := re.search(p, folded))]
        if hits and not any(re.search(p, known) for p in patterns):
            out.append(hits[0])
    return out


def check_specs(description: str, facts: str) -> tuple[str, list[dict[str, Any]]]:
    """(description without the sentences that state unsupported specs, removed sentences with why)."""
    kept: list[str] = []
    removed: list[dict[str, Any]] = []
    for sentence in [s for s in SENTENCE.split(description or "") if s.strip()]:
        bad = unsupported_claims(sentence, facts)
        if bad:
            removed.append({"sentence": sentence.strip(), "unsupported": bad})
        else:
            kept.append(sentence.strip())
    return " ".join(kept).strip(), removed


def _target(product: Model) -> Model:
    return product.template_id or product


@handler(FEATURE)
def prepare(env: Environment, record: Model | None, inputs: dict[str, Any]) -> dict[str, Any]:
    if record is None or record._name != "product.product":
        raise UserError(_("Descriptions are drafted on a product"))
    record.check_access("read")
    facts = facts_text(record)
    target = _target(record)
    source = Source(f"product.product:{target.ids[0]}", target.elevated().display_name or "", facts)

    def on_output(ctx: ToolContext, _text: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        draft = str(data.get("description") or "").strip()
        clean, removed = check_specs(draft, facts)
        data.update(
            description=clean,
            draft=draft,
            removed=removed,
            unsupported=[u for r in removed for u in r["unsupported"]],
            language=language_name(env.lang),
        )
        if not clean:
            return
        try:
            target.with_env(env).check_access("write")
            if not target._check_field_access(target._fields["description_sale"], "write"):
                return
        except AccessError:
            return
        ctx.proposals.append(
            PendingProposal(
                "write",
                "product.product",
                target.ids[0],
                {"description_sale": clean},
                _("Sales description for {product}", product=target.elevated().display_name),
            )
        )

    return {"sources": [source], "on_output": on_output}


# ----------------------------------------------------------------------------- evaluation


def eval_entry(env: Environment, inputs: dict[str, Any], record: Model) -> Any:
    """The API path plus the spec check of what would be written (``data.final_unsupported``)."""
    result = run_feature(env, FEATURE, inputs, record=record, **prepare(env, record, inputs))
    facts = facts_text(record)
    final = [str(p.payload.get("description_sale") or "") for p in result.proposals]
    if isinstance(result.data, dict):
        result.data["final_unsupported"] = [u for text in final for u in unsupported_claims(text, facts)]
        result.text = str(result.data.get("description") or "")
    return result


def score(results: list[dict[str, Any]]) -> dict[str, float]:
    """``invented_specs``: unsupported specs left in proposed descriptions (must be 0);
    ``spec_check_accuracy``: probe drafts whose invented specs were all removed and nothing else."""
    ok = [r for r in results if r.get("status") == "ok" and isinstance(r.get("data"), dict)]
    invented = sum(len(r["data"].get("final_unsupported") or []) for r in ok)
    probes = [r for r in ok if "unsupported" in (r["_case"].get("expected") or {})]
    exact = sum(
        1 for r in probes if sorted(r["data"].get("unsupported") or []) == sorted(r["_case"]["expected"]["unsupported"])
    )
    clean = [r for r in ok if r not in probes]
    out = {
        "invented_specs": float(invented),
        "drafts_kept_whole": sum(1 for r in clean if not r["data"].get("removed")) / len(clean) if clean else 1.0,
    }
    if probes:
        out["spec_check_accuracy"] = exact / len(probes)
    return out
