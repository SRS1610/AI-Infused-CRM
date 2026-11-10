"""Discuss AI features (BUILD_PLAN 1.6): summary, catch me up, reply draft, translation.

Behaviour, permissions (a user never gets content from a channel they cannot read), proposals
(1.6-c: nothing is posted until accept), background summaries with a notification (1.6-f), the
interactive latency budget on the mock provider (1.6-f), the API and the replayed evaluation sets.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import pytest
from gateway.adapters.mock import MockAdapter

from modules.chatter.thread import post_message
from modules.discuss import ai_features as AF
from modules.discuss.models.channel import unread_counts
from tessera.ai import client as C
from tessera.ai import evals as E
from tessera.ai import proposals as P
from tessera.ai.errors import FeatureDisabled
from tessera.ai.runtime import run_feature, stream_feature
from tessera.api.tests.conftest import Session
from tessera.exceptions import AccessError

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _fresh() -> Any:
    MockAdapter.reset()
    C.reset_inprocess_state()
    yield
    C.set_client_factory(None)


def _script(env: Any, rules: list[dict[str, Any]]) -> None:
    p = env.ref("ai_core.provider_local")
    p.write({"options": {**(p.options or {}), "script": rules}})


@pytest.fixture
def people(env: Any) -> list[Any]:
    group = env.ref("core.group_user").ids[0]
    tag = uuid.uuid4().hex[:4]
    names = ["Sam Ortiz", "Priya Nair", "Olga Outsider"]
    return [
        env["core.user"].create({"login": f"ai{i}-{tag}", "name": n, "group_ids": [("set", [group])]})
        for i, n in enumerate(names)
    ]


def _envs(env: Any, people: list[Any]) -> list[Any]:
    return [env.with_user(p.ids[0]) for p in people]


def _post(env: Any, channel: Any, text: str, **kw: Any) -> Any:
    return post_message(env, "discuss.channel", channel.ids[0], f"<p>{text}</p>", check_access=True, **kw)


def _sent() -> str:
    """The prompt the last gateway call sent to the provider (after the gateway's redaction)."""
    last = MockAdapter.captured[-1]
    return json.dumps([last["system"], last["messages"]], ensure_ascii=False)


def _counts(env: Any) -> tuple[int, int, int]:
    env.flush()
    return (
        int(env.cr.scalar("SELECT count(*) FROM chatter_message") or 0),
        int(env.cr.scalar("SELECT count(*) FROM core_mail_queue") or 0),
        int(env.cr.scalar("SELECT count(*) FROM chatter_activity") or 0),
    )


# ----------------------------------------------------------------------------- summary


def test_summary_cites_messages_redacts_names_and_rejects_fabricated_citations(env: Any, people: list[Any]) -> None:
    sam, priya, _ = _envs(env, people)
    ch = sam["discuss.channel"].create({"name": f"deal-room-{uuid.uuid4().hex[:4]}", "privacy": "invite"})
    ch.action_invite([people[1].ids[0]])
    m1 = _post(sam, ch, "Northwind wants the Pro plan for 40 seats.")
    m2 = _post(priya, ch, "I will send the quote on Friday.")
    other = sam["discuss.channel"].create({"name": "elsewhere", "privacy": "invite"})
    _post(sam, other, "Unrelated secret: the merger closes in May.")
    _script(
        env,
        [
            {
                "match": {"feature": "discuss.summary"},
                "respond": {
                    "text": f"- Northwind wants 40 Pro seats [source:chatter.message:{m1.ids[0]}]\n"
                    f"- Priya sends the quote on Friday [source:chatter.message:{m2.ids[0]}]"
                    "[source:chatter.message:999999]"
                },
            }
        ],
    )
    result = run_feature(priya, "discuss.summary", {}, record=ch, **AF.summary_handler(priya, ch, {}))
    sent = _sent()
    assert "40 seats" in sent and "quote on Friday" in sent and "merger" not in sent
    assert "Sam Ortiz" not in sent and "Priya Nair" not in sent  # authors are redacted before leaving
    assert {c["id"] for c in result.citations} == {f"chatter.message:{m1.ids[0]}", f"chatter.message:{m2.ids[0]}"}
    assert "fabricated_citation" in result.flags and "999999" not in result.text
    assert result.run.status == "ok" and not result.proposals


def test_summary_needs_read_access_and_honours_thread_and_limit(env: Any, people: list[Any]) -> None:
    sam, priya, olga = _envs(env, people)
    ch = sam["discuss.channel"].create({"name": "private-ops", "privacy": "invite"})
    ch.action_invite([people[1].ids[0]])
    root = _post(sam, ch, "Thread root: database migration plan.")
    reply = _post(priya, ch, "Migration window Saturday 02:00.", parent_id=root.ids[0])
    loose = _post(sam, ch, "Lunch anyone?")
    with pytest.raises(AccessError):
        AF.summary_handler(olga, ch.with_env(olga), {})
    with pytest.raises(AccessError):
        AF.summarize(olga, ch.ids[0], {})
    thread = AF.summary_handler(priya, ch, {"thread_id": root.ids[0]})["sources"]
    ids = {s.id for s in thread if s.model == "chatter.message"}
    assert ids == {f"chatter.message:{root.ids[0]}", f"chatter.message:{reply.ids[0]}"}
    latest = AF.summary_handler(priya, ch, {"limit": 1})["sources"]
    assert [s.id for s in latest if s.model == "chatter.message"] == [f"chatter.message:{loose.ids[0]}"]


def test_large_summary_runs_in_background_and_notifies_with_a_link(env: Any, people: list[Any]) -> None:
    sam, priya, _ = _envs(env, people)
    ch = sam["discuss.channel"].create({"name": "busy"})
    for i in range(AF.INTERACTIVE_MAX + 1):
        env["chatter.message"].elevated().create(
            {"model": "discuss.channel", "res_id": ch.ids[0], "message_type": "comment", "body": f"<p>update {i}</p>",
             "author_id": people[0].ids[0]}
        )  # fmt: skip
    before = MockAdapter.captured[:]
    out = AF.summarize(priya, ch.ids[0], {})
    assert out["mode"] == "background" and out["count"] == AF.INTERACTIVE_MAX + 1
    assert MockAdapter.captured == before  # nothing sent yet: the job does it
    queued = env["core.job"].elevated().browse([out["job_id"]])
    assert queued.name == "discuss.summarize" and queued.user_id.ids == [people[1].ids[0]]
    done = AF.summarize_job(priya, **queued.kwargs)  # what the worker runs, as the requesting user
    note = env["core.notification"].elevated().search([("user_id", "=", people[1].ids[0])], order="id desc", limit=1)
    assert note.title == "Summary of #busy is ready" and note.kind == "job"
    assert note.model == "discuss.channel" and note.res_id == ch.ids[0]
    assert note.url == AF.summary_url(ch.ids[0], done["run_id"])
    run = env["ai_core.run"].elevated().browse([done["run_id"]])
    assert run.user_id.ids == [people[1].ids[0]] and run.feature == "discuss.summary" and run.status == "ok"
    small = sam["discuss.channel"].create({"name": "quiet"})
    _post(sam, small, "Just one message.")
    assert AF.summarize(sam, small.ids[0], {})["mode"] == "interactive"


# ----------------------------------------------------------------------------- catch me up


def test_catch_up_covers_unread_only_from_readable_channels(env: Any, people: list[Any]) -> None:
    sam, priya, olga = _envs(env, people)
    team = sam["discuss.channel"].create({"name": "team-x"})
    team.with_env(priya).action_join()
    _post(priya, team, "My own message is not unread for me.")
    read = sam["discuss.channel"].create({"name": "read-y"})
    read.with_env(priya).action_join()
    old = _post(sam, read, "Already read: office closed Monday.")
    env["discuss.member"].elevated().search(
        [("channel_id", "=", read.ids[0]), ("user_id", "=", people[1].ids[0])]
    ).write({"last_seen_message_id": old.ids[0]})
    secret = sam["discuss.channel"].create({"name": "secret-z", "privacy": "invite"})
    _post(sam, secret, "Confidential: layoffs planned for Q2.")
    m1 = _post(sam, team, "@" + people[1].login + " please review the budget by Thursday.")
    m2 = _post(sam, team, "Budget file is in the shared drive.")
    dm = priya["discuss.channel"].get_or_create_dm(people[0].ids[0])
    m3 = _post(sam, dm, "Can you call me at 3?")
    sources = AF.catch_up_handler(priya, None, {})["sources"]
    msg_ids = [s.id for s in sources if s.model == "chatter.message"]
    assert msg_ids == [f"chatter.message:{m3.ids[0]}", f"chatter.message:{m1.ids[0]}", f"chatter.message:{m2.ids[0]}"]
    assert any(s.text.startswith("[mentions you]") for s in sources if s.id.endswith(f":{m1.ids[0]}"))
    _script(env, [{"match": {"feature": "discuss.catch_up"}, "respond": {"text": f"1. #team-x — review the budget by Thursday [source:chatter.message:{m1.ids[0]}]"}}])  # fmt: skip
    out = AF.catch_up(priya)
    sent = _sent()
    assert "layoffs" not in sent and "office closed" not in sent and "My own message" not in sent
    assert out["unread"] == 3 and out["links"] == {
        str(m1.ids[0]): {"channel_id": team.ids[0], "author": "Sam Ortiz", "date": m1.date.strftime("%Y-%m-%d %H:%M")}
    }
    assert unread_counts(priya, people[1].ids[0], [team.ids[0]])[team.ids[0]] == 2  # catching up marks nothing read
    # Olga is in none of these channels: nothing to send, no gateway call
    calls = len(MockAdapter.captured)
    assert AF.catch_up(olga) == {"unread": 0, "result": None, "links": {}}
    assert len(MockAdapter.captured) == calls


# ----------------------------------------------------------------------------- reply draft


def test_reply_draft_posts_nothing_until_the_proposal_is_accepted(env: Any, people: list[Any]) -> None:
    """1.6-c: zero messages and zero mail until ``proposal.accept()``; then exactly the reviewed message."""
    sam, priya, _ = _envs(env, people)
    ch = sam["discuss.channel"].create({"name": "launch"})
    ch.with_env(priya).action_join()
    gone = sam["discuss.channel"].create({"name": "left-behind", "privacy": "invite"})
    gone.action_invite([people[1].ids[0]])
    _post(priya, gone, "SECRET TONE SAMPLE from a channel I left")
    gone.with_env(priya).action_leave()
    elsewhere = priya["discuss.channel"].create({"name": "random-priya"})
    _post(priya, elsewhere, "lol yes 😄 ship it")
    ask = _post(sam, ch, "@" + people[1].login + " can you confirm the launch date?")
    _script(env, [{"match": {"feature": "discuss.reply_draft"}, "respond": {"text": f"Yes! 3 March it is 🚀 [source:chatter.message:{ask.ids[0]}]"}}])  # fmt: skip
    before = _counts(env)
    result = run_feature(priya, "discuss.reply_draft", {}, record=ch, **AF.reply_draft_handler(priya, ch, {}))
    sent = _sent()
    assert "lol yes" in sent and "SECRET TONE SAMPLE" not in sent  # tone only from channels still readable
    assert _counts(env) == before  # nothing posted, no mail queued
    (proposal,) = result.proposals
    assert proposal.kind == "message" and proposal.res_model == "discuss.channel" and proposal.res_id == ch.ids[0]
    assert proposal.payload == {"body": "Yes! 3 March it is 🚀"}  # source markers never reach the channel
    with pytest.raises(AccessError):
        P.accept(sam, proposal.with_env(sam))  # only the requester decides
    assert _counts(env) == before
    P.accept(priya, proposal, {"body": "Yes, 3 March 🚀"})
    after = _counts(env)
    assert after[0] == before[0] + 1 and after[1] == before[1]
    posted = env["chatter.message"].elevated().search([("model", "=", "discuss.channel"), ("res_id", "=", ch.ids[0])], order="id desc", limit=1)  # fmt: skip
    assert "3 March" in posted.body and posted.author_id.ids == [people[1].ids[0]]
    assert posted.ai_run_id == result.run.ids[0] and proposal.state == "edited"


def test_reply_draft_streams_and_refuses_unreadable_channels(env: Any, people: list[Any]) -> None:
    sam, priya, olga = _envs(env, people)
    ch = sam["discuss.channel"].create({"name": "private", "privacy": "invite"})
    ch.action_invite([people[1].ids[0]])
    _post(sam, ch, "Can you send the deck?")
    with pytest.raises(AccessError):
        AF.reply_draft_handler(olga, ch.with_env(olga), {})
    events = list(stream_feature(priya, "discuss.reply_draft", {}, record=ch, **AF.reply_draft_handler(priya, ch, {})))
    kinds = [e["event"] for e in events]
    assert kinds[0] == "start" and "delta" in kinds and kinds[-1] == "done"
    result = events[-1]["result"]
    assert result["ai_generated"] and [p["kind"] for p in result["proposals"]] == ["message"]


# ----------------------------------------------------------------------------- translation


def test_translation_requires_access_to_the_thread(env: Any, people: list[Any]) -> None:
    sam, priya, olga = _envs(env, people)
    ch = sam["discuss.channel"].create({"name": "intl", "privacy": "invite"})
    ch.action_invite([people[1].ids[0]])
    msg = _post(sam, ch, "Le déménagement est prévu pour le 14 avril.")
    with pytest.raises(AccessError):
        AF.translate_handler(olga, None, {"message_id": msg.ids[0], "target_lang": "en"})
    _script(
        env, [{"match": {"feature": "discuss.translate"}, "respond": {"text": "The move is planned for 14 April."}}]
    )
    inputs = {"message_id": msg.ids[0], "target_lang": "en"}
    result = run_feature(priya, "discuss.translate", inputs, **AF.translate_handler(priya, None, inputs))
    assert result.text == "The move is planned for 14 April." and result.to_dict()["ai_generated"] is True
    assert "Translate the message into English" in _sent() and not result.proposals


def test_chrf_matches_sacrebleu() -> None:
    """Values checked against sacreBLEU 2.x ``CHRF().sentence_score`` (68.2569, 18.2394, 48.3610)."""
    assert AF.chrf("The move is scheduled for 14 April.", "The move is planned for 14 April.") == pytest.approx(
        0.682569, abs=1e-4
    )
    assert AF.chrf("Le déménagement est prévu pour le 14 avril.", "The move is planned for 14 April.") == pytest.approx(0.182394, abs=1e-4)  # fmt: skip
    assert AF.chrf("Das Backlog ist auf 120 Tickets zurückgegangen.", "Der Rückstand ist auf 120 Tickets gesunken.") == pytest.approx(0.483610, abs=1e-4)  # fmt: skip
    assert AF.chrf("same", "same") == 1.0
    scores = AF.chrf_scorer(
        [
            {"status": "ok", "text": "abc def", "_case": {"expected": {"reference": "abc def", "pair": "en_fr"}}},
            {"status": "error", "text": "", "_case": {"expected": {"reference": "abc", "pair": "en_fr"}}},
        ]
    )
    assert scores == {"chrf_en_fr": 0.5, "chrf": 0.5}


# ----------------------------------------------------------------------------- kill switch, latency, evals


def test_kill_switch_blocks_without_gateway_call(env: Any, people: list[Any]) -> None:
    sam, *_ = _envs(env, people)
    ch = sam["discuss.channel"].create({"name": "off"})
    _post(sam, ch, "hello")
    feature = env["ai_core.feature"].search([("name", "=", "discuss.summary")])
    feature.set_enabled(sam.company.ids[0], False)
    with pytest.raises(FeatureDisabled):
        AF.summarize(sam, ch.ids[0], {})
    assert MockAdapter.captured == []


def test_interactive_features_p95_under_3_seconds_on_the_mock(env: Any, people: list[Any]) -> None:
    """1.6-f: interactive budget ≤ 3 s p95 on the mock adapter (context building + runtime + gateway)."""
    sam, priya, _ = _envs(env, people)
    ch = sam["discuss.channel"].create({"name": "perf"})
    ch.with_env(priya).action_join()
    msgs = [
        _post(sam if i % 2 else priya, ch, f"Status update number {i} about the rollout.")
        for i in range(AF.INTERACTIVE_MAX)
    ]
    durations: dict[str, list[float]] = {}
    for _ in range(20):
        for name, call in (
            ("summary", lambda: AF.summarize(priya, ch.ids[0], {})),
            ("catch_up", lambda: AF.catch_up(priya)),
            ("reply", lambda: run_feature(priya, "discuss.reply_draft", {}, record=ch, **AF.reply_draft_handler(priya, ch, {}))),
            ("translate", lambda: run_feature(priya, "discuss.translate", {"message_id": msgs[-1].ids[0], "target_lang": "fr"},
                                              **AF.translate_handler(priya, None, {"message_id": msgs[-1].ids[0], "target_lang": "fr"}))),
        ):  # fmt: skip
            started = time.monotonic()
            call()
            durations.setdefault(name, []).append(time.monotonic() - started)
    for name, values in durations.items():
        p95 = sorted(values)[int(0.95 * len(values)) - 1]
        assert p95 < 3.0, (name, p95)


def test_replayed_eval_sets_meet_their_thresholds(env: Any) -> None:
    """1.6-a/b: every Discuss feature passes its thresholds on the replayed cassettes."""
    specs = E.feature_specs(modules=["discuss"])
    assert {s.name for s in specs} == {
        "discuss.summary",
        "discuss.catch_up",
        "discuss.reply_draft",
        "discuss.translate",
    }
    assert E.completeness(specs) == []
    for spec in specs:
        with env.cr.savepoint():
            report = E.evaluate(env, spec)
        assert report.passed, (spec.name, report.failures, report.metrics)


# ----------------------------------------------------------------------------- API


def test_api_summary_catch_up_translate_and_stream(admin: Session, app: Any) -> None:
    from fastapi.testclient import TestClient

    tag = uuid.uuid4().hex[:6]
    ch = admin.post("/api/v1/discuss/channels", json={"name": f"ai-{tag}", "privacy": "public"}).json()
    private = admin.post("/api/v1/discuss/channels", json={"name": f"ai-private-{tag}", "privacy": "invite"}).json()
    first = admin.post(f"/api/v1/chatter/discuss.channel/{ch['id']}/messages", json={"body": "<p>Kickoff moved to Monday.</p>"}).json()  # fmt: skip
    admin.post(f"/api/v1/chatter/discuss.channel/{private['id']}/messages", json={"body": "<p>private plan</p>"})

    resp = admin.post(f"/api/v1/discuss/ai/channels/{ch['id']}/summary", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "interactive" and body["count"] == 1 and body["result"]["ai_generated"]
    assert body["result"]["feature"] == "discuss.summary"

    demo = Session(TestClient(app, base_url="http://localhost"), "demo", "demo1234")
    assert demo.post(f"/api/v1/discuss/ai/channels/{private['id']}/summary", json={}).status_code in (403, 404)
    denied = demo.post(
        "/api/v1/ai/features/discuss.translate/run", json={"inputs": {"message_id": 0, "target_lang": "fr"}}
    )
    assert denied.status_code == 404
    demo.post(f"/api/v1/discuss/channels/{ch['id']}/join")
    admin.post(f"/api/v1/chatter/discuss.channel/{ch['id']}/messages", json={"body": "<p>Second update for demo.</p>"})
    caught = demo.post("/api/v1/discuss/ai/catch-up", json={}).json()
    assert caught["unread"] >= 1 and caught["result"]["feature"] == "discuss.catch_up"
    assert "private plan" not in json.dumps(MockAdapter.captured)

    tr = admin.post("/api/v1/ai/features/discuss.translate/run", json={"inputs": {"message_id": first["id"], "target_lang": "fr"}})  # fmt: skip
    assert tr.status_code == 200 and tr.json()["ai_generated"] and tr.json()["proposals"] == []
    assert (
        demo.post("/api/v1/ai/features/discuss.translate/run", json={"inputs": {"message_id": first["id"]}}).status_code
        == 422
    )

    before = len(admin.get(f"/api/v1/chatter/discuss.channel/{ch['id']}/messages").json()["items"])
    with admin.client.stream(
        "POST",
        "/api/v1/ai/features/discuss.reply_draft/stream",
        json={"res_model": "discuss.channel", "res_id": ch["id"]},
        headers=admin.headers,
    ) as resp:
        assert resp.status_code == 200
        events = [json.loads(line[5:]) for line in resp.iter_lines() if line.startswith("data:")]
    assert events[0]["event"] == "start" and events[-1]["event"] == "done"
    (proposal,) = events[-1]["result"]["proposals"]
    assert len(admin.get(f"/api/v1/chatter/discuss.channel/{ch['id']}/messages").json()["items"]) == before
    accepted = admin.post(f"/api/v1/ai/proposals/{proposal['id']}/accept", json={"payload": {"body": "Thanks, noted."}})
    assert accepted.json()["state"] == "edited"
    items = admin.get(f"/api/v1/chatter/discuss.channel/{ch['id']}/messages").json()["items"]
    assert len(items) == before + 1 and items[0]["ai_assisted"] is True
