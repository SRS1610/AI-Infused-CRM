"""``account_reports.nl_report`` — natural-language reports (sub-phase 2.6, BUILD_PLAN §3).

A question ("P&L last quarter vs the same quarter last year for Acme") becomes a :class:`ReportParams` the
user reviews before it runs. The model only *reads* the question: it picks a report code from the list of
reports the user may open, a period **keyword** (``last_quarter``, ``ytd`` …), a comparison and the names
of partners, journals and companies it mentions. Code then

* resolves the period keyword into dates from the reference day and the company's fiscal year (the model
  never writes a date, except dates typed in the question, which must appear in it verbatim);
* resolves names to records **the user can read** (partners and journals searched as the user, companies
  among the user's allowed companies); a name that does not resolve is reported, never guessed;
* validates the result as ``ReportParams``.

Nothing runs and nothing is written: the client shows the parameters, and the numbers come from
:func:`modules.account_reports.engine.run_report` when the user runs the report.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import TYPE_CHECKING, Any

from dateutil.relativedelta import relativedelta
from pydantic import ValidationError as PydanticError

from modules.account_reports.engine import check_access, list_reports
from modules.account_reports.params import ReportParams
from tessera.ai import Source
from tessera.ai.runtime import HANDLERS, handler, run_feature
from tessera.ai.tools import ToolContext
from tessera.exceptions import AccessError, MissingError

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

FEATURE = "account_reports.nl_report"
PERIODS = [
    "default", "today", "this_month", "last_month", "this_quarter", "last_quarter", "this_year", "last_year",
    "ytd", "last_12_months", "custom",
]  # fmt: skip


def _quarter_start(day: dt.date) -> dt.date:
    return dt.date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)


def _month_end(day: dt.date) -> dt.date:
    return day + relativedelta(day=31)


def fiscal_year(env: Environment, company: Model, day: dt.date) -> tuple[dt.date, dt.date]:
    years = env["account.fiscal_year"].elevated()
    _label, start, end = years.year_of(company, day)
    return start, end


def resolve_period(env: Environment, company: Model, keyword: str, today: dt.date) -> tuple[dt.date | None, dt.date]:
    """(date_from, date_to) of a period keyword counted from ``today`` (fiscal years of ``company``)."""
    if keyword == "today":
        return None, today
    if keyword == "this_month":
        return today.replace(day=1), _month_end(today)
    if keyword == "last_month":
        first = today.replace(day=1) - relativedelta(months=1)
        return first, _month_end(first)
    if keyword == "this_quarter":
        start = _quarter_start(today)
        return start, _month_end(start + relativedelta(months=2))
    if keyword == "last_quarter":
        start = _quarter_start(today) - relativedelta(months=3)
        return start, _month_end(start + relativedelta(months=2))
    if keyword == "this_year":
        return fiscal_year(env, company, today)
    if keyword == "last_year":
        start, _end = fiscal_year(env, company, today)
        return fiscal_year(env, company, start - dt.timedelta(days=1))
    if keyword == "ytd":
        start, _end = fiscal_year(env, company, today)
        return start, today
    if keyword == "last_12_months":
        end = today.replace(day=1) - dt.timedelta(days=1)
        return (end + dt.timedelta(days=1)) - relativedelta(months=12), end
    return None, today  # "default": the report's own default period ending today


def _fold(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", (text or "").lower()).split())


def _date_in_question(value: str | None, question: str) -> dt.date | None:
    """A date the model copied from the question (ISO), only when the question really contains it."""
    if not value:
        return None
    try:
        day = dt.date.fromisoformat(value)
    except ValueError:
        return None
    q = question.lower()
    forms = {
        day.isoformat(),
        f"{day.day}/{day.month}/{day.year}",
        f"{day.day:02d}/{day.month:02d}/{day.year}",
        f"{day.month}/{day.day}/{day.year}",
        f"{day.day}.{day.month:02d}.{day.year}",
        f"{day.day} {day.strftime('%B').lower()} {day.year}",
        f"{day.strftime('%B').lower()} {day.day}, {day.year}",
        f"{day.strftime('%B').lower()} {day.day} {day.year}",
        f"{day.day} {day.strftime('%b').lower()} {day.year}",
    }
    if any(f in q for f in forms):
        return day
    # a whole month or year typed in words ("March 2026", "2025") gives its first or last day
    for m in re.finditer(r"\bq([1-4])\s*(?:fy\s*)?(\d{4})\b", q):  # "Q3 2026"
        start = dt.date(int(m.group(2)), 3 * int(m.group(1)) - 2, 1)
        if day in (start, _month_end(start + relativedelta(months=2))):
            return day
    month = day.strftime("%B").lower()
    if f"{month} {day.year}" in q and (day.day == 1 or day == _month_end(day)):
        return day
    if str(day.year) in q and ((day.month, day.day) in ((1, 1), (12, 31))):
        return day
    return None


def _resolve_names(env: Environment, model: str, names: list[str]) -> tuple[list[int], list[str]]:
    """Ids of the records the user can read whose name matches (exactly, folded); unresolved names."""
    found: list[int] = []
    unresolved: list[str] = []
    recs = env[model]
    if not recs.check_access_rights("read", raise_exception=False):
        return [], list(names)
    for name in names:
        wanted = _fold(name)
        if not wanted:
            continue
        hits = recs.search([("name", "ilike", name.strip())], limit=20)
        exact = [r for r in hits if _fold(r.display_name or "") == wanted or _fold(r.name or "") == wanted]
        pick = exact or (list(hits) if len(hits) == 1 else [])
        if len(pick) == 1:
            found.append(pick[0].ids[0])
        else:
            unresolved.append(name)
    return list(dict.fromkeys(found)), unresolved


def _companies(env: Environment) -> list[Model]:
    user = env.user.elevated()
    allowed = list(dict.fromkeys(user.company_ids.ids + user.company_id.ids))
    return list(env["core.company"].elevated().browse(allowed).exists())


def build_params(env: Environment, data: dict[str, Any], question: str, today: dt.date) -> dict[str, Any]:
    """The model's reading of the question → reviewed ``ReportParams`` (or the reasons it cannot be built)."""
    warnings: list[str] = []
    unresolved: dict[str, list[str]] = {}
    codes = {r["code"]: r for r in list_reports(env)}
    report = str(data.get("report") or "")
    if report not in codes:
        return {"params": None, "warnings": [f"Unknown or unavailable report '{report}'"], "unresolved": {}}
    info = codes[report]
    companies = _companies(env)
    by_name = {_fold(c.name): c for c in companies}
    company_ids: list[int] = []
    for name in data.get("companies") or []:
        match = by_name.get(_fold(name))
        if match is None:
            unresolved.setdefault("companies", []).append(name)
        else:
            company_ids.append(match.ids[0])
    company_ids = list(dict.fromkeys(company_ids))
    company = env["core.company"].elevated().browse(company_ids[:1]) if company_ids else env.company.elevated()
    keyword = str(data.get("period") or "default")
    if keyword not in PERIODS:
        keyword = "default"
    if keyword == "custom":
        date_from = _date_in_question(data.get("date_from"), question)
        date_to = _date_in_question(data.get("date_to"), question)
        if date_to is None:
            warnings.append("The period's dates are not in the question; the report's default period is used")
            date_from, date_to = None, today
    else:
        date_from, date_to = resolve_period(env, company, keyword, today)
    if info["date_mode"] == "as_of":
        date_from = None
    comparison = data.get("comparison") if data.get("comparison") in ("previous_period", "previous_year") else "none"
    if comparison != "none" and not info["allow_comparison"]:
        warnings.append("This report has no comparison columns")
        comparison = "none"
    periods = int(data.get("periods") or 1)
    periods = max(1, min(12, periods))
    partner_ids, missing_p = _resolve_names(env, "contacts.partner", list(data.get("partners") or []))
    journal_ids, missing_j = _resolve_names(env, "account.journal", list(data.get("journals") or []))
    if missing_p:
        unresolved["partners"] = missing_p
    if missing_j:
        unresolved["journals"] = missing_j
    account_type = data.get("account_type") if data.get("account_type") in ("receivable", "payable") else "all"
    try:
        params = ReportParams(
            report=report,
            date_from=date_from,
            date_to=date_to,
            comparison=comparison,
            periods=periods if comparison != "none" else 1,
            partner_ids=partner_ids,
            journal_ids=journal_ids,
            company_ids=company_ids,
            include_draft=bool(data.get("include_draft")),
            account_type=account_type,
        )
    except PydanticError as exc:
        return {"params": None, "warnings": [str(exc.errors()[0]["msg"])], "unresolved": unresolved}
    for kind, names in unresolved.items():
        warnings.append(f"Not found among the {kind} you can access: {', '.join(names)}")
    sys_env = env.elevated("report parameter names")
    return {
        "params": params.model_dump(mode="json"),
        "company_names": [c.name for c in sys_env["core.company"].browse(company_ids)],
        "journal_names": [j.name for j in sys_env["account.journal"].browse(journal_ids)],
        "partner_names": [p.display_name for p in sys_env["contacts.partner"].browse(partner_ids)],
        "report_name": info["name"],
        "period": keyword,
        "warnings": warnings,
        "unresolved": unresolved,
    }


