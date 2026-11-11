"""``account_dunning.message_draft`` — personalised payment reminders (sub-phase 2.6, BUILD_PLAN §3).

For a prepared reminder (``account.dunning_run_line`` still to send) the mid-tier model writes the text of
the message: tone by dunning level (friendly first reminder, firm second, formal final notice) and by the
customer's history (earlier reminders, how late they usually pay, disputed invoices). Everything factual
comes from the ledger:

* the facts the model sees are computed here (amounts due, invoices, days late, fee, history);
* **every number in the draft must be one of those facts** — a sentence stating another number (a wrong
  total, an invented discount or deadline) is removed before anything is shown (:func:`ground_text`);
* the invoice table with the amounts and pay links is always appended by
  :func:`modules.account_dunning.dunning.invoice_table`, never written by the model.

The result is a **write proposal** on the reminder (subject and message). Nothing is sent: accepting it only
replaces the prepared text, and the run is sent by the user as before (``send_run``), so no mail is queued
before a person decides twice.
"""

from __future__ import annotations

import datetime as dt
import html
from typing import TYPE_CHECKING, Any

from modules.account_dunning.dunning import OverdueItem, ReminderContext, invoice_table
from tessera.ai import Source
from tessera.ai.facts import ground_text, ungrounded
from tessera.ai.runtime import HANDLERS, RunResult, handler, run_feature
from tessera.ai.tools import PendingProposal, ToolContext
from tessera.exceptions import AccessError, UserError
from tessera.i18n import _

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

FEATURE = "account_dunning.message_draft"
TONES = {0: "friendly", 1: "firm", 2: "formal"}


def _context(line: Model) -> ReminderContext:
    """The reminder's context rebuilt from the ledger (current residuals of its invoices)."""
    sys_line = line.elevated("dunning draft")
    items: list[OverdueItem] = []
    for move in sys_line.invoice_ids:
        open_lines = move.line_ids.filtered(
            lambda r: r.account_id.account_type == "asset_receivable" and not r.reconciled
        )
        residual = sum(float(r.amount_residual) for r in open_lines)
        maturities = [r.date_maturity for r in open_lines if r.date_maturity]
        due = min(maturities) if maturities else (move.invoice_date_due or move.invoice_date or sys_line.date)
        days = max(0, (sys_line.date - due).days) if due else 0
        if residual > 0:
            items.append(OverdueItem(move=move, residual=residual, days=days))
    return ReminderContext(
        partner=sys_line.partner_id,
        company=sys_line.company_id,
        level=sys_line.level_id,
        items=items,
        currency=sys_line.currency_id or sys_line.company_id.currency_id,
    )


def history(line: Model) -> dict[str, Any]:
    """How this customer paid and was reminded before (last 12 months)."""
    env = line.env
    sys_line = line.elevated("dunning history")
    partner = sys_line.partner_id
    since = sys_line.date - dt.timedelta(days=365)
    earlier = env["account.dunning_run_line"].elevated().search(
        [
            ("partner_id", "=", partner.ids[0]),
            ("company_id", "=", sys_line.company_id.ids[0]),
            ("state", "in", ["sent", "letter"]),
            ("date", ">=", since),
            ("id", "!=", sys_line.ids[0]),
        ],
        order="date desc",
    )
    moves = env["account.move"].elevated()
    paid = moves.search(
        [
            ("commercial_partner_id", "=", partner.ids[0]),
            ("company_id", "=", sys_line.company_id.ids[0]),
            ("move_type", "=", "out_invoice"),
            ("state", "=", "posted"),
            ("payment_state", "in", ["paid", "in_payment"]),
            ("invoice_date", ">=", since),
        ]
    )
    delays: list[int] = []
    for move in paid:
        recv = move.line_ids.filtered(lambda r: r.account_id.account_type == "asset_receivable")
        dates = [
            p.max_date
            for r in recv
            for p in (r.matched_credit_ids | r.matched_debit_ids)
            if getattr(p, "max_date", None)
        ]
        due = move.invoice_date_due or move.invoice_date
        if dates and due:
            delays.append((max(dates) - due).days)
    disputed = env["account.move_line"].elevated().search_count(
        [
            ("partner_id", "child_of", partner.ids[0]),
            ("company_id", "=", sys_line.company_id.ids[0]),
            ("blocked", "=", True),
            ("reconciled", "=", False),
            ("account_id.account_type", "=", "asset_receivable"),
        ]
    )
    return {
        "reminders_last_12_months": len(earlier),
        "last_reminder": earlier[0].date.isoformat() if earlier else None,
        "last_reminder_level": earlier[0].level_id.name if earlier else None,
        "invoices_paid_last_12_months": len(paid),
        "average_days_late_when_paid": round(sum(delays) / len(delays)) if delays else None,
        "disputed_invoices": disputed,
    }


