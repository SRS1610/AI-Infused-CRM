"""CRM acceptance tests (1.4-f … 1.4-m, 1.4-o)."""

from __future__ import annotations

import datetime as dt
import itertools
from collections import Counter
from typing import Any

import pytest

from modules.crm import reports
from modules.crm.services import rescore_job
from tessera.exceptions import AccessError, UserError

pytestmark = pytest.mark.db


@pytest.fixture
def team4(env: Any) -> Any:
    users = env["core.user"]
    group = env.ref("crm.group_crm_user").ids[0]
    members = [users.create({"login": f"rr{i}", "name": f"Rep {i}", "group_ids": [("set", [group])]}) for i in range(4)]
    team = env["crm.team"].create({"name": "Round robin team", "assignment_mode": "round_robin"})
    for m in members:
        env["crm.team_member"].create({"team_id": team.ids[0], "user_id": m.ids[0]})
    return team, members


def test_round_robin_distributes_evenly(env: Any, team4: Any) -> None:
    """1.4-g: 100 leads over 4 members → 25 ± 1 each."""
    team, members = team4
    leads = env["crm.lead"].create(
        [{"name": f"RR lead {i}", "type": "lead", "team_id": team.ids[0]} for i in range(100)]
    )
    counts = Counter(lead.user_id.ids[0] for lead in leads)
    assert set(counts) == {m.ids[0] for m in members}
    assert all(24 <= c <= 26 for c in counts.values()), counts
    # one at a time (as intake does) gives the same balance
    for i in range(8):
        env["crm.lead"].create({"name": f"RR single {i}", "type": "lead", "team_id": team.ids[0]})
    singles = env["crm.lead"].search([("name", "like", "RR single")])
    assert sorted(Counter(lead.user_id.ids[0] for lead in singles).values()) == [2, 2, 2, 2]


def test_country_rule_routes_before_round_robin(env: Any, team4: Any) -> None:
    team, members = team4
    europe = env.ref("crm.team_europe")
    lead = env["crm.lead"].create(
        {
            "name": "Paris enquiry",
            "type": "lead",
            "team_id": team.ids[0],
            "country_id": env.ref("core.country_fr").ids[0],
        }
    )
    assert lead.team_id == europe and lead.user_id == env.ref("core.user_demo_fr")
    us_lead = env["crm.lead"].create(
        {
            "name": "Austin enquiry",
            "type": "lead",
            "team_id": team.ids[0],
            "country_id": env.ref("core.country_us").ids[0],
        }
    )
    assert us_lead.team_id == team and us_lead.user_id.ids[0] in [m.ids[0] for m in members]


def test_scoring_and_background_rescore(env: Any) -> None:
    """1.4-h: contributions per lead; changing a rule rescores open leads in a job."""
    lead = env["crm.lead"].create(
        {"name": "Score me", "partner_name": "Big Co", "phone": "+1 555", "expected_revenue": 50000}
    )
    assert lead.score == 50
    assert {f["rule"] for f in lead.score_factors} == {
        "Company known",
        "Expected revenue ≥ 20,000",
        "Phone number given",
    }
    rule = env.ref("crm.score_phone")
    rule.write({"points": 40})
    job = env["core.job"].elevated().search([("name", "=", "crm.rescore")], order="id desc", limit=1)
    assert job.state == "queued"  # recomputed in the background, not inline
    assert lead.score == 50
    out = rescore_job(env.elevated())
    env.cache.invalidate()
    assert lead.score == 80 and out["changed"] >= 1
    factor = next(f for f in lead.score_factors if f["rule"] == "Phone number given")
    assert factor["points"] == 40


