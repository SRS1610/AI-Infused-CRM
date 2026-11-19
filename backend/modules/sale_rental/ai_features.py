"""AI features of rentals (sub-phase 2.6, BUILD_PLAN §3).

``sale_rental.demand_forecast`` — rental demand forecasting (statistics in-process, optional mid-tier narrative)
    Units out per day and per week of a rentable product for the next weeks (``ai_demand.forecast``)
    from its daily history (:func:`modules.sale_rental.rental.rental_history`, the company's own rentals only), shown
    with what is already booked. The optional narrative may only state the forecast's numbers (sentences with other
    figures are removed). Nothing is written.

``sale_rental.usage_anomaly`` — usage alerts (notification)
    A daily check (:func:`modules.sale_rental.ai_demand.detect`) raises ``sale.rental_anomaly`` flags — repeated late
    returns by a customer, unusually long bookings, utilisation spikes and drops against the forecast — and notifies
    the order's salesperson (sales administrators for product-wide alerts). Flags are notifications: nothing else
    changes. An optional narration explains one flag from its figures.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import TYPE_CHECKING, Any

from modules.sale_rental import ai_demand as ML
from modules.sale_rental.rental import rental_history
from tessera.ai import Source
from tessera.ai.facts import audit_run, ensure_enabled, feature_enabled, ground_text
from tessera.ai.runtime import handler
from tessera.ai.tools import ToolContext
from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.jobs import job
from tessera.logging import get_logger

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

log = get_logger(__name__)
FORECAST = "sale_rental.demand_forecast"
ANOMALY = "sale_rental.usage_anomaly"
HISTORY_WEEKS = 26
MAX_WEEKS = 12
WINDOW_DAYS = 180


def _company_id(env: Environment) -> int:
    return int((env.company or env.ref("core.main_company")).ids[0])


def _rentable(env: Environment, product: Model) -> Model:
    if product is None or product._name != "product.product":
        raise UserError(_("Choose a rental product"))
    product.check_access("read")
    if not product.elevated().rent_ok:
        raise UserError(_("{product} is not rented", product=product.elevated().display_name))
    return product


# ============================================================================================ demand forecast


def product_forecast(env: Environment, product: Model, weeks: int = 4, day: dt.date | None = None) -> dict[str, Any]:
    """Forecast of ``product`` for the next ``weeks`` weeks from the company's last :data:`HISTORY_WEEKS` weeks, with
    the units already booked for those days."""
    product = _rentable(env, product)
    weeks = max(1, min(MAX_WEEKS, int(weeks)))
    day = day or env.today()
    company = _company_id(env)
    first = day - dt.timedelta(days=7 * HISTORY_WEEKS)
    past = rental_history(product, first, day - dt.timedelta(days=1), company_id=company)
    units = int(product.elevated().rental_units or 0)
    out = ML.forecast([r["booked_units"] for r in past], first, weeks=weeks, units=units)
    ahead = rental_history(product, day, day + dt.timedelta(days=7 * weeks - 1), company_id=company)
    for w, row in enumerate(out["weeks"]):
        row["booked_unit_days"] = sum(r["booked_units"] for r in ahead[7 * w : 7 * (w + 1)])
    for d, row in zip(out["days"], ahead, strict=False):
        d["booked"] = row["booked_units"]
    last_weeks = [sum(r["booked_units"] for r in past[-7 * (k + 1) : len(past) - 7 * k]) for k in range(4)]
    out.update(
        {
            "product_id": product.ids[0],
            "product": product.elevated().display_name,
            "as_of": day.isoformat(),
            "recent_weeks": list(reversed(last_weeks)),
        }
    )
    return out


def forecast_view(env: Environment, product: Model, weeks: int = 4) -> dict[str, Any]:
    """The forecast panel's data (API), with its audit run."""
    ensure_enabled(env, FORECAST)
    started = time.monotonic()
    out = product_forecast(env, product, weeks)
    run = audit_run(
        env, FORECAST, record=product, inputs={"weeks": weeks, "as_of": out["as_of"]}, started=started,
        model=out["method"], output={k: out[k] for k in ("method", "level", "trend_per_week", "weeks")},
    )  # fmt: skip
    return {**out, "run_id": run.ids[0], "ai_generated": True}


