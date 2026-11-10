"""CRM services: assignment, scoring, conversion and merging."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tessera.exceptions import UserError
from tessera.jobs import job
from tessera.orm.domain import Domain

if TYPE_CHECKING:
    from tessera.orm.model import Model


# -- assignment ---------------------------------------------------------------------------------------


def _matches(records: Model, domain: Any) -> set[int]:
    if not records:
        return set()
    return set(
        records.with_context(active_test=False).search([("id", "in", records.ids), Domain(domain or []).node]).ids
    )


def apply_assignment(leads: Model) -> None:
    """Rules first (in sequence; a rule may route to a team and/or a salesperson), then the team's
    round robin for leads still without a salesperson (1.4-g)."""
    env = leads.env
    sys_leads = leads.elevated("lead assignment")
    rules = env["crm.assignment_rule"].elevated().search([("active", "=", True)], order="sequence, id")
    pending = sys_leads.filtered(lambda lead: not lead.user_id)
    for rule in rules:
        scope = pending.filtered(lambda lead, r=rule: not r.team_id or lead.team_id == r.team_id)
        hits = _matches(scope, rule.domain)
        if not hits:
            continue
        vals: dict[str, Any] = {}
        if rule.assign_team_id:
            vals["team_id"] = rule.assign_team_id.ids[0]
        if rule.assign_user_id:
            vals["user_id"] = rule.assign_user_id.ids[0]
        matched = pending.browse(sorted(hits))
        matched.with_context(crm_scoring=True, tracking_disable=True).write(vals)
        if rule.assign_user_id:
            pending = pending - matched
    for team in {lead.team_id.ids[0] for lead in pending if lead.team_id}:
        team_leads = pending.filtered(
            lambda lead, t=team: lead.team_id and lead.team_id.ids[0] == t and not lead.user_id
        )
        round_robin(env["crm.team"].elevated().browse([team]), team_leads)


def round_robin(team: Model, leads: Model) -> None:
    """Give each lead to the eligible member with the lowest assigned_count / weight (ties: least recently
    assigned). Member rows are locked so concurrent intakes cannot double-count."""
    if team.assignment_mode == "manual" or not leads:
        return
    env = team.env
    members = env["crm.team_member"].elevated().search([("team_id", "=", team.ids[0]), ("active", "=", True)])
    if not members:
        return
    env.cr.execute("SELECT id FROM crm_team_member WHERE id = ANY(:ids) FOR UPDATE", {"ids": list(members.ids)})
    env.cache.invalidate("crm.team_member")
    state = {
        m.ids[0]: {
            "count": m.assigned_count or 0,
            "last": m.last_assigned_at,
            "weight": max(m.assignment_weight or 1, 1),
            "rec": m,
        }
        for m in members
    }
    balanced = team.assignment_mode == "balanced"
    for lead in leads:
        eligible = [
            mid
            for mid, st in state.items()
            if not st["rec"].assignment_domain or lead.ids[0] in _matches(lead, st["rec"].assignment_domain)
        ]
        if not eligible:
            continue

        def key(mid: int) -> tuple[float, str]:
            st = state[mid]
            load = st["count"] / st["weight"] if balanced else st["count"]
            return (load, st["last"].isoformat() if st["last"] else "")

        chosen = min(eligible, key=key)
        now = env.now()
        lead.with_context(crm_scoring=True).write({"user_id": state[chosen]["rec"].user_id.ids[0]})
        state[chosen]["count"] += 1
        state[chosen]["last"] = now
    for st in state.values():
        st["rec"].write({"assigned_count": st["count"], "last_assigned_at": st["last"]})


# -- scoring --------------------------------------------------------------------------------------------


def compute_scores(leads: Model) -> None:
    """score = sum of the points of every active rule whose domain the lead matches; score_factors explains it."""
    if not leads:
        return
    env = leads.env
    sys_leads = leads.elevated("lead scoring").with_context(active_test=False)
    rules = env["crm.scoring_rule"].elevated().search([("active", "=", True)], order="sequence, id")
    factors: dict[int, list[dict[str, Any]]] = {lid: [] for lid in sys_leads.ids}
    for rule in rules:
        companies = None if not rule.company_id else rule.company_id.ids[0]
        scope = (
            sys_leads if companies is None else sys_leads.filtered(lambda lead, c=companies: lead.company_id.ids == [c])
        )
        for lid in _matches(scope, rule.domain):
            factors[lid].append({"rule": rule.name, "rule_id": rule.ids[0], "points": rule.points})
    for lead in sys_leads:
        items = factors.get(lead.ids[0], [])
        score = sum(f["points"] for f in items)
        if lead.score != score or (lead.score_factors or []) != items:
            lead.with_context(crm_scoring=True, tracking_disable=True).write({"score": score, "score_factors": items})


@job("crm.rescore", max_attempts=2, as_system=True, timeout_seconds=1800)
def rescore_job(env: Any, batch: int = 1000) -> dict[str, int]:
    """Recompute the score of every open lead (after a scoring rule changed)."""
    leads = env["crm.lead"].with_context(active_test=True)
    ids = leads.search([("is_won", "=", False)]).ids
    changed = 0
    for start in range(0, len(ids), batch):
        chunk = leads.browse(ids[start : start + batch])
        before = {lead.ids[0]: lead.score for lead in chunk}
        compute_scores(chunk)
        changed += sum(1 for lead in chunk if lead.score != before[lead.ids[0]])
        env.flush()
    return {"leads": len(ids), "changed": changed}


# -- conversion and merging -----------------------------------------------------------------------------


def convert_lead(lead: Model, partner: Model, user_id: int | None, team_id: int | None) -> None:
    vals: dict[str, Any] = {"type": "opportunity", "date_conversion": lead.env.now()}
    if partner:
        vals["partner_id"] = partner.ids[0]
    if user_id:
        vals["user_id"] = user_id
    if team_id:
        vals["team_id"] = team_id
    lead.write(vals)


GENERIC_TABLES = ["chatter_message", "chatter_activity", "core_attachment"]


def merge_leads(target: Model, sources: Model) -> None:
    """1.4-i: move chatter, activities, attachments and followers of ``sources`` onto ``target``, merge the
    notes, tags and missing contact data, keep the highest expected revenue, then archive the sources."""
    target.ensure_one()
    sources = sources - target
    if not sources:
        raise UserError("Nothing to merge")
    (target | sources).check_access("write")
    env = target.env
    cr = env.cr
    ids = list(sources.ids)
    tid = target.ids[0]
    for table in GENERIC_TABLES:
        cr.execute(
            f"UPDATE {table} SET res_id = :t WHERE model = 'crm.lead' AND res_id = ANY(:ids)", {"t": tid, "ids": ids}
        )
    cr.execute(
        """UPDATE chatter_follower f SET res_id = :t WHERE model = 'crm.lead' AND res_id = ANY(:ids)
           AND NOT EXISTS (SELECT 1 FROM chatter_follower g
                           WHERE g.model = 'crm.lead' AND g.res_id = :t AND g.user_id = f.user_id)""",
        {"t": tid, "ids": ids},
    )
    cr.execute("DELETE FROM chatter_follower WHERE model = 'crm.lead' AND res_id = ANY(:ids)", {"ids": ids})
    cr.execute("UPDATE crm_lead_intake SET lead_id = :t WHERE lead_id = ANY(:ids)", {"t": tid, "ids": ids})
    cr.execute("UPDATE sale_quotation SET opportunity_id = :t WHERE opportunity_id = ANY(:ids)", {"t": tid, "ids": ids})
    env.cache.invalidate()
    sys_target = target.elevated("merge leads")
    vals: dict[str, Any] = {}
    notes = [sys_target.description or ""]
    for src in sources.elevated():
        if src.description:
            notes.append(f"<hr/><p><b>{src.name}</b></p>{src.description}")
        for fname in (
            "partner_id",
            "email_from",
            "phone",
            "mobile",
            "contact_name",
            "partner_name",
            "website",
            "city",
            "country_id",
            "date_deadline",
        ):
            if not sys_target[fname] and src[fname] and fname not in vals:
                value = src[fname]
                vals[fname] = value.ids[0] if hasattr(value, "_ids") else value
    tags = set(sys_target.tag_ids.ids)
    for src in sources.elevated():
        tags |= set(src.tag_ids.ids)
    vals["tag_ids"] = [("set", sorted(tags))]
    vals["description"] = "".join(notes)
    best = max([sys_target.expected_revenue or 0, *[s.expected_revenue or 0 for s in sources.elevated()]])
    if best != (sys_target.expected_revenue or 0):
        vals["expected_revenue"] = best
    target.write(vals)
    names = ", ".join(sources.elevated().mapped("name"))
    sources.with_context(tracking_disable=True).write({"active": False})
    from modules.chatter.thread import post_message

    post_message(
        env, "crm.lead", tid, f"<p>Merged {names} into this opportunity.</p>", message_type="notification", notify=False
    )
