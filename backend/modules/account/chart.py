"""Chart-of-accounts templates (``data/charts/<code>.json``) and their installation on a company.

A template lists accounts, tax grids (tags), tax groups, taxes with their invoice / refund distribution,
fiscal positions, journals and the company's default accounts, all referring to each other by template id.
Installing creates the records for the company and registers them as ``account.<company id>_<template id>``
so code and data can find them (``chart_ref(company, "receivable")``). Installing is idempotent: records that
already exist are left alone.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.orm import Model

CHARTS_DIR = Path(__file__).parent / "data" / "charts"
EU_COUNTRIES = [
    "AT",
    "BE",
    "BG",
    "HR",
    "CY",
    "CZ",
    "DK",
    "EE",
    "FI",
    "FR",
    "DE",
    "GR",
    "HU",
    "IE",
    "IT",
    "LV",
    "LT",
    "LU",
    "MT",
    "NL",
    "PL",
    "PT",
    "RO",
    "SK",
    "SI",
    "ES",
    "SE",
]
COMPANY_DEFAULTS = {
    "receivable": "default_receivable_account_id",
    "payable": "default_payable_account_id",
    "income": "default_income_account_id",
    "expense": "default_expense_account_id",
    "suspense": "account_suspense_id",
    "outstanding_receipts": "account_outstanding_receipts_id",
    "outstanding_payments": "account_outstanding_payments_id",
    "exchange_gain": "income_currency_exchange_account_id",
    "exchange_loss": "expense_currency_exchange_account_id",
    "discount_given": "account_epd_discount_expense_id",
    "discount_received": "account_epd_discount_income_id",
    "retained_earnings": "account_retained_earnings_id",
    "rounding": "account_rounding_id",
}


@cache
def available_charts() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(CHARTS_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        out[data["code"]] = data
    return out


def guess_chart(company: Model) -> str:
    """``us`` for US companies, ``eu`` for EU ones, ``generic`` otherwise."""
    code = company.elevated().country_id.code if company.elevated().country_id else None
    if code == "US":
        return "us"
    if code in EU_COUNTRIES:
        return "eu"
    return "generic"


def chart_ref(company: Model, template_id: str) -> Model | None:
    return company.env.ref(f"account.{company.ids[0]}_{template_id}", raise_if_not_found=False)


def install_chart(company: Model, code: str | None = None) -> dict[str, int]:
    """Create the template's records for ``company`` (idempotent). Returns counts per kind."""
    env = company.env
    company = company.elevated("chart installation")
    code = code or company.chart_template or guess_chart(company)
    template = available_charts().get(code)
    if template is None:
        raise UserError(_("Unknown chart template '{code}'", code=code))
    if company.chart_template and company.chart_template != code:
        raise UserError(
            _("{company} already uses the '{chart}' chart", company=company.name, chart=company.chart_template)
        )
    counts = {"accounts": 0, "taxes": 0, "journals": 0, "fiscal_positions": 0}
    cid = company.ids[0]
    country = company.country_id
    country_code = country.code if country else None

    def ref(tid: str) -> Model | None:
        return chart_ref(company, tid)

    def register(tid: str, record: Model) -> None:
        env.set_ref(f"account.{cid}_{tid}", record, noupdate=True)

    def country_of(codes: list[str]) -> list[int]:
        if not codes:
            return []
        found = env["core.country"].elevated().search([("code", "in", codes)])
        return found.ids

    account_model = env["account.account"].elevated()
    for row in template.get("accounts", []):
        if ref(row["id"]):
            continue
        existing = account_model.search([("code", "=", row["code"]), ("company_id", "=", cid)], limit=1)
        record = existing or account_model.create(
            {
                "code": row["code"],
                "name": row["name"],
                "account_type": row["type"],
                "reconcile": bool(row.get("reconcile")),
                "company_id": cid,
            }
        )
        register(row["id"], record)
        counts["accounts"] += 0 if existing else 1
    tag_model = env["account.account_tag"].elevated()
    tag_country = country.ids[0] if country and template.get("country_tags") else None

    def tags(names: list[str]) -> list[int]:
        ids: list[int] = []
        for name in names or []:
            domain: list[Any] = [("name", "=", name), ("applicability", "=", "taxes")]
            domain.append(("country_id", "=", tag_country) if tag_country else ("country_id", "is_not_set"))
            tag = tag_model.search(domain, limit=1)
            if not tag:
                tag = tag_model.create(
                    {
                        "name": name,
                        "applicability": "taxes",
                        "country_id": tag_country,
                        "tax_negate": name.startswith("-"),
                    }
                )
            ids.append(tag.ids[0])
        return ids

    def acc(tid: str | None) -> int | None:
        if not tid:
            return None
        rec = ref(tid)
        if rec is None:
            raise UserError(_("Chart '{code}' refers to unknown account '{tid}'", code=code, tid=tid))
        return rec.ids[0]

    group_model = env["account.tax_group"].elevated()
    for row in template.get("tax_groups", []):
        if ref(row["id"]):
            continue
        register(
            row["id"],
            group_model.create(
                {
                    "name": row["name"],
                    "sequence": row.get("sequence", 10),
                    "company_id": cid,
                    "tax_payable_account_id": acc(row.get("payable")),
                    "tax_receivable_account_id": acc(row.get("receivable")),
                }
            ),
        )
    tax_model = env["account.tax"].elevated()
    for row in template.get("taxes", []):
        if ref(row["id"]):
            continue
        reps: list[tuple[str, dict[str, Any]]] = []
        if row.get("amount_type", "percent") != "group":
            for doc in ("invoice", "refund"):
                spec = row.get(doc, {})
                reps.append(
                    (
                        "create",
                        {
                            "document_type": doc,
                            "repartition_type": "base",
                            "factor_percent": 100.0,
                            "tag_ids": [("set", tags(spec.get("base_tags", [])))],
                        },
                    )
                )
                for i, part in enumerate(spec.get("tax", [{"factor": 100}])):
                    reps.append(
                        (
                            "create",
                            {
                                "document_type": doc,
                                "repartition_type": "tax",
                                "factor_percent": float(part.get("factor", 100)),
                                "account_id": acc(part.get("account")),
                                "tag_ids": [("set", tags(part.get("tags", [])))],
                                "sequence": i + 1,
                            },
                        )
                    )
        vals = {
            "name": row["name"],
            "description": row.get("description") or row["name"],
            "type_tax_use": row.get("use", "sale"),
            "amount_type": row.get("amount_type", "percent"),
            "amount": float(row.get("amount", 0)),
            "price_include": bool(row.get("price_include")),
            "include_base_amount": bool(row.get("include_base_amount")),
            "sequence": row.get("sequence", 1),
            "tax_group_id": ref(row["group"]).ids[0] if row.get("group") and ref(row["group"]) else None,
            "company_id": cid,
            "country_id": country.ids[0] if country else None,
            "repartition_line_ids": reps,
        }
        if row.get("children"):
            vals["children_tax_ids"] = [("set", [ref(c).ids[0] for c in row["children"] if ref(c)])]
        register(row["id"], tax_model.create(vals))
        counts["taxes"] += 1
    position_model = env["account.fiscal_position"].elevated()
    for row in template.get("fiscal_positions", []):
        if ref(row["id"]):
            continue
        countries = list(EU_COUNTRIES if row.get("countries") == "EU" else row.get("countries", []))
        countries = [c for c in countries if c != country_code]
        excluded = list(EU_COUNTRIES) if row.get("exclude_countries") == "EU" else row.get("exclude_countries", [])
        if row.get("exclude_own_country") and country_code:
            excluded = [*excluded, country_code]
        position = position_model.create(
            {
                "name": row["name"],
                "sequence": row.get("sequence", 10),
                "auto_apply": bool(row.get("auto_apply")),
                "vat_required": bool(row.get("vat_required")),
                "note": row.get("note"),
                "country_ids": [("set", country_of(countries))],
                "exclude_country_ids": [("set", country_of(excluded))],
                "company_id": cid,
                "tax_ids": [
                    (
                        "create",
                        {
                            "tax_src_id": ref(m["src"]).ids[0],
                            "tax_dest_id": ref(m["dest"]).ids[0] if m.get("dest") else None,
                        },
                    )
                    for m in row.get("taxes", [])
                    if ref(m["src"])
                ],
                "account_ids": [
                    ("create", {"account_src_id": acc(m["src"]), "account_dest_id": acc(m["dest"])})
                    for m in row.get("accounts", [])
                ],
            }
        )
        register(row["id"], position)
        counts["fiscal_positions"] += 1
    journal_model = env["account.journal"].elevated()
    for row in template.get("journals", []):
        if ref(row["id"]):
            continue
        existing = journal_model.search([("code", "=", row["code"]), ("company_id", "=", cid)], limit=1)
        journal = existing or journal_model.create(
            {
                "name": row["name"],
                "code": row["code"],
                "type": row["type"],
                "sequence": row.get("sequence", 10),
                "default_account_id": acc(row.get("default_account")),
                "suspense_account_id": acc(row.get("suspense_account")),
                "restrict_mode_hash_table": bool(row.get("hash")),
                "company_id": cid,
            }
        )
        register(row["id"], journal)
        counts["journals"] += 0 if existing else 1
    updates: dict[str, Any] = {"chart_template": code}
    for tid, field in COMPANY_DEFAULTS.items():
        rec = ref(template.get("defaults", {}).get(tid, tid))
        if rec is not None and not company[field]:
            updates[field] = rec.ids[0]
    for key, field in (
        ("exchange_journal", "exchange_journal_id"),
        ("sale_tax", "default_sale_tax_id"),
        ("purchase_tax", "default_purchase_tax_id"),
    ):
        tid = template.get("defaults", {}).get(key)
        rec = ref(tid) if tid else None
        if rec is not None and not company[field]:
            updates[field] = rec.ids[0]
    if not company.account_fiscal_country_id and country:
        updates["account_fiscal_country_id"] = country.ids[0]
    company.write(updates)
    return counts