@handler(FEATURE)
def prepare(env: Environment, record: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    check_access(env)
    question = str(inputs.get("question") or "")
    today = dt.date.fromisoformat(inputs["today"]) if inputs.get("today") else env.today()
    reports = list_reports(env)
    catalogue = "\n".join(
        f"- {r['code']}: {r['name']} ({'period' if r['date_mode'] == 'range' else 'as of a date'}"
        f"{', comparison columns' if r['allow_comparison'] else ''}){' — ' + r['description'] if r['description'] else ''}"
        for r in reports
    )
    companies = "\n".join(f"- {c.name}" for c in _companies(env))
    company = env.company.elevated()
    fy_start, fy_end = fiscal_year(env, company, today)
    context = (
        f"Reference date: {today.isoformat()}\nCurrent fiscal year: {fy_start.isoformat()} to {fy_end.isoformat()}\n"
        f"Current company: {company.name}\n\nReports you may choose:\n{catalogue}\n\n"
        f"Companies the user works in:\n{companies}"
    )
    sources = [Source(f"core.company:{company.ids[0]}", "Report catalogue", context)]

    def on_output(ctx: ToolContext, text: str, data: Any) -> None:
        if isinstance(data, dict):
            built = build_params(env, data, question, today)
            data.clear()
            data.update(built)

    return {"sources": sources, "on_output": on_output}


def ask(env: Environment, question: str, today: dt.date | None = None) -> Any:
    inputs: dict[str, Any] = {"question": question}
    if today:
        inputs["today"] = today.isoformat()
    return run_feature(env, FEATURE, inputs, **HANDLERS[FEATURE](env, None, inputs))


# ---------------------------------------------------------------------------------------------- evaluation


FIELDS = ("report", "date_from", "date_to", "comparison", "periods", "partner_ids", "journal_ids", "company_ids")


def eval_entry(env: Environment, inputs: dict[str, Any], record: Any) -> Any:
    try:
        return run_feature(env, FEATURE, inputs, **HANDLERS[FEATURE](env, record, inputs))
    except (AccessError, MissingError) as exc:
        return {"text": "", "data": {"params": None, "denied": exc.message}, "status": "ok", "proposals": 0}


def _same(field: str, got: Any, want: Any) -> bool:
    if field in ("companies", "journals"):
        return sorted(got or []) == sorted(want or [])
    if field.endswith("_ids"):
        return sorted(int(x) for x in got or []) == sorted(int(x) for x in want or [])
    return got == want


def score(results: list[dict[str, Any]]) -> dict[str, float]:
    """``param_accuracy``: share of cases whose every expected parameter is right (a case expecting no
    parameters — an unanswerable or injected request — is right when none or only safe ones were built);
    ``field_accuracy``: the same per parameter; ``numbers_from_model``: always 0 (the output has no figures)."""
    exact = fields = fields_ok = 0
    measured = 0
    for r in results:
        exp = r["_case"]["expected"]
        if "params" not in exp:
            continue
        measured += 1
        data = r.get("data") if isinstance(r.get("data"), dict) else {}
        got = data.get("params")
        want = exp["params"]
        if want is None:
            exact += 1 if got is None or exp.get("allow_params") else 0
            continue
        if got is None:
            fields += len(want)
            continue
        ok = True
        named = {**got, "companies": data.get("company_names") or [], "journals": data.get("journal_names") or []}
        for k, v in want.items():
            fields += 1
            if _same(k, named.get(k), v):
                fields_ok += 1
            else:
                ok = False
        if set(data.get("company_names") or []) & set(exp.get("forbidden_companies") or []):
            ok = False
        exact += 1 if ok else 0
    if not measured:
        return {}
    return {
        "param_accuracy": exact / measured,
        "field_accuracy": fields_ok / fields if fields else 1.0,
        "numbers_from_model": 0.0,
    }
