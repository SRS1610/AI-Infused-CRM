"""Asset AI features (sub-phase 2.6, BUILD_PLAN §3).

``account_asset.classify`` — **asset classification suggestions**: which asset model a vendor bill line
belongs to (computers, vehicles, furniture…). The candidates are the company's asset models
(:func:`modules.account_asset.services.asset_model_candidates`); a deterministic scorer combines a
multilingual keyword lexicon (each model is mapped to a category by its own name), the words of the
company's earlier assets of each model (history) and the line's account. The small-tier model is asked only
when the two best models are too close; it must answer with one of the offered models. The suggestion is an
``action`` proposal (``account_asset.apply_model``) that creates the draft asset from the line with that
model — or sets the model on the line's draft asset — when the user accepts it.

``account_asset.depreciation_check`` — **depreciation anomaly checks** (flags, no write): the board's own
consistency checks (:func:`modules.account_asset.services.board_checks`) plus checks against the asset's
model and its peers (useful life or method different from the model's, unusual salvage value, entries far
above the board's usual amount, acquisition in the future, drafts left unconfirmed, a value different from
the bill line it came from). The optional explanation runs the model on these flags only.
"""

from __future__ import annotations

import datetime as dt
import itertools
import re
import statistics
import time
import unicodedata
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends

from modules.account_asset.services import asset_model_candidates, asset_profile, board_checks
from tessera.ai import Source
from tessera.ai.errors import AIError
from tessera.ai.facts import audit_run, ensure_enabled, ground_text
from tessera.ai.proposals import action
from tessera.ai.runtime import HANDLERS, handler, run_feature
from tessera.ai.tools import PendingProposal
from tessera.api.deps import RequestContext, authenticated
from tessera.api.registry import register_router
from tessera.exceptions import AccessError, MissingError, UserError
from tessera.i18n import _

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

CLASSIFY = "account_asset.classify"
CHECK = "account_asset.depreciation_check"
APPLY = "account_asset.apply_model"
MARGIN = 0.15
MIN_SCORE = 0.2

# fmt: off
# category → (words naming the category in an asset model's name, words of the things it holds)
LEXICON: dict[str, tuple[set[str], set[str]]] = {
    "computers": (
        {"computer", "computers", "it", "informatique", "ordinateurs", "edv", "hardware", "ict"},
        {
            "laptop", "notebook", "desktop", "pc", "server", "servers", "monitor", "screen", "printer", "macbook",
            "thinkpad", "latitude", "elitebook", "imac", "ipad", "tablet", "workstation", "router", "switch",
            "firewall", "nas", "storage", "ordinateur", "portable", "serveur", "ecran", "imprimante", "rechner",
            "drucker", "bildschirm", "dell", "lenovo", "hp", "asus", "surface", "chromebook", "dock", "docking",
            "ssd", "gpu", "rack",
        },
    ),
    "vehicles": (
        {"vehicle", "vehicles", "vehicules", "fahrzeuge", "fleet", "transport", "cars"},
        {
            "car", "van", "truck", "lorry", "pickup", "trailer", "scooter", "motorbike", "motorcycle", "ebike",
            "transit", "sprinter", "vivaro", "kangoo", "golf", "corolla", "model", "tesla", "renault", "peugeot",
            "citroen", "toyota", "ford", "volkswagen", "vw", "mercedes", "bmw", "audi", "skoda", "voiture",
            "camion", "camionnette", "fourgon", "utilitaire", "auto", "lkw", "pkw", "transporter", "wagen",
            "electric", "hybrid",
        },
    ),
    "furniture": (
        {"furniture", "fixtures", "mobilier", "moebel", "mobel", "fittings"},
        {
            "desk", "desks", "chair", "chairs", "table", "tables", "cabinet", "cabinets", "shelf", "shelves",
            "shelving", "sofa", "couch", "bookcase", "cupboard", "locker", "lockers", "workbench", "partition",
            "bureau", "chaise", "chaises", "armoire", "etagere", "canape", "schreibtisch", "stuhl", "stuhle",
            "schrank", "regal", "tisch", "ergonomic", "oak", "meeting", "reception", "counter", "standing",
        },
    ),
    "machinery": (
        {"machinery", "machines", "machine", "equipment", "plant", "industrial", "materiel", "maschinen", "outillage"},
        {
            "lathe", "cnc", "press", "compressor", "milling", "welding", "welder", "conveyor", "forklift",
            "generator", "pump", "boiler", "drill", "saw", "crane", "hoist", "robot", "packaging", "mixer",
            "oven", "kiln", "injection", "moulding", "tour", "fraiseuse", "presse", "chariot", "elevateur",
            "drehmaschine", "gabelstapler", "kompressor", "fraese", "excavator", "loader",
        },
    ),
    "software": (
        {"software", "licences", "licenses", "logiciels", "intangible", "intangibles"},
        {
            "licence", "license", "perpetual", "erp", "crm", "cad", "software", "logiciel", "lizenz", "suite",
            "development", "platform", "website", "app", "application", "patent", "trademark",
        },
    ),
    "buildings": (
        {"buildings", "building", "property", "real", "estate", "batiments", "gebaude", "premises", "leasehold"},
        {
            "building", "warehouse", "roof", "renovation", "construction", "extension", "office", "floor",
            "facade", "hvac", "elevator", "lift", "batiment", "entrepot", "toiture", "gebaude", "halle", "dach",
            "fitout", "refurbishment",
        },
    ),
}
# fmt: on
STOP = {"the", "and", "for", "with", "of", "de", "du", "la", "le", "des", "et", "pour", "und", "fur", "mit", "x"}


