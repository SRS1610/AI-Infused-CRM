"""Invoice and bill reading with auto-coding (``account.bill_ocr``, acceptance 2-i).

:func:`read_document` turns an uploaded PDF or image into **one proposal** that creates the draft bill
(or customer invoice, or credit note), with every auto-coded line in it; nothing is written before a person
accepts it:

1. a digital PDF's text layer is read deterministically (:mod:`modules.account.ai_ocr`); when that
   reading is incomplete or does not add up, the text goes to the model, and a scan or an image-only PDF
   goes to the vision tier as an image (task class ``vision``) — the gateway receives the page, replayed
   from cassettes in CI;
2. the partner is matched among the partners the user can read (VAT ID, IBAN, name); an unknown one is
   proposed for creation with the bill;
3. each line is coded to an account and taxes (:mod:`modules.account.ai_coding`);
4. totals are re-checked: the lines must add up to the untaxed amount, the taxes computed by the tax
   engine on the coded lines to the printed tax, and both to the total — otherwise the proposal says
   **needs review** with each difference, and the draft is created "to check";
5. anomaly checks (:mod:`modules.account.ai_anomaly`) run on the reading before the bill exists.

Numbers a model read are compared with the document's text layer when it has one (ungrounded amounts make
the proposal "needs review"). Every reading is audited in ``ai_core.run``, the deterministic ones too.
"""

from __future__ import annotations

import base64
import contextvars
import datetime as dt
from typing import TYPE_CHECKING, Any

from modules.account import ai_anomaly as A
from modules.account import ai_coding as K
from modules.account import ai_ocr as O
from modules.account.models.move import INVOICE_TYPES
from tessera.ai import Source
from tessera.ai import proposals as P
from tessera.ai.runtime import check_enabled, get_feature, proposal_dict, run_feature
from tessera.ai.tools import PendingProposal, ToolContext
from tessera.exceptions import AccessError, UserError, ValidationError
from tessera.i18n import _

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

FEATURE = "account.bill_ocr"
ACTION = "account.bill_from_document"
MAX_BYTES = 15 * 1024 * 1024
MEDIA_TYPES = ("application/pdf", "image/png", "image/jpeg", "image/webp", "image/gif")
MOVE_FIELDS = {
    "move_type",
    "partner_id",
    "ref",
    "invoice_date",
    "invoice_date_due",
    "currency_id",
    "to_check",
    "payment_reference",
    "invoice_line_ids",
}
LINE_FIELDS = {"name", "quantity", "price_unit", "account_id", "tax_ids", "product_id"}
TOLERANCE = 0.02
_created: contextvars.ContextVar[int | None] = contextvars.ContextVar("account_bill_created", default=None)


def media_type(data: bytes, declared: str | None) -> str:
    """The real type of an upload (magic bytes win over what the browser said)."""
    if data.startswith(b"%PDF"):
        return "application/pdf"
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"GIF8":
        return "image/gif"
    return (declared or "application/octet-stream").split(";")[0].strip().lower()


def _company(env: Environment) -> Model:
    return env.company or env.ref("core.main_company")


def _direction(ex: O.Extraction, requested: str | None) -> tuple[str, list[str]]:
    warnings: list[str] = []
    sale = ex.seller_is_company
    refund = ex.document_type == "credit_note"
    detected = ("out" if sale else "in") + ("_refund" if refund else "_invoice")
    if requested and requested in INVOICE_TYPES and requested != detected:
        warnings.append(
            _(
                "The document looks like a {detected}; it is read as a {requested} as asked.",
                detected=detected,
                requested=requested,
            )
        )
        return requested, warnings
    return detected, warnings


def _country(env: Environment, ex: O.Extraction, partner: dict[str, Any]) -> str | None:
    """The partner's country: from its record when matched, else from its VAT ID prefix."""
    if partner.get("id"):
        p = env["contacts.partner"].browse([partner["id"]])
        if p.country_id:
            return str(p.country_id.code)
    vat = O.norm_vat(ex.partner_vat)
    if vat[:2].isalpha():
        return {"EL": "GR", "XI": "GB"}.get(vat[:2], vat[:2])
    return None


def _tax_total(env: Environment, coded: list[dict[str, Any]], currency: Model | None, refund: bool) -> float:
    total = 0.0
    for ln in coded:
        if not ln["tax_ids"]:
            continue
        taxes = env["account.tax"].browse(ln["tax_ids"])
        res = taxes.compute_all(ln["unit_price"], currency, ln["quantity"], is_refund=refund)
        total += sum(t["amount"] for t in res["taxes"])
    return round(total, 2)


