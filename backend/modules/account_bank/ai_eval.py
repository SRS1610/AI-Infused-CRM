"""Evaluation entry and scorer of ``account_bank.reconcile_suggest`` (``ai/features.yaml``).

A case's ``setup`` creates the partners and the *draft* invoices / bills of a scenario (so cassettes can name
them as ``$id:<ref>``); the entry posts them, books the counterparty's history (earlier transactions matched
with the partner's invoices), creates and posts the bank transaction from ``inputs.line`` (its label is a
template: ``{name:a}`` is invoice ``a``'s number, ``{typo1:a}`` … ``{typo3:a}`` misspell it, ``{serial:a}`` is
its bare serial number, ``{upper:p1}`` / ``{short:p1}`` the partner's name as banks print it) and runs the
feature as the case's user. The top-1 suggestion is compared with ``expected.match`` (the documents the
transaction pays). Ledger mutations between the feature's start and end are counted: a suggestion never
matches anything by itself.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from modules.account_bank import ai_features as A


def _env_sys(env: Any) -> Any:
    return env.elevated("ai eval scenario")


def _parts(name: str) -> tuple[str, str, str]:
    m = re.match(r"^(.*?)(\d{4})(\D+)(\d+)$", name or "")
    if not m:
        return name, "", ""
    return m.group(1), m.group(2), m.group(4)


def mangle(kind: str, name: str) -> str:
    prefix, year, serial = _parts(name)
    letters = re.sub(r"[^A-Za-z]", "", prefix) or "INV"
    if kind == "typo1":  # one zero dropped: INV/2026/0012
        cut = serial[1:] if serial.startswith("0") else serial[:-1]
        return f"{prefix}{year}/{cut}"
    if kind == "typo2":  # short year, no padding: INV-26-12
        return f"{letters}-{year[2:]}-{serial.lstrip('0')}"
    if kind == "typo3":  # two letters swapped, lower case, spaces: ivn 2026 00012
        swapped = letters[1] + letters[0] + letters[2:] if len(letters) > 1 else letters
        return f"{swapped.lower()} {year} {serial}"
    return name


def render_label(template: str | None, moves: dict[str, Any], partners: dict[str, Any]) -> str | None:
    if template is None:
        return None

    def sub(m: re.Match[str]) -> str:
        kind, key = m.group(1), m.group(2)
        if kind in ("upper", "short"):
            name = partners[key].name or ""
            return name.upper() if kind == "upper" else name.split()[0].upper()
        name = moves[key].name or ""
        if kind == "name":
            return name
        if kind == "serial":
            return _parts(name)[2].lstrip("0")
        return mangle(kind, name)

    return re.sub(r"\{(name|typo1|typo2|typo3|serial|upper|short):([a-z0-9_]+)\}", sub, template)


def _bank_journal(env: Any, company_id: int) -> Any:
    return env["account.journal"].search(
        [("type", "=", "bank"), ("company_id", "=", company_id)], order="sequence, id", limit=1
    )


def _transaction(env: Any, journal: Any, vals: dict[str, Any]) -> Any:
    line = env["account.bank_statement_line"].create({"journal_id": journal.ids[0], **vals})
    line._post()
    return line


def _open_line(move: Any) -> Any:
    return move.line_ids.filtered(lambda r: r.account_id.reconcile and r.display_type == "payment_term")


def build(env: Any, inputs: dict[str, Any]) -> Any:
    """Post the scenario's documents, book the history, create the transaction; returns it."""
    from modules.account_bank.matching import match_line

    sys_env = _env_sys(env)
    company_id = env.company.ids[0]
    moves = {k: sys_env["account.move"].browse([int(v)]) for k, v in (inputs.get("docs") or {}).items()}
    partners = {k: sys_env["contacts.partner"].browse([int(v)]) for k, v in (inputs.get("partners") or {}).items()}
    for move in moves.values():
        if move.state == "draft":
            move.action_post()
    journal = _bank_journal(sys_env, company_id)
    for i, past in enumerate(inputs.get("history") or []):
        partner = partners[past["partner"]]
        bill = sys_env["account.move"].create(
            {
                "move_type": "out_invoice",
                "partner_id": partner.ids[0],
                "company_id": company_id,
                "invoice_date": dt.date.fromisoformat(past["date"]) - dt.timedelta(days=30),
                "invoice_line_ids": [
                    (
                        "create",
                        {
                            "name": "Earlier order",
                            "quantity": 1,
                            "price_unit": past["amount"],
                            "tax_ids": [("set", [])],
                        },
                    )
                ],
            }
        )
        bill.action_post()
        earlier = _transaction(
            sys_env,
            journal,
            {
                "date": dt.date.fromisoformat(past["date"]),
                "payment_ref": past.get("label") or f"TRANSFER {i + 1}",
                "account_number": past.get("account_number"),
                "partner_name": past.get("partner_name"),
                "amount": past["amount"],
            },
        )
        match_line(earlier, [{"line_id": _open_line(bill).ids[0]}], [])
    spec = dict(inputs["line"])
    vals = {
        "date": dt.date.fromisoformat(spec["date"]),
        "payment_ref": render_label(spec.get("label"), moves, partners) or "/",
        "ref": spec.get("ref"),
        "partner_name": render_label(spec.get("partner_name"), moves, partners),
        "account_number": spec.get("account_number"),
        "narration": render_label(spec.get("narration"), moves, partners),
        "amount": spec["amount"],
    }
    return _transaction(sys_env, journal, vals)


