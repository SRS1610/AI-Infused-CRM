"""Financial report engine.

Every figure is aggregated in SQL from journal items (posted, or posted and draft) — never by looping over
records — then combined in Python per report line. :func:`run_report` takes a :class:`ReportParams` and
returns a JSON-ready tree::

    {"report": {...}, "columns": [...], "lines": [{"id", "parent_id", "name", "level", "style", "values",
     "domains", "unfoldable", "unfolded", ...}], "currency": {...}, "companies": [...], "warnings": [...]}

``domains[i]`` is the journal-item domain behind ``values[i]`` (drill-down), or ``None``.

Consolidation (several companies, or a report currency other than the company's): each company is computed
in its own currency and converted with the shared exchange-rate table (rates without a company): balance
sheet amounts at the closing rate of the column's end, profit and loss amounts and flows at the average rate
over their period (time-weighted daily average), earlier years' results and equity at the closing rate (the
historical rate is approximated by the closing rate). The difference lands on the lines flagged
``consolidation_only`` (currency translation adjustment) so the balance sheet balances.
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from modules.account_reports.expressions import Expression, Term, parse
from modules.account_reports.params import ReportParams
from tessera.exceptions import AccessError, MissingError, UserError
from tessera.i18n import _
from tessera.orm import Model

PNL_GROUPS = ("income", "expense")
DAY = dt.timedelta(days=1)


# -- dates ------------------------------------------------------------------------------------------------


def month_end(day: dt.date) -> dt.date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def add_months(day: dt.date, months: int) -> dt.date:
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    last = calendar.monthrange(year, month + 1)[1]
    if day == month_end(day):
        return dt.date(year, month + 1, last)
    return dt.date(year, month + 1, min(day.day, last))


def _full_months(start: dt.date, end: dt.date) -> int | None:
    if start.day != 1 or end != month_end(end):
        return None
    return (end.year - start.year) * 12 + end.month - start.month + 1


@dataclass(frozen=True)
class Period:
    date_from: dt.date | None  # None: from the beginning (as-of reports)
    date_to: dt.date
    label: str


def build_periods(params: ReportParams, date_mode: str, default_start: dt.date, today: dt.date) -> list[Period]:
    end = params.date_to or today
    if date_mode == "as_of":
        start = params.date_from
    else:
        start = params.date_from or default_start
        if start > end:
            raise UserError(_("The start date is after the end date"))
    periods = [_period(start, end, date_mode)]
    for n in range(1, params.periods + 1 if params.comparison != "none" else 1):
        if params.comparison == "previous_year":
            s, e = (add_months(start, -12 * n) if start else None), add_months(end, -12 * n)
        elif date_mode == "as_of" and start is None:
            s, e = None, add_months(end, -n)
        else:
            assert start is not None
            months = _full_months(start, end)
            if months:
                s, e = add_months(start, -months * n), month_end(add_months(end, -months * n))
            else:
                days = (end - start).days + 1
                s, e = start - dt.timedelta(days=days * n), end - dt.timedelta(days=days * n)
        periods.append(_period(s, e, date_mode))
    return periods


def _period(start: dt.date | None, end: dt.date, date_mode: str) -> Period:
    if date_mode == "as_of" or start is None:
        return Period(start, end, str(_("As of {date}", date=end.isoformat())))
    return Period(start, end, f"{start.isoformat()} → {end.isoformat()}")


# -- exchange rates ---------------------------------------------------------------------------------------


class Rates:
    """Conversions with the shared rate table (rates without a company): units per one reference unit."""

    def __init__(self, env: Any) -> None:
        self.env = env
        self._series: dict[int, list[tuple[dt.date, float]]] = {}

    def _rates(self, currency_id: int) -> list[tuple[dt.date, float]]:
        if currency_id not in self._series:
            rows = self.env.cr.fetchall(
                "SELECT date, rate FROM core_currency_rate WHERE currency_id = :c AND company_id IS NULL ORDER BY date",
                {"c": currency_id},
            )
            self._series[currency_id] = [(r[0], float(r[1])) for r in rows]
        return self._series[currency_id]

    def rate(self, currency_id: int, day: dt.date) -> float:
        series = self._rates(currency_id)
        if not series:
            return 1.0  # the reference currency
        value = series[0][1]
        for date, rate in series:
            if date > day:
                break
            value = rate
        return value

    def factor(self, from_id: int, to_id: int, day: dt.date) -> float:
        if from_id == to_id:
            return 1.0
        return self.rate(to_id, day) / self.rate(from_id, day)

    def average(self, from_id: int, to_id: int, start: dt.date | None, end: dt.date) -> float:
        """Time-weighted average of the daily factor over [start, end]."""
        if from_id == to_id:
            return 1.0
        if start is None or start >= end:
            return self.factor(from_id, to_id, end)
        changes = sorted({d for cid in (from_id, to_id) for d, _r in self._rates(cid) if start < d <= end} | {start})
        total = 0.0
        for i, seg_start in enumerate(changes):
            seg_end = changes[i + 1] if i + 1 < len(changes) else end + DAY
            total += self.factor(from_id, to_id, seg_start) * (seg_end - seg_start).days
        return total / ((end - start).days + 1)


# -- context ----------------------------------------------------------------------------------------------


@dataclass
class AccountInfo:
    id: int
    code: str
    name: str
    type: str
    group: str
    tags: frozenset[str]


@dataclass
class Company:
    id: int
    name: str
    currency_id: int
    chart: str | None
    exchange_accounts: frozenset[int]
    record: Model


@dataclass
class Context:
    env: Any  # elevated environment for the queries (access checked before)
    params: ReportParams
    report: Model
    companies: list[Company]
    currency: Model
    periods: list[Period] = field(default_factory=list)
    rates: Rates | None = None
    warnings: list[str] = field(default_factory=list)
    _accounts: dict[int, dict[int, AccountInfo]] = field(default_factory=dict)
    _cache: dict[tuple[Any, ...], Any] = field(default_factory=dict)
    _fy: dict[tuple[int, dt.date], dt.date] = field(default_factory=dict)

    @property
    def states(self) -> list[str]:
        return ["posted", "draft"] if self.params.include_draft else ["posted"]

    @property
    def consolidating(self) -> bool:
        return any(c.currency_id != self.currency.ids[0] for c in self.companies)

    @property
    def cids(self) -> list[int]:
        return [c.id for c in self.companies]

    def fy_start(self, company: Company, day: dt.date) -> dt.date:
        key = (company.id, day)
        if key not in self._fy:
            self._fy[key] = self.env["account.fiscal_year"].year_of(company.record, day)[1]
        return self._fy[key]

    def accounts(self, company: Company) -> dict[int, AccountInfo]:
        if company.id not in self._accounts:
            records = (
                self.env["account.account"]
                .with_context(active_test=False)
                .search([("company_id", "=", company.id)], order="code")
            )
            tag_names: dict[int, str] = {}
            english = self.env["account.account_tag"].with_context(lang="en_US", active_test=False)
            for tag in english.search([("applicability", "=", "accounts")]):
                tag_names[tag.ids[0]] = tag.name
            self._accounts[company.id] = {
                acc.ids[0]: AccountInfo(
                    acc.ids[0],
                    acc.code,
                    acc.name,
                    acc.account_type,
                    acc.internal_group,
                    frozenset(tag_names[t] for t in acc.tag_ids.ids if t in tag_names),
                )
                for acc in records
            }
        return self._accounts[company.id]

    # -- conversion

    def convert(self, company: Company, amount: float, *, average: bool, start: dt.date | None, end: dt.date) -> float:
        if company.currency_id == self.currency.ids[0] or not amount:
            return amount
        assert self.rates is not None
        if average:
            return amount * self.rates.average(company.currency_id, self.currency.ids[0], start, end)
        return amount * self.rates.factor(company.currency_id, self.currency.ids[0], end)

    def round(self, amount: float) -> float:
        return float(self.currency.round(amount)) + 0.0  # no negative zero

    # -- SQL helpers

    def filters(self, alias: str = "l") -> tuple[str, dict[str, Any]]:
        sql = [f"{alias}.parent_state = ANY(:states)"]
        args: dict[str, Any] = {"states": self.states}
        if self.params.journal_ids:
            sql.append(f"{alias}.journal_id = ANY(:journal_ids)")
            args["journal_ids"] = list(self.params.journal_ids)
        if self.params.partner_ids:
            sql.append(f"{alias}.partner_id = ANY(:partner_ids)")
            args["partner_ids"] = list(self.params.partner_ids)
        return " AND ".join(sql), args

    def base_domain(self, companies: list[int] | None = None) -> list[Any]:
        domain: list[Any] = [
            ["company_id", "in", companies or self.cids],
            ["parent_state", "in", self.states],
        ]
        if self.params.journal_ids:
            domain.append(["journal_id", "in", list(self.params.journal_ids)])
        if self.params.partner_ids:
            domain.append(["partner_id", "in", list(self.params.partner_ids)])
        return domain

    @staticmethod
    def date_domain(start: dt.date | None, end: dt.date | None) -> list[Any]:
        out: list[Any] = []
        if start:
            out.append(["date", ">=", start.isoformat()])
        if end:
            out.append(["date", "<=", end.isoformat()])
        return out

    # -- aggregates (cached per company and date range)

    def balances(self, company: Company, start: dt.date | None, end: dt.date | None) -> dict[int, float]:
        """Balance per account of the items dated in [start, end] (open bounds when None)."""
        key = ("bal", company.id, start, end)
        if key not in self._cache:
            where, args = self.filters()
            conds = [where, "l.company_id = :cid"]
            args["cid"] = company.id
            if start:
                conds.append("l.date >= :start")
                args["start"] = start
            if end:
                conds.append("l.date <= :end")
                args["end"] = end
            rows = self.env.cr.fetchall(
                f"SELECT l.account_id, SUM(l.balance) FROM account_move_line l WHERE {' AND '.join(conds)} "  # noqa: S608
                "GROUP BY l.account_id",
                args,
            )
            self._cache[key] = {int(r[0]): float(r[1] or 0) for r in rows if r[0] is not None}
        return self._cache[key]  # type: ignore[no-any-return]

    def cash_flows(self, company: Company, start: dt.date | None, end: dt.date | None) -> dict[int, float]:
        """Per non-cash account: minus the balance of its items in entries that touch a bank / cash account."""
        key = ("cash", company.id, start, end)
        if key not in self._cache:
            where, args = self.filters()
            args["cid"] = company.id
            dates = ""
            if start:
                dates += " AND l.date >= :start"
                args["start"] = start
            if end:
                dates += " AND l.date <= :end"
                args["end"] = end
            rows = self.env.cr.fetchall(
                f"""
                SELECT l.account_id, -SUM(l.balance)
                  FROM account_move_line l
                  JOIN account_account a ON a.id = l.account_id
                 WHERE {where} AND l.company_id = :cid{dates} AND a.account_type <> 'asset_cash'
                   AND l.move_id IN (
                       SELECT c.move_id FROM account_move_line c JOIN account_account ca ON ca.id = c.account_id
                        WHERE ca.account_type = 'asset_cash' AND c.company_id = :cid
                          AND c.parent_state = ANY(:states){dates.replace("l.", "c.")})
                 GROUP BY l.account_id
                """,  # noqa: S608
                args,
            )
            self._cache[key] = {int(r[0]): float(r[1] or 0) for r in rows}
        return self._cache[key]  # type: ignore[no-any-return]

    def grids(self, company: Company, start: dt.date | None, end: dt.date | None) -> dict[str, tuple[float, set[int]]]:
        """Tax-grid amounts: per grid name (without its +/- sign), the signed amount and the tag ids.

        An item's amount is its balance in the document's direction (positive for the lines of an invoice,
        negative for those of a credit note), negated for ``-NN`` tags (``tax_negate``)."""
        key = ("grid", company.id, start, end)
        if key not in self._cache:
            where, args = self.filters()
            args["cid"] = company.id
            dates = ""
            if start:
                dates += " AND l.date >= :start"
                args["start"] = start
            if end:
                dates += " AND l.date <= :end"
                args["end"] = end
            rows = self.env.cr.fetchall(
                f"""
                SELECT t.id, t.name->>'en_US', bool_or(t.tax_negate),
                       SUM(l.balance
                           * CASE WHEN l.move_type IN ('out_invoice', 'in_refund') THEN -1
                                  WHEN l.move_type IN ('in_invoice', 'out_refund') THEN 1
                                  WHEN COALESCE(lt.type_tax_use, bt.type_tax_use) = 'sale' THEN -1
                                  ELSE 1 END
                           * CASE WHEN t.tax_negate THEN -1 ELSE 1 END)
                  FROM account_move_line l
                  JOIN account_move_line_tag_rel r ON r.line_id = l.id
                  JOIN account_account_tag t ON t.id = r.tag_id
                  LEFT JOIN account_tax lt ON lt.id = l.tax_line_id
                  LEFT JOIN LATERAL (
                       SELECT x.type_tax_use FROM account_move_line_tax_rel tr JOIN account_tax x ON x.id = tr.tax_id
                        WHERE tr.line_id = l.id LIMIT 1) bt ON true
                 WHERE {where} AND l.company_id = :cid{dates}
                 GROUP BY t.id
                """,  # noqa: S608
                args,
            )
            out: dict[str, tuple[float, set[int]]] = {}
            for tag_id, name, _negate, amount in rows:
                grid = (name or "").lstrip("+-")
                total, ids = out.get(grid, (0.0, set()))
                out[grid] = (total + float(amount or 0), ids | {int(tag_id)})
            self._cache[key] = out
        return self._cache[key]  # type: ignore[no-any-return]


# -- entry point ------------------------------------------------------------------------------------------


def check_access(env: Any) -> None:
    if env.su or env.is_system:
        return
    if not env.user.has_group("account.group_account_readonly"):
        raise AccessError(_("Financial reports need the Accounting / Read-only rights"))
    env["account.move_line"].check_access_rights("read")


def resolve_companies(env: Any, company_ids: list[int]) -> list[Model]:
    requested = list(dict.fromkeys(company_ids)) or (env.company.ids[:1] if env.company else [])
    if not requested:
        raise UserError(_("Choose a company"))
    if not env.su:
        user = env.user.elevated()
        allowed = set(user.company_ids.ids) | set(user.company_id.ids)
        if set(requested) - allowed:
            raise AccessError(_("You do not have access to every company of this report"))
    companies = env["core.company"].elevated().browse(requested).exists()
    if len(companies) != len(requested):
        raise MissingError(_("Unknown company"))
    return [companies.browse([cid]) for cid in requested]


def resolve_report(env: Any, code: str, company: Model) -> Model:
    reports = env["account.report"].elevated()
    variants = reports.search([("variant_of", "=", code)])
    if variants:
        chart = company.elevated().chart_template
        match = variants.filtered(lambda r: r.chart_template == chart)
        if not match:
            raise MissingError(
                _("No '{code}' report for the '{chart}' chart of accounts", code=code, chart=chart or "-")
            )
        return match[:1]  # type: ignore[no-any-return]
    report = reports.search([("code", "=", code)], limit=1)
    if not report:
        raise MissingError(_("Unknown report '{code}'", code=code))
    return report  # type: ignore[no-any-return]


def make_context(env: Any, params: ReportParams) -> Context:
    check_access(env)
    company_records = resolve_companies(env, list(params.company_ids))
    report = resolve_report(env, params.report, company_records[0])
    sudo = env.elevated("financial reports")
    currencies = sudo["core.currency"]
    if params.currency:
        currency = currencies.with_context(active_test=False).search([("name", "=", params.currency)], limit=1)
        if not currency:
            raise MissingError(_("Unknown currency '{code}'", code=params.currency))
    else:
        currency = company_records[0].elevated().currency_id
    companies = []
    for rec in company_records:
        rec = rec.elevated()
        exchange = set(
            rec.income_currency_exchange_account_id.ids[:1] + rec.expense_currency_exchange_account_id.ids[:1]
        )
        companies.append(
            Company(rec.ids[0], rec.name, rec.currency_id.ids[0], rec.chart_template, frozenset(exchange), rec)
        )
    ctx = Context(sudo, params, report, companies, currency)
    ctx.rates = Rates(sudo)
    today = env.today()
    end = params.date_to or today
    default_start = ctx.fy_start(companies[0], end) if report.date_mode == "range" else end
    comparison_ok = report.allow_comparison and report.engine in ("lines", "trial_balance")
    if params.comparison != "none" and not comparison_ok:
        ctx.warnings.append(str(_("This report has no comparison columns")))
        params = params.model_copy(update={"comparison": "none"})
        ctx.params = params
    ctx.periods = build_periods(params, report.date_mode, default_start, today)
    return ctx


def run_report(env: Any, params: ReportParams | dict[str, Any]) -> dict[str, Any]:
    """Run a report with these parameters as the current user (accounting read-only rights needed)."""
    if not isinstance(params, ReportParams):
        params = ReportParams.model_validate(params)
    ctx = make_context(env, params)
    from modules.account_reports import ledgers

    handlers = {
        "lines": lines_report,
        "general_ledger": ledgers.general_ledger,
        "partner_ledger": ledgers.partner_ledger,
        "trial_balance": ledgers.trial_balance,
        "aged_receivable": ledgers.aged_receivable,
        "aged_payable": ledgers.aged_payable,
    }
    columns, lines = handlers[ctx.report.engine](ctx)
    return {
        "report": {
            "code": ctx.report.code,
            "requested": params.report,
            "name": ctx.report.name,
            "engine": ctx.report.engine,
            "date_mode": ctx.report.date_mode,
            "allow_comparison": bool(ctx.report.allow_comparison) and ctx.report.engine in ("lines", "trial_balance"),
        },
        "params": ctx.params.model_dump(mode="json"),
        "periods": [
            {"date_from": p.date_from.isoformat() if p.date_from else None, "date_to": p.date_to.isoformat()}
            for p in ctx.periods
        ],
        "currency": {
            "name": ctx.currency.name,
            "symbol": ctx.currency.symbol,
            "decimal_places": int(ctx.currency.decimal_places or 0),
        },
        "companies": [{"id": c.id, "name": c.name} for c in ctx.companies],
        "consolidated": len(ctx.companies) > 1 or ctx.consolidating,
        "columns": columns,
        "lines": lines,
        "warnings": ctx.warnings,
    }


def list_reports(env: Any, company: Model | None = None) -> list[dict[str, Any]]:
    """The reports available to the current user (for menus and the 2.6 natural-language feature)."""
    check_access(env)
    company = company or env.company
    chart = company.elevated().chart_template if company else None
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for report in env["account.report"].elevated().search([]):
        if report.variant_of:
            if report.chart_template != chart or report.variant_of in seen:
                continue
            code = report.variant_of
        else:
            code = report.code
        seen.add(code)
        out.append(
            {
                "code": code,
                "name": report.name,
                "engine": report.engine,
                "date_mode": report.date_mode,
                "allow_comparison": bool(report.allow_comparison) and report.engine in ("lines", "trial_balance"),
                "description": report.description or "",
            }
        )
    return out


# -- line reports -----------------------------------------------------------------------------------------


@dataclass
class _TermValue:
    total: float = 0.0
    by_account: dict[str, float] = field(default_factory=dict)  # account code -> converted amount
    names: dict[str, str] = field(default_factory=dict)
    domains: list[Any] = field(default_factory=list)  # OR-ed drill-down domains
    accounts: dict[str, list[int]] = field(default_factory=dict)  # account code -> ids (all companies)


def _scope_range(
    ctx: Context, company: Company, period: Period, scope: str
) -> tuple[dt.date | None, dt.date | None] | None:
    start = period.date_from
    end = period.date_to
    if scope == "period":
        return (start or ctx.fy_start(company, end), end)
    if scope == "as_of":
        return (None, end)
    if scope == "opening":
        return (None, start - DAY) if start else None
    fy = ctx.fy_start(company, end)
    if scope == "fy_to_date":
        return (fy, end)
    if scope == "before_fy":
        return (None, fy - DAY)
    raise UserError(_("Unknown date scope '{scope}'", scope=scope))


def _matches(term: Term, acc: AccountInfo, company: Company) -> bool:
    if term.func == "type":
        return acc.type in term.args
    if term.func == "code":
        return any(acc.code.startswith(p) for p in term.args)
    if term.func == "tag":
        return bool(acc.tags & set(term.args))
    if term.func == "cash":
        if acc.id in company.exchange_accounts:
            return "exchange" in term.args
        return acc.type in term.args
    return False


def evaluate_term(ctx: Context, term: Term, scope: str, period: Period) -> _TermValue:
    out = _TermValue()
    for company in ctx.companies:
        rng = _scope_range(ctx, company, period, term.scope or scope)
        if rng is None:
            continue
        start, end = rng
        if end is not None and start is not None and end < start:
            continue
        effective_scope = term.scope or scope
        if term.func == "grid":
            grids = ctx.grids(company, start, end)
            tag_ids: set[int] = set()
            for name in term.args:
                amount, ids = grids.get(name, (0.0, set()))
                tag_ids |= ids
                out.total += term.sign * ctx.convert(
                    company, amount, average=True, start=start, end=end or period.date_to
                )
            if tag_ids:
                out.domains.append(
                    [
                        *ctx.base_domain([company.id]),
                        *ctx.date_domain(start, end),
                        ["tax_tag_ids", "in", sorted(tag_ids)],
                    ]
                )
            continue
        accounts = ctx.accounts(company)
        values = ctx.cash_flows(company, start, end) if term.func == "cash" else ctx.balances(company, start, end)
        matched: list[int] = []
        for acc_id, amount in values.items():
            acc = accounts.get(acc_id)
            if acc is None or not _matches(term, acc, company):
                continue
            matched.append(acc_id)
            average = term.func == "cash" or (acc.group in PNL_GROUPS and effective_scope in ("period", "fy_to_date"))
            value = term.sign * ctx.convert(company, amount, average=average, start=start, end=end or period.date_to)
            out.total += value
            out.by_account[acc.code] = out.by_account.get(acc.code, 0.0) + value
            out.names.setdefault(acc.code, f"{acc.code} {acc.name}")
            out.accounts.setdefault(acc.code, []).append(acc_id)
        all_matching = [a.id for a in accounts.values() if _matches(term, a, company)]
        if all_matching:
            leaf: list[Any] = [
                *ctx.base_domain([company.id]),
                *ctx.date_domain(start, end),
                ["account_id", "in", all_matching],
            ]
            if term.func == "cash":
                leaf.append(["move_id.line_ids.account_id.account_type", "=", "asset_cash"])
            out.domains.append(leaf)
    return out


@dataclass
class _LineState:
    record: Model
    expr: Expression
    children: list[_LineState] = field(default_factory=list)
    parent: _LineState | None = None
    level: int = 0


def _line_tree(report: Model) -> list[_LineState]:
    states = {line.ids[0]: _LineState(line, parse(line.expression)) for line in report.line_ids}
    roots: list[_LineState] = []
    for state in sorted(states.values(), key=lambda s: (s.record.sequence or 0, s.record.ids[0])):
        parent_id = state.record.parent_id.ids[0] if state.record.parent_id else None
        if parent_id and parent_id in states:
            state.parent = states[parent_id]
            states[parent_id].children.append(state)
        else:
            roots.append(state)

    def depth(state: _LineState, level: int) -> None:
        state.level = level
        for child in state.children:
            depth(child, level + 1)

    for root in roots:
        depth(root, 0)
    return roots


def _walk(states: list[_LineState]) -> list[_LineState]:
    out: list[_LineState] = []
    for state in states:
        out.append(state)
        out.extend(_walk(state.children))
    return out


@dataclass
class _Column:
    """Evaluation of every definition line for one column (memoised, cycles refused)."""

    ctx: Context
    period: Period
    by_code: dict[str, _LineState]
    values: dict[int, float] = field(default_factory=dict)
    detail: dict[int, _TermValue] = field(default_factory=dict)
    visiting: set[int] = field(default_factory=set)

    def value_of(self, state: _LineState) -> float:
        lid = state.record.ids[0]
        if lid in self.values:
            return self.values[lid]
        if lid in self.visiting:
            raise UserError(_("Line '{line}' depends on itself", line=state.record.name))
        self.visiting.add(lid)
        sign = int(state.record.sign or "1")
        total = 0.0
        merged = _TermValue()
        for term in state.expr.terms:
            if term.func == "children":
                total += term.sign * sum(self.value_of(c) for c in state.children)
            elif term.func == "line":
                ref = self.by_code.get(term.args[0])
                if ref is None:
                    raise UserError(_("Unknown line code '{code}'", code=term.args[0]))
                total += term.sign * self.value_of(ref)
            else:
                tv = evaluate_term(self.ctx, term, state.record.date_scope or "period", self.period)
                total += tv.total
                for code, amount in tv.by_account.items():
                    merged.by_account[code] = merged.by_account.get(code, 0.0) + amount
                merged.names.update(tv.names)
                for code, ids in tv.accounts.items():
                    merged.accounts.setdefault(code, []).extend(ids)
                merged.domains.extend(tv.domains)
        if state.record.consolidation_only and not self.ctx.consolidating:
            total = 0.0
            merged = _TermValue()
        merged.total = sign * total
        merged.by_account = {k: sign * v for k, v in merged.by_account.items()}
        self.values[lid] = sign * total
        self.detail[lid] = merged
        self.visiting.discard(lid)
        return self.values[lid]


def compute_lines(ctx: Context) -> tuple[list[_LineState], list[dict[int, float]], list[dict[int, _TermValue]]]:
    """Values of every definition line per column: ([tree], [{line id: value}], [{line id: account detail}])."""
    roots = _line_tree(ctx.report)
    ordered = _walk(roots)
    by_code = {s.record.code: s for s in ordered if s.record.code}
    results: list[dict[int, float]] = []
    details: list[dict[int, _TermValue]] = []
    for period in ctx.periods:
        column = _Column(ctx, period, by_code)
        for state in ordered:
            column.value_of(state)
        results.append(column.values)
        details.append(column.detail)
    return roots, results, details


def _or(domains: list[Any]) -> Any:
    if not domains:
        return None
    if len(domains) == 1:
        return domains[0]
    return {"any": [{"all": d} for d in domains]}


def lines_report(ctx: Context) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    roots, results, details = compute_lines(ctx)
    columns = [
        {
            "key": f"p{i}",
            "label": p.label,
            "type": "monetary",
            "date_from": p.date_from.isoformat() if p.date_from else None,
            "date_to": p.date_to.isoformat(),
        }
        for i, p in enumerate(ctx.periods)
    ]
    unfold = set(ctx.params.unfold)
    rows: list[dict[str, Any]] = []
    for state in _walk(roots):
        rec = state.record
        lid = rec.ids[0]
        row_id = f"line:{lid}"
        vals = [ctx.round(r[lid]) for r in results]
        if (rec.hide_if_zero or rec.consolidation_only) and not any(vals):
            continue
        if state.parent is not None and _hidden_parent(state, results):
            continue
        unfoldable = bool(rec.groupby_account) and state.expr.account_based
        unfolded = unfoldable and (ctx.params.unfold_all or row_id in unfold)
        rows.append(
            {
                "id": row_id,
                "parent_id": f"line:{state.parent.record.ids[0]}" if state.parent else None,
                "code": rec.code,
                "name": rec.name,
                "level": state.level,
                "style": rec.style or "normal",
                "values": vals,
                "domains": [_or(d[lid].domains) for d in details],
                "unfoldable": unfoldable,
                "unfolded": unfolded,
            }
        )
        if unfolded:
            codes = sorted({code for d in details for code in d[lid].by_account})
            for code in codes:
                acc_vals = [ctx.round(d[lid].by_account.get(code, 0.0)) for d in details]
                if ctx.params.hide_zero and not any(acc_vals):
                    continue
                name = next(d[lid].names[code] for d in details if code in d[lid].names)
                rows.append(
                    {
                        "id": f"{row_id}:acc:{code}",
                        "parent_id": row_id,
                        "code": None,
                        "name": name,
                        "level": state.level + 1,
                        "style": "account",
                        "values": acc_vals,
                        "domains": [_account_domain(d[lid], code) for d in details],
                        "unfoldable": False,
                        "unfolded": False,
                    }
                )
    return columns, rows


def _hidden_parent(state: _LineState, results: list[dict[int, float]]) -> bool:
    parent = state.parent
    while parent is not None:
        rec = parent.record
        if (rec.hide_if_zero or rec.consolidation_only) and not any(abs(r[rec.ids[0]]) >= 0.005 for r in results):
            return True
        parent = parent.parent
    return False


def _account_domain(detail: _TermValue, code: str) -> Any:
    ids = set(detail.accounts.get(code, []))
    if not ids:
        return None
    narrowed = []
    for dom in detail.domains:
        leaf = [x for x in dom if not (isinstance(x, list) and x[:2] == ["account_id", "in"])]
        accounts = next((x[2] for x in dom if isinstance(x, list) and x[:2] == ["account_id", "in"]), None)
        if accounts is None:
            continue
        keep = sorted(ids & set(accounts))
        if keep:
            narrowed.append([*leaf, ["account_id", "in", keep]])
    return _or(narrowed)
