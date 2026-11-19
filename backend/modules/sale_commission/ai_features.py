"""``sale_commission.explain`` — plain-language commission statement explanations (sub-phase 2.6, BUILD_PLAN §3).

The mid-tier model explains a statement to its salesperson in a few sentences: what the commission is made of (base,
rates by category, credit notes), how the target and the accelerator band apply, adjustments and the total. It only
reads :func:`modules.sale_commission.engine.statement_facts`, recomputed live, as the requesting user (salespeople
only reach their own statements):

* **every number in the explanation must be one of the statement's numbers** (amounts, rates, percentages, counts,
  dates). Names (salesperson, plan) and the adjustment note are text, not numbers: digits inside them never vouch for
  a figure. Sentences stating any other number are removed before the text is shown (``removed_numbers``);
* nothing is written: the explanation is shown beside the statement with the AI label (acceptance 2-k, second half).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from modules.sale_commission.engine import statement_facts
from tessera.ai import Source
from tessera.ai.facts import ground_text, ungrounded
from tessera.ai.runtime import HANDLERS, RunResult, handler, run_feature
from tessera.ai.tools import ToolContext
from tessera.exceptions import UserError
from tessera.i18n import _

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

FEATURE = "sale_commission.explain"
TEXT_KEYS = ("salesperson", "plan", "statement", "adjustment_note")


def _money(value: Any, currency: str) -> str:
    return f"{float(value or 0):,.2f} {currency}"


def facts_text(f: dict[str, Any]) -> str:
    cur = f["currency"]
    cats = "\n".join(
        f"- {c['category'] if c['category'] != '-' else 'Without category'}: base {_money(c['base'], cur)}, "
        f"commission {_money(c['commission'], cur)} ({c['lines']} lines)"
        for c in f["by_category"]
    )
    rates = "\n".join(
        f"- {r['target']}: {r['rate']:g}%"
        + (f" + {_money(r['fixed_per_unit'], cur)} per unit" if r["fixed_per_unit"] else "")
        for r in f["rates"]
    )
    bands = ", ".join(f"from {b['from_achievement']:g}%: x{b['multiplier']:g}" for b in f["bands"]) or "none"
    cn = f["credit_notes"]
    target = (
        f"Target: {_money(f['target'], cur)}; achievement {f['achievement_percent']:g}%; "
        + (
            f"band reached: from {f['band']['from_achievement']:g}%, multiplier x{f['band']['multiplier']:g}"
            if f["band"]
            else "no accelerator band reached (multiplier x1)"
        )
        if f["target"]
        else "No target on this plan (multiplier x1)"
    )
    return "\n".join(
        [
            f"Statement: {f['statement']} ({f['state']})",
            f"Salesperson: {f['salesperson']}; plan: {f['plan']}; period {f['period']} ({f['date_from']} to "
            f"{f['date_to']}); basis: {f['basis']}; amounts in {cur}",
            f"Commission base (untaxed invoices minus credit notes): {_money(f['base_total'], cur)}",
            f"Base and commission by category:\n{cats or '- none'}",
            f"Rates applied:\n{rates or '- none'}",
            f"Credit notes: {cn['count']}, base {_money(cn['base'], cur)}, commission {_money(cn['commission'], cur)}",
            target,
            f"Accelerator bands of the plan: {bands}",
            f"Commission before the accelerator: {_money(f['commission_before_multiplier'], cur)}",
            f"Multiplier: x{f['multiplier']:g}; commission: {_money(f['commission'], cur)}",
            f"Adjustments: {_money(f['adjustments'], cur)}"
            + (f" (reason: {f['adjustment_note']})" if f["adjustment_note"] else ""),
            f"Total: {_money(f['total'], cur)}",
            f"Invoice and credit note lines counted: {f['lines']} (from {f['documents']} documents)",
        ]
    )


def numbers_only(f: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in f.items() if k not in TEXT_KEYS}


def _masked(text: str, f: dict[str, Any]) -> tuple[str, dict[str, str]]:
    names = sorted({str(f[k]) for k in TEXT_KEYS if f.get(k)}, key=len, reverse=True)
    marks = {f"⟪{chr(65 + i)}⟫": n for i, n in enumerate(names)}
    masked = text or ""
    for mark, name in marks.items():
        masked = masked.replace(name, mark)
    return masked, marks


def ground(text: str, f: dict[str, Any]) -> tuple[str, list[str]]:
    """The explanation without the sentences stating a number that is not the statement's, and those numbers."""
    masked, marks = _masked(text, f)
    clean, removed = ground_text(masked, numbers_only(f))
    for mark, name in marks.items():
        clean = clean.replace(mark, name)
    return clean, removed