def _forecast_text(fc: dict[str, Any]) -> str:
    weeks = "\n".join(
        f"- week {w['week']} ({w['week_start']} to {w['week_end']}): {w['unit_days']} unit-days expected, "
        f"{w['average']} units out per day on average, 80% range {w['low']} to {w['high']}, "
        f"{w['booked_unit_days']} already booked"
        + (f", utilisation {round(w['utilisation'] * 100, 1)}%" if "utilisation" in w else "")
        for w in fc["weeks"]
    )
    return "\n".join(
        [
            f"Product: {fc['product']} ({fc['units']} rentable units)",
            f"As of {fc['as_of']}; method: {fc['method']} on {fc['history_days']} days of history",
            f"Unit-days out in the last 4 weeks (oldest first): {', '.join(str(v) for v in fc['recent_weeks'])}",
            f"Trend: {fc['trend_per_week']} units out per day, per week",
            f"Forecast:\n{weeks}",
        ]
    )


def forecast_facts(fc: dict[str, Any]) -> dict[str, Any]:
    pct = [round(w["utilisation"] * 100, 1) for w in fc["weeks"] if "utilisation" in w]
    return {"forecast": {k: v for k, v in fc.items() if k != "days"}, "percent": pct, "horizon": [80]}


@handler(FORECAST)
def narrate_forecast(env: Environment, record: Model, inputs: dict[str, Any]) -> dict[str, Any]:
    """Optional narrative of a product's forecast (``/api/v1/ai/features/…/run``); its numbers are the forecast's."""
    fc = product_forecast(env, record, int(inputs.get("weeks") or 4))
    facts = forecast_facts(fc)

    def on_output(ctx: ToolContext, text: str, data: Any) -> None:
        clean, removed = ground_text(str((data or {}).get("narrative") or text), facts)
        if isinstance(data, dict):
            data.clear()
            data.update({"narrative": clean, "removed_numbers": removed, "forecast": fc["weeks"]})

    source = Source(f"product.product:{record.ids[0]}", f"Demand forecast: {fc['product']}", _forecast_text(fc))
    return {"sources": [source], "on_output": on_output}


# ============================================================================================ usage anomalies


def usage_facts(env: Environment, company_id: int, now: dt.datetime | None = None) -> dict[str, Any]:
    """The company's rentals of the last :data:`WINDOW_DAYS` days and the daily units out of its rental products."""
    now = now or env.now()
    day = now.date()
    since = now - dt.timedelta(days=WINDOW_DAYS)
    lines = (
        env["sale.quotation_line"]
        .elevated("rental usage check")
        .search(
            [
                ("is_rental", "=", True),
                ("company_id", "=", company_id),
                ("quotation_id.state", "=", "accepted"),
                ("start_date", ">=", since),
                ("rental_status", "!=", None),
            ]
        )
    )
    rentals = []
    products: dict[int, Any] = {}
    for line in lines:
        start = line.pickup_datetime or line.start_date
        rentals.append(
            {
                "line_id": line.ids[0],
                "order_id": line.quotation_id.ids[0],
                "partner_id": line.quotation_id.partner_id.commercial_partner_id.ids[0],
                "partner": line.quotation_id.partner_id.commercial_partner_id.name,
                "product_id": line.product_id.ids[0],
                "start": start,
                "planned_end": line.return_date,
                "returned": line.returned_datetime,
                "status": line.rental_status,
            }
        )
        products[line.product_id.ids[0]] = line.product_id
    history: dict[int, dict[str, Any]] = {}
    first = day - dt.timedelta(days=70)
    for pid, product in products.items():
        rows = rental_history(product, first, day - dt.timedelta(days=1), company_id=company_id)
        history[pid] = {
            "name": product.elevated().display_name,
            "units": int(product.elevated().rental_units or 0),
            "history": [r["booked_units"] for r in rows],
        }
    return {"now": now, "day": day, "rentals": rentals, "products": history}


def _notify_user(env: Environment, flag: dict[str, Any]) -> list[int]:
    if flag.get("order_id"):
        order = env["sale.quotation"].elevated().browse([int(flag["order_id"])])
        user = order.user_id or order.create_uid
        if user:
            return list(user.ids)
    group = env.ref("sale.group_sale_manager", raise_if_not_found=False)
    if group is None:
        return []
    return list(group.elevated().user_ids.filtered(lambda u: u.active and not u.share).ids)


def raise_flags(env: Environment, company_id: int, flags: list[dict[str, Any]], day: dt.date) -> Any:
    """Store new flags (one open or recently dismissed flag per subject) and notify; returns the created alerts."""
    alerts = env["sale.rental_anomaly"].elevated("rental usage alert")
    created = alerts.browse([])
    recent = day - dt.timedelta(days=30)
    for flag in flags:
        known = alerts.search_count(
            [
                ("company_id", "=", company_id),
                ("key", "=", flag["key"]),
                {"any": [("state", "=", "open"), ("detected_on", ">=", recent)]},
            ]
        )
        if known:
            continue
        users = _notify_user(env, flag)
        alert = alerts.create(
            {
                "company_id": company_id,
                "kind": flag["kind"],
                "key": flag["key"],
                "product_id": flag.get("product_id"),
                "partner_id": flag.get("partner_id"),
                "line_id": flag.get("line_id"),
                "order_id": flag.get("order_id"),
                "user_id": users[0] if users else None,
                "score": flag["score"],
                "details": flag["details"],
                "detected_on": day,
            }
        )
        created |= alert
        if users:
            env["core.notification"].notify(
                users,
                _("Rental alert: {alert}", alert=alert.describe()),
                str(_("Flagged by the rental usage check (AI). Review it in Rentals > Usage alerts.")),
                model="sale.rental_anomaly",
                res_id=alert.ids[0],
            )
    return created


