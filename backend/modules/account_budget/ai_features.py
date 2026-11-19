"""``account_budget.variance_explain`` — plain-language variance explanations (sub-phase 2.6).

The mid-tier model explains a budget's variances in a few sentences: which lines are over or under plan,
whether that is ahead of or behind the time elapsed, what stands out. It only reads
:func:`modules.account_budget.services.variance_report` (recomputed live from the ledger, as the user):

* every number in the explanation must be one of the report's numbers (amounts, percentages, dates) —
  sentences stating any other figure are removed before the text is shown (:func:`ground_text`) and listed
  as ``removed_numbers``;
* nothing is written: the explanation is shown beside the budget with the AI label.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from modules.account_budget.services import variance_report
from tessera.ai import Source
from tessera.ai.facts import ground_text, ungrounded
from tessera.ai.runtime import HANDLERS, RunResult, handler, run_feature
from tessera.ai.tools import ToolContext
from tessera.exceptions import UserError
from tessera.i18n import _

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

FEATURE = "account_budget.variance_explain"


def _money(value: float, currency: str) -> str:
    return f"{value:,.2f} {currency}"


def report_text(rep: dict[str, Any]) -> str:
    cur = rep["currency"]
    lines = []
    for line in rep["lines"]:
        pct = f"{line['variance_pct']:+.2f}%" if line["variance_pct"] is not None else "n/a"
        lines.append(
            f"- {line['name']} ({line['line_type']}, {line['date_from']} to {line['date_to']}): planned "
            f"{_money(line['planned'], cur)}, actual {_money(line['actual'], cur)}, expected by now "
            f"{_money(line['theoretical'], cur)}, variance {_money(line['variance'], cur)} ({pct}), achievement "
            f"{line['achievement_pct']:.2f}%, elapsed {line['elapsed_share'] * 100:.2f}% of the period, "
            f"{'favourable' if line['favourable'] else 'unfavourable'}"
        )
    totals = [
        f"- {kind}: planned {_money(t['planned'], cur)}, actual {_money(t['actual'], cur)}, expected by now "
        f"{_money(t['theoretical'], cur)}, variance {_money(t['variance'], cur)}"
        for kind, t in rep["totals"].items()
    ]
    net = rep["net"]
    return "\n".join(
        [
            f"Budget: {rep['name']} ({rep['state']}), {rep['date_from']} to {rep['date_to']}, as of {rep['as_of']}",
            f"Company: {rep['company']}; amounts in {cur}",
            "Lines:",
            *lines,
            "Totals:",
            *totals,
            f"Net (revenue - expense): planned {_money(net['planned'], cur)}, actual {_money(net['actual'], cur)}, "
            f"variance {_money(net['variance'], cur)}",
        ]
    )


def facts_for(rep: dict[str, Any]) -> dict[str, Any]:
    """The numbers the explanation may use: the report, plus percentages as the source text shows them."""
    extra = [round(line["elapsed_share"] * 100, 2) for line in rep["lines"]]
    return {"report": rep, "elapsed_pct": extra}


@handler(FEATURE)
def prepare(env: Environment, record: Model, inputs: dict[str, Any]) -> dict[str, Any]:
    if record is None or record._name != "account.budget":
        raise UserError(_("Choose a budget"))
    record.check_access("read")
    rep = variance_report(record, inputs.get("as_of"))
    facts = facts_for(rep)
    sources = [Source(f"account.budget:{record.ids[0]}", rep["name"], report_text(rep))]

    def on_output(ctx: ToolContext, text: str, data: Any) -> None:
        raw = str(data.get("explanation") or "") if isinstance(data, dict) else text
        clean, removed = ground_text(raw, facts)
        if isinstance(data, dict):
            data.clear()
            data.update({"explanation": clean, "removed_numbers": removed, "as_of": rep["as_of"]})

    return {"sources": sources, "on_output": on_output}


def explain(env: Environment, budget: Model, inputs: dict[str, Any] | None = None) -> RunResult:
    """Run the feature: ``data.explanation`` is the grounded explanation, ``data.removed_numbers`` the
    figures that were dropped."""
    inputs = inputs or {}
    result = run_feature(env, FEATURE, inputs, record=budget, **HANDLERS[FEATURE](env, budget, inputs))
    if isinstance(result.data, dict):
        result.text = str(result.data.get("explanation") or "")
        if result.data.get("removed_numbers"):
            result.flags.append("numbers_removed")
    return result


# ---------------------------------------------------------------------------------------------- evaluation


def eval_entry(env: Environment, inputs: dict[str, Any], record: Any) -> RunResult:
    result = explain(env, record, inputs)
    rep = variance_report(record, inputs.get("as_of"))
    data = result.data if isinstance(result.data, dict) else {}
    result.data = {**data, "left_ungrounded": ungrounded(result.text, facts_for(rep))}
    return result


def score(results: list[dict[str, Any]]) -> dict[str, float]:
    """``numeric_consistency``: share of explanations in which every number is the report's (1.0 required);
    ``guard_accuracy``: probe cases where exactly the wrong figures were removed."""
    ok = [r for r in results if r.get("status") == "ok" and isinstance(r.get("data"), dict)]
    if not ok:
        return {}
    probes = [r for r in ok if "removed" in r["_case"]["expected"]]
    out = {"numeric_consistency": sum(1 for r in ok if not r["data"]["left_ungrounded"]) / len(ok)}
    if probes:
        out["guard_accuracy"] = sum(
            1 for r in probes if sorted(r["data"]["removed_numbers"]) == sorted(r["_case"]["expected"]["removed"])
        ) / len(probes)
    return out