def check(text: str, f: dict[str, Any]) -> list[str]:
    """Numbers of ``text`` that are not the statement's (names and the adjustment note written verbatim aside)."""
    return ungrounded(_masked(text, f)[0], numbers_only(f))


def readable_statement(record: Model | None) -> Model:
    if record is None or record._name != "sale.commission_statement":
        raise UserError(_("Choose a commission statement"))
    record.check_access("read")
    return record


@handler(FEATURE)
def prepare(env: Environment, record: Model, inputs: dict[str, Any]) -> dict[str, Any]:
    statement = readable_statement(record)
    f = statement_facts(statement)
    sources = [Source(f"sale.commission_statement:{statement.ids[0]}", f["statement"], facts_text(f))]

    def on_output(ctx: ToolContext, text: str, data: Any) -> None:
        raw = str(data.get("explanation") or "") if isinstance(data, dict) else text
        clean, removed = ground(raw, f)
        if isinstance(data, dict):
            data.clear()
            data.update(
                {"explanation": clean, "removed_numbers": removed, "total": f["total"], "currency": f["currency"]}
            )

    return {"sources": sources, "on_output": on_output}


def explain(env: Environment, statement: Model, inputs: dict[str, Any] | None = None) -> RunResult:
    """Run the feature: ``data.explanation`` is the grounded explanation, ``data.removed_numbers`` what was dropped."""
    inputs = inputs or {}
    result = run_feature(env, FEATURE, inputs, record=statement, **HANDLERS[FEATURE](env, statement, inputs))
    if isinstance(result.data, dict):
        result.text = str(result.data.get("explanation") or "")
        if result.data.get("removed_numbers"):
            result.flags.append("numbers_removed")
    return result


# ---------------------------------------------------------------------------------------------- evaluation


def eval_entry(env: Environment, inputs: dict[str, Any], record: Any) -> RunResult:
    result = explain(env, record, inputs)
    f = statement_facts(record)
    data = result.data if isinstance(result.data, dict) else {}
    result.data = {**data, "left_ungrounded": check(result.text, f), "facts_total": f["total"]}
    return result


def score(results: list[dict[str, Any]]) -> dict[str, float]:
    """``numeric_consistency``: share of explanations whose every number is the statement's (1.0 required);
    ``guard_accuracy``: probe cases where exactly the invented figures were removed; ``total_stated``: explanations
    that still state the statement's total after the guard."""
    ok = [r for r in results if r.get("status") == "ok" and isinstance(r.get("data"), dict)]
    if not ok:
        return {}
    probes = [r for r in ok if "removed" in r["_case"]["expected"]]
    out = {
        "numeric_consistency": sum(1 for r in ok if not r["data"]["left_ungrounded"]) / len(ok),
        "total_stated": sum(1 for r in ok if f"{float(r['data']['facts_total']):,.2f}" in (r.get("text") or ""))
        / len(ok),
    }
    if probes:
        out["guard_accuracy"] = sum(
            1 for r in probes if sorted(r["data"]["removed_numbers"]) == sorted(r["_case"]["expected"]["removed"])
        ) / len(probes)
    return out
