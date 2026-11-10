"""Built-in ledger reports: trial balance, general ledger, partner ledger, aged receivable / payable.

Opening balances: balance-sheet accounts carry everything dated before the period; profit and loss accounts
start again at the beginning of the fiscal year of the period's start, and the results of earlier years
appear on a synthetic *Undistributed earnings of previous years* line (there is no closing entry, D-077), so
the trial balance sums to zero.

Consolidated ledgers (several currencies): the trial balance converts balance-sheet amounts at the closing
rate (opening: the day before the period, closing: its last day), movements and profit and loss amounts at
the average rate, and puts the difference on a *Currency translation adjustment* line so every column sums
to zero. The general and partner ledgers and the aged balances convert every amount at the closing rate of
the report date (an approximation that keeps running balances consistent).
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

from modules.account_reports.engine import DAY, PNL_GROUPS, Company, Context
from tessera.i18n import _

RECEIVABLE = ("asset_receivable",)
PAYABLE = ("liability_payable",)


def _money(label: str, key: str, **extra: Any) -> dict[str, Any]:
    return {"key": key, "label": label, "type": "monetary", **extra}


def _sums(
    ctx: Context, company: Company, start: dt.date, end: dt.date, *, group: str = "account", types: tuple[str, ...] = ()
) -> dict[Any, tuple[float, float, float, float, int]]:
    """Per account (or partner): balance before ``start``, balance before the fiscal year of ``start``,
    debit and credit in the period and the number of items in the period."""
    where, args = ctx.filters()
    fy = ctx.fy_start(company, start)
    args.update({"cid": company.id, "start": start, "end": end, "fy": fy})
    key = "l.account_id" if group == "account" else "l.partner_id"
    join = ""
    if types:
        join = "JOIN account_account a ON a.id = l.account_id AND a.account_type = ANY(:types)"
        args["types"] = list(types)
    rows = ctx.env.cr.fetchall(
        f"""
        SELECT {key},
               COALESCE(SUM(l.balance) FILTER (WHERE l.date < :start), 0),
               COALESCE(SUM(l.balance) FILTER (WHERE l.date < :fy), 0),
               COALESCE(SUM(l.debit) FILTER (WHERE l.date >= :start), 0),
               COALESCE(SUM(l.credit) FILTER (WHERE l.date >= :start), 0),
               COUNT(*) FILTER (WHERE l.date >= :start)
          FROM account_move_line l {join}
         WHERE {where} AND l.company_id = :cid AND l.date <= :end
         GROUP BY {key}
        """,  # noqa: S608
        args,
    )
    return {r[0]: (float(r[1]), float(r[2]), float(r[3]), float(r[4]), int(r[5])) for r in rows}


# -- trial balance ----------------------------------------------------------------------------------------


def trial_balance(ctx: Context) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    keys = ("opening", "debit", "credit", "closing")
    labels = {
        "opening": str(_("Opening balance")),
        "debit": str(_("Debit")),
        "credit": str(_("Credit")),
        "closing": str(_("Closing balance")),
    }
    columns = [
        _money(
            labels[k],
            f"p{i}_{k}",
            group=p.label,
            date_from=p.date_from.isoformat() if p.date_from else None,
            date_to=p.date_to.isoformat(),
        )
        for i, p in enumerate(ctx.periods)
        for k in keys
    ]
    width = len(columns)
    rows: dict[str, dict[str, Any]] = {}
    undistributed = [0.0] * width
    undistributed_domains: list[list[Any]] = [[] for _ in range(width)]
    for i, period in enumerate(ctx.periods):
        start = period.date_from or ctx.fy_start(ctx.companies[0], period.date_to)
        end = period.date_to
        base = i * 4
        for company in ctx.companies:
            fy = ctx.fy_start(company, start)
            accounts = ctx.accounts(company)
            for acc_id, (before_start, before_fy, debit, credit, _count) in _sums(ctx, company, start, end).items():
                acc = accounts.get(acc_id)
                if acc is None:
                    continue
                pnl = acc.group in PNL_GROUPS
                opening = before_start - (before_fy if pnl else 0.0)
                closing = opening + debit - credit
                if ctx.consolidating:
                    if pnl:
                        opening = ctx.convert(company, opening, average=True, start=fy, end=start - DAY)
                        closing = ctx.convert(company, closing, average=True, start=fy, end=end)
                    else:
                        opening = ctx.convert(company, opening, average=False, start=None, end=start - DAY)
                        closing = ctx.convert(company, closing, average=False, start=None, end=end)
                    debit = ctx.convert(company, debit, average=True, start=start, end=end)
                    credit = ctx.convert(company, credit, average=True, start=start, end=end)
                    if pnl and before_fy:
                        prior = ctx.convert(company, before_fy, average=False, start=None, end=start - DAY)
                        undistributed[base] += prior
                        undistributed[base + 3] += ctx.convert(company, before_fy, average=False, start=None, end=end)
                elif pnl:
                    undistributed[base] += before_fy
                    undistributed[base + 3] += before_fy
                row = rows.setdefault(
                    acc.code,
                    {"name": f"{acc.code} {acc.name}", "values": [0.0] * width, "domains": [[] for _ in range(width)]},
                )
                for k, value in zip(keys, (opening, debit, credit, closing), strict=True):
                    row["values"][base + keys.index(k)] += value
                common = [*ctx.base_domain([company.id]), ["account_id", "=", acc_id]]
                opening_start = fy if pnl else None
                row["domains"][base].append([*common, *ctx.date_domain(opening_start, start - DAY)])
                for offset in (1, 2):
                    row["domains"][base + offset].append([*common, *ctx.date_domain(start, end)])
                row["domains"][base + 3].append([*common, *ctx.date_domain(opening_start, end)])
                if pnl and before_fy:
                    undistributed_domains[base].append([*common, *ctx.date_domain(None, fy - DAY)])
    out: list[dict[str, Any]] = []
    totals = [0.0] * width
    for code in sorted(rows):
        row = rows[code]
        values = [ctx.round(v) for v in row["values"]]
        if ctx.params.hide_zero and not any(values):
            continue
        totals = [t + v for t, v in zip(totals, values, strict=True)]
        out.append(_row(f"acc:{code}", row["name"], values, domains=[_or(d) for d in row["domains"]], style="account"))
    values = [ctx.round(v) for v in undistributed]
    if any(values):
        totals = [t + v for t, v in zip(totals, values, strict=True)]
        out.append(
            _row(
                "undistributed",
                str(_("Undistributed earnings of previous years")),
                values,
                domains=[_or(d) for d in undistributed_domains],
            )
        )
    if ctx.consolidating:
        cta = [0.0] * width
        for i in range(len(ctx.periods)):
            base = i * 4
            cta[base] = -totals[base]
            cta[base + 3] = -totals[base + 3]
            diff = totals[base + 1] - totals[base + 2]
            if diff > 0:
                cta[base + 2] = diff
            else:
                cta[base + 1] = -diff
        cta = [ctx.round(v) for v in cta]
        if any(cta):
            totals = [t + v for t, v in zip(totals, cta, strict=True)]
            out.append(_row("cta", str(_("Currency translation adjustment")), cta))
    out.append(_row("total", str(_("Total")), [ctx.round(t) for t in totals], style="total"))
    return columns, out


# -- general and partner ledgers ---------------------------------------------------------------------------


def general_ledger(ctx: Context) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return _ledger(ctx, "account", ())


def partner_ledger(ctx: Context) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    types = {"receivable": RECEIVABLE, "payable": PAYABLE}.get(ctx.params.account_type, RECEIVABLE + PAYABLE)
    return _ledger(ctx, "partner", types)


def _factors(ctx: Context, day: dt.date) -> dict[int, float]:
    assert ctx.rates is not None
    return {c.id: ctx.rates.factor(c.currency_id, ctx.currency.ids[0], day) for c in ctx.companies}


def _factor_sql(factors: dict[int, float]) -> str:
    if all(f == 1.0 for f in factors.values()):
        return "1"
    cases = " ".join(f"WHEN {int(cid)} THEN {float(f)!r}" for cid, f in factors.items())
    return f"(CASE l.company_id {cases} ELSE 1 END)"


def _partner_names(ctx: Context, ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    table = ctx.env["contacts.partner"]._table
    rows = ctx.env.cr.fetchall(f'SELECT id, name FROM "{table}" WHERE id = ANY(:ids)', {"ids": ids})  # noqa: S608
    return {int(r[0]): r[1] for r in rows}


def _ledger(ctx: Context, group: str, types: tuple[str, ...]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    period = ctx.periods[0]
    start = period.date_from or ctx.fy_start(ctx.companies[0], period.date_to)
    end = period.date_to
    third = str(_("Partner")) if group == "account" else str(_("Account"))
    columns = [
        {"key": "date", "label": str(_("Date")), "type": "date"},
        {"key": "entry", "label": str(_("Entry")), "type": "text"},
        {"key": "third", "label": third, "type": "text"},
        {"key": "label", "label": str(_("Label")), "type": "text"},
        _money(str(_("Debit")), "debit"),
        _money(str(_("Credit")), "credit"),
        _money(str(_("Balance")), "balance"),
    ]
    factors = _factors(ctx, end)
    groups: dict[str, dict[str, Any]] = {}
    for company in ctx.companies:
        factor = factors[company.id]
        accounts = ctx.accounts(company)
        for key, (before_start, before_fy, debit, credit, count) in _sums(
            ctx, company, start, end, group=group, types=types
        ).items():
            if group == "account":
                acc = accounts.get(key)
                if acc is None:
                    continue
                pnl = acc.group in PNL_GROUPS
                opening = before_start - (before_fy if pnl else 0.0)
                gkey, name = acc.code, f"{acc.code} {acc.name}"
                domain = [*ctx.base_domain([company.id]), ["account_id", "=", key], *ctx.date_domain(start, end)]
            else:
                opening = before_start
                gkey, name = str(key or 0), ""
                domain = [
                    *ctx.base_domain([company.id]),
                    ["partner_id", "=", key] if key else ["partner_id", "is_not_set"],
                    ["account_id.account_type", "in", list(types)],
                    *ctx.date_domain(start, end),
                ]
            grp = groups.setdefault(
                gkey,
                {"name": name, "key": key, "opening": 0.0, "debit": 0.0, "credit": 0.0, "count": 0, "domains": []},
            )
            grp["opening"] += opening * factor
            grp["debit"] += debit * factor
            grp["credit"] += credit * factor
            grp["count"] += count
            grp["domains"].append(domain)
    if group == "partner":
        names = _partner_names(ctx, [int(g["key"]) for g in groups.values() if g["key"]])
        for grp in groups.values():
            grp["name"] = names.get(int(grp["key"] or 0)) or str(_("Unknown partner"))
    order = sorted(groups, key=lambda k: (groups[k]["name"].lower(), k)) if group == "partner" else sorted(groups)
    unfold = set(ctx.params.unfold)
    prefix = "acc" if group == "account" else "partner"
    unfolded = [k for k in order if ctx.params.unfold_all or f"{prefix}:{k}" in unfold]
    if ctx.params.expand:
        wanted = ctx.params.expand.split(":", 1)[-1]
        unfolded = [wanted] if wanted in groups else []
    items = _items(ctx, group, types, unfolded, start, end, factors) if unfolded else {}
    out: list[dict[str, Any]] = []
    total_debit = total_credit = total_balance = 0.0
    for k in order:
        grp = groups[k]
        closing = grp["opening"] + grp["debit"] - grp["credit"]
        vals = [ctx.round(grp["debit"]), ctx.round(grp["credit"]), ctx.round(closing)]
        if ctx.params.hide_zero and not any(vals) and not ctx.round(grp["opening"]):
            continue
        total_debit += vals[0]
        total_credit += vals[1]
        total_balance += vals[2]
        row_id = f"{prefix}:{k}"
        is_open = k in unfolded
        if ctx.params.expand:
            if is_open:
                out.extend(_item_rows(ctx, row_id, grp, items.get(k, []), group))
            continue
        out.append(
            {
                **_row(row_id, grp["name"], [None, None, None, None, *vals], style="account"),
                "domains": [None] * 4 + [_or(grp["domains"])] * 3,
                "unfoldable": grp["count"] > 0 or bool(ctx.round(grp["opening"])),
                "unfolded": is_open,
            }
        )
        if is_open:
            out.extend(_item_rows(ctx, row_id, grp, items.get(k, []), group))
    if not ctx.params.expand:
        out.append(
            _row(
                "total",
                str(_("Total")),
                [None, None, None, None, ctx.round(total_debit), ctx.round(total_credit), ctx.round(total_balance)],
                style="total",
            )
        )
    return columns, out


def _items(
    ctx: Context,
    group: str,
    types: tuple[str, ...],
    keys: list[str],
    start: dt.date,
    end: dt.date,
    factors: dict[int, float],
) -> dict[str, list[tuple[Any, ...]]]:
    where, args = ctx.filters()
    key_expr = "a.code" if group == "account" else "COALESCE(l.partner_id, 0)::text"
    extra = ""
    if types:
        extra = " AND a.account_type = ANY(:types)"
        args["types"] = list(types)
    offset = ctx.params.offset if ctx.params.expand else 0
    args.update(
        {
            "cids": ctx.cids,
            "start": start,
            "end": end,
            "keys": keys,
            "offset": offset,
            "stop": offset + ctx.params.limit,
        }
    )
    factor = _factor_sql(factors)
    rows = ctx.env.cr.fetchall(
        f"""
        SELECT gkey, id, date, move_id, move_name, name, partner_id, code, debit, credit, cumul, rn, total
          FROM (SELECT {key_expr} AS gkey, l.id, l.date, l.move_id, l.move_name, l.name, l.partner_id, a.code,
                       l.debit * {factor} AS debit, l.credit * {factor} AS credit,
                       ROW_NUMBER() OVER w AS rn,
                       SUM(l.balance * {factor}) OVER w AS cumul,
                       COUNT(*) OVER (PARTITION BY {key_expr}) AS total
                  FROM account_move_line l
                  JOIN account_account a ON a.id = l.account_id
                 WHERE {where} AND l.company_id = ANY(:cids) AND l.date >= :start AND l.date <= :end
                   AND {key_expr} = ANY(:keys){extra}
                WINDOW w AS (PARTITION BY {key_expr} ORDER BY l.date, l.move_name, l.id)
                OFFSET 0) s
         WHERE rn > :offset AND rn <= :stop
         ORDER BY gkey, rn
        """,  # noqa: S608
        args,
    )
    # the window's ordering is unique, so its default frame is the running total; OFFSET 0 keeps PostgreSQL 16
    # from turning the outer row-number filter into a run condition (it then mishandles the second window)
    out: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
    for row in rows:
        out[row[0]].append(row[1:])
    return out


def _item_rows(
    ctx: Context, row_id: str, grp: dict[str, Any], items: list[tuple[Any, ...]], group: str
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    offset = ctx.params.offset if ctx.params.expand else 0
    if offset == 0:
        out.append(
            _row(
                f"{row_id}:initial",
                str(_("Initial balance")),
                [None, None, None, None, None, None, ctx.round(grp["opening"])],
                parent_id=row_id,
                level=1,
                style="initial",
            )
        )
    partners = _partner_names(ctx, sorted({int(i[5]) for i in items if i[5]})) if group == "account" else {}
    total = 0
    for line_id, date, move_id, move_name, label, partner_id, code, debit, credit, cumul, rn, count in items:
        total = int(count)
        third = (partners.get(int(partner_id), "") if partner_id else "") if group == "account" else code
        out.append(
            {
                **_row(
                    f"{row_id}:item:{line_id}",
                    move_name or "/",
                    [
                        date.isoformat(),
                        move_name or "/",
                        third,
                        label or "",
                        ctx.round(float(debit)),
                        ctx.round(float(credit)),
                        ctx.round(grp["opening"] + float(cumul)),
                    ],
                    parent_id=row_id,
                    level=1,
                    style="item",
                ),
                "domains": [[["id", "=", line_id]]] * 7,
                "move_id": move_id,
                "line_id": line_id,
                "rn": int(rn),
            }
        )
    if items and offset + len(items) < total:
        out.append(
            {
                **_row(
                    f"{row_id}:more", str(_("Load more…")), [None] * 7, parent_id=row_id, level=1, style="load_more"
                ),
                "next_offset": offset + len(items),
                "remaining": total - offset - len(items),
            }
        )
    return out


# -- aged balances ----------------------------------------------------------------------------------------

BUCKETS = ("not_due", "d30", "d60", "d90", "d120", "older")


def aged_receivable(ctx: Context) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return _aged(ctx, "asset_receivable", 1)


def aged_payable(ctx: Context) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return _aged(ctx, "liability_payable", -1)


def _aged(ctx: Context, account_type: str, sign: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    day = ctx.periods[0].date_to
    labels = {
        "not_due": str(_("Not due")),
        "d30": str(_("1-30")),
        "d60": str(_("31-60")),
        "d90": str(_("61-90")),
        "d120": str(_("91-120")),
        "older": str(_("Older")),
    }
    columns = [
        {"key": "due", "label": str(_("Due date")), "type": "date"},
        *(_money(labels[b], b) for b in BUCKETS),
        _money(str(_("Total")), "total"),
    ]
    factors = _factors(ctx, day)
    factor = _factor_sql(factors)
    where, args = ctx.filters()
    args.update({"cids": ctx.cids, "day": day, "type": account_type})
    open_items = f"""
        SELECT l.id, COALESCE(l.partner_id, 0) AS partner, l.move_id, l.move_name, l.name,
               COALESCE(l.date_maturity, l.date) AS due,
               (l.balance
                - COALESCE((SELECT SUM(p.amount) FROM account_partial_reconcile p
                             WHERE p.debit_move_id = l.id AND p.max_date <= :day), 0)
                + COALESCE((SELECT SUM(p.amount) FROM account_partial_reconcile p
                             WHERE p.credit_move_id = l.id AND p.max_date <= :day), 0)) * {factor} AS residual
          FROM account_move_line l
          JOIN account_account a ON a.id = l.account_id
         WHERE {where} AND l.company_id = ANY(:cids) AND a.account_type = :type AND l.date <= :day
           AND (NOT COALESCE(l.reconciled, false) OR EXISTS (
                SELECT 1 FROM account_partial_reconcile p
                 WHERE (p.debit_move_id = l.id OR p.credit_move_id = l.id) AND p.max_date > :day))
    """  # noqa: S608
    bucket_sql = """
        CASE WHEN due >= :day THEN 0
             WHEN due >= :day - 30 THEN 1
             WHEN due >= :day - 60 THEN 2
             WHEN due >= :day - 90 THEN 3
             WHEN due >= :day - 120 THEN 4
             ELSE 5 END
    """
    summary = ctx.env.cr.fetchall(
        f"""
        WITH o AS ({open_items})
        SELECT partner, {bucket_sql} AS bucket, SUM(residual), COUNT(*)
          FROM o WHERE ROUND(residual::numeric, 6) <> 0
         GROUP BY partner, bucket
        """,  # noqa: S608
        args,
    )
    partners: dict[int, dict[str, Any]] = {}
    for partner, bucket, amount, count in summary:
        slot = partners.setdefault(int(partner), {"values": [0.0] * 6, "count": 0})
        slot["values"][int(bucket)] += sign * float(amount)
        slot["count"] += int(count)
    names = _partner_names(ctx, [p for p in partners if p])
    order = sorted(partners, key=lambda p: ((names.get(p) or "").lower(), p))
    unfold = set(ctx.params.unfold)
    unfolded = [p for p in order if ctx.params.unfold_all or f"partner:{p}" in unfold]
    if ctx.params.expand:
        wanted = ctx.params.expand.split(":", 1)[-1]
        unfolded = [int(wanted)] if wanted.isdigit() and int(wanted) in partners else []
    items: dict[int, list[tuple[Any, ...]]] = defaultdict(list)
    if unfolded:
        offset = ctx.params.offset if ctx.params.expand else 0
        args.update({"partners": unfolded, "offset": offset, "stop": offset + ctx.params.limit})
        rows = ctx.env.cr.fetchall(
            f"""
            WITH o AS ({open_items})
            SELECT partner, id, move_id, move_name, name, due, residual, bucket, rn, total FROM (
                SELECT o.*, {bucket_sql} AS bucket,
                       ROW_NUMBER() OVER (PARTITION BY partner ORDER BY due, id) AS rn,
                       COUNT(*) OVER (PARTITION BY partner) AS total
                  FROM o WHERE ROUND(residual::numeric, 6) <> 0 AND partner = ANY(:partners) OFFSET 0) s
             WHERE rn > :offset AND rn <= :stop
             ORDER BY partner, rn
            """,  # noqa: S608
            args,
        )
        for row in rows:
            items[int(row[0])].append(row[1:])
    out: list[dict[str, Any]] = []
    totals = [0.0] * 7
    domain_base = [
        ["company_id", "in", ctx.cids],
        ["parent_state", "in", ctx.states],
        ["account_id.account_type", "=", account_type],
        ["date", "<=", day.isoformat()],
        ["reconciled", "=", False],
    ]
    for p in order:
        values = [ctx.round(v) for v in partners[p]["values"]]
        values.append(ctx.round(sum(values)))
        if not any(values):
            continue
        totals = [t + v for t, v in zip(totals, values, strict=True)]
        row_id = f"partner:{p}"
        partner_leaf = ["partner_id", "=", p] if p else ["partner_id", "is_not_set"]
        is_open = p in unfolded
        if not ctx.params.expand:
            out.append(
                {
                    **_row(row_id, names.get(p) or str(_("Unknown partner")), [None, *values], style="account"),
                    "domains": [None] + [[*domain_base, partner_leaf]] * 7,
                    "unfoldable": True,
                    "unfolded": is_open,
                }
            )
        if is_open:
            offset = ctx.params.offset if ctx.params.expand else 0
            count = 0
            for line_id, move_id, move_name, label, due, residual, bucket, _rn, total in items.get(p, []):
                count = int(total)
                vals: list[Any] = [due.isoformat(), *([None] * 6), ctx.round(sign * float(residual))]
                vals[1 + int(bucket)] = ctx.round(sign * float(residual))
                out.append(
                    {
                        **_row(
                            f"{row_id}:item:{line_id}",
                            " ".join(x for x in (move_name or "/", label or "") if x),
                            vals,
                            parent_id=row_id,
                            level=1,
                            style="item",
                        ),
                        "domains": [[["id", "=", line_id]]] * 8,
                        "move_id": move_id,
                        "line_id": line_id,
                    }
                )
            shown = len(items.get(p, []))
            if shown and offset + shown < count:
                out.append(
                    {
                        **_row(
                            f"{row_id}:more",
                            str(_("Load more…")),
                            [None] * 8,
                            parent_id=row_id,
                            level=1,
                            style="load_more",
                        ),
                        "next_offset": offset + shown,
                        "remaining": count - offset - shown,
                    }
                )
    if not ctx.params.expand:
        out.append(_row("total", str(_("Total")), [None, *(ctx.round(t) for t in totals)], style="total"))
    return columns, out


# -- helpers ----------------------------------------------------------------------------------------------


def _row(
    row_id: str,
    name: str,
    values: list[Any],
    *,
    domains: list[Any] | None = None,
    parent_id: str | None = None,
    level: int = 0,
    style: str = "normal",
) -> dict[str, Any]:
    return {
        "id": row_id,
        "parent_id": parent_id,
        "code": None,
        "name": name,
        "level": level,
        "style": style,
        "values": values,
        "domains": domains if domains is not None else [None] * len(values),
        "unfoldable": False,
        "unfolded": False,
    }


def _or(domains: list[Any]) -> Any:
    if not domains:
        return None
    if len(domains) == 1:
        return domains[0]
    return {"any": [{"all": d} for d in domains]}
