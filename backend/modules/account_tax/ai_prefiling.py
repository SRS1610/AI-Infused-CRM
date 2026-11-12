"""``account_tax.prefiling`` — pre-filing anomaly checks with explanations (sub-phase 2.6, BUILD_PLAN §3: flag).

Before a tax return is closed, :func:`review` ranks and explains

* the deterministic findings of :func:`modules.account_tax.prefiling.prefiling_checks` (grids, reverse charge
  without tax ID, unusual tax for a country, fiscal position mismatch, drafts, report vs ledger, lock);
* two statistical outliers computed here against the **previous periods** of the company:

  - ``rate_outlier_for_country`` — a customer invoice line whose tax rate was never (or almost never) used for
    customers of the same country and kind (business with a tax ID / consumer) in the previous four periods;
  - ``grid_swing`` — a grid of the tax report that moved far from its history (robust: change above both 60 %
    of the median and 3.5 robust deviations, and above a floor), appeared from nothing or vanished.

Each item gets a rank (severity first, then the amount at stake relative to the net tax, then the number of
documents), a plain-language explanation of why it matters and what to do. Nothing is written and nothing
blocks the closing: these are flags shown on the return. The optional narrative (``narrate``) is written by the
small-tier model from the ranked items only; a sentence quoting a figure that is not among them is removed.
"""

from __future__ import annotations

import datetime as dt
import statistics
import time
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends

from modules.account_tax.closing import period_of, tax_report
from modules.account_tax.prefiling import prefiling_checks
from tessera.ai import Source
from tessera.ai.facts import audit_run, ensure_enabled, ground_text
from tessera.ai.runtime import HANDLERS, handler, run_feature
from tessera.api.deps import RequestContext, authenticated
from tessera.api.registry import register_router
from tessera.exceptions import MissingError, UserError
from tessera.i18n import _

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

FEATURE = "account_tax.prefiling"
HISTORY_PERIODS = 4
RATE_MIN_HISTORY = 4  # history lines of a country needed to call a rate unusual
RATE_RARE_SHARE = 0.1  # a rate used by less than this share of the country's history lines is unusual
SWING_RELATIVE = 0.6
SWING_MAD = 3.5
SWING_FLOOR = 200.0
SEVERITY_WEIGHT = {"error": 300, "warning": 200, "info": 100}
STATISTICAL = ("rate_outlier_for_country", "grid_swing")


# ----------------------------------------------------------------------------------------- statistics


def previous_periods(company: Model, date_from: dt.date, count: int = HISTORY_PERIODS) -> list[tuple[dt.date, dt.date]]:
    out = []
    day = date_from
    for _i in range(count):
        start, end = period_of(company, day - dt.timedelta(days=1))
        out.append((start, end))
        day = start
    return out


def _rate_lines(env: Environment, company: Model, start: dt.date, end: dt.date) -> list[tuple[Any, ...]]:
    """(move id, country id, business?, rate, date) of the customer invoice lines of the dates."""
    sudo = env.elevated("pre-filing statistics")
    return sudo.cr.fetchall(
        f"""
        SELECT m.id, p.country_id, COALESCE(p.vat, '') <> '' AS business,
               COALESCE((SELECT sum(t.amount) FROM account_move_line_tax_rel tr
                           JOIN account_tax t ON t.id = tr.tax_id
                          WHERE tr.line_id = l.id AND t.amount_type = 'percent'), 0) AS rate,
               m.date
          FROM account_move_line l
          JOIN account_move m ON m.id = l.move_id
          JOIN "{sudo["contacts.partner"]._table}" p ON p.id = m.commercial_partner_id
         WHERE m.company_id = :cid AND m.state = 'posted' AND m.move_type IN ('out_invoice', 'out_refund')
           AND l.display_type = 'product' AND m.date >= :start AND m.date <= :end
        """,  # noqa: S608
        {"cid": company.ids[0], "start": start, "end": end},
    )


