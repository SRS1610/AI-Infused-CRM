"""Calendar AI features (Phase 1.6): meeting brief, suggested meeting times, natural-language scheduling.

Context is built **as the requesting user**: the event, its attendees, the linked document, open
deals and earlier meetings are read through the user's environment, so a record the user cannot
read (another company's lead, a private meeting, a contact outside their companies) never reaches a
prompt. ``unauthorised_texts`` lists what the user must not see so tests and the evaluation can
prove it.

Evaluation notes (honesty): ``calendar.suggest_times`` is a deterministic search and is scored for
real by an independent validator. ``calendar.meeting_brief`` and ``calendar.nl_schedule`` call a
model; in CI their answers (and the judge's grades) are replayed from hand-written cassettes, so the
replay measures the pipeline — context building, citation checks, attendee resolution, time-zone
conversion, proposals — not model quality, which the nightly live run measures.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from modules.calendar import scheduling as S
from tessera.ai import Source
from tessera.ai.errors import AIError
from tessera.ai.prompts import html_to_text
from tessera.ai.runtime import HANDLERS, check_enabled, get_feature, handler, run_feature
from tessera.ai.tools import PendingProposal, ToolContext
from tessera.exceptions import AccessError, MissingError, UserError
from tessera.jobs import job
from tessera.logging import get_logger

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

log = get_logger(__name__)
BRIEF = "calendar.meeting_brief"
SUGGEST = "calendar.suggest_times"
NL = "calendar.nl_schedule"
LINKED_FIELDS = [
    "stage_id",
    "partner_id",
    "user_id",
    "expected_revenue",
    "probability",
    "date_deadline",
    "state",
    "amount_total",
    "description",
]


def feature_enabled(env: Environment, name: str) -> bool:
    if "ai_core.feature" not in env.registry.models:
        return False
    try:
        check_enabled(env, get_feature(env, name))
    except (AIError, AccessError, MissingError):
        return False
    return True


def _readable(rec: Model) -> bool:
    try:
        rec.check_access("read")
    except (AccessError, MissingError):
        return False
    return bool(rec.exists())


def _fmt(value: dt.datetime | None, tz: Any) -> str:
    return S.aware(value).astimezone(tz).strftime("%a %d %b %Y %H:%M") if value else ""


# -- meeting brief ----------------------------------------------------------------------------------


def _messages(env: Environment, model: str, res_id: int, limit: int = 3) -> list[str]:
    """Latest discussion of a record the user may read (same rule as the thread itself)."""
    from modules.chatter.thread import check_thread_access

    try:
        check_thread_access(env, model, res_id, "read")
    except (AccessError, MissingError):
        return []
    msgs = (
        env["chatter.message"]
        .elevated()
        .search(
            [("model", "=", model), ("res_id", "=", res_id), ("message_type", "in", ["comment", "note", "email"])],
            order="id desc",
            limit=limit,
        )
    )
    out = []
    for m in msgs:
        author = m.author_id.display_name if m.author_id else (m.email_from or "")
        out.append(f"{m.create_date:%d %b %Y} {author}: {html_to_text(m.body or '').strip()[:400]}")
    return out


def _record_text(rec: Model) -> str:
    lines = [f"{rec._description}: {rec.display_name}"]
    for name in LINKED_FIELDS:
        f = rec._fields.get(name)
        if f is None or not rec._check_field_access(f, "read"):
            continue
        try:
            value = rec[name]
        except AccessError:
            continue
        if hasattr(value, "_ids"):
            value = ", ".join(value.mapped("display_name")) if value else ""
        elif name == "description":
            value = html_to_text(value or "").strip()[:1000]
        if value not in (None, False, ""):
            lines.append(f"{f.label}: {value}")
    return "\n".join(lines)


def brief_sources(env: Environment, event: Model) -> list[Source]:
    """Everything the brief may use, read as ``env``'s user."""
    from modules.contacts.ai_features import partner_text

    event.check_access("read")
    tz = S.zone(env.user.elevated().tz)
    ev = event
    me = env.user.elevated().partner_id
    internal = set(env["core.user"].elevated().search([("share", "=", False)]).mapped("partner_id").ids)
    attendees, people = [], []
    for att in ev.attendee_ids:
        p = att.partner_id.with_env(env)
        if _readable(p):
            role = " (colleague)" if p.ids[0] in internal else ""
            attendees.append(f"- {p.display_name}{role}: {att.state.replace('_', ' ')}")
            if p != me:
                people.append(p)
        else:
            attendees.append("- an attendee whose contact you cannot see")
    lines = [
        f"Subject: {ev.name}",
        f"When: {_fmt(ev.start, tz)} - {_fmt(ev.stop, tz)[-5:]} ({tz.key})"
        if not ev.allday
        else f"When: {ev.start:%a %d %b %Y} (all day)",
    ]
    if ev.location:
        lines.append(f"Location: {ev.location}")
    if ev.room_id and _readable(ev.room_id):
        lines.append(f"Room: {ev.room_id.name}")
    if ev.user_id:
        lines.append(f"Organizer: {ev.user_id.elevated().name}")
    if ev.description:
        lines.append(f"Agenda / description: {html_to_text(ev.description).strip()[:1500]}")
    lines.append("Attendees:\n" + "\n".join(attendees))
    sources = []
    linked = None
    if ev.res_model and ev.res_id and ev.res_model in env.registry.models:
        rec = env[ev.res_model].browse([ev.res_id])
        if _readable(rec):
            linked = rec
            lines.append(f"Linked document: {rec.display_name}")
    sources.append(Source(f"calendar.event:{ev.ids[0]}", ev.name or "Meeting", "\n".join(lines)))
    if linked is not None:
        text = _record_text(linked)
        thread = _messages(env, linked._name, linked.ids[0])
        if thread:
            text += "\nLatest discussion:\n" + "\n".join(thread)
        sources.append(Source(f"{linked._name}:{linked.ids[0]}", linked.display_name or linked._name, text))
    external = [p for p in people if p.ids[0] not in internal]
    for p in external[:6]:
        text = partner_text(p)
        thread = _messages(env, "contacts.partner", p.ids[0], 2)
        if thread:
            text += "\nLatest notes:\n" + "\n".join(thread)
        sources.append(Source(f"contacts.partner:{p.ids[0]}", p.display_name or "", text))
    commercial = list(
        {(p.commercial_partner_id or p).ids[0] for p in external if p.commercial_partner_id or p.is_company}
    )
    if (
        commercial
        and "crm.lead" in env.registry.models
        and env["crm.lead"].check_access_rights("read", raise_exception=False)
    ):
        leads = env["crm.lead"].search(
            [("partner_id.commercial_partner_id", "in", commercial), ("lost", "=", False), ("is_won", "=", False)],
            order="expected_revenue desc",
            limit=3,
        )
        for lead in leads:
            if linked is not None and lead._name == linked._name and lead.ids[0] == linked.ids[0]:
                continue
            sources.append(Source(f"crm.lead:{lead.ids[0]}", lead.display_name or "", _record_text(lead)))
    if external:
        earlier = env["calendar.event"].search(
            [
                ("id", "!=", ev.ids[0]),
                ("partner_ids", "in", [p.ids[0] for p in external]),
                ("start", "<", ev.start.isoformat()),
            ],
            order="start desc",
            limit=2,
        )
        for old in earlier:
            text = f"Earlier meeting: {old.name} on {_fmt(old.start, tz)}"
            if old.description:
                text += f"\nNotes: {html_to_text(old.description).strip()[:800]}"
            sources.append(Source(f"calendar.event:{old.ids[0]}", old.name or "Meeting", text))
    return sources


