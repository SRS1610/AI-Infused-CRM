"""Calendar: recurrence, rooms (1.4-e), RSVP, booking links, privacy, and the sync contract (1.4-d)."""

from __future__ import annotations

import datetime as dt
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterator
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, make_server

import httpx
import pytest

from modules.calendar.ics import RemoteEvent, from_ics, to_ics
from modules.calendar.models.event import expand
from modules.calendar.sync.base import CalendarAdapter, SyncConflict
from modules.calendar.sync.caldav import CalDavAdapter
from modules.calendar.sync.engine import sync_account
from modules.calendar.sync.google import GoogleAdapter
from modules.calendar.sync.microsoft import MicrosoftAdapter, pattern_to_rrule, rrule_to_pattern
from modules.calendar.tests.fakes import FakeGoogle, FakeGraph
from tessera.exceptions import ConflictError, ValidationError

pytestmark = pytest.mark.db
UTC = dt.UTC


def d(*args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=UTC)


def test_recurrence_expansion_exceptions_and_exdates(env: Any) -> None:
    ev = env["calendar.event"].create(
        {
            "name": "Standup",
            "start": d(2026, 3, 2, 9),
            "stop": d(2026, 3, 2, 9, 15),
            "rrule": "freq=daily;byday=mo,tu,we,th,fr;count=10",
        }
    )
    assert ev.rrule == "FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR;COUNT=10"
    occ = [o["start"] for o in expand(env, d(2026, 3, 1), d(2026, 3, 31)) if o["id"] == ev.ids[0]]
    assert len(occ) == 10 and occ[0] == "2026-03-02T09:00:00+00:00" and occ[-1] == "2026-03-13T09:00:00+00:00"
    ev.action_cancel_occurrence("2026-03-04T09:00:00+00:00")
    moved = ev.action_edit_occurrence(
        "2026-03-05T09:00:00+00:00",
        {"start": d(2026, 3, 5, 14), "stop": d(2026, 3, 5, 14, 30), "name": "Standup (moved)"},
    )["event_id"]
    rows = expand(env, d(2026, 3, 1), d(2026, 3, 31))
    series = [o["start"][:10] for o in rows if o["id"] == ev.ids[0]]
    assert "2026-03-04" not in series and "2026-03-05" not in series and len(series) == 8
    assert [o["start"] for o in rows if o["id"] == moved] == ["2026-03-05T14:00:00+00:00"]
    with pytest.raises(ValidationError), env.cr.savepoint():
        env["calendar.event"].create(
            {"name": "bad", "start": d(2026, 1, 1, 9), "stop": d(2026, 1, 1, 10), "rrule": "FREQ=HOURLY"}
        )
    with pytest.raises(ValidationError), env.cr.savepoint():
        env["calendar.event"].create({"name": "bad", "start": d(2026, 1, 1, 9), "stop": d(2026, 1, 1, 8)})


def test_room_conflicts_name_the_clashing_event(env: Any) -> None:
    """1.4-e"""
    room = env["calendar.room"].create({"name": "Boardroom"})
    events = env["calendar.event"]
    events.create(
        {"name": "Budget review", "start": d(2026, 4, 1, 10), "stop": d(2026, 4, 1, 11), "room_id": room.ids[0]}
    )
    with pytest.raises(ConflictError, match="Boardroom is already booked for 'Budget review'"), env.cr.savepoint():
        events.create(
            {"name": "Interview", "start": d(2026, 4, 1, 10, 30), "stop": d(2026, 4, 1, 11, 30), "room_id": room.ids[0]}
        )
    back_to_back = events.create(
        {"name": "Next", "start": d(2026, 4, 1, 11), "stop": d(2026, 4, 1, 12), "room_id": room.ids[0]}
    )
    assert back_to_back.room_id == room
    # a weekly series clashes with a meeting three weeks later
    events.create(
        {
            "name": "Weekly sync",
            "start": d(2026, 4, 6, 15),
            "stop": d(2026, 4, 6, 16),
            "room_id": room.ids[0],
            "rrule": "FREQ=WEEKLY;COUNT=8",
        }
    )
    with pytest.raises(ConflictError, match="Weekly sync"), env.cr.savepoint():
        events.create(
            {
                "name": "Offsite prep",
                "start": d(2026, 4, 27, 15, 30),
                "stop": d(2026, 4, 27, 17),
                "room_id": room.ids[0],
            }
        )
    free = events.create(
        {
            "name": "Free-time hold",
            "start": d(2026, 4, 27, 15, 30),
            "stop": d(2026, 4, 27, 17),
            "room_id": room.ids[0],
            "show_as": "free",
        }
    )
    assert free  # "free" meetings never block (and are never blocked)