def facts(line: Model) -> dict[str, Any]:
    ctx = _context(line)
    sys_line = line.elevated()
    levels = sys_line.env["account.dunning_level"].elevated().search(
        [("company_id", "=", sys_line.company_id.ids[0])], order="delay_days"
    )
    position = list(levels.ids).index(sys_line.level_id.ids[0]) if sys_line.level_id.ids[0] in levels.ids else 0
    last = position == len(levels) - 1 and len(levels) > 1
    tone = "formal" if last else TONES.get(min(position, 1), "firm")
    cur = ctx.currency
    level = sys_line.level_id
    return {
        "customer": sys_line.partner_id.name,
        "company": sys_line.company_id.name,
        "currency": cur.name,
        "date": sys_line.date.isoformat(),
        "level": {
            "name": level.name,
            "position": position + 1,
            "of": len(levels),
            "final": last,
            "tone": tone,
            "delay_days": int(level.delay_days or 0),
            "fee": cur.format(float(level.fee_amount)) if level.fee_amount else None,
            "template": level.message or "",
        },
        "amount_due": cur.format(ctx.amount_due),
        "amount_due_value": ctx.amount_due,
        "max_days_overdue": ctx.max_days,
        "invoices": [
            {
                "number": i.move.name,
                "date": i.move.invoice_date.isoformat() if i.move.invoice_date else None,
                "due": i.move.invoice_date_due.isoformat() if i.move.invoice_date_due else None,
                "days_late": i.days,
                "amount_due": cur.format(i.residual),
                "value": round(i.residual, 2),
            }
            for i in sorted(ctx.items, key=lambda x: -x.days)
        ],
        "invoice_count": len(ctx.items),
        "history": history(line),
    }


def _facts_text(f: dict[str, Any]) -> str:
    lv, h = f["level"], f["history"]
    rows = "\n".join(
        f"- {i['number']} dated {i['date']}, due {i['due']}, {i['days_late']} days late, {i['amount_due']} due"
        for i in f["invoices"]
    )
    return "\n".join(
        [
            f"Customer: {f['customer']}",
            f"Reminder date: {f['date']}",
            f"Level: {lv['name']} (level {lv['position']} of {lv['of']}{', the final notice' if lv['final'] else ''}), "
            f"reached {lv['delay_days']} days after the due date",
            f"Tone: {lv['tone']}",
            f"Reminder fee of this level: {lv['fee'] or 'none'}",
            f"The level's standard message: {lv['template']}",
            f"Total overdue: {f['amount_due']} on {f['invoice_count']} invoice(s), up to {f['max_days_overdue']} days late",
            f"Overdue invoices:\n{rows}",
            f"Reminders sent in the last 12 months: {h['reminders_last_12_months']}"
            + (f" (last on {h['last_reminder']}, {h['last_reminder_level']})" if h["last_reminder"] else ""),
            f"Invoices paid in the last 12 months: {h['invoices_paid_last_12_months']}"
            + (
                f", on average {h['average_days_late_when_paid']} days after the due date"
                if h["average_days_late_when_paid"] is not None
                else ""
            ),
            f"Disputed invoices (not part of this reminder): {h['disputed_invoices']}",
        ]
    )


def _html(body: str) -> str:
    paragraphs = [p for p in body.replace("\r\n", "\n").split("\n\n") if p.strip()]
    return "".join(f"<p>{html.escape(p.strip()).replace(chr(10), '<br>')}</p>" for p in paragraphs)


def readable_line(env: Environment, record: Model) -> Model:
    if record is None or record._name != "account.dunning_run_line":
        raise UserError(_("Choose a payment reminder"))
    record.check_access("read")
    return record