def review(
    env: Environment,
    ex: O.Extraction,
    *,
    move_type: str | None = None,
    target: Model | None = None,
    attachment_id: int | None = None,
    ungrounded: list[float] | None = None,
) -> tuple[dict[str, Any], PendingProposal | None]:
    """Match, code and check one reading; returns the review shown to the user and the proposal."""
    company = _company(env)
    mtype, warnings = _direction(ex, move_type)
    partner = K.match_partner(env, ex)
    country = _country(env, ex, partner)
    coder = K.Coder(env, mtype, partner["id"], reverse_charge=ex.reverse_charge, partner_country=country)
    coded = [coder.code(line) for line in ex.lines]
    currency = company.elevated().currency_id
    if ex.currency:
        found = env["core.currency"].with_context(active_test=False).search([("name", "=", ex.currency)], limit=1)
        if found:
            currency = found
        else:
            warnings.append(_("Unknown currency {code}: the company currency is used", code=ex.currency))
    issues: list[str] = []
    for key in ex.missing():
        issues.append(_("Not found on the document: {field}", field=key))
    for ln in coded:
        if ln["issue"]:
            issues.append(f"{ln['description']}: {ln['issue']}")
        if not ln["account_id"]:
            issues.append(_("No account for {line}", line=ln["description"]))
    if ungrounded:
        issues.append(
            _("Amounts not found in the document's text: {amounts}", amounts=", ".join(f"{a:,.2f}" for a in ungrounded))
        )
    # totals re-check: lines → untaxed, computed taxes → printed tax, both → total
    lines_sum = round(sum(ln["amount"] for ln in coded), 2)
    computed_tax = _tax_total(env, coded, currency, mtype.endswith("refund"))
    differences = []
    if ex.amount_untaxed is not None and abs(lines_sum - ex.amount_untaxed) > TOLERANCE:
        differences.append({"check": "lines_vs_untaxed", "document": ex.amount_untaxed, "computed": lines_sum,
                            "difference": round(lines_sum - ex.amount_untaxed, 2)})  # fmt: skip
    if ex.amount_tax is not None and abs(computed_tax - ex.amount_tax) > TOLERANCE:
        differences.append({"check": "tax", "document": ex.amount_tax, "computed": computed_tax,
                            "difference": round(computed_tax - ex.amount_tax, 2)})  # fmt: skip
    if ex.amount_total is not None and abs(lines_sum + computed_tax - ex.amount_total) > TOLERANCE:
        total = round(lines_sum + computed_tax, 2)
        differences.append({"check": "total", "document": ex.amount_total, "computed": total,
                            "difference": round(total - ex.amount_total, 2)})  # fmt: skip
    needs_review = bool(differences or issues)
    # anomalies of the bill-to-be (vendor bills only)
    anomalies: list[dict[str, Any]] = []
    if mtype.startswith("in_") and A.feature_enabled(env) and ex.invoice_date and ex.amount_total is not None:
        bill = A.Bill(None, str(partner["id"] or K.name_key(ex.partner_name or "")), ex.invoice_number or "",
                      ex.invoice_date, abs(ex.amount_total), ex.text, "")  # fmt: skip
        anomalies = [f.to_dict() for f in A.detect_for(env, bill, partner["id"], company.ids[0], ex.iban)]
    data = {
        "method": ex.method,
        "move_type": mtype,
        "extraction": ex.to_dict(),
        "partner": partner,
        "lines": coded,
        "currency": currency.name,
        "checks": {"lines_total": lines_sum, "computed_tax": computed_tax, "differences": differences},
        "needs_review": needs_review,
        "issues": issues,
        "warnings": warnings,
        "anomalies": anomalies,
        "ai_generated": True,
    }
    if not coded:
        return data, None
    values: dict[str, Any] = {
        "move_type": mtype,
        "ref": ex.invoice_number,
        "invoice_date": ex.invoice_date.isoformat() if ex.invoice_date else None,
        "invoice_date_due": ex.due_date.isoformat() if ex.due_date else None,
        "currency_id": currency.ids[0],
        "to_check": needs_review,
        "invoice_line_ids": [
            [
                "create",
                {
                    "name": ln["description"],
                    "quantity": ln["quantity"],
                    "price_unit": ln["unit_price"],
                    "account_id": ln["account_id"],
                    "tax_ids": [["set", ln["tax_ids"]]],
                    **({"product_id": ln["product_id"]} if ln["product_id"] else {}),
                },
            ]
            for ln in coded
        ],
    }
    if partner["id"]:
        values["partner_id"] = partner["id"]
    new_partner = None
    if not partner["id"] and (ex.partner_name or ex.partner_vat):
        new_partner = {"name": ex.partner_name or ex.partner_vat, "vat": ex.partner_vat, "iban": ex.iban}
    args = {
        "values": {k: v for k, v in values.items() if v is not None},
        "new_partner": new_partner,
        "attachment_id": attachment_id,
        "move_id": target.ids[0] if target is not None else None,
        "ocr_text": ex.text[:20000],
    }
    who = ex.partner_name or partner.get("display_name") or ""
    title = _("Create {kind} from {who} ({count} lines)", kind=mtype, who=who, count=len(coded))
    if target is not None:
        title = _("Fill {name} from the document ({count} lines)", name=target.display_name, count=len(coded))
    pending = PendingProposal("action", "account.move", target.ids[0] if target is not None else None,
                              {"action": ACTION, "args": args}, title)  # fmt: skip
    return data, pending


