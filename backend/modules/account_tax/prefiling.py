"""Deterministic pre-filing checks of a tax period.

``prefiling_checks(company, date_from, date_to)`` returns findings::

    {"code": "reverse_charge_without_vat", "severity": "error" | "warning" | "info",
     "message": "...", "model": "account.move", "res_ids": [...], "count": 3, "details": {...}}

The codes are stable: the AI feature of sub-phase 2.6 explains and ranks them, it does not compute them.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from typing import Any

from modules.account_tax.closing import ledger_check
from tessera.i18n import _
from tessera.orm import Model

MAX_IDS = 200
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
INVOICE_TYPES = ["out_invoice", "out_refund", "in_invoice", "in_refund"]
# a tax-and-country combination used by fewer than this share of a country's invoice lines is unusual
UNUSUAL_SHARE = 0.2
UNUSUAL_MIN_LINES = 5


def _finding(
    code: str, severity: str, message: Any, model: str | None, ids: list[int], **details: Any
) -> dict[str, Any]:
    ids = sorted(set(ids))
    return {
        "code": code,
        "severity": severity,
        "message": str(message),
        "model": model,
        "res_ids": ids[:MAX_IDS],
        "count": len(ids),
        "details": details,
    }


def prefiling_checks(company: Model, date_from: dt.date, date_to: dt.date) -> list[dict[str, Any]]:
    env = company.env
    sudo = env.elevated("pre-filing checks")
    cid = company.ids[0]
    args = {"cid": cid, "start": date_from, "end": date_to, "types": INVOICE_TYPES}
    findings: list[dict[str, Any]] = []

    # 1. taxes whose distribution has no grid (their amounts never reach the return)
    missing: list[int] = []
    for tax in sudo["account.tax"].search([("company_id", "=", cid), ("amount_type", "!=", "group")]):
        for rep in tax.repartition_line_ids:
            taxed = rep.repartition_type == "tax" and float(tax.amount or 0) and float(rep.factor_percent or 0)
            if not rep.tag_ids and (rep.repartition_type == "base" or taxed):
                missing.append(tax.ids[0])
                break
    if missing:
        findings.append(
            _finding(
                "taxes_without_grids",
                "warning",
                _("{count} taxes have no tax grid on their base or tax distribution", count=len(missing)),
                "account.tax",
                missing,
            )
        )

    # 2. tax items of the period without a grid
    rows = sudo.cr.fetchall(
        """
        SELECT DISTINCT l.move_id FROM account_move_line l
         WHERE l.company_id = :cid AND l.parent_state = 'posted' AND l.date >= :start AND l.date <= :end
           AND l.tax_line_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM account_move_line_tag_rel r WHERE r.line_id = l.id)
        """,
        args,
    )
    if rows:
        findings.append(
            _finding(
                "tax_lines_without_grid",
                "error",
                _("{count} entries have tax amounts outside every grid", count=len(rows)),
                "account.move",
                [int(r[0]) for r in rows],
            )
        )

    # 3. reverse charge / intra-EU documents for partners without a tax ID
    rows = sudo.cr.fetchall(
        f"""
        SELECT DISTINCT m.id FROM account_move m
          JOIN "{sudo["contacts.partner"]._table}" p ON p.id = m.commercial_partner_id
          LEFT JOIN account_fiscal_position fp ON fp.id = m.fiscal_position_id
         WHERE m.company_id = :cid AND m.state = 'posted' AND m.move_type = ANY(:types)
           AND m.date >= :start AND m.date <= :end AND COALESCE(p.vat, '') = ''
           AND (COALESCE(fp.vat_required, false) OR EXISTS (
                SELECT 1 FROM account_move_line l
                  JOIN account_tax_repartition_line r ON r.tax_id = l.tax_line_id
                 WHERE l.move_id = m.id AND r.factor_percent < 0))
        """,  # noqa: S608
        args,
    )
    if rows:
        findings.append(
            _finding(
                "reverse_charge_without_vat",
                "error",
                _("{count} reverse-charge or intra-EU documents have a partner without a tax ID", count=len(rows)),
                "account.move",
                [int(r[0]) for r in rows],
            )
        )

    # 4. unusual tax per partner country (customer invoices)
    rows = sudo.cr.fetchall(
        f"""
        SELECT l.move_id, p.country_id,
               COALESCE((SELECT string_agg(tr.tax_id::text, ',' ORDER BY tr.tax_id)
                           FROM account_move_line_tax_rel tr WHERE tr.line_id = l.id), '')
          FROM account_move_line l
          JOIN account_move m ON m.id = l.move_id
          JOIN "{sudo["contacts.partner"]._table}" p ON p.id = m.commercial_partner_id
         WHERE m.company_id = :cid AND m.state = 'posted' AND m.move_type IN ('out_invoice', 'out_refund')
           AND l.display_type = 'product' AND m.date >= :start AND m.date <= :end
        """,  # noqa: S608
        args,
    )
    by_country: dict[Any, Counter[str]] = defaultdict(Counter)
    moves_by_key: dict[tuple[Any, str], set[int]] = defaultdict(set)
    for move_id, country_id, taxes in rows:
        by_country[country_id][taxes] += 1
        moves_by_key[(country_id, taxes)].add(int(move_id))
    unusual: set[int] = set()
    combos: list[dict[str, Any]] = []
    for country_id, counts in by_country.items():
        total = sum(counts.values())
        if total < UNUSUAL_MIN_LINES:
            continue
        usual = counts.most_common(1)[0][0]
        for taxes, count in counts.items():
            if taxes != usual and count / total < UNUSUAL_SHARE:
                unusual |= moves_by_key[(country_id, taxes)]
                combos.append(
                    {
                        "country_id": country_id,
                        "tax_ids": [int(t) for t in taxes.split(",") if t],
                        "usual_tax_ids": [int(t) for t in usual.split(",") if t],
                        "lines": count,
                        "country_lines": total,
                    }
                )
    if unusual:
        findings.append(
            _finding(
                "unusual_tax_for_country",
                "warning",
                _("{count} customer invoices use a tax that is unusual for the customer's country", count=len(unusual)),
                "account.move",
                sorted(unusual),
                combinations=combos,
            )
        )

    # 5. fiscal position different from the one the partner's country and tax ID call for
    rows = sudo.cr.fetchall(
        "SELECT m.id, m.commercial_partner_id, m.fiscal_position_id FROM account_move m "
        "WHERE m.company_id = :cid AND m.state = 'posted' AND m.move_type = ANY(:types) "
        "AND m.date >= :start AND m.date <= :end AND m.commercial_partner_id IS NOT NULL",
        args,
    )
    positions = sudo["account.fiscal_position"]
    partners = sudo["contacts.partner"]
    expected: dict[int, int | None] = {}
    mismatched: list[int] = []
    for move_id, partner_id, fp_id in rows:
        if partner_id not in expected:
            fp = positions._get_for_partner(partners.browse([int(partner_id)]), company.elevated())
            expected[partner_id] = fp.ids[0] if fp else None
        if expected[partner_id] != fp_id:
            mismatched.append(int(move_id))
    if mismatched:
        findings.append(
            _finding(
                "fiscal_position_mismatch",
                "warning",
                _(
                    "{count} documents use another fiscal position than the partner's country and tax ID call for",
                    count=len(set(mismatched)),
                ),
                "account.move",
                mismatched,
            )
        )

    # 6. draft entries dated in the period (they would be locked out by the closing)
    rows = sudo.cr.fetchall(
        "SELECT id FROM account_move WHERE company_id = :cid AND state = 'draft' "
        "AND ((date >= :start AND date <= :end) OR (invoice_date >= :start AND invoice_date <= :end))",
        args,
    )
    if rows:
        findings.append(
            _finding(
                "draft_entries",
                "warning",
                _("{count} draft entries are dated in the period", count=len(rows)),
                "account.move",
                [int(r[0]) for r in rows],
            )
        )

    # 7. the report must tie to the tax accounts
    check = ledger_check(env, company, date_from, date_to)
    if check["difference"]:
        findings.append(
            _finding(
                "report_ledger_mismatch",
                "error",
                _(
                    "The return (net {report}) differs from the tax accounts' movement ({ledger}) by {difference}",
                    report=check["NET"],
                    ledger=check["ledger_net"],
                    difference=check["difference"],
                ),
                None,
                [],
                **check,
            )
        )

    # 8. already closed
    lock = company.elevated().tax_lock_date
    if lock and lock >= date_from:
        findings.append(
            _finding(
                "period_locked",
                "info",
                _("Taxes are locked up to {date}", date=lock.isoformat()),
                "core.company",
                [cid],
                tax_lock_date=lock.isoformat(),
            )
        )
    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["code"]))
    return findings


def summarise(findings: list[dict[str, Any]]) -> str:
    if not findings:
        return str(_("No findings"))
    labels = {"error": _("Error"), "warning": _("Warning"), "info": _("Information")}
    return "\n".join(f"{labels[f['severity']]}: {f['message']}" for f in findings)