def test_email_intake_creates_lead_with_contact_and_attachment(env: Any) -> None:
    """1.4-f (email): sender becomes the contact, body the description, attachments are kept."""
    from modules.chatter.inbound import process_email

    raw = (
        "From: Nora Quinn <nora@quinn-labs.example>\r\nTo: crm@tessera.local\r\nSubject: Need 3 robot cells\r\n"
        "Message-ID: <intake-1@quinn.example>\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=b\r\n\r\n"
        "--b\r\nContent-Type: text/plain\r\n\r\nWe plan a new line in Q3.\r\n"
        "--b\r\nContent-Type: application/pdf; name=spec.pdf\r\nContent-Disposition: attachment; filename=spec.pdf\r\n\r\n%PDF-1.4 spec\r\n--b--\r\n"
    )
    result = process_email(env, raw)
    assert result["status"] == "created" and result["model"] == "crm.lead"
    lead = env["crm.lead"].browse([result["res_id"]])
    assert lead.name == "Need 3 robot cells" and lead.type == "lead"
    assert lead.partner_id.email == "nora@quinn-labs.example" and lead.contact_name == "Nora Quinn"
    assert "new line in Q3" in lead.description
    assert lead.team_id == env.ref("crm.team_sales") and lead.user_id  # assigned by the team
    attachments = env["core.attachment"].elevated().search([("model", "=", "crm.lead"), ("res_id", "=", lead.ids[0])])
    assert attachments.mapped("name") == ["spec.pdf"]
    assert lead.intake_ids.channel == "email"


def test_convert_and_merge_into_existing(env: Any) -> None:
    """1.4-i: merge moves chatter, activities and attachments and archives the source."""
    target = env.ref("crm.opp_1")
    lead = env["crm.lead"].create(
        {
            "name": "Acme again",
            "type": "lead",
            "email_from": "jordan.lee@acme-robotics.example",
            "description": "<p>more arms</p>",
        }
    )
    lead.message_post("lead history")
    lead.activity_schedule(summary="call back about arms")
    env["core.attachment"].create({"name": "rfq.pdf", "raw": b"%PDF", "model": "crm.lead", "res_id": lead.ids[0]})
    assert target.ids[0] in [m["id"] for m in lead.action_find_matches()]
    out = lead.action_convert("none", merge_into_id=target.ids[0])
    env.cache.invalidate()
    assert out == {"opportunity_id": target.ids[0], "merged": True}
    assert not lead.active
    msgs = env["chatter.message"].elevated().search([("model", "=", "crm.lead"), ("res_id", "=", target.ids[0])])
    assert any("lead history" in (m.body or "") for m in msgs) and any("Merged" in (m.body or "") for m in msgs)
    acts = env["chatter.activity"].elevated().search([("model", "=", "crm.lead"), ("res_id", "=", target.ids[0])])
    assert "call back about arms" in acts.mapped("summary")
    assert env["core.attachment"].elevated().search([("name", "=", "rfq.pdf")]).res_id == target.ids[0]
    assert "more arms" in target.description


def test_convert_creates_company_and_contact(env: Any) -> None:
    lead = env["crm.lead"].create(
        {
            "name": "New logo",
            "type": "lead",
            "contact_name": "Rui Alves",
            "partner_name": "Alves Lda",
            "email_from": "rui@alves.example",
        }
    )
    lead.action_convert("create")
    assert lead.type == "opportunity" and lead.date_conversion
    assert (
        lead.partner_id.name == "Rui Alves"
        and lead.partner_id.parent_id.name == "Alves Lda"
        and lead.partner_id.parent_id.is_company
    )


def test_won_lost_restore(env: Any) -> None:
    opp = env.ref("crm.opp_3")
    opp.action_set_lost(env.ref("crm.lost_price").ids[0], "budget cut")
    assert not opp.active and opp.lost and opp.probability == 0 and opp.date_closed
    with pytest.raises(UserError):
        opp.action_set_won()
    opp.action_restore()
    assert opp.active and not opp.lost
    opp.action_set_won()
    assert opp.is_won and opp.probability == 100 and opp.stage_id == env.ref("crm.stage_won")


def test_stage_change_sets_probability(env: Any) -> None:
    opp = env["crm.lead"].create({"name": "Prob", "expected_revenue": 1000})
    assert opp.probability == 10 and opp.prorated_revenue == 100
    opp.write({"stage_id": env.ref("crm.stage_negotiation").ids[0]})
    assert opp.probability == 80 and opp.prorated_revenue == 800


