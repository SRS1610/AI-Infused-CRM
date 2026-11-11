# ruff: noqa: RUF001 - the numbers guard reads the typographic minus sign
"""Cash-flow forecasting (``account.cashflow_forecast``): statistics compute, a model may narrate.

The engine (:func:`forecast`) projects the daily cash position for the next weeks from

1. the **bank balance** today (liquidity accounts);
2. **open receivables and payables** by due date, shifted by each partner's historical payment delay
   (median of its last payments; the company's median for partners without history; long-overdue
   receivables only half-expected);
3. documents **not issued yet**: the recent daily invoicing / billing rate convolved with the empirical
   distribution of issue-to-payment lags;
4. **recurring** bank movements (salaries, rent, subscriptions): monthly groups with a stable amount and
   day of month, projected on their usual day;
5. the **remaining** movements as their trailing weekly mean, with a band from their weekly variability.

Every number shown comes from the engine. The narrative (optional, ``ai/prompts/cashflow_forecast.yaml``)
is checked by :func:`numbers_guard`: an answer quoting a number that is not among the forecast's facts is
replaced by a plain summary built from the facts.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import statistics
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from tessera.ai import Source
from tessera.ai.runtime import check_enabled, get_feature, handler
from tessera.i18n import _

if TYPE_CHECKING:
    from tessera.ai.tools import ToolContext
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

FEATURE = "account.cashflow_forecast"
OVERDUE_DOUBTFUL = 90
MAX_WEEKS = 26


@dataclass
class OpenItem:
    kind: str  # receivable | payable
    partner: str
    due: dt.date
    amount: float  # positive residual
    label: str = ""


@dataclass
class Document:
    """An issued invoice or bill of the past months: when it was issued, for how much, when it was paid."""

    kind: str
    partner: str
    issued: dt.date
    due: dt.date
    amount: float
    paid: dt.date | None = None


@dataclass
class Flow:
    """A bank movement not explained by an invoice or a bill (salaries, rent, fees, taxes…)."""

    date: dt.date
    amount: float  # signed: + in, - out
    label: str


@dataclass
class CashData:
    today: dt.date
    opening: float
    open_items: list[OpenItem] = field(default_factory=list)
    documents: list[Document] = field(default_factory=list)
    flows: list[Flow] = field(default_factory=list)
    currency: str = ""


# ----------------------------------------------------------------------------- engine


def _median(values: list[float], default: float) -> float:
    return float(statistics.median(values)) if values else default


def partner_delays(docs: list[Document]) -> tuple[dict[tuple[str, str], float], dict[str, float]]:
    """Median days paid after due, per (kind, partner) and per kind (last six payments per partner)."""
    per: dict[tuple[str, str], list[tuple[dt.date, float]]] = defaultdict(list)
    for d in docs:
        if d.paid is not None:
            per[(d.kind, d.partner)].append((d.paid, float((d.paid - d.due).days)))
    out = {k: _median([x for _, x in sorted(v)[-6:]], 0.0) for k, v in per.items() if len(v) >= 2}
    by_kind = {
        kind: _median([float((d.paid - d.due).days) for d in docs if d.kind == kind and d.paid is not None], 0.0)
        for kind in ("receivable", "payable")
    }
    return out, by_kind


def lag_pmf(docs: list[Document], kind: str, today: dt.date) -> dict[int, float]:
    """Distribution of issue-to-payment days, from documents old enough to have been paid (issued 75 to
    210 days ago), so recent unpaid documents do not bias it towards short delays."""
    lo, hi = today - dt.timedelta(days=210), today - dt.timedelta(days=75)
    lags = [(d.paid - d.issued).days for d in docs if d.kind == kind and d.paid is not None and lo <= d.issued <= hi]
    if not lags:
        return {30: 1.0}
    pmf: dict[int, float] = defaultdict(float)
    for lag in lags:
        pmf[max(0, lag)] += 1.0 / len(lags)
    return dict(pmf)


def issue_rate(docs: list[Document], kind: str, today: dt.date, days: int = 91) -> float:
    """Mean amount issued per day over the last ``days`` days."""
    since = today - dt.timedelta(days=days)
    return sum(d.amount for d in docs if d.kind == kind and since < d.issued <= today) / days


def recurring_groups(flows: list[Flow], today: dt.date) -> tuple[list[dict[str, Any]], set[int]]:
    """Monthly movements with a stable amount and day of month (≥ 3 of the last 4 months)."""
    groups: dict[str, list[int]] = defaultdict(list)
    for i, f in enumerate(flows):
        key = re.sub(r"\d+", "", f.label.lower()).strip() + ("+" if f.amount > 0 else "-")
        groups[key].append(i)
    out: list[dict[str, Any]] = []
    used: set[int] = set()
    start = today - dt.timedelta(days=122)
    for idx in groups.values():
        recent = [flows[i] for i in idx if flows[i].date > start]
        months = {(f.date.year, f.date.month) for f in recent}
        if len(months) < 3 or len(recent) > len(months) + 1:
            continue
        amounts = [f.amount for f in recent]
        mean = statistics.mean(amounts)
        cv = statistics.pstdev(amounts) / abs(mean) if mean else 1.0
        days = [f.date.day for f in recent]
        if cv > 0.15 or statistics.pstdev(days) > 3:
            continue
        last = max(recent, key=lambda f: f.date)
        out.append(
            {"label": last.label, "amount": last.amount, "day": int(statistics.median(days)), "count": len(recent)}
        )
        used.update(i for i in idx)
    return out, used


def forecast(data: CashData, weeks: int = 8) -> dict[str, Any]:
    """Daily and weekly cash position for ``weeks`` weeks after ``data.today`` (see the module docstring)."""
    weeks = max(1, min(MAX_WEEKS, int(weeks)))
    horizon = weeks * 7
    today = data.today
    days = [today + dt.timedelta(days=d) for d in range(1, horizon + 1)]
    index = {d: i for i, d in enumerate(days)}
    comp: dict[str, list[float]] = {
        k: [0.0] * horizon
        for k in ("receipts_open", "payments_open", "receipts_new", "payments_new", "recurring", "other")
    }
    delays, by_kind = partner_delays(data.documents)
    big: list[dict[str, Any]] = []
    # 1. open items, shifted by the partner's usual delay
    for item in data.open_items:
        delay = delays.get((item.kind, item.partner), by_kind.get(item.kind, 0.0))
        expected = item.due + dt.timedelta(days=round(delay))
        weight = 1.0
        if expected <= today:
            overdue = (today - item.due).days
            weight = 0.5 if item.kind == "receivable" and overdue > OVERDUE_DOUBTFUL else 1.0
            expected = today + dt.timedelta(days=7)  # overdue: expected within the next week
        if expected in index:
            key = "receipts_open" if item.kind == "receivable" else "payments_open"
            sign = 1 if item.kind == "receivable" else -1
            comp[key][index[expected]] += sign * item.amount * weight
            big.append({"label": item.label or item.partner, "partner": item.partner, "date": expected.isoformat(),
                        "amount": round(sign * item.amount * weight, 2), "kind": item.kind})  # fmt: skip
    # 2. documents not issued yet
    for kind, key, sign in (("receivable", "receipts_new", 1), ("payable", "payments_new", -1)):
        rate = issue_rate(data.documents, kind, today)
        pmf = lag_pmf(data.documents, kind, today)
        if not rate:
            continue
        for s in range(1, horizon + 1):  # issued on day s
            for lag, p in pmf.items():
                t = s + lag
                if t <= horizon:
                    comp[key][t - 1] += sign * rate * p
    # 3. recurring movements
    groups, used = recurring_groups(data.flows, today)
    for g in groups:
        for i, d in enumerate(days):
            last_day = (dt.date(d.year + d.month // 12, d.month % 12 + 1, 1) - dt.timedelta(days=1)).day
            if d.day == min(g["day"], last_day):
                comp["recurring"][i] += g["amount"]
    # 4. the rest: trailing weekly mean over 13 weeks
    since = today - dt.timedelta(days=91)
    rest = [f for i, f in enumerate(data.flows) if i not in used and f.date > since]
    weekly_rest = [0.0] * 13
    for f in rest:
        weekly_rest[min(12, (today - f.date).days // 7)] += f.amount
    mean_rest = statistics.mean(weekly_rest)
    sd_rest = statistics.pstdev(weekly_rest)
    for i in range(horizon):
        comp["other"][i] += mean_rest / 7
    # positions
    balance = data.opening
    daily = []
    for i, d in enumerate(days):
        net = sum(comp[k][i] for k in comp)
        balance += net
        daily.append({"date": d.isoformat(), "balance": round(balance, 2), "net": round(net, 2)})
    out_weeks = []
    for w in range(weeks):
        sl = slice(w * 7, (w + 1) * 7)
        inflow = sum(v for k in comp for v in comp[k][sl] if v > 0)
        outflow = sum(v for k in comp for v in comp[k][sl] if v < 0)
        position = daily[(w + 1) * 7 - 1]["balance"]
        band = 1.28 * sd_rest * math.sqrt(w + 1)
        out_weeks.append(
            {
                "week": w + 1,
                "start": days[w * 7].isoformat(),
                "end": days[(w + 1) * 7 - 1].isoformat(),
                "inflow": round(inflow, 2),
                "outflow": round(outflow, 2),
                "net": round(inflow + outflow, 2),
                "position": round(position, 2),
                "low": round(position - band, 2),
                "high": round(position + band, 2),
                **{k: round(sum(comp[k][sl]), 2) for k in comp},
            }
        )
    lowest = min(daily, key=lambda d: d["balance"])
    big.sort(key=lambda b: -abs(b["amount"]))
    return {
        "today": today.isoformat(),
        "currency": data.currency,
        "opening": round(data.opening, 2),
        "weeks": out_weeks,
        "daily": daily,
        "closing": out_weeks[-1]["position"],
        "lowest": {"date": lowest["date"], "balance": lowest["balance"]},
        "recurring": [{**g, "amount": round(g["amount"], 2)} for g in groups],
        "largest_items": big[:5],
        "open_receivables": round(sum(i.amount for i in data.open_items if i.kind == "receivable"), 2),
        "open_payables": round(sum(i.amount for i in data.open_items if i.kind == "payable"), 2),
        "method": "engine",
    }


def mape(predicted: list[float], actual: list[float]) -> float:
    pairs = [(p, a) for p, a in zip(predicted, actual, strict=False) if a]
    return sum(abs(p - a) / abs(a) for p, a in pairs) / len(pairs) if pairs else 0.0


# ----------------------------------------------------------------------------- the ledger as data


def cash_data(env: Environment, today: dt.date | None = None, history_days: int = 365) -> CashData:
    """The company's bank balance, open items, recent documents and other bank movements, read as the
    user (record rules and companies apply)."""
    company = env.company or env.ref("core.main_company")
    cid = company.ids[0]
    today = today or env.today()
    lines = env["account.move_line"]
    cash_accounts = env["account.account"].search([("company_id", "=", cid), ("account_type", "=", "asset_cash")])
    own = company.elevated()
    for fname in ("account_outstanding_receipts_id", "account_outstanding_payments_id"):
        if own[fname]:  # payments registered but not on a bank statement yet are cash already moved
            cash_accounts |= env["account.account"].browse(own[fname].ids)
    opening = 0.0
    if cash_accounts:
        groups = lines.read_group(
            [("account_id", "in", cash_accounts.ids), ("parent_state", "=", "posted"), ("date", "<=", today)],
            ["balance:sum"],
            [],
        )
        opening = float((groups[0].get("balance:sum") if groups else 0.0) or 0.0)
    moves = env["account.move"].search(
        [
            ("company_id", "=", cid),
            ("state", "=", "posted"),
            ("move_type", "in", ["out_invoice", "in_invoice", "out_refund", "in_refund"]),
            ("invoice_date", ">=", today - dt.timedelta(days=history_days)),
        ],
        limit=20000,
    )
    data = CashData(today, opening, currency=company.elevated().currency_id.name or "")
    for m in moves:
        kind = "receivable" if m.move_type in ("out_invoice", "out_refund") else "payable"
        sign = 1 if m.move_type in ("out_invoice", "in_invoice") else -1
        partner = (m.commercial_partner_id or m.partner_id).display_name or "" if m.partner_id else ""
        due = m.invoice_date_due or m.invoice_date or m.date
        total = abs(float(m.amount_total_signed or 0.0))
        residual = abs(float(m.amount_residual_signed or 0.0))
        paid = None
        if m.payment_state in ("paid", "in_payment"):
            term = m.line_ids.filtered(lambda ln: ln.display_type == "payment_term")
            dates = [p.max_date for ln in term for p in [*ln.matched_debit_ids, *ln.matched_credit_ids] if p.max_date]
            paid = max(dates) if dates else None
        data.documents.append(Document(kind, partner, m.invoice_date or m.date, due, sign * total, paid))
        if residual > 0.005 and m.payment_state not in ("paid", "in_payment", "reversed"):
            data.open_items.append(OpenItem(kind, partner, due, sign * residual, m.display_name or ""))
    if cash_accounts:
        cash_lines = lines.search(
            [
                ("account_id", "in", cash_accounts.ids),
                ("parent_state", "=", "posted"),
                ("date", ">", today - dt.timedelta(days=history_days)),
                ("date", "<=", today),
            ],
            limit=50000,
        )
        for ln in cash_lines:
            others = ln.move_id.line_ids.filtered(
                lambda o: o.account_id.account_type in ("asset_receivable", "liability_payable")
            )
            if others:
                continue  # explained by an invoice or a bill (counted through the documents)
            data.flows.append(Flow(ln.date, float(ln.balance or 0.0), ln.name or ln.move_id.ref or ""))
    return data


def ensure_enabled(env: Environment) -> None:
    check_enabled(env, get_feature(env, FEATURE))


# ----------------------------------------------------------------------------- narrative

NUMBER_RE = re.compile(r"\d{4}-\d{2}-\d{2}|[-−]?\d[\d,.\u202f]*\d(?:\s?%)?|\d(?:\s?%)?")


def _values(text: str) -> set[str]:
    """Numbers of a text, normalised (dates kept whole; 1,234.5 / 1 234,50 / 1234.50 → '1234.5')."""
    from modules.account.ai_ocr import parse_number

    out: set[str] = set()
    for m in NUMBER_RE.finditer(text):
        tok = m.group(0).strip().rstrip("%").strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", tok):
            out.add(tok)
            continue
        for candidate in (tok, tok.replace(" ", "")):
            v = parse_number(candidate)
            if v is not None:
                out.add(f"{abs(v):.2f}".rstrip("0").rstrip("."))
                out.add(f"{abs(round(v)):d}")
    return out


def foreign_numbers(text: str, facts: str) -> list[str]:
    """Numbers of ``text`` that are not among the numbers of ``facts`` (amounts rounded to units allowed)."""
    known = _values(facts)
    bad = []
    text = re.sub(r"\[source:[^\]]+\]", "", text)  # citations carry record ids, not figures
    for m in NUMBER_RE.finditer(text):
        tok = m.group(0).strip()
        vals = _values(tok)
        if vals and not (vals & known):
            bad.append(tok)
    return bad


def numbers_guard(facts: str, fallback: str) -> Callable[[ToolContext, str, Any], None]:
    """``on_output`` hook of the narrated features: keeps the model's narrative only when every number in it
    is one of the facts; otherwise the narrative is replaced by ``fallback`` and the run is flagged."""

    def hook(ctx: ToolContext, text: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        narrative = str(data.get("narrative") or text or "")
        bad = foreign_numbers(narrative, facts)
        data["numbers_checked"] = True
        data["foreign_numbers"] = bad
        if bad:
            data["narrative_model"] = narrative
            data["narrative"] = fallback
            data["narrative_source"] = "facts"
        else:
            data["narrative"] = narrative
            data["narrative_source"] = "model"

    return hook


def _fmt(v: float) -> str:
    return f"{v:,.2f}"


def facts_text(result: dict[str, Any]) -> str:
    cur = result.get("currency") or ""
    lines = [
        f"Forecast made on {result['today']} for {len(result['weeks'])} weeks ({cur}).",
        f"Cash today: {_fmt(result['opening'])}",
        f"Open receivables: {_fmt(result['open_receivables'])}; open payables: {_fmt(result['open_payables'])}",
        f"Lowest position: {_fmt(result['lowest']['balance'])} on {result['lowest']['date']}",
        f"Position at the end: {_fmt(result['closing'])} on {result['weeks'][-1]['end']}",
        "Weekly positions:",
    ]
    for w in result["weeks"]:
        lines.append(
            f"- week {w['week']} ({w['start']} to {w['end']}): in {_fmt(w['inflow'])}, out {_fmt(w['outflow'])}, "
            f"position {_fmt(w['position'])}"
        )
    if result["recurring"]:
        lines.append(
            "Recurring movements: "
            + "; ".join(f"{g['label']} {_fmt(g['amount'])} on day {g['day']}" for g in result["recurring"])
        )
    if result["largest_items"]:
        lines.append(
            "Largest expected items: "
            + "; ".join(f"{b['label']} {_fmt(b['amount'])} on {b['date']}" for b in result["largest_items"])
        )
    return "\n".join(lines)


def plain_summary(result: dict[str, Any]) -> str:
    """The narrative written from the facts alone (shown when the model is off or quoted a wrong number)."""
    return _(
        "Cash today is {opening}. The forecast reaches its lowest point, {low}, on {low_date} and ends at "
        "{closing} on {end}. Open receivables: {receivables}; open payables: {payables}.",
        opening=_fmt(result["opening"]),
        low=_fmt(result["lowest"]["balance"]),
        low_date=result["lowest"]["date"],
        closing=_fmt(result["closing"]),
        end=result["weeks"][-1]["end"],
        receivables=_fmt(result["open_receivables"]),
        payables=_fmt(result["open_payables"]),
    )


def narrative_kwargs(result: dict[str, Any], company_id: int) -> dict[str, Any]:
    facts = facts_text(result)
    return {
        "sources": [Source(f"core.company:{company_id}", _("Cash-flow forecast"), facts)],
        "on_output": numbers_guard(facts, plain_summary(result)),
    }


@handler(FEATURE)
def narrative_context(env: Environment, record: Model | None, inputs: dict[str, Any]) -> dict[str, Any]:
    """``/ai/features/account.cashflow_forecast/run``: the narrative of the current forecast."""
    env["account.move"].check_access("read")
    weeks = int(inputs.get("weeks") or 8)
    result = forecast(cash_data(env), weeks)
    company = env.company or env.ref("core.main_company")
    return narrative_kwargs(result, company.ids[0])