def rate_outliers(env: Environment, company: Model, date_from: dt.date, date_to: dt.date) -> list[dict[str, Any]]:
    history_start = previous_periods(company, date_from)[-1][0]
    rows = _rate_lines(env, company, history_start, date_to)
    history: dict[tuple[Any, bool], Counter[float]] = defaultdict(Counter)
    current: dict[tuple[Any, bool], list[tuple[int, float]]] = defaultdict(list)
    for move_id, country_id, business, rate, day in rows:
        key = (country_id, bool(business))
        if day < date_from:
            history[key][round(float(rate), 2)] += 1
        else:
            current[key].append((int(move_id), round(float(rate), 2)))
    countries = env["core.country"].elevated()
    out = []
    for key, lines in current.items():
        past = history.get(key)
        if not past or sum(past.values()) < RATE_MIN_HISTORY:
            continue
        total = sum(past.values())
        usual = past.most_common(1)[0][0]
        odd: dict[float, set[int]] = defaultdict(set)
        for move_id, rate in lines:
            if past.get(rate, 0) / total < RATE_RARE_SHARE:
                odd[rate].add(move_id)
        country = countries.browse([key[0]]).name if key[0] else str(_("no country"))
        for rate, moves in sorted(odd.items()):
            out.append(
                {
                    "country": country,
                    "country_id": key[0],
                    "business": key[1],
                    "rate": rate,
                    "usual_rate": usual,
                    "history_lines": total,
                    "move_ids": sorted(moves),
                }
            )
    return out


def _grids(env: Environment, company: Model, start: dt.date, end: dt.date) -> dict[str, tuple[str, float]]:
    data = tax_report(env.elevated("pre-filing statistics"), company, start, end)
    out = {}
    for line in data["lines"]:
        code = line.get("code")
        if code and code not in ("NET",):
            out[str(code)] = (str(line.get("name") or code), round(float(line["values"][0] or 0), 2))
    return out


def grid_swings(env: Environment, company: Model, date_from: dt.date, date_to: dt.date) -> list[dict[str, Any]]:
    periods = previous_periods(company, date_from)
    current = _grids(env, company, date_from, date_to)
    past = [_grids(env, company, s, e) for s, e in periods]
    active = [p for p in past if any(v for _n, v in p.values())]
    if len(active) < 2:  # a first return (or almost): nothing to compare with
        return []
    out = []
    for code, (name, value) in current.items():
        hist = [p.get(code, ("", 0.0))[1] for p in past]
        if sum(1 for h in hist if h) < 2 and not value:
            continue
        med = statistics.median(hist)
        mad = statistics.median([abs(h - med) for h in hist])
        change = value - med
        if not any(hist):
            kind = "new" if abs(value) >= SWING_FLOOR else None
        elif all(hist) and not value:
            kind = "vanished" if abs(med) >= SWING_FLOOR else None
        elif abs(change) > max(SWING_RELATIVE * abs(med), SWING_MAD * 1.4826 * mad, SWING_FLOOR):
            kind = "increase" if change > 0 else "decrease"
        else:
            kind = None
        if kind:
            out.append(
                {
                    "grid": code,
                    "name": name,
                    "value": value,
                    "median": round(med, 2),
                    "change": round(change, 2),
                    "kind": kind,
                    "history": hist,
                }
            )
    return out


# ----------------------------------------------------------------------------------------- explanations


def title(code: str) -> str:
    titles = {
        "taxes_without_grids": _("Taxes without tax grids"),
        "tax_lines_without_grid": _("Tax amounts outside every grid"),
        "reverse_charge_without_vat": _("Reverse charge for a partner without tax ID"),
        "unusual_tax_for_country": _("Unusual tax for the customer's country"),
        "fiscal_position_mismatch": _("Fiscal position different from the partner's"),
        "draft_entries": _("Draft entries in the period"),
        "report_ledger_mismatch": _("Return does not tie to the tax accounts"),
        "period_locked": _("Period already locked"),
        "rate_outlier_for_country": _("Tax rate never used for this country before"),
        "grid_swing": _("Grid far from previous periods"),
    }
    return str(titles.get(code, code))