@handler(BRIEF)
def meeting_brief_context(env: Environment, record: Model, inputs: dict[str, Any]) -> dict[str, Any]:
    if record is None or record._name != "calendar.event":
        raise UserError("A meeting brief is prepared for a meeting")
    return {"sources": brief_sources(env, record)}


def unauthorised_texts(env: Environment, event: Model) -> list[str]:
    """Texts of records around ``event`` that ``env``'s user may not read (names, descriptions, notes):
    none of them may ever appear in a brief's prompt or output."""
    sys_env = env.elevated("brief audit")
    ev = event.with_env(sys_env)
    candidates: list[Model] = [a.partner_id for a in ev.attendee_ids]
    if ev.res_model and ev.res_id and ev.res_model in sys_env.registry.models:
        candidates.append(sys_env[ev.res_model].browse([ev.res_id]))
    partners = ev.attendee_ids.mapped("partner_id")
    if "crm.lead" in sys_env.registry.models and partners:
        commercial = (partners.mapped("commercial_partner_id") | partners).ids
        candidates += list(sys_env["crm.lead"].search([("partner_id.commercial_partner_id", "in", commercial)]))
    if partners:
        candidates += list(
            sys_env["calendar.event"].search([("id", "!=", ev.ids[0]), ("partner_ids", "in", partners.ids)])
        )
    texts: list[str] = []
    for rec in candidates:
        if not rec.exists() or _readable(rec.with_env(env)):
            continue
        if rec.display_name and len(rec.display_name) >= 6:
            texts.append(rec.display_name)
        for name in ("description", "comment"):
            if name in rec._fields and rec[name]:
                body = html_to_text(rec[name])
                texts += [t.strip() for t in re.split(r"(?<=[.!?])\s+|\n", body) if len(t.strip()) >= 20]
    return sorted(set(texts))


