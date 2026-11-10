"""Discuss (1.4-c): fan-out to 50 members, mentions, member-only files, unread counters, DMs, presence."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import pytest

from modules.chatter.thread import post_message
from modules.discuss.models.channel import unread_counts
from tessera.exceptions import AccessError, UserError

pytestmark = pytest.mark.db


@pytest.fixture
def people(env: Any) -> list[Any]:
    group = env.ref("core.group_user").ids[0]
    return [
        env["core.user"].create(
            {"login": f"chat{i}-{uuid.uuid4().hex[:4]}", "name": f"Chatter {i}", "group_ids": [("set", [group])]}
        )
        for i in range(3)
    ]


def test_public_channel_visibility_and_join(env: Any, people: list[Any]) -> None:
    a, b, _ = (env.with_user(p.ids[0]) for p in people)
    ch = a["discuss.channel"].create({"name": "random"})
    assert ch.elevated().member_ids.user_id.ids == [people[0].ids[0]]
    assert b["discuss.channel"].browse(ch.ids).name == "random"  # public: visible
    with pytest.raises(AccessError):
        b["discuss.channel"].browse(ch.ids).write({"name": "mine now"})  # not a member
    b["discuss.channel"].browse(ch.ids).action_join()
    b["discuss.channel"].browse(ch.ids).write({"description": "joined"})


def test_private_channel_and_member_only_files(env: Any, people: list[Any]) -> None:
    """1.4-c: an uploaded file is downloadable only by channel members."""
    a, b, c = (env.with_user(p.ids[0]) for p in people)
    secret = a["discuss.channel"].create({"name": "secret", "privacy": "invite"})
    secret.action_invite([people[1].ids[0]])
    att = a["core.attachment"].create({"name": "plan.pdf", "raw": b"%PDF plan"})
    post_message(a, "discuss.channel", secret.ids[0], "<p>the plan</p>", attachment_ids=att.ids, check_access=True)
    assert b["core.attachment"].browse(att.ids).check_record_access("read") is None  # member
    with pytest.raises(AccessError):
        c["core.attachment"].browse(att.ids).check_record_access("read")
    assert not c["discuss.channel"].search([("id", "=", secret.ids[0])])
    with pytest.raises(UserError):
        c["discuss.channel"].elevated().browse(secret.ids).with_env(c).action_join()


def test_mentions_unread_and_notify_settings(env: Any, people: list[Any]) -> None:
    a, b, c = (env.with_user(p.ids[0]) for p in people)
    ch = a["discuss.channel"].create({"name": "team"})
    ch.with_env(b).action_join()
    ch.with_env(c).action_join()
    env["discuss.member"].elevated().search([("channel_id", "=", ch.ids[0]), ("user_id", "=", people[2].ids[0])]).write(
        {"notify": "all"}
    )
    notes = env["core.notification"].elevated()
    before = {p.ids[0]: notes.search_count([("user_id", "=", p.ids[0])]) for p in people}
    m1 = post_message(a, "discuss.channel", ch.ids[0], f"<p>hi @{people[1].login}</p>", check_access=True)
    post_message(a, "discuss.channel", ch.ids[0], "<p>second</p>", check_access=True)
    after = {p.ids[0]: notes.search_count([("user_id", "=", p.ids[0])]) for p in people}
    assert after[people[1].ids[0]] - before[people[1].ids[0]] == 1  # the mention only (notify = mentions)
    assert after[people[2].ids[0]] - before[people[2].ids[0]] == 2  # notify = all
    assert after[people[0].ids[0]] == before[people[0].ids[0]]  # never the author
    assert notes.search([("user_id", "=", people[1].ids[0])], order="id desc", limit=1).kind == "mention"
    assert unread_counts(env, people[1].ids[0], ch.ids) == {ch.ids[0]: 2}
    assert unread_counts(env, people[0].ids[0], ch.ids) == {ch.ids[0]: 0}  # own messages are read
    env["discuss.member"].elevated().search([("channel_id", "=", ch.ids[0]), ("user_id", "=", people[1].ids[0])]).write(
        {"last_seen_message_id": m1.ids[0]}
    )
    assert unread_counts(env, people[1].ids[0], ch.ids) == {ch.ids[0]: 1}
    # channel members are not turned into record followers
    assert (
        not env["chatter.follower"].elevated().search([("model", "=", "discuss.channel"), ("res_id", "=", ch.ids[0])])
    )


def test_direct_messages(env: Any, people: list[Any]) -> None:
    a, b, c = (env.with_user(p.ids[0]) for p in people)
    dm = a["discuss.channel"].get_or_create_dm(people[1].ids[0])
    assert dm.channel_type == "dm" and dm.privacy == "invite"
    assert b["discuss.channel"].get_or_create_dm(people[0].ids[0]) == dm  # the same conversation both ways
    assert not c["discuss.channel"].search([("id", "=", dm.ids[0])])
    with pytest.raises(UserError):
        dm.action_invite([people[2].ids[0]])
    with pytest.raises(UserError):
        a["discuss.channel"].get_or_create_dm(people[0].ids[0])


def test_fanout_to_50_connected_members_under_a_second(env: Any) -> None:
    """1.4-c: one publish reaches 50 connected clients subscribed to the channel within 1 s."""
    from tessera.realtime.hub import Hub

    hub = Hub()

    async def scenario() -> float:
        conns = [hub.connect(1000 + i, "db") for i in range(50)]
        for conn in conns:
            hub.subscribe(conn, ["record:discuss.channel:7"])
        started = time.perf_counter()
        hub.deliver_local(
            "db",
            "record:discuss.channel:7",
            {"channel": "record:discuss.channel:7", "event": "chatter", "data": {"message_id": 1}},
        )
        results = await asyncio.gather(*(asyncio.wait_for(c.queue.get(), 1.0) for c in conns))
        assert all(r["event"] == "chatter" for r in results)
        assert hub.online_uids("db") == {1000 + i for i in range(50)}
        for conn in conns:
            hub.disconnect(conn)
        assert hub.online_uids("db") == set()
        return time.perf_counter() - started

    elapsed = asyncio.run(scenario())
    assert elapsed < 1.0


def test_api_flow(admin, app) -> None:  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    from tessera.api.tests.conftest import Session

    tag = uuid.uuid4().hex[:6]
    created = admin.post("/api/v1/discuss/channels", json={"name": f"api-{tag}", "privacy": "invite"}).json()
    assert created["member"] and created["privacy"] == "invite"
    cid = created["id"]
    general = [
        c
        for c in admin.get("/api/v1/discuss/channels/browse", params={"q": "general"}).json()
        if c["name"] == "general"
    ]
    assert general
    admin.post(f"/api/v1/discuss/channels/{general[0]['id']}/join")
    msg = admin.post(f"/api/v1/chatter/discuss.channel/{cid}/messages", json={"body": "<p>hello api</p>"})
    assert msg.status_code == 201
    mine = {c["id"]: c for c in admin.get("/api/v1/discuss/channels").json()}
    assert cid in mine and mine[cid]["unread"] == 0
    demo = admin.get("/api/v1/models/core.user", params={"domain": '[["login","=","demo"]]', "fields": "id"}).json()[
        "items"
    ][0]["id"]
    dm = admin.post(f"/api/v1/discuss/dm/{demo}").json()
    assert dm["type"] == "dm" and dm["name"] == "Dana Demo" and dm["partner_user_id"] == demo
    admin.post(f"/api/v1/chatter/discuss.channel/{dm['id']}/messages", json={"body": "<p>ping</p>"})
    demo_session = Session(TestClient(app, base_url="http://localhost"), "demo", "demo1234")
    unread = {c["id"]: c["unread"] for c in demo_session.get("/api/v1/discuss/channels").json()}
    assert unread[dm["id"]] == 1
    last = demo_session.get(f"/api/v1/chatter/discuss.channel/{dm['id']}/messages").json()["items"][0]["id"]
    assert demo_session.post(f"/api/v1/discuss/channels/{dm['id']}/seen", json={"message_id": last}).json() == {
        "unread": 0
    }
    assert demo_session.get(f"/api/v1/chatter/discuss.channel/{cid}/messages").status_code in (403, 404)  # private
    assert demo_session.post(f"/api/v1/discuss/channels/{cid}/notify", json={"notify": "all"}).status_code == 404
    assert admin.post(f"/api/v1/discuss/channels/{cid}/notify", json={"notify": "none"}).json() == {"notify": "none"}
    assert "online" in admin.get("/api/v1/discuss/presence").json()