def why(code: str) -> str:
    texts = {
        "taxes_without_grids": _("Amounts of these taxes never reach the return; the declared figures are too low."),
        "tax_lines_without_grid": _("Tax amounts were booked outside the return's grids, so the return will not tie."),
        "reverse_charge_without_vat": _(
            "Reverse charge and intra-EU treatment require the partner's tax ID; without it the exemption can be "
            "refused in an audit."
        ),
        "unusual_tax_for_country": _("Most invoices to this country use another tax: the odd ones may be wrong."),
        "fiscal_position_mismatch": _(
            "The tax treatment differs from what the partner's country and tax ID call for (for example domestic "
            "VAT charged to an EU business)."
        ),
        "draft_entries": _("Drafts dated in the period are left out of the return and locked out by the closing."),
        "report_ledger_mismatch": _("The return and the tax accounts disagree: a manual entry or a grid is missing."),
        "period_locked": _("The period is closed: corrections go in the next return."),
        "rate_outlier_for_country": _(
            "Customers of this country and kind were never charged this rate in the previous periods."
        ),
        "grid_swing": _(
            "The grid moved far from its usual level in the previous periods; check it is a real change in "
            "business and not a coding error."
        ),
    }
    return str(texts.get(code, ""))


def action(code: str) -> str:
    texts = {
        "taxes_without_grids": _("Add tax grids to the base and tax distribution of these taxes."),
        "tax_lines_without_grid": _("Fix the taxes of these entries, or add the grids and reset them to draft."),
        "reverse_charge_without_vat": _("Add the partner's tax ID, or correct the fiscal position of the documents."),
        "unusual_tax_for_country": _("Open the listed invoices and check their taxes."),
        "fiscal_position_mismatch": _("Correct the fiscal position (credit note and new invoice if posted)."),
        "draft_entries": _("Post or delete the drafts before closing."),
        "report_ledger_mismatch": _("Find the manual entries on tax accounts and correct them before closing."),
        "period_locked": _("Prepare the correction in the next period."),
        "rate_outlier_for_country": _("Open the listed invoices and confirm the rate or correct the tax."),
        "grid_swing": _("Compare the grid's documents with the previous period's before filing."),
    }
    return str(texts.get(code, ""))


def _impact(f: dict[str, Any]) -> float:
    d = f.get("details") or {}
    if f["code"] == "report_ledger_mismatch":
        return abs(float(d.get("difference") or 0))
    if f["code"] == "grid_swing":
        return max((abs(float(s["change"])) for s in d.get("swings", [])), default=0.0)
    return 0.0


def rank(items: list[dict[str, Any]], net: float) -> list[dict[str, Any]]:
    """Order findings: severity, then the amount at stake against the net tax, then the documents involved."""
    scale = max(abs(net), 1.0)
    for f in items:
        f["score"] = round(
            SEVERITY_WEIGHT.get(f["severity"], 0) + min(80.0, 80.0 * _impact(f) / scale) + min(20, f["count"]), 2
        )
    items.sort(key=lambda f: (-f["score"], f["code"]))
    for i, f in enumerate(items, 1):
        f["rank"] = i
        f["title"] = title(f["code"])
        f["why"] = why(f["code"])
        f["action"] = action(f["code"])
    return items


def _statistical(env: Environment, company: Model, date_from: dt.date, date_to: dt.date) -> list[dict[str, Any]]:
    out = []
    outliers = rate_outliers(env, company, date_from, date_to)
    if outliers:
        moves = sorted({m for o in outliers for m in o["move_ids"]})
        out.append(
            {
                "code": "rate_outlier_for_country",
                "severity": "warning",
                "message": str(
                    _(
                        "{count} customer invoices use a tax rate never used for their country before",
                        count=len(moves),
                    )
                ),
                "model": "account.move",
                "res_ids": moves[:200],
                "count": len(moves),
                "details": {"outliers": outliers},
            }
        )
    swings = grid_swings(env, company, date_from, date_to)
    if swings:
        out.append(
            {
                "code": "grid_swing",
                "severity": "warning",
                "message": str(
                    _(
                        "{count} grids moved far from the previous periods: {grids}",
                        count=len(swings),
                        grids=", ".join(s["grid"] for s in swings),
                    )
                ),
                "model": None,
                "res_ids": [],
                "count": len(swings),
                "details": {"swings": swings},
            }
        )
    return out