def _local_run(env: Environment, feature: Model, inputs: dict[str, Any], data: dict[str, Any], text_len: int) -> Model:
    """Audit row of a reading done without a model (the text layer was enough)."""
    from tessera.metrics import record_ai_run

    prompt = env["ai_core.prompt"].elevated().search([("name", "=", feature.prompt or feature.name)], limit=1)
    version = prompt.version_for(env.company.ids[0] if env.company else None, env.uid) if prompt else None
    run = (
        env["ai_core.run"]
        .elevated("ai run")
        .create(
            {
                "feature_id": feature.ids[0],
                "feature": feature.name,
                "user_id": env.uid,
                "company_id": env.company.ids[0] if env.company else None,
                "prompt_version": version.ref if version else None,
                "provider": "tessera",
                "model": "bill-parser",
                "status": "ok",
                "input_ref": {"inputs": inputs, "sources": []},
                "input_text": f"[document text layer: {text_len} characters, read without a model]",
                "output_data": data,
                "steps": 0,
                "cost": 0.0,
            }
        )
    )
    record_ai_run(feature.name, "ok", {})
    return run


def read_document(
    env: Environment,
    content: bytes,
    mimetype: str | None = None,
    filename: str = "document",
    *,
    move_type: str | None = None,
    target: Model | None = None,
    attachment_id: int | None = None,
) -> dict[str, Any]:
    """Read one document as ``env``'s user and propose the draft (see the module docstring)."""
    feature = get_feature(env, FEATURE)
    check_enabled(env, feature)
    env["account.move"].check_access("create")
    if len(content) > MAX_BYTES:
        raise ValidationError(_("The document is too large (15 MB at most)"))
    mt = media_type(content, mimetype)
    if mt not in MEDIA_TYPES:
        raise ValidationError(_("Upload a PDF or an image (PNG, JPEG, WebP)"))
    if target is not None:
        target.check_access("write")
        if target.state != "draft" or not target.is_invoice() or target.invoice_line_ids:
            raise UserError(_("Only an empty draft invoice or bill can be filled from a document"))
        move_type = target.move_type
    company = _company(env).elevated()
    text = O.text_layer(content) if mt == "application/pdf" else ""
    ex: O.Extraction | None = None
    if text:
        parsed = O.parse_text(text, company_names=[company.name or ""], company_vats=[company.vat or ""])
        if not parsed.missing() and parsed.consistent():
            ex = parsed
    inputs = {"move_type": move_type or "auto", "has_text": bool(text), "company_name": company.name or ""}
    out: dict[str, Any]
    if ex is not None:
        data, pending = review(env, ex, move_type=move_type, target=target, attachment_id=attachment_id)
        run = _local_run(env, feature, inputs, data, len(text))
        proposals = [P.create(env, run, FEATURE, pending)] if pending else []
        if proposals:
            run.elevated().write({"decision": "pending"})
        out = {"run_id": run.ids[0], "data": data, "proposals": [proposal_dict(p) for p in proposals], "status": "ok"}
    else:
        holder: dict[str, Any] = {}

        def on_output(ctx: ToolContext, answer: str, parsed: Any) -> None:
            read = O.from_model(parsed, method="model" if text else "vision", text=text)
            data, pending = review(
                ctx.env, read, move_type=move_type, target=target, attachment_id=attachment_id,
                ungrounded=O.grounding(read, text),
            )  # fmt: skip
            holder["data"] = data
            if isinstance(parsed, dict):
                parsed["review"] = data
            if pending:
                ctx.proposals.append(pending)

        sources = [Source(f"core.attachment:{attachment_id or 0}", filename, text)] if text else []
        attachments = (
            None if text else [{"name": filename, "media_type": mt, "data": base64.b64encode(content).decode()}]
        )
        result = run_feature(env, FEATURE, inputs, sources=sources, on_output=on_output, attachments=attachments)
        out = {
            "run_id": result.run.ids[0],
            "data": holder.get("data"),
            "proposals": [proposal_dict(p) for p in result.proposals],
            "status": result.run.status,
            "flags": result.flags,
        }
    out["ai_generated"] = True
    return out