def check_company(env: Environment, company_id: int, now: dt.datetime | None = None) -> dict[str, Any]:
    started = time.monotonic()
    facts = usage_facts(env, company_id, now)
    flags = ML.detect(facts)
    created = raise_flags(env, company_id, flags, facts["day"])
    result = {"flags": len(flags), "new": len(created), "rentals": len(facts["rentals"])}
    audit_run(
        env.with_company(company_id), ANOMALY, inputs={"company_id": company_id, "day": facts["day"]},
        output={**result, "alerts": created.ids}, started=started, model="usage rules",
    )  # fmt: skip
    return result


@job("sale_rental.ai_usage_anomaly", queue="ai", max_attempts=2, as_system=True, timeout_seconds=1800)
def usage_anomaly_job(env: Any, company_ids: list[int] | None = None) -> dict[str, Any]:
    """Daily: check each company's rental usage and raise alerts (companies where the feature is on)."""
    companies = env["core.company"].elevated().search([("id", "in", company_ids)] if company_ids else [])
    out: dict[str, Any] = {}
    for company in companies:
        cid = company.ids[0]
        out[str(cid)] = check_company(env, cid) if feature_enabled(env, ANOMALY, cid) else {"skipped": True}
    log.info("rental usage check", result=out)
    return out


def alert_dict(alert: Model) -> dict[str, Any]:
    rec = alert.elevated()
    return {
        "id": rec.ids[0],
        "kind": rec.kind,
        "kind_label": str(dict(rec._fields["kind"].selection).get(rec.kind, rec.kind)),
        "summary": alert.describe(),
        "score": rec.score,
        "details": rec.details or {},
        "product_id": rec.product_id.ids[0] if rec.product_id else None,
        "partner_id": rec.partner_id.ids[0] if rec.partner_id else None,
        "order_id": rec.order_id.ids[0] if rec.order_id else None,
        "detected_on": rec.detected_on.isoformat(),
        "state": rec.state,
        "ai_generated": True,
    }


def alerts_view(env: Environment, *, product_id: int | None = None, order_id: int | None = None) -> dict[str, Any]:
    """Open alerts the user can read about a product or an order (an order: its customer's and its rentals')."""
    ensure_enabled(env, ANOMALY)
    domain: list[Any] = [("state", "=", "open")]
    if product_id:
        env["product.product"].browse([product_id]).check_access("read")
        domain.append(("product_id", "=", product_id))
    if order_id:
        order = env["sale.quotation"].browse([order_id])
        order.check_access("read")
        partner = order.elevated().partner_id.commercial_partner_id
        domain.append({"any": [("order_id", "=", order_id), ("partner_id", "=", partner.ids[0])]})
    found = env["sale.rental_anomaly"].search(domain, limit=50)
    return {"alerts": [alert_dict(a) for a in found], "ai_generated": True}


@handler(ANOMALY)
def narrate_alert(env: Environment, record: Model, inputs: dict[str, Any]) -> dict[str, Any]:
    """Optional narration of one alert from its figures (``/api/v1/ai/features/…/run``)."""
    if record is None or record._name != "sale.rental_anomaly":
        raise UserError(_("Choose a rental alert"))
    record.check_access("read")
    data = alert_dict(record)
    text = f"{data['kind_label']}: {data['summary']}. Figures: {data['details']}. Detected on {data['detected_on']}."

    def on_output(ctx: ToolContext, out: str, result: Any) -> None:
        clean, removed = ground_text(str((result or {}).get("explanation") or out), data)
        if isinstance(result, dict):
            result.clear()
            result.update({"explanation": clean, "removed_numbers": removed})

    return {
        "sources": [Source(f"sale.rental_anomaly:{record.ids[0]}", data["kind_label"], text)],
        "on_output": on_output,
    }


# ============================================================================================ evaluation


def _result(data: dict[str, Any]) -> dict[str, Any]:
    import json

    return {"data": data, "text": json.dumps(data, sort_keys=True, default=str), "proposals": 0, "flags": []}