def test_team_scoped_editing_and_audit(env: Any, env_for: Any) -> None:
    """1.4-j: another team's lead → AccessError and no audit row; a manager succeeds with before/after."""
    lead = env.ref("crm.opp_3")  # Sales team, owned by demo
    rep = env["core.user"].create(
        {"login": "europe-rep", "name": "Europe Rep", "group_ids": [("set", [env.ref("crm.group_crm_user").ids[0]])]}
    )
    env["crm.team_member"].create({"team_id": env.ref("crm.team_europe").ids[0], "user_id": rep.ids[0]})
    marie = env.with_user(rep.ids[0])  # same company, other team
    audits = env["core.audit_log"].elevated()
    before = audits.search_count([("model", "=", "crm.lead"), ("res_id", "=", lead.ids[0])])
    assert marie["crm.lead"].browse(lead.ids).name  # reading the pipeline is allowed
    with pytest.raises(AccessError):
        marie["crm.lead"].browse(lead.ids).write({"name": "hijacked"})
    assert audits.search_count([("model", "=", "crm.lead"), ("res_id", "=", lead.ids[0])]) == before
    env["crm.lead"].browse(lead.ids).write({"expected_revenue": 40000})  # admin is a CRM manager
    last = audits.search([("model", "=", "crm.lead"), ("res_id", "=", lead.ids[0])], order="id desc", limit=1)
    assert last.changes["expected_revenue"] == {"old": 36000.0, "new": 40000.0} or "expected_revenue" in str(
        last.changes
    )


def test_quotation_acceptance_wins_opportunity(env: Any) -> None:
    """1.4-m: quote from opportunity → send (tokenised link) → accept → opportunity won, both audited."""
    opp = env.ref("crm.opp_7")
    quote_id = opp.action_new_quotation()["quotation_id"]
    quote = env["sale.quotation"].browse([quote_id])
    env["sale.quotation_line"].create(
        {"quotation_id": quote_id, "product_id": env.ref("product.product_robot_arm").ids[0]}
    )
    assert quote.opportunity_id == opp and opp.quotation_count == 1
    link = quote.action_send()["portal_url"]
    assert "token=" in link
    pdf, mimetype, _ = env.ref("sale.report_quotation").render(quote.ids, "pdf")
    assert mimetype == "application/pdf" and pdf.startswith(b"%PDF")
    quote.accept("Sam Okafor", "sam.okafor@mail.example", "198.51.100.7")
    assert quote.state == "accepted" and opp.is_won and opp.probability == 100
    audits = env["core.audit_log"].elevated()
    assert audits.search_count([("model", "=", "sale.quotation"), ("res_id", "=", quote_id)]) >= 2
    assert audits.search_count([("model", "=", "crm.lead"), ("res_id", "=", opp.ids[0])]) >= 1


def test_forecast_matches_sql_to_the_cent(env: Any) -> None:
    """1.4-l"""
    rows = reports.forecast(env)
    sql = env.cr.fetchall(
        """SELECT date_trunc('month', date_deadline)::date, COUNT(*), SUM(expected_revenue),
                  SUM(ROUND((expected_revenue * probability / 100)::numeric, 2))
             FROM crm_lead WHERE type = 'opportunity' AND active AND NOT COALESCE(is_won, false)
         GROUP BY 1 ORDER BY 1 NULLS FIRST"""
    )
    by_month = {(r["month"] or "")[:10] or None: r for r in rows}
    for month, count, pipeline, weighted in sql:
        key = month.isoformat() if month else None
        assert by_month[key]["count"] == count
        assert by_month[key]["pipeline"] == pytest.approx(float(pipeline or 0), abs=0.005)
        assert by_month[key]["weighted"] == pytest.approx(float(weighted or 0), abs=0.005)


def test_reports_reconcile_with_read_group(env: Any) -> None:
    """1.4-o on the demo seed."""
    leads = env["crm.lead"].with_context(active_test=False)
    rates = {r["label"]: r for r in reports.win_rate(env, "user_id")}
    won = leads.read_group([("type", "=", "opportunity"), ("is_won", "=", True)], [], ["user_id"])
    for g in won:
        assert rates[g["user_id"]["display_name"]]["won"] == g["__count"]
    marie = rates["Marie Martin"]
    assert (marie["won"], marie["lost"], marie["win_rate"]) == (0, 1, 0.0)
    funnel = reports.funnel(env)
    open_groups = env["crm.lead"].read_group([("type", "=", "opportunity")], [], ["stage_id"])
    assert sum(r["count"] for r in funnel) == sum(g["__count"] for g in open_groups)
    assert funnel[0]["reached"] == sum(r["count"] for r in funnel) and funnel[0]["conversion"] == 100.0
    assert all(a["reached"] >= b["reached"] for a, b in itertools.pairwise(funnel))
    velocity = reports.sales_velocity(env)
    assert velocity["open_opportunities"] == env["crm.lead"].search_count(
        [("type", "=", "opportunity"), ("is_won", "=", False)]
    )
    assert velocity["win_rate"] == pytest.approx(100 * 2 / 3, abs=0.01)