# ----------------------------------------------------------------------------- acceptance


def _clean_values(env: Environment, values: dict[str, Any], *, create: bool) -> dict[str, Any]:
    vals = {k: v for k, v in values.items() if k in MOVE_FIELDS}
    if create and vals.get("move_type") not in INVOICE_TYPES:
        raise ValidationError(_("A document becomes an invoice, a bill or a credit note"))
    if not create:
        vals.pop("move_type", None)
    lines = []
    company = _company(env)
    for cmd in vals.get("invoice_line_ids") or []:
        if not (isinstance(cmd, list | tuple) and len(cmd) == 2 and cmd[0] == "create" and isinstance(cmd[1], dict)):
            raise ValidationError(_("Lines can only be created from a document"))
        line = {k: v for k, v in cmd[1].items() if k in LINE_FIELDS}
        if line.get("account_id"):
            acc = env["account.account"].browse([int(line["account_id"])])
            if not acc.exists() or acc.company_id.ids != company.ids:
                raise ValidationError(_("Account {id} does not belong to this company", id=line["account_id"]))
        taxes = line.get("tax_ids") or []
        ids = [int(i) for c in taxes if isinstance(c, list | tuple) and c and c[0] == "set" for i in c[1]]
        if ids:
            found = env["account.tax"].browse(ids)
            if any(t.company_id.ids != company.ids for t in found):
                raise ValidationError(_("A tax does not belong to this company"))
        line["tax_ids"] = [("set", ids)]
        lines.append(("create", line))
    vals["invoice_line_ids"] = lines
    for key in ("invoice_date", "invoice_date_due"):
        if vals.get(key):
            vals[key] = dt.date.fromisoformat(str(vals[key])[:10])
    return vals


@P.action(ACTION)
def bill_from_document(env: Environment, model: str, res_id: int | None, args: dict[str, Any]) -> int:
    """Accepting the proposal: create the partner when it is new, then the draft — as the accepting user."""
    target = env["account.move"].browse([res_id]) if res_id else None
    vals = _clean_values(env, dict(args.get("values") or {}), create=target is None)
    new = args.get("new_partner")
    if new and not vals.get("partner_id"):
        partner = env["contacts.partner"].create(
            {"name": str(new.get("name") or _("New vendor")), "vat": new.get("vat") or None, "is_company": True}
        )
        if new.get("iban") and O.iban_valid(str(new["iban"])):
            env["account.bank_account"].create({"partner_id": partner.ids[0], "acc_number": str(new["iban"])})
        vals["partner_id"] = partner.ids[0]
    text = str(args.get("ocr_text") or "")[:20000]
    if target is not None:
        if target.state != "draft" or target.invoice_line_ids:
            raise UserError(_("The draft was changed in the meantime: read the document again"))
        target.write({**vals, "ai_ocr_text": text or None})
        move = target
    else:
        move = env["account.move"].create({**vals, "ai_ocr_text": text or None})
    att_id = args.get("attachment_id")
    if att_id:
        att = env["core.attachment"].elevated("attach source document").browse([int(att_id)]).exists()
        if att and att.create_uid.ids == [env.uid] and not att.res_id:
            att.write({"model": "account.move", "res_id": move.ids[0]})
    _created.set(move.ids[0])
    return int(move.ids[0])


def accept(env: Environment, proposal: Model, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Accept (or edit then accept) a reading's proposal; returns the proposal and the created document."""
    p = proposal.elevated().exists()
    if not p or p.feature != FEATURE:
        raise AccessError(_("This is not a document-reading proposal"))
    token = _created.set(None)
    try:
        decided = P.accept(env, proposal, payload)
        move_id = _created.get()
    finally:
        _created.reset(token)
    if move_id and decided.state in ("accepted", "edited"):
        decided.elevated().write({"result_res_id": move_id})
    return {"proposal": proposal_dict(decided), "move_id": move_id if decided.state in ("accepted", "edited") else None}