def eval_forecast(env: Environment, inputs: dict[str, Any], record: Any) -> dict[str, Any]:
    """Back-test: forecast the case's next weeks from its history (the production model)."""
    ensure_enabled(env, FORECAST)
    fc = ML.forecast(
        inputs["history"], dt.date.fromisoformat(inputs["first_day"]), weeks=int(inputs.get("weeks", 4)),
        units=inputs.get("units"),
    )  # fmt: skip
    return _result({"weeks": fc["weeks"], "days": [d["units_out"] for d in fc["days"]], "method": fc["method"]})


def score_forecast(results: list[dict[str, Any]]) -> dict[str, float]:
    """``mape``: weekly units out (unit-days) over every case and week — the planning granularity; ``mape_daily``
    for information; ``coverage_80``: actual weeks inside the 80 % range."""
    ok = [r for r in results if r.get("status") == "ok" and r.get("data") and "actual_weeks" in r["_case"]["expected"]]
    if not ok:
        return {}
    pw, aw, pd, ad, inside, total = [], [], [], [], 0, 0
    for r in ok:
        exp = r["_case"]["expected"]
        for w, actual in zip(r["data"]["weeks"], exp["actual_weeks"], strict=True):
            pw.append(w["unit_days"])
            aw.append(actual)
            inside += 1 if w["low"] <= actual <= w["high"] else 0
            total += 1
        pd += r["data"]["days"]
        ad += exp["actual"]
    out = {"mape": ML.mape(pw, aw), "mape_daily": ML.mape(pd, ad), "coverage_80": inside / total}
    by_id = {r["case"]: r for r in results}
    twins = [
        1.0 if r["data"]["weeks"] == by_id[t]["data"]["weeks"] else 0.0
        for r in results
        if (t := r["_case"]["expected"].get("same_as")) and r.get("data") and by_id.get(t, {}).get("data")
    ]
    if twins:
        out["injection_invariance"] = sum(twins) / len(twins)
    return out


def _parse_dates(facts: dict[str, Any]) -> dict[str, Any]:
    def when(v: Any) -> Any:
        return dt.datetime.fromisoformat(v) if isinstance(v, str) else v

    rentals = [
        {**r, **{k: when(r.get(k)) for k in ("start", "planned_end", "returned")}} for r in facts.get("rentals") or []
    ]
    products = {int(k): v for k, v in (facts.get("products") or {}).items()}
    now = when(facts["now"])
    return {**facts, "now": now, "day": now.date(), "rentals": rentals, "products": products}


def _subject(key: str) -> str:
    """Flag key → case subject (spikes and drops are both "utilisation")."""
    return key.replace("utilisation_spike", "utilisation").replace("utilisation_drop", "utilisation")


def eval_anomaly(env: Environment, inputs: dict[str, Any], record: Any) -> dict[str, Any]:
    """Runs every detector on the case's usage facts and reports whether its subject (``inputs.subject``) is flagged."""
    ensure_enabled(env, ANOMALY)
    flags = ML.detect(_parse_dates(inputs["facts"]))
    subject = inputs["subject"]
    hit = [f for f in flags if _subject(f["key"]).startswith(subject)]
    return _result({"flagged": bool(hit), "kinds": sorted({f["kind"] for f in hit}), "flags": len(flags)})


def score_anomaly(results: list[dict[str, Any]]) -> dict[str, float]:
    ok = [r for r in results if r.get("status") == "ok" and r.get("data") and "anomalous" in r["_case"]["expected"]]
    if not ok:
        return {}
    flags = [bool(r["data"]["flagged"]) for r in ok]
    labels = [bool(r["_case"]["expected"]["anomalous"]) for r in ok]
    tp = sum(1 for f, y in zip(flags, labels, strict=True) if f and y)
    out = {
        "precision": tp / sum(flags) if any(flags) else 0.0,
        "recall": tp / sum(labels) if any(labels) else 0.0,
        "flag_rate": sum(flags) / len(flags),
    }
    for kind in ("late_returns", "long_booking", "utilisation"):
        sub = [(f, y) for r, f, y in zip(ok, flags, labels, strict=True) if r["_case"]["expected"].get("kind") == kind]
        flagged = sum(1 for f, _y in sub if f)
        if flagged:
            out[f"precision_{kind}"] = sum(1 for f, y in sub if f and y) / flagged
    by_id = {r["case"]: r for r in results}
    twins = [
        1.0 if r["data"]["flagged"] == by_id[t]["data"]["flagged"] else 0.0
        for r in results
        if (t := r["_case"]["expected"].get("same_as")) and r.get("data") and by_id.get(t, {}).get("data")
    ]
    if twins:
        out["injection_invariance"] = sum(twins) / len(twins)
    return out
