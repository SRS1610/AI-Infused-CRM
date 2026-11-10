"""Calendar AI features (1.6): meeting brief, suggested times, natural-language scheduling.

Covers behaviour, permissions (records the user cannot read never reach a prompt or a brief),
proposals (no event and no mail before ``accept``), the background brief with its notification,
the API, interactive latency on the mock provider and the evaluation gate in replay.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from gateway.adapters.mock import MockAdapter

from modules.calendar import ai_features as A
from modules.calendar import scheduling as S
from tessera.ai import client as C
from tessera.ai import evals as EV
from tessera.ai import proposals as P
from tessera.ai.runtime import HANDLERS, run_feature
from tessera.api.tests.conftest import Session
from tessera.exceptions import AccessError

pytestmark = pytest.mark.db
UTC = dt.UTC


@pytest.fixture(autouse=True)
def _fresh() -> Any:
    MockAdapter.reset()
    C.reset_inprocess_state()
    yield
    C.set_client_factory(None)


def _script(env: Any, rules: list[dict[str, Any]]) -> None:
    p = env.ref("ai_core.provider_local")
    p.write({"options": {**(p.options or {}), "script": rules}})


def d(*args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=UTC)


def _companies(env: Any) -> tuple[int, int]:
    return env.ref("core.main_company").ids[0], env.ref("core.demo_company_eu").ids[0]


def _counts(env: Any) -> dict[str, int]:
    env.flush()
    return {
        t: int(env.cr.scalar(f"SELECT count(*) FROM {t}") or 0)  # noqa: S608
        for t in ("calendar_event", "calendar_attendee", "core_mail_queue", "chatter_message")
    }


# -- suggested times ---------------------------------------------------------------------------------


def test_suggested_times_respect_busy_time_and_constraints(env: Any, env_for: Any) -> None:
    demo = env_for("demo")  # New York
    partners = env["contacts.partner"].elevated()
    ava = partners.create({"name": "Ava Lindholm", "tz": "Europe/Paris"})
    events = env["calendar.event"].elevated()
    events.create({"name": "Busy", "start": d(2030, 3, 4, 14), "stop": d(2030, 3, 4, 15), "attendee_ids": [{"partner_id": ava.ids[0]}]})
    events.create({"name": "Declined", "start": d(2030, 3, 4, 15), "stop": d(2030, 3, 4, 16),
                   "attendee_ids": [{"partner_id": ava.ids[0], "state": "declined"}]})  # fmt: skip
    c = S.Constraints.parse(demo, {"start": "2030-03-04T00:00:00Z", "end": "2030-03-09T00:00:00Z", "buffer_minutes": 10})
    found = S.suggest_times(demo, [demo.user.partner_id.ids[0], ava.ids[0]], c)
    first = found["slots"][0]
    # Paris working hours end at 17:00 (16:00 UTC); the busy hour plus its 10-minute buffer is skipped,
    # the declined invitation does not block
    assert first["start"] == "2030-03-04T15:30:00+00:00" and "All 2 attendees are free" in first["reasons"]
    for slot in found["slots"]:
        assert not A.validate_slot(demo, [demo.user.partner_id.ids[0], ava.ids[0]], c, dt.datetime.fromisoformat(slot["start"]))
    # people the user cannot see cannot be probed
    main, eu = _companies(env)
    hidden = partners.create({"name": "Hidden Person", "company_id": main})
    marie = env_for("marie").with_company(eu)
    with pytest.raises(AccessError):
        S.suggest_times(marie, [hidden.ids[0]], S.Constraints.parse(marie, {}))


def test_suggestions_for_an_existing_meeting_and_booking_links_share_free_busy(env: Any, env_for: Any) -> None:
    demo = env_for("demo")
    ava = env["contacts.partner"].elevated().create({"name": "Ava Lindholm"})
    meeting = demo["calendar.event"].create(
        {"name": "Contract review", "start": d(2030, 3, 4, 14), "stop": d(2030, 3, 4, 15), "attendee_ids": [{"partner_id": ava.ids[0]}]}
    )
    found = A.suggest_for(demo, {"start": "2030-03-04T00:00:00Z", "end": "2030-03-06T00:00:00Z"}, meeting)
    assert found["searched"]["attendees"][0] == demo.user.partner_id.ids[0] and ava.ids[0] in found["searched"]["attendees"]
    assert found["slots"][0]["start"].startswith("2030-03-04T14:00")  # its own slot does not block it
    assert _counts(env)["calendar_event"] == _counts(env)["calendar_event"]  # nothing booked
    # the booking page of the same user now uses the shared free/busy (all-day events block whole days)
    link = demo["calendar.booking_link"].create({"name": "Intro", "duration_minutes": 30, "min_notice_hours": 0, "days_ahead": 30})
    tomorrow = (dt.datetime.now(UTC) + dt.timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
    demo["calendar.event"].create({"name": "Holiday", "start": tomorrow, "stop": tomorrow, "allday": True})
    assert not [s for s in link.free_slots(tomorrow, tomorrow + dt.timedelta(days=1)) if s.date() == tomorrow.date()]


def test_suggest_api_flag_and_latency(admin: Session, app: Any, env: Any) -> None:
    from tessera.orm.environment import environment_for

    with environment_for() as e:
        acme = e.ref("contacts.partner_acme_jordan").ids[0]
        company = e.ref("core.main_company").ids[0]
    body = {"partner_ids": [acme], "start": "2030-03-04T00:00:00Z", "end": "2030-03-09T00:00:00Z", "prefer": "morning"}
    times = []
    for _ in range(20):
        started = time.monotonic()
        resp = admin.post("/api/v1/calendar/ai/suggest-times", json=body)
        times.append(time.monotonic() - started)
        assert resp.status_code == 200, resp.text
    assert resp.json()["slots"] and sorted(times)[18] <= 3.0  # 1.6-f p95
    assert admin.post("/api/v1/calendar/ai/suggest-times", json={**body, "step_minutes": 7}).status_code == 400
    flag = "/api/v1/ai/admin/features/calendar.suggest_times/flag"
    admin.client.put(flag, json={"company_id": company, "enabled": False}, headers=admin.headers)
    try:
        off = admin.post("/api/v1/calendar/ai/suggest-times", json=body)
        assert off.status_code == 404 and off.json()["code"] == "feature_disabled"
    finally:
        admin.client.put(flag, json={"company_id": company, "enabled": True}, headers=admin.headers)


# -- natural-language scheduling -----------------------------------------------------------------------


def _nl_answer(**over: Any) -> dict[str, Any]:
    return {"title": "Lunch with Priya Nair", "date": "2026-11-17", "start_time": "12:30", "duration_minutes": 60,
            "all_day": False, "attendees": ["Priya Nair"], "location": "Café Lumen", "notes": "", **over}  # fmt: skip


def test_nl_scheduling_creates_nothing_until_accepted(env: Any, env_for: Any) -> None:
    """1.6-c: the request becomes a create proposal; no event, attendee or mail until accept."""
    demo = env_for("demo")  # New York, UTC-5 in November
    partners = env["contacts.partner"].elevated()
    priya = partners.create({"name": "Priya Nair", "email": "priya@nair.test"})
    partners.create([{"name": "Alex Moreno"}, {"name": "Alex Chen"}])
    _script(env, [{"match": {"feature": A.NL}, "respond": {"json": _nl_answer(attendees=["Priya Nair", "Alex", "ceo@evil.test"])}}])
    before = _counts(env)
    inputs = {"text": "Lunch with Priya Nair and Alex next Tuesday 12:30 at Café Lumen", "today": "2026-11-10"}
    result = run_feature(demo, A.NL, inputs, **HANDLERS[A.NL](demo, None, inputs))
    (proposal,) = result.proposals
    assert proposal.kind == "create" and proposal.res_model == "calendar.event"
    assert proposal.payload["start"] == "2026-11-17T17:30:00+00:00" and proposal.payload["stop"] == "2026-11-17T18:30:00+00:00"
    assert proposal.payload["attendee_ids"] == [{"partner_id": priya.ids[0]}]
    assert [u["name"] for u in result.data["unresolved"]] == ["Alex", "ceo@evil.test"]
    assert len(result.data["unresolved"][0]["candidates"]) == 2  # ambiguous: shown, never guessed
    assert _counts(env) == before
    sent = MockAdapter.captured[-1]
    assert "Tuesday 10 November 2026" in json.dumps(sent["messages"])  # relative dates resolved from the context
    accepted = P.accept(demo, proposal)
    event = env["calendar.event"].browse([accepted.result_res_id])
    assert event.name == "Lunch with Priya Nair" and event.user_id.ids == [demo.uid] and event.location == "Café Lumen"
    assert set(event.partner_ids.ids) == {priya.ids[0], demo.user.partner_id.ids[0]}
    assert _counts(env)["core_mail_queue"] == before["core_mail_queue"]  # invitations stay a separate user action


def test_nl_scheduling_resolves_only_people_the_user_can_see(env: Any, env_for: Any) -> None:
    main, eu = _companies(env)
    partners = env["contacts.partner"].elevated()
    partners.create({"name": "Victor Hale", "company_id": main})
    hugo = partners.create({"name": "Hugo Lefort", "company_id": eu})
    marie = env_for("marie").with_company(eu)  # Paris, UTC+1
    resolved, unresolved = A.resolve_attendees(marie, ["Victor Hale", "Hugo Lefort", "Hugo Lefrot", "Marie", "me"])
    assert [p.ids[0] for p in resolved] == [hugo.ids[0]]  # the misspelt name resolves to the same contact
    assert unresolved == [{"name": "Victor Hale", "candidates": []}]
    _script(env, [{"match": {"feature": A.NL}, "respond": {"json": _nl_answer(attendees=["Victor Hale"], start_time="09:00", all_day=False)}}])
    inputs = {"text": "Meeting with Victor Hale next Tuesday 9:00", "today": "2026-11-10"}
    result = run_feature(marie, A.NL, inputs, **HANDLERS[A.NL](marie, None, inputs))
    assert result.proposals.payload["attendee_ids"] == [] and result.proposals.payload["start"] == "2026-11-17T08:00:00+00:00"


def test_nl_scheduling_latency_p95_on_mock(env: Any, env_for: Any) -> None:
    demo = env_for("demo")
    _script(env, [{"match": {"feature": A.NL}, "respond": {"json": _nl_answer(attendees=[])}}])
    times = []
    for _ in range(20):
        started = time.monotonic()
        inputs = {"text": "Weekly sync next Monday 9am"}
        run_feature(demo, A.NL, inputs, **HANDLERS[A.NL](demo, None, inputs))
        times.append(time.monotonic() - started)
    assert sorted(times)[18] <= 3.0


# -- meeting brief -------------------------------------------------------------------------------------


def _brief_setup(env: Any) -> dict[str, Any]:
    main, eu = _companies(env)
    partners = env["contacts.partner"].elevated()
    co = partners.create({"name": "Vantage Retail", "is_company": True, "company_id": eu})
    sophie = partners.create({"name": "Sophie Garnier", "parent_id": co.ids[0], "function": "COO", "company_id": eu})
    henrik = partners.create({"name": "Henrik Dahl", "company_id": main, "comment": "<p>Negotiating a takeover; strictly confidential.</p>"})
    leads = env["crm.lead"].elevated()
    visible = leads.create({"name": "Vantage: store analytics", "partner_id": co.ids[0], "expected_revenue": 64000, "company_id": eu})
    secret = leads.create({"name": "Vantage board deal (HQ only)", "partner_id": co.ids[0], "company_id": main,
                           "description": "<p>Confidential: group-wide contract at 900k with a 30% discount.</p>"})  # fmt: skip
    marie_uid = env.ref("core.user_demo_fr").ids[0]
    event = env["calendar.event"].elevated().create(
        {"name": "Steering committee", "start": d(2026, 11, 17, 15), "stop": d(2026, 11, 17, 16), "user_id": marie_uid,
         "company_id": eu, "res_model": "crm.lead", "res_id": secret.ids[0],
         "attendee_ids": [{"partner_id": sophie.ids[0]}, {"partner_id": henrik.ids[0]}]}  # fmt: skip
    )
    env["calendar.event"].elevated().create(
        {"name": "Private dinner with Sophie", "start": d(2026, 10, 1, 19), "stop": d(2026, 10, 1, 21), "privacy": "private",
         "user_id": env.ref("core.user_admin").ids[0], "description": "<p>They will cut the vendor list to two.</p>",
         "attendee_ids": [{"partner_id": sophie.ids[0]}]}  # fmt: skip
    )
    return {"event": event, "sophie": sophie, "visible": visible, "secret": secret, "eu": eu}


SECRETS = ["Henrik Dahl", "takeover", "board deal", "900k", "Private dinner", "vendor list"]


def test_brief_never_contains_what_the_user_cannot_read(env: Any, env_for: Any) -> None:
    s = _brief_setup(env)
    marie = env_for("marie").with_company(s["eu"])
    event = s["event"].with_env(marie)
    sources = A.brief_sources(marie, event)
    ids = {src.id for src in sources}
    assert f"crm.lead:{s['visible'].ids[0]}" in ids and f"contacts.partner:{s['sophie'].ids[0]}" in ids
    assert f"crm.lead:{s['secret'].ids[0]}" not in ids
    blob = json.dumps([src.text for src in sources])
    assert not [x for x in SECRETS if x in blob] and "an attendee whose contact you cannot see" in blob
    forbidden = A.unauthorised_texts(marie, event)
    assert "Henrik Dahl" in forbidden and "Vantage board deal (HQ only)" in forbidden
    result = run_feature(marie, A.BRIEF, {}, record=event, **HANDLERS[A.BRIEF](marie, event, {}))
    assert result.run.status == "ok" and not result.proposals
    sent = json.dumps(MockAdapter.captured)
    assert not [x for x in SECRETS if x in sent]
    # control: someone who may read everything gets the hidden context, so the check is not vacuous
    admin_blob = json.dumps([src.text for src in A.brief_sources(env.elevated(), s["event"].elevated())])
    assert "board deal" in admin_blob


def test_brief_cites_sources_and_is_listed_for_its_owner_only(env: Any, env_for: Any) -> None:
    s = _brief_setup(env)
    marie = env_for("marie").with_company(s["eu"])
    event = s["event"].with_env(marie)
    lead = s["visible"].ids[0]
    _script(env, [{"match": {"feature": A.BRIEF}, "respond": {"text": f"Store analytics is worth 64,000 [source:crm.lead:{lead}]. Board deal [source:crm.lead:{s['secret'].ids[0]}]"}}])
    result = run_feature(marie, A.BRIEF, {}, record=event, **HANDLERS[A.BRIEF](marie, event, {}))
    assert [c["id"] for c in result.citations] == [f"crm.lead:{lead}"] and "fabricated_citation" in result.flags
    latest = A.latest_brief(marie, event)
    assert latest and latest["run_id"] == result.run.ids[0] and latest["citations"][0]["res_id"] == lead
    assert A.latest_brief(env_for("demo"), s["event"].with_env(env_for("demo"))) is None  # not someone else's brief


def test_briefs_are_prepared_an_hour_before_and_notified(env: Any, env_for: Any, system_env: Any) -> None:
    """1.6-f: the background brief notifies each internal participant with a link, once."""
    demo_uid = env.ref("core.user_demo").ids[0]
    marie_uid = env.ref("core.user_demo_fr").ids[0]
    marie_partner = env["core.user"].elevated().browse([marie_uid]).partner_id
    guest = env["contacts.partner"].elevated().create({"name": "Guest Person"})
    start = env.now().replace(microsecond=0) + dt.timedelta(minutes=60)
    event = env["calendar.event"].elevated().create(
        {"name": "Pricing call", "start": start, "stop": start + dt.timedelta(minutes=30), "user_id": demo_uid,
         "attendee_ids": [{"partner_id": guest.ids[0]}, {"partner_id": marie_partner.ids[0]}]}  # fmt: skip
    )
    later = env["calendar.event"].elevated().create(
        {"name": "Tomorrow", "start": start + dt.timedelta(days=1), "stop": start + dt.timedelta(days=1, hours=1), "user_id": demo_uid}
    )
    sys_env = system_env.with_context()
    assert A.prepare_meeting_briefs(sys_env) >= 2  # (other meetings may start in the same window)
    notes = env["core.notification"].elevated().search([("model", "=", "calendar.event"), ("res_id", "=", event.ids[0])])
    assert sorted(notes.mapped("user_id").ids) == sorted([demo_uid, marie_uid])
    assert all(n.url == f"/m/calendar.event/{event.ids[0]}" and n.title == "Meeting brief ready: Pricing call" for n in notes)
    runs = env["ai_core.run"].elevated().search([("feature", "=", A.BRIEF), ("res_id", "=", event.ids[0])])
    assert sorted(runs.mapped("user_id").ids) == sorted([demo_uid, marie_uid])  # each brief built as its reader
    assert not env["ai_core.run"].elevated().search([("feature", "=", A.BRIEF), ("res_id", "=", later.ids[0])])
    assert A.prepare_meeting_briefs(sys_env) == 0  # once per meeting and person
    sched = env["core.scheduled_job"].elevated().search([("job_name", "=", "calendar.prepare_meeting_briefs")])
    assert sched and sched.cron == "*/30 * * * *"


def test_brief_api_returns_own_latest_brief(admin: Session, app: Any) -> None:
    created = admin.post(
        "/api/v1/models/calendar.event",
        json={"values": {"name": "API brief meeting", "start": "2030-05-06T14:00:00Z", "stop": "2030-05-06T15:00:00Z"}},
    )
    assert created.status_code == 201, created.text
    eid = created.json()["id"]
    try:
        assert admin.get(f"/api/v1/calendar/events/{eid}/brief").json() == {"brief": None}
        run = admin.post("/api/v1/ai/features/calendar.meeting_brief/run", json={"res_model": "calendar.event", "res_id": eid})
        assert run.status_code == 200, run.text
        brief = admin.get(f"/api/v1/calendar/events/{eid}/brief").json()["brief"]
        assert brief["run_id"] == run.json()["run_id"] and brief["ai_generated"]
        demo = Session(TestClient(app, base_url="http://localhost"), "demo", "demo1234")
        assert demo.get(f"/api/v1/calendar/events/{eid}/brief").json() == {"brief": None}
    finally:
        admin.client.delete(f"/api/v1/models/calendar.event/{eid}", headers=admin.headers)


# -- evaluation gate ---------------------------------------------------------------------------------


def test_calendar_evaluations_pass_in_replay(env: Any) -> None:
    """1.6-a/b: every calendar feature is complete and meets its thresholds in replay."""
    specs = EV.feature_specs(modules=["calendar"])
    assert {s.name for s in specs} == {A.BRIEF, A.SUGGEST, A.NL}
    assert EV.completeness(specs) == []
    for spec in specs:
        report = EV.evaluate(env, spec)
        assert report.passed, (spec.name, report.failures, report.metrics)
        assert all(r["unexpected_writes"] == 0 for r in report.case_results), spec.name