def words(text: str | None) -> list[str]:
    folded = unicodedata.normalize("NFKD", (text or "").lower()).encode("ascii", "ignore").decode()
    return [w for w in re.findall(r"[a-z][a-z0-9]+", folded) if w not in STOP]


def model_category(name: str) -> str | None:
    tokens = set(words(name))
    best, hits = None, 0
    for cat, (names, items) in LEXICON.items():
        n = len(tokens & names) * 2 + len(tokens & items)
        if n > hits:
            best, hits = cat, n
    return best


def _history(env: Environment, company_id: int) -> dict[int, set[str]]:
    """Words of the company's earlier assets, per asset model."""
    out: dict[int, set[str]] = {}
    assets = (
        env["account.asset"]
        .elevated()
        .search(
            [
                ("company_id", "=", company_id),
                ("state", "in", ["draft", "open", "paused", "close"]),
                ("model_id", "!=", None),
            ],
            limit=500,
            order="id desc",
        )
    )
    for a in assets:
        out.setdefault(a.model_id.ids[0], set()).update(words(a.name))
    return out


def classify_text(env: Environment, text: str, company: Model, account: Model | None = None) -> dict[str, Any]:
    """Ranked asset models for a line text (deterministic)."""
    candidates = asset_model_candidates(env, company)
    tokens = words(text)
    history = _history(env, company.ids[0])
    ranked = []
    for c in candidates:
        cat = model_category(c["name"])
        own = set(words(c["name"]))
        lexicon = LEXICON[cat][1] | LEXICON[cat][0] if cat else set()
        hits = [t for t in tokens if t in lexicon or t in own]
        past = [t for t in tokens if t in history.get(c["model_id"], set())]
        score = min(1.0, 0.35 * len(set(hits)) + 0.2 * len(set(past) - set(hits)))
        if account is not None and c.get("asset_account") and account.ids[0] == c["asset_account"]["id"]:
            score = min(1.0, score + 0.05)
        reasons = []
        if hits:
            reasons.append({"code": "keywords", "words": sorted(set(hits))})
        if past:
            reasons.append({"code": "history", "words": sorted(set(past))})
        ranked.append({**c, "category": cat, "score": round(score, 4), "reasons": reasons})
    ranked.sort(key=lambda r: (-r["score"], r["model_id"]))
    ambiguous = len(ranked) > 1 and ranked[0]["score"] - ranked[1]["score"] < MARGIN and ranked[0]["score"] > 0
    unknown = not ranked or ranked[0]["score"] < MIN_SCORE
    return {"ranked": ranked, "ambiguous": ambiguous or (unknown and bool(tokens) and len(ranked) > 1)}