def review_facts(env: Environment, company: Model, date_from: dt.date, date_to: dt.date) -> dict[str, Any]:
    checks = [{**f, "source": "check"} for f in prefiling_checks(company, date_from, date_to)]
    stats = [{**f, "source": "statistics"} for f in _statistical(env, company, date_from, date_to)]
    net = 0.0
    for line in tax_report(env.elevated("pre-filing"), company, date_from, date_to)["lines"]:
        if line.get("code") == "NET":
            net = round(float(line["values"][0] or 0), 2)
    items = rank(checks + stats, net)
    return {
        "company": company.elevated().name,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "net": net,
        "items": items,
    }


def _readable_return(env: Environment, ret: Model) -> Model:
    if ret is None or ret._name != "account.tax_return":
        raise UserError(_("Choose a tax return"))
    ret.check_access("read")  # tax returns: accountants (read-only, user, adviser) of the return's company
    return ret


def review(env: Environment, ret: Model) -> dict[str, Any]:
    """Ranked, explained findings of a tax return's period (flags; nothing is written)."""
    ensure_enabled(env, FEATURE)
    _readable_return(env, ret)
    started = time.monotonic()
    rec = ret.elevated()
    facts = review_facts(env, rec.company_id, rec.date_from, rec.date_to)
    run = audit_run(
        env,
        FEATURE,
        record=ret,
        output=[{k: f[k] for k in ("rank", "code", "severity", "count", "score")} for f in facts["items"]],
        model="pre-filing checks + period statistics",
        started=started,
    )
    return {**facts, "return_id": ret.ids[0], "run_id": run.ids[0], "ai_generated": True}


def _facts_text(f: dict[str, Any]) -> str:
    lines = [
        f"Tax return {f['date_from']} to {f['date_to']}, net tax {f['net']:,.2f}",
        "Findings, most important first:",
    ]
    for i in f["items"]:
        lines.append(f"{i['rank']}. [{i['severity']}] {i['title']}: {i['message']} — {i['why']} {i['action']}")
        for s in (i.get("details") or {}).get("swings", []):
            lines.append(f"   grid {s['grid']} {s['name']}: {s['value']:,.2f} (usual {s['median']:,.2f}, {s['kind']})")
        for o in (i.get("details") or {}).get("outliers", []):
            lines.append(
                f"   {o['country']}: rate {o['rate']:g}% on {len(o['move_ids'])} invoices (usual {o['usual_rate']:g}%)"
            )
    if not f["items"]:
        lines.append("none")
    return "\n".join(lines)


@handler(FEATURE)
def prepare(env: Environment, record: Model | None, inputs: dict[str, Any]) -> dict[str, Any]:
    ret = _readable_return(env, record)  # type: ignore[arg-type]
    rec = ret.elevated()
    facts = review_facts(env, rec.company_id, rec.date_from, rec.date_to)
    return {"sources": [Source(f"account.tax_return:{ret.ids[0]}", rec.name, _facts_text(facts))]}


def narrate(env: Environment, ret: Model) -> dict[str, Any]:
    """A short plain-language briefing on the findings; every figure must be one of theirs."""
    ensure_enabled(env, FEATURE)
    context = HANDLERS[FEATURE](env, ret, {})
    rec = ret.elevated()
    facts = review_facts(env, rec.company_id, rec.date_from, rec.date_to)
    result = run_feature(env, FEATURE, {}, record=ret, **context)
    raw = str(result.data.get("narrative") or "") if isinstance(result.data, dict) else result.text
    clean, removed = ground_text(raw, facts)
    return {"run_id": result.run.ids[0], "narrative": clean, "removed_numbers": removed, "ai_generated": True}


# ----------------------------------------------------------------------------------------- API

router = register_router(APIRouter(prefix="/account_tax/ai", tags=["account_tax", "ai"]))


def _ret(ctx: RequestContext, ret_id: int) -> Model:
    ret = ctx.env["account.tax_return"].browse([ret_id])
    if not ret.exists():
        raise MissingError(_("No such tax return"))
    return ret


@router.get("/returns/{ret_id}/prefiling")
def prefiling(ret_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """Ranked and explained pre-filing findings of a tax return's period."""
    ensure_enabled(ctx.env, FEATURE)
    return review(ctx.env, _ret(ctx, ret_id))


@router.post("/returns/{ret_id}/prefiling/narrative")
def prefiling_narrative(ret_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    ensure_enabled(ctx.env, FEATURE)
    return narrate(ctx.env, _ret(ctx, ret_id))
