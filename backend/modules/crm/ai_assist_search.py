"""``crm.nl_search`` — a sentence such as "open deals over 10k in EMEA closing this quarter owned by me"
becomes a reviewable domain on ``crm.lead``.

The model sees the fields the user may read (with selection values and the names of stages, teams,
tags, sources and salespeople), a table of sales regions → country codes and pre-computed date ranges
(this month, quarter, year…), and answers with a list of conditions. The runtime then checks the answer
before anyone sees it: every path must exist and be readable by the user field by field, operators and
values must suit the field type and only the ``$uid`` / ``$today`` / ``$now`` variables are allowed; the
domain must compile. An answer failing any of this is rejected as invalid output. Nothing is applied:
the user reviews and edits the conditions and applies them as a filter in one click.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any

from modules.crm.ai_assist import LEAD
from tessera.ai import Source
from tessera.ai.errors import InvalidOutput
from tessera.ai.runtime import HANDLERS, RunResult, handler, run_feature
from tessera.ai.tools import ToolContext
from tessera.exceptions import AccessError, UserError
from tessera.i18n import _
from tessera.orm import fields as F
from tessera.orm.domain import And, Leaf, Not, Or, parse

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

FEATURE = "crm.nl_search"
FIXTURE_MARK = "[nlq] "
OPERATORS = {"=", "!=", ">", ">=", "<", "<=", "in", "not_in", "ilike", "not_ilike", "is_set", "is_not_set"}
VARIABLES = {"$uid", "$today", "$now"}
PROMPT_FIELDS = [
    "name", "type", "partner_id", "partner_name", "contact_name", "email_from", "phone", "function", "city",
    "country_id", "user_id", "team_id", "stage_id", "probability", "expected_revenue", "prorated_revenue",
    "date_deadline", "date_closed", "create_date", "priority", "tag_ids", "source_id", "medium_id", "campaign_id",
    "lost", "lost_reason_id", "active", "score",
]  # fmt: skip
REGIONS = {
    "EMEA": ["GB", "IE", "FR", "DE", "ES", "IT", "NL", "BE", "CH", "AT", "PT", "SE", "NO", "DK", "PL", "AE", "ZA"],
    "Europe": ["GB", "IE", "FR", "DE", "ES", "IT", "NL", "BE", "CH", "AT", "PT", "SE", "NO", "DK", "PL"],
    "DACH": ["DE", "AT", "CH"],
    "Benelux": ["BE", "NL"],
    "Nordics": ["SE", "NO", "DK"],
    "UK & Ireland": ["GB", "IE"],
    "Middle East & Africa": ["AE", "ZA"],
    "APAC": ["AU", "NZ", "IN", "SG", "JP", "CN", "HK"],
    "North America": ["US", "CA"],
    "LATAM": ["BR", "MX"],
    "Americas": ["US", "CA", "BR", "MX"],
}
NUMERIC = (F.Integer, F.Float, F.Monetary)


# ---------------------------------------------------------------------------------------------- prompt context


def _quarter(day: dt.date, shift: int = 0) -> tuple[dt.date, dt.date]:
    index = (day.year * 4 + (day.month - 1) // 3) + shift
    year, q = divmod(index, 4)
    start = dt.date(year, q * 3 + 1, 1)
    end_month = start.month + 2
    end = dt.date(year + (end_month // 13), (end_month - 1) % 12 + 1, 1)
    end = (end.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
    return start, end


def _month(day: dt.date, shift: int = 0) -> tuple[dt.date, dt.date]:
    index = day.year * 12 + day.month - 1 + shift
    year, month = divmod(index, 12)
    start = dt.date(year, month + 1, 1)
    end = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
    return start, end


def date_ranges(today: dt.date) -> dict[str, tuple[dt.date, dt.date]]:
    week = today - dt.timedelta(days=today.weekday())
    return {
        "this week": (week, week + dt.timedelta(days=6)),
        "next week": (week + dt.timedelta(days=7), week + dt.timedelta(days=13)),
        "this month": _month(today),
        "next month": _month(today, 1),
        "last month": _month(today, -1),
        "this quarter": _quarter(today),
        "next quarter": _quarter(today, 1),
        "last quarter": _quarter(today, -1),
        "this year": (dt.date(today.year, 1, 1), dt.date(today.year, 12, 31)),
        "next year": (dt.date(today.year + 1, 1, 1), dt.date(today.year + 1, 12, 31)),
        "last year": (dt.date(today.year - 1, 1, 1), dt.date(today.year - 1, 12, 31)),
    }


def _names(env: Environment, model: str, domain: list[Any] | None = None, limit: int = 40) -> list[str]:
    try:
        return [str(n) for n in env[model].search(domain or [], limit=limit).mapped("display_name")]
    except AccessError:
        return []


def field_options(env: Environment) -> dict[str, dict[str, Any]]:
    """Paths offered to the model and to the filter editor: label, type and allowed values."""
    lead = env[LEAD]
    out: dict[str, dict[str, Any]] = {}
    for name in PROMPT_FIELDS:
        f = lead._fields.get(name)
        if f is None or not lead._check_field_access(f, "read"):
            continue
        if f.comodel_name and not env[f.comodel_name].check_access_rights("read", raise_exception=False):
            continue
        info: dict[str, Any] = {"label": f.label, "type": f.type_name}
        if isinstance(f, F.Selection) and isinstance(f.selection, list | tuple):
            info["selection"] = [[k, v] for k, v in f.selection]
        if name == "user_id":
            info["hint"] = 'use "$uid" for the current user ("me", "my")'
        out[name] = info
    related = {
        "stage_id.name": ("Stage name", "char", _names(env, "crm.stage")),
        "stage_id.is_won": ("Stage is won", "boolean", None),
        "country_id.code": ("Country ISO code", "char", None),
        "user_id.name": ("Salesperson name", "char", _names(env, "core.user", [("share", "=", False)])),
        "team_id.name": ("Sales team name", "char", _names(env, "crm.team")),
        "tag_ids.name": ("Tag name", "char", _names(env, "crm.tag")),
        "source_id.name": ("Source name", "char", _names(env, "crm.source")),
        "lost_reason_id.name": ("Lost reason", "char", _names(env, "crm.lost_reason")),
        "partner_id.display_name": ("Customer name", "char", None),
    }
    for path, (label, typ, values) in related.items():
        if path.split(".")[0] in out:
            out[path] = {"label": label, "type": typ, **({"values": values} if values else {})}
    return out


def metadata_text(env: Environment) -> str:
    today = env.today()
    lines = [
        "Model: crm.lead (leads and opportunities).",
        '"Deals" and "opportunities" mean type = "opportunity"; "leads" means type = "lead".',
        '"Open" means not won: stage_id.is_won = false. Lost deals are archived: lost = true and active = false.',
        "",
        "Fields (path | label | type | values):",
    ]
    for path, info in field_options(env).items():
        values = info.get("values") or [k for k, _ in info.get("selection") or []]
        extra = f" | one of: {', '.join(map(str, values))}" if values else ""
        hint = f" | {info['hint']}" if info.get("hint") else ""
        lines.append(f"- {path} | {info['label']} | {info['type']}{extra}{hint}")
    lines += ["", "Sales regions (use country_id.code in [...]):"]
    lines += [f"- {region}: {', '.join(codes)}" for region, codes in REGIONS.items()]
    lines += ["", f"Today is {today.isoformat()} ({today.strftime('%A')}). Date ranges (inclusive):"]
    lines += [f"- {k}: {a.isoformat()} to {b.isoformat()}" for k, (a, b) in date_ranges(today).items()]
    lines += [
        "",
        'Variables: "$uid" is the current user, "$today" is today\'s date. Amounts such as "10k" mean 10000.',
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------- validation


def _leaves(node: Any) -> list[Leaf]:
    if isinstance(node, Leaf):
        return [node]
    if isinstance(node, And | Or):
        return [leaf for child in node.children for leaf in _leaves(child)]
    if isinstance(node, Not):
        return _leaves(node.child)
    return []


def _check_value(field: F.Field, leaf: Leaf) -> str | None:
    op, value = leaf.operator, leaf.value
    if op in ("is_set", "is_not_set"):
        return None
    values = value if isinstance(value, list) else [value]
    if op in ("in", "not_in") and not isinstance(value, list):
        return f"'{leaf.path}': '{op}' needs a list of values"
    if op not in ("in", "not_in") and isinstance(value, list):
        return f"'{leaf.path}': '{op}' needs a single value"
    for v in values:
        if isinstance(v, str) and v.startswith("$"):
            if v not in VARIABLES:
                return f"'{leaf.path}': unknown variable {v}"
            continue
        if field.comodel_name:
            if not isinstance(v, int | str) or isinstance(v, bool):
                return f"'{leaf.path}' needs a record id or name, got {v!r}"
            continue
        if isinstance(field, NUMERIC) and (not isinstance(v, int | float) or isinstance(v, bool)):
            return f"'{leaf.path}' is a number, got {v!r}"
        if isinstance(field, F.Boolean) and not isinstance(v, bool):
            return f"'{leaf.path}' is true or false, got {v!r}"
        if isinstance(field, F.Date | F.Datetime):
            try:
                dt.date.fromisoformat(str(v)[:10])
            except ValueError:
                return f"'{leaf.path}' is a date, got {v!r}"
        if (
            isinstance(field, F.Selection)
            and isinstance(field.selection, list | tuple)
            and v not in [k for k, _ in field.selection]
        ):
            return f"'{leaf.path}': {v!r} is not one of its values"
    return None


def validate_domain(env: Environment, domain: Any) -> list[str]:
    """Problems with ``domain`` for the requesting user (empty list: it can be applied as is)."""
    try:
        node = parse(domain)
    except UserError as exc:
        return [exc.message]
    errors: list[str] = []
    for leaf in _leaves(node):
        if leaf.operator not in OPERATORS:
            errors.append(f"'{leaf.path}': operator '{leaf.operator}' is not offered")
            continue
        rec: Model = env[LEAD]
        field: F.Field | None = None
        segments = leaf.path.split(".")
        for i, seg in enumerate(segments):
            if seg == "display_name" and i == len(segments) - 1 and i > 0:
                field = rec._fields.get("display_name") or F.Char()
                break
            field = rec._fields.get(seg)
            if field is None:
                errors.append(f"'{leaf.path}': '{seg}' is not a field of {rec._description}")
                break
            if not rec._check_field_access(field, "read"):
                errors.append(f"'{leaf.path}': you may not read '{seg}'")
                field = None
                break
            if isinstance(field, F.Binary | F.Json | F.Vector):
                errors.append(f"'{leaf.path}' cannot be searched")
                field = None
                break
            if field.comodel_name:
                comodel = env[field.comodel_name]
                if not comodel.check_access_rights("read", raise_exception=False):
                    errors.append(f"'{leaf.path}': you may not read {comodel._description}")
                    field = None
                    break
                rec = comodel
            elif i < len(segments) - 1:
                errors.append(f"'{leaf.path}': '{seg}' has no sub-fields")
                field = None
                break
        if field is not None and (problem := _check_value(field, leaf)):
            errors.append(problem)
    if not errors:
        try:
            with env.cr.savepoint():
                env[LEAD].search_count(domain)
        except Exception as exc:  # any compile or SQL error makes the domain unusable
            errors.append(f"The filter cannot be applied: {getattr(exc, 'message', None) or type(exc).__name__}")
    return errors


def to_domain(data: dict[str, Any]) -> list[Any] | dict[str, Any]:
    leaves: list[Any] = []
    for c in data.get("conditions") or []:
        if not isinstance(c, dict):
            continue
        if c.get("operator") in ("is_set", "is_not_set"):
            leaves.append([c.get("field"), c.get("operator")])
        else:
            leaves.append([c.get("field"), c.get("operator"), c.get("value")])
    if data.get("match") == "any" and len(leaves) > 1:
        return {"any": leaves}
    return leaves


def describe(options: dict[str, dict[str, Any]], leaf: list[Any]) -> str:
    path, op = leaf[0], leaf[1]
    label = options.get(path, {}).get("label") or path
    words = {"=": "is", "!=": "is not", ">": ">", ">=": "≥", "<": "<", "<=": "≤", "in": "is one of",
             "not_in": "is none of", "ilike": "contains", "not_ilike": "does not contain",
             "is_set": "is set", "is_not_set": "is not set"}  # fmt: skip
    value = "" if len(leaf) < 3 else leaf[2]
    shown = {"$uid": "me", "$today": "today"}.get(value, value) if isinstance(value, str) else value
    if isinstance(shown, list):
        shown = ", ".join(map(str, shown))
    return f"{label} {words.get(op, op)} {shown}".strip()


@handler(FEATURE)
def prepare(env: Environment, record: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    env[LEAD].check_access_rights("read")
    meta = Source(f"{LEAD}:0", "Searchable fields of leads and opportunities", metadata_text(env))

    def on_output(ctx: ToolContext, _text: str, data: Any) -> None:
        if not isinstance(data, dict):
            raise InvalidOutput(_("The AI answer was not a list of conditions"))
        domain = to_domain(data)
        errors = validate_domain(env, domain)
        if errors:
            raise InvalidOutput(_("The suggested filter cannot be used: {errors}", errors="; ".join(errors[:3])))
        options = field_options(env)
        leaves = domain["any"] if isinstance(domain, dict) else domain
        data["domain"] = domain
        data["labels"] = [describe(options, leaf) for leaf in leaves]
        data["fields"] = options

    return {"sources": [meta], "on_output": on_output}


# ---------------------------------------------------------------------------------------------- evaluation


def canonical(domain: Any) -> str:
    """Order-insensitive normal form (children of all/any and values of in/not_in sorted)."""

    def norm(node: Any) -> Any:
        if isinstance(node, Leaf):
            value = sorted(node.value, key=str) if isinstance(node.value, list) else node.value
            return [node.path, node.operator, value]
        if isinstance(node, And | Or):
            children = sorted((norm(c) for c in node.children), key=lambda x: json.dumps(x, sort_keys=True))
            if len(children) == 1:
                return children[0]
            return {"all" if isinstance(node, And) else "any": children}
        if isinstance(node, Not):
            return {"not": norm(node.child)}
        return node

    return json.dumps(norm(parse(domain)), sort_keys=True, default=str)


def matches(env: Environment, domain: Any) -> set[int]:
    """Ids of the evaluation fixture leads (names start with ``[nlq]``) selected by ``domain``."""
    leads = env[LEAD]
    return set(leads.search({"all": [["name", "starts_with", FIXTURE_MARK], domain]}).ids)


def eval_entry(env: Environment, inputs: dict[str, Any], record: Any) -> RunResult:
    """Run the feature, then compare the domain with the reference (the case's saved ``web.filter``):
    exact after normalisation, or equivalent — selecting the same fixture leads, as the same user."""
    result = run_feature(env, FEATURE, inputs, **HANDLERS[FEATURE](env, None, inputs))
    if record is None or record._name != "web.filter" or result.run.status != "ok":
        return result
    expected = record.elevated().domain
    got = result.data["domain"]
    expected_ids, got_ids = matches(env, expected), matches(env, got)
    result.data["_eval"] = {
        "exact": canonical(got) == canonical(expected),
        "equivalent": got_ids == expected_ids,
        "expected_count": len(expected_ids),
        "got_count": len(got_ids),
    }
    return result


def score(results: list[dict[str, Any]]) -> dict[str, float]:
    measured = [
        (r.get("data") or {}).get("_eval") if isinstance(r.get("data"), dict) else None
        for r in results
        if isinstance(r.get("_case", {}).get("record"), str)
    ]
    ok = [m for m in measured if m]
    total = len(measured)
    if not total:
        return {}
    return {
        "domain_match": sum(1 for m in ok if m["exact"] or m["equivalent"]) / total,
        "domain_exact": sum(1 for m in ok if m["exact"]) / total,
    }