def test_attendees_rsvp_and_invitations(env: Any, env_for: Any) -> None:
    guest = env["contacts.partner"].create({"name": "Guest", "email": "guest@outside.example"})
    demo = env_for("demo")
    ev = env["calendar.event"].create({"name": "Kick-off", "start": d(2026, 5, 4, 9), "stop": d(2026, 5, 4, 10)})
    assert ev.attendee_ids.partner_id == env.ref("core.user_admin").partner_id and ev.attendee_ids.state == "accepted"
    ev.action_add_attendees([guest.ids[0], demo.user.partner_id.ids[0]])
    assert set(ev.partner_ids.ids) == {
        env.ref("core.user_admin").partner_id.ids[0],
        guest.ids[0],
        demo.user.partner_id.ids[0],
    }
    out = ev.action_send_invitations()
    assert out["sent"] == 2
    mail = (
        env["core.mail_queue"].elevated().search([("email_to", "=", "guest@outside.example")], order="id desc", limit=1)
    )
    assert "Kick-off" in mail.subject and "/api/v1/calendar/rsvp/" in mail.body_html
    ics = mail.attachment_ids.raw.decode()
    assert "BEGIN:VEVENT" in ics and "METHOD:REQUEST" in ics and "SUMMARY:Kick-off" in ics


def test_private_meetings_are_hidden_from_non_participants(env: Any, env_for: Any) -> None:
    ev = env["calendar.event"].create(
        {"name": "1:1 salary", "start": d(2026, 6, 1, 9), "stop": d(2026, 6, 1, 9, 30), "privacy": "private"}
    )
    marie = env_for("marie")
    assert not marie["calendar.event"].search([("id", "=", ev.ids[0])])
    ev.action_add_attendees([marie.user.partner_id.ids[0]])
    assert marie["calendar.event"].search([("id", "=", ev.ids[0])])