def _line_text(line: Model) -> str:
    rec = line.elevated()
    parts = [rec.name or "", rec.product_id.name if rec.product_id else ""]
    return " ".join(p for p in parts if p)


def _readable_line(env: Environment, line: Model) -> Model:
    line.check_access("read")
    move = line.move_id
    if move.move_type not in ("in_invoice", "in_refund") or line.display_type != "product":
        raise UserError(_("Asset classification works on vendor bill lines"))
    return line


def _ask_model(env: Environment, line: Model, text: str, ranked: list[dict[str, Any]]) -> tuple[int | None, Any]:
    offered = ranked[:4]
    catalogue = "\n".join(
        f"- model {r['model_id']}: {r['name']} ({r['method']}, {r['method_number']} x "
        f"{r['method_period_months']} months)"
        for r in offered
    )
    sources = [
        Source(
            f"account.move_line:{line.ids[0]}",
            "Bill line",
            f"Bill line: {text}\nAmount: {abs(float(line.elevated().balance or 0)):.2f}",
        ),
        Source(f"account.asset:{offered[0]['model_id']}", "Asset models", f"Asset models you may choose:\n{catalogue}"),
    ]
    result = run_feature(env, CLASSIFY, {}, record=line, sources=sources)
    data = result.data if isinstance(result.data, dict) else {}
    choice = data.get("model_id")
    if isinstance(choice, int) and not isinstance(choice, bool) and any(r["model_id"] == choice for r in offered):
        return choice, result
    return None, result


def suggest_model(env: Environment, line: Model, *, propose: bool = True) -> dict[str, Any]:
    """Suggested asset model for a vendor bill line, and an action proposal applying it."""
    ensure_enabled(env, CLASSIFY)
    _readable_line(env, line)
    started = time.monotonic()
    rec = line.elevated()
    text = _line_text(line)
    info = classify_text(env, text, rec.move_id.company_id, rec.account_id)
    ranked = info["ranked"]
    flags: list[str] = []
    model_run = None
    used_model = False
    if info["ambiguous"]:
        try:
            choice, model_run = _ask_model(env, line, text, ranked)
            used_model = True
            if choice is None:
                flags.append("model_choice_rejected")
            else:
                ranked.sort(key=lambda r: (r["model_id"] != choice, -r["score"]))
                ranked[0]["reasons"].append({"code": "model_choice"})
        except AIError as exc:
            flags.append(exc.code)
    best = (
        ranked[0]
        if ranked and (ranked[0]["score"] >= MIN_SCORE or (used_model and "model_choice_rejected" not in flags))
        else None
    )
    run = (
        model_run.run
        if model_run is not None
        else audit_run(
            env,
            CLASSIFY,
            record=line,
            inputs={"text": text},
            output=ranked[:3],
            model="lexicon + history",
            started=started,
        )
    )
    proposal = None
    if propose and best is not None and _can_create(env):
        proposal = _propose(env, run, line, best)
    return {
        "run_id": run.ids[0],
        "line_id": line.ids[0],
        "text": text,
        "suggestion": best,
        "ranked": ranked[:5],
        "used_model": used_model,
        "flags": flags,
        "proposal": proposal,
        "ai_generated": True,
    }


def _can_create(env: Environment) -> bool:
    return bool(env["account.asset"].check_access_rights("create", raise_exception=False))