def brief_entry(env: Environment, inputs: dict[str, Any], record: Model) -> Any:
    """Eval entry: the API path, then an audit — any unauthorised text in the prompt or the brief flags
    the case ``unauthorised_fact`` (the gate is zero)."""
    kwargs = HANDLERS[BRIEF](env, record, inputs)
    result = run_feature(env, BRIEF, inputs, record=record, **kwargs)
    hay = f"{result.run.elevated().input_text or ''}\n{result.text}".lower()
    if any(t.lower() in hay for t in unauthorised_texts(env, record)):
        result.flags.append("unauthorised_fact")
    return result


def brief_scorer(results: list[dict[str, Any]]) -> dict[str, float]:
    return {"unauthorised_facts": sum(1 for r in results if "unauthorised_fact" in (r.get("flags") or []))}


@job("calendar.prepare_meeting_briefs", queue="ai", max_attempts=1, as_system=True)
def prepare_meeting_briefs(env: Environment, lead_minutes: int = 60, window_minutes: int = 30) -> int:
    """Scheduled: brief every internal participant of meetings starting in about ``lead_minutes``,
    each as themselves, and notify them with a link (once per meeting and person)."""
    now = env.now()
    lo = now + dt.timedelta(minutes=lead_minutes - window_minutes // 2)
    hi = now + dt.timedelta(minutes=lead_minutes + window_minutes // 2)
    from modules.calendar.models.event import expand

    done = 0
    for occ in expand(env, lo, hi):
        start = dt.datetime.fromisoformat(occ["start"])
        if not lo <= start < hi:
            continue
        event = env["calendar.event"].browse([occ["id"]])
        users = env["core.user"].search(
            [
                ("share", "=", False),
                {
                    "any": [
                        ["partner_id", "in", event.partner_ids.ids],
                        ["id", "=", event.user_id.ids[0] if event.user_id else 0],
                    ]
                },
            ]
        )
        for user in users:
            already = env["ai_core.run"].search_count(
                [
                    ("feature", "=", BRIEF),
                    ("res_model", "=", "calendar.event"),
                    ("res_id", "=", event.ids[0]),
                    ("user_id", "=", user.ids[0]),
                    ("create_date", ">=", now - dt.timedelta(hours=3)),
                ]
            )
            if already:
                continue
            company = event.company_id.ids[0] if event.company_id else user.company_id.ids[0]
            uenv = env.with_user(user.ids[0])
            if company in uenv.companies.ids:
                uenv = uenv.with_company(company)
            mine = event.with_env(uenv)
            if not feature_enabled(uenv, BRIEF) or not _readable(mine):
                continue
            try:
                with env.cr.savepoint():
                    result = run_feature(uenv, BRIEF, {}, record=mine, **meeting_brief_context(uenv, mine, {}))
            except (AIError, UserError) as exc:
                log.warning("meeting brief failed", event=event.ids[0], user=user.ids[0], error=str(exc)[:200])
                continue
            summary = re.sub(r"\s*\[source:[^\]]+\]", "", result.text).strip()
            env["core.notification"].notify(
                [user.ids[0]],
                f"Meeting brief ready: {event.name}",
                summary[:240],
                kind="system",
                model="calendar.event",
                res_id=event.ids[0],
                url=f"/m/calendar.event/{event.ids[0]}",
            )
            done += 1
    return done


def latest_brief(env: Environment, event: Model, hours: int = 24) -> dict[str, Any] | None:
    """The current user's own most recent brief of ``event`` (prepared in the background or on demand)."""
    event.check_access("read")
    run = (
        env["ai_core.run"]
        .elevated()
        .search(
            [
                ("feature", "=", BRIEF),
                ("res_model", "=", "calendar.event"),
                ("res_id", "=", event.ids[0]),
                ("user_id", "=", env.uid),
                ("status", "=", "ok"),
                ("create_date", ">=", env.now() - dt.timedelta(hours=hours)),
            ],
            order="id desc",
            limit=1,
        )
    )
    if not run:
        return None
    return {
        "run_id": run.ids[0],
        "text": run.output_text or "",
        "created": run.create_date.isoformat() if run.create_date else None,
        "citations": [
            {"id": c.source, "res_model": c.res_model, "res_id": c.res_id, "title": c.title} for c in run.citation_ids
        ],
        "ai_generated": True,
    }


# -- suggested times -------------------------------------------------------------------------------


def suggest_for(env: Environment, inputs: dict[str, Any], record: Model | None) -> dict[str, Any]:
    """Inputs: ``partner_ids`` (else the meeting's attendees), constraints (see ``Constraints``)."""
    data = dict(inputs)
    pids = [int(p) for p in data.pop("partner_ids", None) or []]
    for uid in data.pop("user_ids", None) or []:  # internal users by user id
        partner = env["core.user"].elevated().browse([int(uid)]).partner_id
        if partner and partner.ids[0] not in pids:
            pids.append(partner.ids[0])
    if record is not None and record._name == "calendar.event":
        record.check_access("read")
        pids = pids or record.partner_ids.ids
        data.setdefault("duration_minutes", round((record.duration or 0.5) * 60))
        data.setdefault("exclude_event_id", record.ids[0])
        if record.room_id and "room_id" not in data:
            data["room_id"] = record.room_id.ids[0]
    me = env.user.elevated().partner_id
    if me and me.ids[0] not in pids:
        pids.insert(0, me.ids[0])
    return S.suggest_times(env, pids, S.Constraints.parse(env, data), limit=int(data.get("limit") or 5))


@handler(SUGGEST)
def suggest_times_context(env: Environment, record: Model, inputs: dict[str, Any]) -> dict[str, Any]:
    """Context for the optional explanation (the slots themselves are computed, not generated)."""
    found = suggest_for(env, inputs, record)
    text = "\n".join(f"{s['local']}: {'; '.join(s['reasons'])}" for s in found["slots"]) or "No free slot."
    sid = f"calendar.event:{record.ids[0]}" if record is not None else f"core.user:{env.uid}"
    return {"sources": [Source(sid, "Suggested times", text)]}


def validate_slot(env: Environment, partner_ids: list[int], c: S.Constraints, start: dt.datetime) -> list[str]:
    """Independent check of one slot (evaluation): returns the rules it breaks. Re-reads events with
    plain SQL and expands occurrences per event, rather than reusing the search's busy table."""
    tz = S.zone(c.tz)
    stop = start + dt.timedelta(minutes=c.duration_minutes)
    broken = []
    lo = max(S.aware(c.start) if c.start else env.now(), env.now() + dt.timedelta(minutes=c.min_notice_minutes))
    if start < lo or (c.end and stop > S.aware(c.end)):
        broken.append("outside window")
    ls, le = start.astimezone(tz), stop.astimezone(tz)
    day_start, day_end = dt.time.fromisoformat(c.day_start), dt.time.fromisoformat(c.day_end)
    if ls.weekday() not in c.weekdays or ls.date() != le.date() or ls.time() < day_start or le.time() > day_end:
        broken.append("outside working day")
    minutes = ls.hour * 60 + ls.minute - (day_start.hour * 60 + day_start.minute)
    if minutes % c.step_minutes:
        broken.append("not on the slot grid")
    if c.avoid_lunch and ls.time() < dt.time(13) and le.time() > dt.time(12):
        broken.append("lunch")
    if broken:
        return broken
    pad = dt.timedelta(minutes=c.buffer_minutes)
    sys_env = env.elevated("slot validation")
    rows = env.cr.fetchall(
        "SELECT DISTINCT e.id FROM calendar_event e "
        "LEFT JOIN calendar_event__partner_ids rel ON rel.event_id = e.id "
        "LEFT JOIN core_user u ON u.id = e.user_id "
        "WHERE e.active AND e.show_as = 'busy' AND (e.id != :ex) AND "
        "(rel.partner_id = ANY(:p) OR u.partner_id = ANY(:p) OR e.room_id = :room)",
        {"p": partner_ids, "room": c.room_id or 0, "ex": c.exclude_event_id or 0},
    )
    for (eid,) in rows:
        ev = sys_env["calendar.event"].browse([eid])
        involved = set(ev.partner_ids.ids) | (
            {ev.user_id.partner_id.ids[0]} if ev.user_id and ev.user_id.partner_id else set()
        )
        declined = set(ev.attendee_ids.filtered(lambda a: a.state == "declined").mapped("partner_id").ids)
        organizer = ev.user_id.partner_id.ids[0] if ev.user_id and ev.user_id.partner_id else None
        blocking = {p for p in involved & set(partner_ids) if p not in declined or p == organizer}
        if not blocking and not (c.room_id and ev.room_id.ids[:1] == [c.room_id]):
            continue
        for s, e in S.event_spans(ev, start - dt.timedelta(days=2), stop + dt.timedelta(days=2)):
            if s - pad < stop and start < e + pad:
                broken.append(f"busy: event {eid}")
                break
    if c.respect_attendee_hours:
        for pid, z in S.attendee_zones(env, partner_ids).items():
            if not S.in_working_hours(start, stop, z):
                broken.append(f"outside working hours of contact {pid}")
    return broken


def suggest_times_entry(env: Environment, inputs: dict[str, Any], record: Model | None) -> dict[str, Any]:
    """Eval entry: search, then validate the top three independently and check that "no slot" answers
    are right (by validating every grid start in the window)."""
    found = suggest_for(env, inputs, record)
    pids = list(found["searched"]["attendees"])
    data = dict(inputs)
    if record is not None:
        data.setdefault("duration_minutes", round((record.duration or 0.5) * 60))
        data.setdefault("exclude_event_id", record.ids[0])
        if record.room_id and "room_id" not in data:
            data["room_id"] = record.room_id.ids[0]
    data.pop("partner_ids", None)
    data.pop("user_ids", None)
    c = S.Constraints.parse(env, data)
    top = found["slots"][:3]
    valid = [not validate_slot(env, pids, c, dt.datetime.fromisoformat(s["start"])) for s in top]
    any_valid = any(valid)
    if not top:  # brute force: is there really no valid grid slot?
        cursor = S.aware(c.start) if c.start else env.now()
        cursor = cursor.replace(minute=0, second=0, microsecond=0)
        end = S.aware(c.end) if c.end else cursor + dt.timedelta(days=7)
        while cursor < end and not any_valid:
            any_valid = not validate_slot(env, pids, c, cursor)
            cursor += dt.timedelta(minutes=5)
    return {
        "text": "",
        "data": {
            "slots": [s["start"] for s in found["slots"]],
            "top3_valid": valid,
            "valid_exists": any_valid,
            "found": found["found"],
        },
        "proposals": 0,
        "status": "ok",
    }


def suggest_scorer(results: list[dict[str, Any]]) -> dict[str, float]:
    """``top3_valid``: share of cases whose top three contain a valid slot — or, when no valid slot
    exists, that correctly return none. ``all_top3_valid``: share of returned top-3 slots that are valid."""
    ok = total = slots = good = 0
    for r in results:
        d = r.get("data") or {}
        if r.get("status") != "ok":
            total += 1
            continue
        total += 1
        flags = d.get("top3_valid") or []
        slots += len(flags)
        good += sum(1 for f in flags if f)
        if flags:
            ok += 1 if any(flags) else 0
        else:
            ok += 0 if d.get("valid_exists") else 1
    return {"top3_valid": ok / total if total else 0.0, "all_top3_valid": good / slots if slots else 1.0}


# -- natural-language scheduling --------------------------------------------------------------------


def _fold(value: str) -> str:
    from modules.contacts.duplicates import fold

    return " ".join(fold(value).split())


def resolve_attendees(env: Environment, names: list[str]) -> tuple[list[Model], list[dict[str, Any]]]:
    """Match names (or e-mail addresses) to contacts the user can read. Ambiguous or unknown names stay
    unresolved — they are reported, never guessed."""
    from modules.contacts.models.partner import normalize_email

    partners = env["contacts.partner"]
    me = env.user.elevated().partner_id
    resolved: list[Model] = []
    unresolved: list[dict[str, Any]] = []
    for raw in names:
        q = (raw or "").strip()
        if not q or _fold(q) in ("me", "i", "myself", "us", "we"):
            continue
        if "@" in q:
            found = partners.search([("email_normalized", "=", normalize_email(q))], limit=5)
            pick = list(found)
        else:
            found = partners.search([("name", "ilike", q), ("is_company", "=", False)], limit=25)
            exact = [p for p in found if _fold(p.name or "") == _fold(q)]
            first = [p for p in found if (_fold(p.name or "").split() or [""])[0] == _fold(q)] if " " not in q else []
            pick = exact or first or (list(found) if len(found) == 1 else [])
            if not pick and len(found) > 1:
                pick = list(found)
            if not found:  # a misspelt name: trigram candidates, re-filtered as the user, then a close match
                pick = _close_matches(env, q)
        pick = [p for p in pick if not me or p.ids[0] != me.ids[0]]
        if len(pick) == 1:
            if all(r.ids[0] != pick[0].ids[0] for r in resolved):
                resolved.append(pick[0])
        elif me and not pick and found and all(p.ids[0] == me.ids[0] for p in found):
            continue  # the user named themselves
        else:
            unresolved.append({"name": q, "candidates": [{"id": p.ids[0], "name": p.display_name} for p in pick[:5]]})
    return resolved, unresolved


def _close_matches(env: Environment, name: str) -> list[Model]:
    from modules.contacts.duplicates import name_similarity

    rows = env.cr.fetchall(
        "SELECT id FROM contacts_partner WHERE active AND NOT is_company AND name IS NOT NULL "
        "AND similarity(name, :n) > 0.3 ORDER BY similarity(name, :n) DESC LIMIT 10",
        {"n": name},
    )
    visible = env["contacts.partner"].search([("id", "in", [r[0] for r in rows])]) if rows else []
    return [p for p in visible if name_similarity(p.name, name) >= 0.85]


@handler(NL)
def nl_schedule_context(env: Environment, record: Model | None, inputs: dict[str, Any]) -> dict[str, Any]:
    user = env.user.elevated()
    tz = S.zone(user.tz)
    today = dt.date.fromisoformat(inputs["today"]) if inputs.get("today") else dt.datetime.now(tz).date()
    context = (
        f"Today is {today:%A %d %B %Y} ({today.isoformat()}). The user's time zone is {tz.key}.\n"
        "Working hours: 09:00-17:00. Default length: 30 minutes; lunch and dinner: 60 minutes."
    )
    return {"sources": [Source(f"core.user:{env.uid}", "Scheduling context", context)], "on_output": _nl_output(tz)}


def _nl_output(tz: Any) -> Callable[[ToolContext, str, Any], None]:
    def hook_(ctx: ToolContext, text: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        env = ctx.env
        problems = []
        title = str(data.get("title") or "").strip()[:200] or "Meeting"
        try:
            day = dt.date.fromisoformat(str(data.get("date")))
        except ValueError:
            data.update(problems=["The date could not be understood"], payload=None)
            return
        all_day = bool(data.get("all_day")) or not data.get("start_time")
        duration = int(data.get("duration_minutes") or 30)
        if all_day:
            start = dt.datetime.combine(day, dt.time(), dt.UTC)
            stop = start
        else:
            try:
                at = dt.time.fromisoformat(str(data["start_time"]))
            except ValueError:
                data.update(problems=["The time could not be understood"], payload=None)
                return
            start = dt.datetime.combine(day, at, tz).astimezone(dt.UTC)
            stop = start + dt.timedelta(minutes=max(5, min(duration, 24 * 60)))
        resolved, unresolved = resolve_attendees(env, [str(a) for a in data.get("attendees") or []])
        if unresolved:
            problems.append("Some people could not be matched to a contact; add them yourself")
        location = str(data.get("location") or "").strip() or None
        payload: dict[str, Any] = {
            "name": title,
            "start": start.isoformat(),
            "stop": stop.isoformat(),
            "allday": all_day,
            "location": location,
            "attendee_ids": [{"partner_id": p.ids[0]} for p in resolved],
        }
        if data.get("notes"):
            payload["description"] = f"<p>{html.escape(str(data['notes']))}</p>"
        data.update(
            fields={
                "title": title,
                "start": day.isoformat() if all_day else start.isoformat(),
                "duration_minutes": None if all_day else int((stop - start).total_seconds() // 60),
                "location": location,
                "attendees": "; ".join(sorted(p.display_name or "" for p in resolved)) or None,
            },
            resolved=[{"id": p.ids[0], "name": p.display_name} for p in resolved],
            unresolved=unresolved,
            problems=problems,
            payload=payload,
        )
        when = f"{day:%a %d %b}" if all_day else start.astimezone(tz).strftime("%a %d %b %H:%M")
        ctx.proposals.append(
            PendingProposal("create", "calendar.event", None, payload, f"New meeting: {title}, {when}")
        )

    return hook_


def check_feature(env: Environment, name: str) -> None:
    """Raise ``FeatureDisabled`` (HTTP 404) when the feature is off for the user's company."""
    check_enabled(env, get_feature(env, name))