def _ledger_state(env: Any) -> tuple[int, int, int]:
    env.flush()
    return (
        int(env.cr.scalar("SELECT count(*) FROM account_partial_reconcile") or 0),
        int(env.cr.scalar("SELECT count(*) FROM account_bank_statement_line WHERE state = 'reconciled'") or 0),
        int(env.cr.scalar("SELECT count(*) FROM account_move_line") or 0),
    )


def entry(env: Any, inputs: dict[str, Any], record: Any) -> dict[str, Any]:
    line = build(env, inputs)
    before = _ledger_state(env)
    out = A.suggest(env, line.with_env(env), limit=5)
    after = _ledger_state(env)
    top = out["suggestions"][0] if out["suggestions"] else None
    top_moves = sorted(int(i["move_id"]) for i in top["items"]) if top else []
    data = {
        "top_moves": top_moves,
        "ranked": [sorted(int(i["move_id"]) for i in s["items"]) for s in out["suggestions"]],
        "scores": [s["score"] for s in out["suggestions"]],
        "used_model": out["used_model"],
        "ambiguous": out["ambiguous"],
        "model_flags": out["flags"],
        "mutations": sum(abs(a - b) for a, b in zip(after, before, strict=True)),
        "proposal": bool(out["proposal"]),
        "label": line.elevated().payment_ref,
    }
    return {"text": "", "data": data, "proposals": 1 if out["proposal"] else 0, "status": "ok", "flags": out["flags"]}


def score(results: list[dict[str, Any]]) -> dict[str, float]:
    ok = [r for r in results if r.get("status") == "ok" and isinstance(r.get("data"), dict)]
    if not ok:
        return {}
    hits = [sorted(r["data"]["top_moves"]) == sorted(int(x) for x in r["_case"]["expected"]["match"]) for r in ok]
    top3 = [
        sorted(int(x) for x in r["_case"]["expected"]["match"]) in [sorted(m) for m in r["data"]["ranked"][:3]]
        for r in ok
    ]
    model = [r for r in ok if r["data"]["used_model"]]
    return {
        "top1_accuracy": sum(hits) / len(ok),
        "top3_recall": sum(top3) / len(ok),
        "zero_mutations": sum(1 for r in ok if r["data"]["mutations"] == 0) / len(ok),
        "model_calls": float(len(model)),
        "proposal_rate": sum(1 for r in ok if r["data"]["proposal"]) / len(ok),
    }