def test_webform_and_intake_api(client, admin) -> None:  # type: ignore[no-untyped-def]
    """1.4-f (web form): public endpoint creates a lead and fires on_create automations."""
    from tessera.config import get_settings
    from tessera.orm.environment import SYSTEM_UID, environment_for

    url = get_settings().database_url
    with environment_for(url, SYSTEM_UID, su=True) as env:
        token = env.ref("crm.team_sales").webform_token
        auto = env["workflow.automation"].create(
            {
                "name": "tag web leads",
                "model_name": "crm.lead",
                "trigger": "on_create",
                "filter_domain": [["name", "like", "web enquiry"]],
                "actions": [{"type": "update", "values": {"priority": "'2'"}}],
            }
        )
        auto_id = auto.ids[0]
    try:
        assert client.options(f"/api/v1/crm/webform/{token}").headers["access-control-allow-origin"] == "*"
        assert client.get(f"/api/v1/crm/webform/{token}/embed.js").text.startswith("(function(){")
        bad = client.post("/api/v1/crm/webform/nope", json={"name": "x", "email": "x@y.z"})
        assert bad.status_code == 404
        resp = client.post(
            f"/api/v1/crm/webform/{token}",
            json={
                "name": "Wen Li",
                "email": "Wen@LI.example",
                "company": "Li Optics",
                "message": "<b>quote</b> please",
            },
        )
        assert resp.status_code == 201
        lead = admin.get(
            f"/api/v1/models/crm.lead/{resp.json()['lead_id']}",
            params={"fields": "name,email_from,priority,description,user_id,team_id"},
        ).json()
        assert lead["email_from"] == "wen@li.example" and lead["priority"] == "2" and "&lt;b&gt;" in lead["description"]
        assert lead["user_id"] is not None
        bot = client.post(
            f"/api/v1/crm/webform/{token}", json={"name": "bot", "email": "b@b.b", "website_url": "http://spam"}
        )
        assert bot.json() == {"ok": True}
        api_lead = admin.post(
            "/api/v1/crm/leads/intake", json={"name": "From Zapier", "email": "z@z.example", "source": "zapier"}
        )
        assert api_lead.status_code == 201
        assert client.post("/api/v1/crm/leads/intake", json={"name": "anon"}).status_code == 401
        assert admin.get("/api/v1/crm/reports/funnel").status_code == 200
        assert admin.get("/api/v1/crm/reports/win-rate", params={"group_by": "team_id"}).status_code == 200
        assert admin.get("/api/v1/crm/reports/velocity", params={"date_from": "2026-01-01"}).status_code == 200
    finally:
        with environment_for(url, SYSTEM_UID, su=True) as env:
            env["workflow.automation"].browse([auto_id]).unlink()


def test_manual_mode_team_does_not_assign(env: Any) -> None:
    team = env["crm.team"].create({"name": "Manual", "assignment_mode": "manual"})
    env["crm.team_member"].create({"team_id": team.ids[0], "user_id": env.ref("core.user_demo").ids[0]})
    lead = env["crm.lead"].create({"name": "Stay unassigned", "team_id": team.ids[0]})
    assert not lead.user_id


def test_dates(env: Any) -> None:
    opp = env["crm.lead"].create(
        {"name": "Dated", "date_deadline": (dt.date.today() + dt.timedelta(days=10)).isoformat()}
    )
    assert opp.date_last_stage_update


def test_schedule_meeting_from_lead(env: Any) -> None:
    opp = env.ref("crm.opp_1")
    event_id = opp.action_schedule_meeting("2026-12-03T15:00:00+00:00", 45)["event_id"]
    event = env["calendar.event"].browse([event_id])
    assert event.res_model == "crm.lead" and event.res_id == opp.ids[0] and event.duration == 0.75
    assert opp.partner_id in event.partner_ids and event.user_id == opp.user_id