@handler(FEATURE)
def prepare(env: Environment, record: Model, inputs: dict[str, Any]) -> dict[str, Any]:
    line = readable_line(env, record)
    sys_line = line.elevated()
    if sys_line.state not in ("draft", "no_email") or sys_line.run_id.state != "draft":
        raise UserError(_("Only reminders that are still to send can be redrafted"))
    f = facts(line)
    ctx = _context(line)
    sources = [Source(f"account.dunning_run_line:{line.ids[0]}", f"Reminder for {f['customer']}", _facts_text(f))]

    def on_output(tctx: ToolContext, text: str, data: Any) -> None:
        raw = data if isinstance(data, dict) else {"subject": "", "body": text}
        body, removed = ground_text(str(raw.get("body") or ""), f)
        subject, removed_subject = ground_text(str(raw.get("subject") or ""), f)
        removed += removed_subject
        if not body.strip():
            body = ""
        out = {
            "subject": subject or sys_line.subject or "",
            "body": body,
            "removed_numbers": removed,
            "facts": f,
            "tone": f["level"]["tone"],
        }
        if isinstance(data, dict):
            data.clear()
            data.update(out)
        if body and _can_write(line):
            message = _html(body) + invoice_table(ctx)
            title = f"Personalised reminder for {f['customer']}"
            if removed:
                title += " — unverified figures removed"
            tctx.proposals.append(
                PendingProposal(
                    "write",
                    "account.dunning_run_line",
                    line.ids[0],
                    {"subject": out["subject"], "message": message},
                    title,
                )
            )

    return {"sources": sources, "on_output": on_output}


def _can_write(line: Model) -> bool:
    try:
        line.check_access("write")
    except AccessError:
        return False
    return True


def draft(env: Environment, line: Model, inputs: dict[str, Any] | None = None) -> RunResult:
    inputs = inputs or {}
    return run_feature(env, FEATURE, inputs, record=line, **HANDLERS[FEATURE](env, line, inputs))


# ---------------------------------------------------------------------------------------------- evaluation


def eval_entry(env: Environment, inputs: dict[str, Any], record: Any) -> Any:
    """Builds the case's ledger (``inputs.scenario``), prepares the run, drafts the reminder of its customer."""
    from modules.account_dunning.ai_eval import build

    line = build(env, inputs["scenario"])
    result = draft(env, line.with_env(env), {k: v for k, v in inputs.items() if k != "scenario"})
    data = result.data if isinstance(result.data, dict) else {}
    final = data.get("body", "")
    result.text = final
    result.data = {
        "body": final,
        "removed_numbers": data.get("removed_numbers", []),
        "left_ungrounded": ungrounded(final + " " + str(data.get("subject", "")), data.get("facts") or {}),
        "tone": data.get("tone"),
        "amount_due": (data.get("facts") or {}).get("amount_due"),
        "mail_queued": int(env.cr.scalar("SELECT count(*) FROM core_mail_queue WHERE model = 'account.dunning_run'") or 0),
    }
    return result


def score(results: list[dict[str, Any]]) -> dict[str, float]:
    """``amount_correctness``: share of final drafts in which every number is a ledger fact (1.0 required);
    ``guard_accuracy``: probe cases (a replayed draft with a wrong figure) where exactly the wrong figures were
    removed; ``tone_match``: the drafted tone is the level's."""
    ok = [r for r in results if r.get("status") == "ok" and isinstance(r.get("data"), dict)]
    if not ok:
        return {}
    probes = [r for r in ok if "removed" in r["_case"]["expected"]]
    out = {
        "amount_correctness": sum(1 for r in ok if not r["data"]["left_ungrounded"]) / len(ok),
        "tone_match": sum(1 for r in ok if r["data"]["tone"] == r["_case"]["expected"].get("tone")) / len(ok),
        "mail_before_accept": float(sum(r["data"]["mail_queued"] for r in ok)),
    }
    if probes:
        out["guard_accuracy"] = sum(
            1 for r in probes if sorted(r["data"]["removed_numbers"]) == sorted(r["_case"]["expected"]["removed"])
        ) / len(probes)
    return out