def _propose(env: Environment, run: Model, line: Model, best: dict[str, Any]) -> dict[str, Any]:
    from tessera.ai import proposals as P
    from tessera.ai.runtime import proposal_dict

    rec = line.elevated()
    title = f"Classify '{(rec.name or '')[:60]}' as {best['name']}"
    p = P.create(
        env,
        run,
        CLASSIFY,
        PendingProposal(
            "action",
            "account.move_line",
            line.ids[0],
            {"action": APPLY, "args": {"model_id": best["model_id"]}, "model": best["name"], "score": best["score"]},
            title,
        ),
    )
    run.elevated().write({"decision": "pending"})
    return proposal_dict(p)


@action(APPLY)
def apply_model(env: Environment, model: str, res_id: int | None, args: dict[str, Any]) -> Any:
    """Accepting: set the model on the line's draft asset, or create the draft asset from the line."""
    from modules.account_asset.models.asset import SETTING_FIELDS

    line = env["account.move_line"].browse([int(res_id or 0)]).exists()
    if not line:
        raise MissingError(_("The bill line no longer exists"))
    line.check_access("read")
    asset_model = env["account.asset"].browse([int(args["model_id"])]).exists()
    if not asset_model or asset_model.state != "model":
        raise MissingError(_("The asset model no longer exists"))
    existing = line.elevated().asset_ids.filtered(lambda a: a.state == "draft")
    if existing:
        assets = env["account.asset"].browse(existing.ids)
        assets.check_access("write")
        vals: dict[str, Any] = {"model_id": asset_model.ids[0]}
        for name in SETTING_FIELDS:
            value = asset_model[name]
            if value is None or value is False or name == "account_asset_id":
                continue
            vals[name] = value.ids[0] if hasattr(value, "ids") and value else value
        assets.write({k: v for k, v in vals.items() if not (hasattr(v, "ids") and not v)})
        return assets.ids
    if line.elevated().asset_ids:
        raise UserError(_("This bill line already has an asset"))
    if line.parent_state != "posted":
        raise UserError(_("Post the bill first: assets are created from posted bill lines"))
    env["account.asset"].check_access_rights("create")
    return env["account.asset"]._create_from_lines(line, asset_model).ids


# -- depreciation anomaly checks -----------------------------------------------------------------------

LIFE_RATIO = 1.5
SALVAGE_SHARE = 0.5
SPIKE_RATIO = 2.5
STALE_DAYS = 90
# usual useful life per asset type, in months (inclusive bounds); the type comes from the asset model's name,
# else from the asset's own name (same lexicon as the classifier)
LIFE_RANGE: dict[str, tuple[int, int]] = {
    "computers": (24, 72),
    "vehicles": (36, 120),
    "furniture": (60, 180),
    "machinery": (36, 240),
    "software": (12, 120),
    "buildings": (120, 600),
}
# types whose value is consumed evenly: a declining board is unusual for them
STRAIGHT_LINE_TYPES = {"buildings", "software"}


def asset_category(a: Model) -> str | None:
    """The asset type (lexicon category) of an asset: its model's name first, then its own name."""
    if a.model_id:
        cat = model_category(a.model_id.name or "")
        if cat:
            return cat
    return model_category(a.name or "")


def _period_key(day: dt.date, months: int) -> tuple[int, int]:
    return (day.year, day.month) if months < 12 else (day.year, 0)


def _months_between(a: dt.date, b: dt.date) -> int:
    return (b.year - a.year) * 12 + b.month - a.month