def test_booking_link_slots_and_booking(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    link = env["calendar.booking_link"].create(
        {"name": "Intro call", "duration_minutes": 30, "min_notice_hours": 0, "days_ahead": 400, "timezone": "UTC"}
    )
    monday = d(2026, 12, 7)  # a Monday
    slots = link.free_slots(monday, monday + dt.timedelta(days=1))
    assert slots[0] == d(2026, 12, 7, 9) and slots[-1] == d(2026, 12, 7, 16, 30) and len(slots) == 16
    env["calendar.event"].create({"name": "Busy", "start": d(2026, 12, 7, 10), "stop": d(2026, 12, 7, 11)})
    slots = link.free_slots(monday, monday + dt.timedelta(days=1))
    assert d(2026, 12, 7, 10) not in slots and d(2026, 12, 7, 10, 30) not in slots and d(2026, 12, 7, 11) in slots
    assert link.free_slots(d(2026, 12, 12), d(2026, 12, 14)) == []  # weekend
    with pytest.raises(ValidationError):
        link.write({"availability": [{"weekday": 9, "start": "09:00", "end": "10:00"}]})


def test_public_booking_endpoint(client, admin) -> None:  # type: ignore[no-untyped-def]
    link = admin.post(
        "/api/v1/models/calendar.booking_link",
        json={
            "values": {"name": "Demo", "duration_minutes": 60, "min_notice_hours": 0, "days_ahead": 30},
            "fields": ["slug"],
        },
    ).json()
    slug = link["slug"]
    assert client.get(f"/api/v1/calendar/book/{slug}").json()["name"] == "Demo"
    now = dt.datetime.now(UTC)
    slots = client.get(
        f"/api/v1/calendar/book/{slug}/slots",
        params={"start": now.isoformat(), "end": (now + dt.timedelta(days=10)).isoformat()},
    ).json()["slots"]
    assert slots
    tag = uuid.uuid4().hex[:6]
    booked = client.post(
        f"/api/v1/calendar/book/{slug}", json={"start": slots[0], "name": "Prospect", "email": f"p-{tag}@example.test"}
    )
    assert booked.status_code == 201
    again = client.post(
        f"/api/v1/calendar/book/{slug}", json={"start": slots[0], "name": "Other", "email": f"o-{tag}@example.test"}
    )
    assert again.status_code == 400  # the slot is taken now
    week = admin.get(
        "/api/v1/calendar/events",
        params={"start": slots[0], "end": (dt.datetime.fromisoformat(slots[0]) + dt.timedelta(hours=1)).isoformat()},
    ).json()
    assert any(e["name"] == "Demo with Prospect" for e in week)


def test_rsvp_link(admin, client) -> None:  # type: ignore[no-untyped-def]
    from tessera.config import get_settings
    from tessera.orm.environment import SYSTEM_UID, environment_for

    with environment_for(get_settings().database_url, SYSTEM_UID, su=True) as env:
        guest = env["contacts.partner"].create(
            {"name": "RSVP guest", "email": f"r-{uuid.uuid4().hex[:6]}@example.test"}
        )
        ev = env["calendar.event"].create(
            {"name": "RSVP test", "start": d(2026, 7, 1, 9), "stop": d(2026, 7, 1, 10), "user_id": 2}
        )
        att = env["calendar.attendee"].create({"event_id": ev.ids[0], "partner_id": guest.ids[0]})
        token, att_id = att.access_token, att.ids[0]
    page = client.get(f"/api/v1/calendar/rsvp/{token}", params={"answer": "tentative"})
    assert page.status_code == 200 and "You might attend" in page.text
    assert client.get("/api/v1/calendar/rsvp/nope", params={"answer": "accepted"}).status_code == 404
    with environment_for(get_settings().database_url, SYSTEM_UID, su=True) as env:
        assert env["calendar.attendee"].browse([att_id]).state == "tentative"


# -- sync contract (1.4-d) ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def radicale_url() -> Iterator[str]:
    import radicale
    import radicale.config

    class Quiet(WSGIRequestHandler):
        def log_message(self, *args: Any) -> None:
            return None

    cfg = radicale.config.load()
    cfg.update(
        {
            "storage": {"filesystem_folder": tempfile.mkdtemp()},
            "auth": {"type": "none"},
            "rights": {"type": "owner_only"},
        },
        "test",
        privileged=True,
    )
    server = make_server("127.0.0.1", 0, radicale.Application(cfg), handler_class=Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def caldav_factory(base: str) -> CalendarAdapter:
    client = httpx.Client(auth=("alice", "x"), trust_env=False, timeout=10)
    url = f"{base}/alice/{uuid.uuid4().hex[:8]}/"
    assert client.request("MKCALENDAR", url).status_code == 201
    return CalDavAdapter(url, "alice", "x", client=client)


def google_factory(_: str) -> CalendarAdapter:
    fake = FakeGoogle()
    return GoogleAdapter("token", "primary", client=httpx.Client(transport=httpx.MockTransport(fake.handler)))


def graph_factory(_: str) -> CalendarAdapter:
    fake = FakeGraph()
    return MicrosoftAdapter("token", None, client=httpx.Client(transport=httpx.MockTransport(fake.handler)))


FACTORIES: dict[str, Callable[[str], CalendarAdapter]] = {
    "caldav": caldav_factory,
    "google": google_factory,
    "microsoft": graph_factory,
}


@pytest.mark.parametrize("provider", sorted(FACTORIES))
def test_adapter_contract(provider: str, radicale_url: str) -> None:
    """Every adapter: full listing, create recurring, incremental change, update with etag, stale etag
    refused, cancelled occurrence round-trips, delete reported in the next delta."""
    adapter = FACTORIES[provider](radicale_url)
    changed, deleted, token = adapter.changes(None)
    assert changed == [] and deleted == []
    event = RemoteEvent(
        uid=f"{uuid.uuid4().hex}@tessera",
        summary="Weekly review",
        start=d(2026, 9, 7, 14),
        end=d(2026, 9, 7, 15),
        rrule="FREQ=WEEKLY;COUNT=6",
        location="Room 1",
    )
    rid, etag = adapter.put(event, None, None)
    assert rid and etag
    changed, deleted, token = adapter.changes(token)
    mine = [c for c in changed if (c.href or c.uid) == rid]
    assert (
        len(mine) == 1
        and mine[0].summary == "Weekly review"
        and "FREQ=WEEKLY" in (mine[0].rrule or "")
        and "COUNT=6" in mine[0].rrule
    )
    assert mine[0].start == event.start and mine[0].end == event.end and mine[0].etag == etag
    event.summary = "Weekly review (new time)"
    event.exdates = [d(2026, 9, 14, 14)]
    rid2, etag2 = adapter.put(event, rid, etag)
    assert rid2 == rid and etag2 != etag
    with pytest.raises(SyncConflict):
        adapter.put(event, rid, etag)  # stale etag
    changed, deleted, token = adapter.changes(token)
    updated = next(c for c in changed if (c.href or c.uid) == rid)
    assert updated.summary == "Weekly review (new time)"
    if provider != "microsoft":  # Graph models cancelled occurrences as instances, not on the master
        assert updated.exdates == [d(2026, 9, 14, 14)]
    adapter.delete(rid, etag2)
    changed, deleted, _ = adapter.changes(token)
    assert rid in deleted and not [c for c in changed if (c.href or c.uid) == rid]


def test_two_way_caldav_sync_round_trip(env: Any, radicale_url: str) -> None:
    """1.4-d: a recurring event created, updated and deleted on either side converges in one sync cycle."""
    adapter = caldav_factory(radicale_url)
    account = env["calendar.sync_account"].create({"provider": "caldav", "name": "Radicale", "url": adapter.url})
    events = env["calendar.event"].with_context(active_test=False)
    # local → remote
    local = events.create(
        {
            "name": "Team lunch",
            "start": d(2026, 10, 2, 12),
            "stop": d(2026, 10, 2, 13),
            "rrule": "FREQ=WEEKLY;BYDAY=FR;COUNT=4",
        }
    )
    assert sync_account(account, adapter)["pushed"] >= 1
    href = account.link_ids.filtered(lambda lk: lk.event_id == local).remote_uid
    remote = from_ics(adapter.client.get(adapter._abs(href)).text)[0]
    assert remote.summary == "Team lunch" and "BYDAY=FR" in remote.rrule
    # remote change (another client) → local
    remote.summary = "Team lunch (Thai)"
    remote.exdates = [d(2026, 10, 9, 12)]
    link = account.link_ids.filtered(lambda lk: lk.event_id == local)
    adapter.client.put(adapter._abs(href), content=to_ics(remote).encode(), headers={"Content-Type": "text/calendar"})
    stats = sync_account(account, adapter)
    assert stats["pulled"] == 1
    assert local.name == "Team lunch (Thai)" and local.exdates == ["2026-10-09T12:00:00+00:00"]
    # local change → remote; no echo on the next cycle
    local.write({"location": "Kitchen"})
    assert sync_account(account, adapter)["pushed"] == 1
    assert from_ics(adapter.client.get(adapter._abs(href)).text)[0].location == "Kitchen"
    assert sync_account(account, adapter) == {
        "pulled": 0,
        "pushed": 0,
        "deleted_local": 0,
        "deleted_remote": 0,
        "conflicts": 0,
    }
    # remote creation → local
    other = RemoteEvent(
        uid=f"{uuid.uuid4().hex}@phone", summary="Dentist", start=d(2026, 10, 5, 8), end=d(2026, 10, 5, 9)
    )
    adapter.put(other, None, None)
    sync_account(account, adapter)
    created = events.search([("sync_uid", "=", other.uid)])
    assert created.name == "Dentist" and created.user_id == env.ref("core.user_admin")
    # remote deletion → local archive; local deletion → remote delete
    adapter.delete(account.link_ids.filtered(lambda lk: lk.event_id == created).remote_uid, None)
    sync_account(account, adapter)
    assert not created.active
    local.write({"active": False})
    assert sync_account(account, adapter)["deleted_remote"] == 1
    assert adapter.client.get(adapter._abs(href)).status_code == 404
    assert link.exists() == link.browse([])


def test_graph_recurrence_mapping() -> None:
    pattern = rrule_to_pattern("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,TH;COUNT=6", d(2026, 1, 5, 9))
    assert pattern["pattern"] == {
        "interval": 2,
        "type": "weekly",
        "daysOfWeek": ["monday", "thursday"],
        "firstDayOfWeek": "monday",
    }
    assert pattern["range"]["type"] == "numbered" and pattern["range"]["numberOfOccurrences"] == 6
    assert pattern_to_rrule(pattern) == "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,TH;COUNT=6"
    monthly = rrule_to_pattern("FREQ=MONTHLY;BYMONTHDAY=15;UNTIL=20261231T235959Z", d(2026, 1, 15, 9))
    assert pattern_to_rrule(monthly) == "FREQ=MONTHLY;BYMONTHDAY=15;UNTIL=20261231T235959Z"


def test_ics_round_trip() -> None:
    ev = RemoteEvent(
        uid="x@y", summary="Trip", start=d(2026, 8, 1), end=d(2026, 8, 3), allday=True, attendees=["a@b.c"]
    )
    back = from_ics(to_ics(ev))[0]
    assert back.allday and back.start == d(2026, 8, 1) and back.end == d(2026, 8, 3) and back.attendees == ["a@b.c"]