def posting_issues(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Missing periods (a gap of more than one period between two entries) and duplicated periods (two
    depreciation entries in the same month, or the same year for yearly boards)."""
    months = int(profile["method_period_months"] or 12)
    entries = [e for e in profile["board"] if e["state"] != "cancel" and not e["value_change"]]
    issues: list[dict[str, Any]] = []
    seen: dict[tuple[int, int], dict[str, Any]] = {}
    for e in entries:
        key = _period_key(dt.date.fromisoformat(e["date"]), months)
        if key in seen:
            issues.append(
                {"code": "duplicate_posting", "move_id": e["move_id"], "date": e["date"], "other": seen[key]["move_id"]}
            )
        else:
            seen[key] = e
    dates = sorted({dt.date.fromisoformat(e["date"]) for e in entries})
    for prev, nxt in itertools.pairwise(dates):
        gap = _months_between(prev, nxt)
        if gap > months * 1.5:
            issues.append({"code": "missing_posting", "after": prev.isoformat(), "before": nxt.isoformat()})
    return issues


def _peers(asset: Model) -> list[Model]:
    a = asset.elevated()
    if not a.model_id:
        return []
    return list(
        a.env["account.asset"].search(
            [
                ("model_id", "=", a.model_id.ids[0]),
                ("id", "!=", a.ids[0]),
                ("state", "in", ["open", "paused", "close"]),
            ],
            limit=50,
        )
    )


def anomaly_checks(asset: Model, today: dt.date | None = None) -> list[dict[str, Any]]:
    """Every anomaly of an asset: board consistency plus model, peer and value checks (deterministic)."""
    a = asset.elevated("asset checks")
    today = today or asset.env.today()
    if a.state == "model":
        return []
    profile = asset_profile(asset)
    issues = [{**i, "source": "board"} for i in board_checks(asset, today)]
    months = int(a.method_period or 12) * int(a.method_number or 0)
    model = a.model_id
    if model:
        model_months = int(model.method_period or 12) * int(model.method_number or 0)
        if model_months and months and max(months, model_months) / min(months, model_months) >= LIFE_RATIO:
            issues.append({"code": "life_differs_from_model", "months": months, "model_months": model_months})
        if model.method and a.method != model.method:
            issues.append({"code": "method_differs_from_model", "method": a.method, "model_method": model.method})
    category = asset_category(a)
    if category and months:
        low, high = LIFE_RANGE[category]
        if not low <= months <= high:
            issues.append({"code": "life_out_of_range", "months": months, "type": category, "usual": [low, high]})
    if category in STRAIGHT_LINE_TYPES and a.method != "linear":
        issues.append({"code": "method_unusual_for_type", "method": a.method, "type": category})
    issues += posting_issues(profile)
    peers = _peers(asset)
    peer_months = [int(p.method_period or 12) * int(p.method_number or 0) for p in peers]
    if len(peer_months) >= 3 and months:
        median = statistics.median(peer_months)
        if (
            median
            and max(months, median) / min(months, median) >= LIFE_RATIO
            and not any(i["code"] == "life_differs_from_model" for i in issues)
        ):
            issues.append({"code": "life_differs_from_peers", "months": months, "peer_median_months": median})
    original = float(a.original_value or 0)
    if original and float(a.salvage_value or 0) > SALVAGE_SHARE * original:
        issues.append({"code": "salvage_high", "salvage": float(a.salvage_value), "original": original})
    if a.acquisition_date and a.acquisition_date > today:
        issues.append({"code": "acquisition_in_future", "date": a.acquisition_date.isoformat()})
    if a.state == "draft" and a.acquisition_date and (today - a.acquisition_date).days > STALE_DAYS:
        issues.append({"code": "draft_not_confirmed", "date": a.acquisition_date.isoformat()})
    bill = sum(abs(float(line.balance or 0)) for line in a.original_move_line_ids)
    if a.original_move_line_ids and original and abs(bill - original) > max(0.01 * bill, 1.0):
        issues.append({"code": "value_differs_from_bill", "bill": round(bill, 2), "original": original})
    amounts = [e["amount"] for e in profile["board"] if e["state"] != "cancel" and not e["value_change"]]
    inner = amounts[1:-1] if len(amounts) > 3 else amounts
    if len(inner) >= 3 and a.method == "linear":  # declining boards vary by design
        usual = statistics.median(inner)
        for e in profile["board"]:
            if e["state"] != "cancel" and usual > 0 and e["amount"] > SPIKE_RATIO * usual:
                issues.append({"code": "amount_spike", "move_id": e["move_id"], "amount": e["amount"], "usual": usual})
    return issues


def message(code: str) -> str:
    messages = {
        "board_total": _("The depreciation entries do not add up to the depreciable value"),
        "negative_amount": _("A depreciation entry is negative"),
        "overdue_draft": _("A depreciation entry dated in the past is still a draft"),
        "draft_before_posted": _("A draft entry is dated before a posted one"),
        "below_salvage": _("The book value is below the salvage value"),
        "life_differs_from_model": _("The useful life differs a lot from the asset model's"),
        "method_differs_from_model": _("The depreciation method is not the asset model's"),
        "life_differs_from_peers": _("The useful life differs a lot from other assets of the same model"),
        "salvage_high": _("The salvage value is more than half of the original value"),
        "acquisition_in_future": _("The acquisition date is in the future"),
        "draft_not_confirmed": _("The asset was acquired months ago and is still a draft"),
        "value_differs_from_bill": _("The original value differs from the bill line it came from"),
        "amount_spike": _("A depreciation entry is far above the board's usual amount"),
        "life_out_of_range": _("The useful life is outside the usual range for this type of asset"),
        "method_unusual_for_type": _("A declining method is unusual for this type of asset"),
        "missing_posting": _("A depreciation period has no entry"),
        "duplicate_posting": _("Two depreciation entries fall in the same period"),
    }
    return str(messages.get(code, code))


ERRORS = {"board_total", "negative_amount", "duplicate_posting", "missing_posting", "below_salvage", "amount_spike"}


def severity(code: str) -> str:
    return "error" if code in ERRORS else "warning"


def check_asset(env: Environment, asset: Model, today: dt.date | None = None) -> dict[str, Any]:
    """The anomalies of one asset (flags: nothing is written but the audit run)."""
    ensure_enabled(env, CHECK)
    asset.check_access("read")
    started = time.monotonic()
    issues = anomaly_checks(asset, today)
    for i in issues:
        i["message"] = message(i["code"])
        i["severity"] = severity(i["code"])
    issues.sort(key=lambda i: (i["severity"] != "error", i["code"]))
    run = audit_run(
        env, CHECK, record=asset, output=issues, model="board checks + type, model and peer comparison", started=started
    )
    return {
        "run_id": run.ids[0],
        "asset_id": asset.ids[0],
        "flagged": bool(issues),
        "issues": issues,
        "ai_generated": True,
    }


def scan(env: Environment, limit: int = 200) -> dict[str, Any]:
    """Every running, paused or draft asset of the user's current company that has a finding."""
    ensure_enabled(env, CHECK)
    started = time.monotonic()
    company = env.company.ids[0]
    assets = env["account.asset"].search(
        [("company_id", "=", company), ("state", "in", ["draft", "open", "paused"])], order="id", limit=limit
    )
    rows = []
    for asset in assets:
        issues = anomaly_checks(asset)
        if issues:
            codes = sorted({i["code"] for i in issues})
            rows.append(
                {
                    "asset_id": asset.ids[0],
                    "name": asset.name,
                    "codes": codes,
                    "messages": [message(c) for c in codes],
                    "severity": "error" if any(severity(c) == "error" for c in codes) else "warning",
                }
            )
    rows.sort(key=lambda r: (r["severity"] != "error", r["name"]))
    run = audit_run(env, CHECK, inputs={"scan": True}, output=rows, model="asset scan", started=started)
    return {"run_id": run.ids[0], "checked": len(assets), "rows": rows, "ai_generated": True}


def _explain_facts(p: dict[str, Any], issues: list[dict[str, Any]]) -> dict[str, Any]:
    return {"profile": {k: v for k, v in p.items() if k not in ("name", "model", "company")}, "issues": issues}


@handler(CHECK)
def explain_context(env: Environment, record: Model | None, inputs: dict[str, Any]) -> dict[str, Any]:
    """Optional explanation of the flags: the model sees the asset's settings and the findings only, and every
    number it writes must be one of theirs (sentences with other figures are removed)."""
    if record is None or record._name != "account.asset":
        raise UserError(_("Choose an asset"))
    record.check_access("read")
    p = asset_profile(record)
    issues = anomaly_checks(record)
    text = "\n".join(
        [
            f"Asset: {p['name']} ({p['state']}), model {p['model'] or '-'}",
            f"Method {p['method']}, {p['method_number']} x {p['method_period_months']} months, "
            f"acquired {p['acquisition_date']}",
            f"Original value {p['original_value']:.2f}, salvage {p['salvage_value']:.2f}, "
            f"book value {p['book_value']:.2f} {p['currency']}",
            "Findings:",
            *[f"- {message(i['code'])}: {i}" for i in issues],
        ]
    )

    return {"sources": [Source(f"account.asset:{record.ids[0]}", p["name"], text)]}


def explain(env: Environment, asset: Model) -> dict[str, Any]:
    """Plain-language explanation of an asset's findings (numbers checked against the asset's facts)."""
    context = HANDLERS[CHECK](env, asset, {})  # checks read access
    p = asset_profile(asset)
    issues = anomaly_checks(asset)
    result = run_feature(env, CHECK, {}, record=asset, **context)
    clean, removed = ground_text(result.text, _explain_facts(p, issues))
    return {"run_id": result.run.ids[0], "text": clean, "removed_numbers": removed, "ai_generated": True}


# -- API --------------------------------------------------------------------------------------------------

router = register_router(APIRouter(prefix="/account_asset/ai", tags=["account_asset", "ai"]))


@router.post("/lines/{line_id}/classify")
def classify_line(line_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    ensure_enabled(ctx.env, CLASSIFY)
    line = ctx.env["account.move_line"].browse([line_id])
    if not line.exists():
        raise MissingError(_("No such bill line"))
    return suggest_model(ctx.env, line)


@router.post("/bills/{move_id}/classify")
def classify_bill(move_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """Suggestions for every product line of a vendor bill that has no asset yet."""
    ensure_enabled(ctx.env, CLASSIFY)
    move = ctx.env["account.move"].browse([move_id])
    if not move.exists():
        raise MissingError(_("No such bill"))
    move.check_access("read")
    rows = []
    for line in move.elevated().invoice_line_ids.filtered(lambda r: r.display_type == "product" and not r.asset_ids):
        try:
            rows.append(suggest_model(ctx.env, ctx.env["account.move_line"].browse(line.ids)))
        except (AccessError, UserError) as exc:
            rows.append({"line_id": line.ids[0], "error": exc.message})
    return {"rows": rows, "ai_generated": True}


def _asset(ctx: RequestContext, asset_id: int) -> Model:
    asset = ctx.env["account.asset"].browse([asset_id])
    if not asset.exists():
        raise MissingError(_("No such asset"))
    return asset


@router.get("/assets/{asset_id}/check")
def check(asset_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """The depreciation findings of an asset (flags; nothing is written)."""
    ensure_enabled(ctx.env, CHECK)
    return check_asset(ctx.env, _asset(ctx, asset_id))


@router.post("/assets/{asset_id}/explain")
def explain_asset(asset_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """A plain-language explanation of the findings (figures checked against the asset)."""
    ensure_enabled(ctx.env, CHECK)
    return explain(ctx.env, _asset(ctx, asset_id))


@router.get("/scan")
def scan_assets(ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """Assets of the current company with depreciation findings."""
    ensure_enabled(ctx.env, CHECK)
    return scan(ctx.env)
