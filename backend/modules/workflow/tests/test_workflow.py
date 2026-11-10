"""Workflows (1.3-g) and automations (1.3-h) on the kernel test books."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest

from modules.workflow.api import statusbar_payload
from modules.workflow.engine import run_time_automations
from tessera.exceptions import AccessError, ConflictError, ValidationError

pytestmark = pytest.mark.db


def make_workflow(env: Any, strict: bool = True) -> SimpleNamespace:
    wf = env["workflow.definition"].create(
        {"name": "Publishing", "model_name": "ktest.book", "state_field": "status", "strict": strict}
    )
    states = {
        key: env["workflow.state"].create({"definition_id": wf.ids[0], "key": key, "name": name, "sequence": i})
        for i, (key, name) in enumerate([("draft", "Draft"), ("review", "In review"), ("published", "Published")])
    }
    submit = env["workflow.transition"].create(
        {
            "definition_id": wf.ids[0],
            "name": "Submit",
            "from_state_ids": [("set", states["draft"].ids)],
            "to_state_id": states["review"].ids[0],
            "guard_domain": [["page_count", ">", 0]],
            "guard_message": "Add pages first",
            "highlight": True,
        }
    )
    publish = env["workflow.transition"].create(
        {
            "definition_id": wf.ids[0],
            "name": "Publish",
            "from_state_ids": [("set", states["review"].ids)],
            "to_state_id": states["published"].ids[0],
            "group_ids": [("set", [env.ref("kernel_test_a.group_librarian").ids[0]])],
        }
    )
    return SimpleNamespace(wf=wf, states=states, submit=submit, publish=publish)


def ctx_for(env: Any) -> Any:
    return SimpleNamespace(env=env, uid=env.uid, registry=env.registry)


def test_statusbar_hides_guarded_transitions_and_apply_returns_conflict(env: Any) -> None:
    w = make_workflow(env)
    book = env["ktest.book"].create({"name": "WF", "page_count": 0})
    bar = statusbar_payload(ctx_for(env), "ktest.book", book.ids[0])
    assert [s["key"] for s in bar["states"]] == ["draft", "review", "published"] and bar["current"] == "draft"
    assert bar["transitions"] == []  # guard fails: page_count is 0
    with pytest.raises(ConflictError, match="Add pages first"):
        w.submit.apply(book)
    book.write({"page_count": 5})
    assert [t["name"] for t in statusbar_payload(ctx_for(env), "ktest.book", book.ids[0])["transitions"]] == ["Submit"]
    w.submit.apply(book)
    assert book.status == "review"
    with pytest.raises(ConflictError):
        w.submit.apply(book)  # not available from 'review'


def test_strict_workflow_refuses_direct_state_writes(env: Any, env_for: Any) -> None:
    make_workflow(env)
    book = env_for("librarian")["ktest.book"].create({"name": "Strict", "page_count": 1})
    with pytest.raises(ConflictError, match="workflow"):
        book.write({"status": "published"})
    with pytest.raises(ConflictError):
        book.action_publish()  # the model's own button writes the state directly too
    book.write({"name": "Strict 2", "status": "draft"})  # unchanged state is fine


def test_transition_groups(env: Any, env_for: Any) -> None:
    w = make_workflow(env, strict=False)
    librarian = env_for("librarian")
    book = librarian["ktest.book"].create({"name": "Groups", "page_count": 2, "status": "review"})
    assert [t["name"] for t in statusbar_payload(ctx_for(librarian), "ktest.book", book.ids[0])["transitions"]] == [
        "Publish"
    ]
    w.publish.with_env(librarian).apply(book)
    assert book.status == "published"
    other = env["ktest.book"].create({"name": "Groups 2", "page_count": 2, "status": "review"})
    # admin is not a librarian: the transition is hidden and refused
    assert statusbar_payload(ctx_for(env), "ktest.book", other.ids[0])["transitions"] == []
    with pytest.raises(AccessError):
        w.publish.apply(other)


def test_only_one_active_workflow_per_model(env: Any) -> None:
    make_workflow(env)
    with pytest.raises(ValidationError):
        env["workflow.definition"].create({"name": "Second", "model_name": "ktest.book", "state_field": "status"})
    with pytest.raises(ValidationError):
        env["workflow.definition"].create({"name": "Bad", "model_name": "ktest.book", "state_field": "page_count"})


# -- automations -------------------------------------------------------------------------------------


def auto(env: Any, **vals: Any) -> Any:
    vals.setdefault("name", "auto")
    vals.setdefault("model_name", "ktest.book")
    return env["workflow.automation"].create(vals)


def runs(env: Any, automation: Any) -> list[tuple[int, str]]:
    found = env["workflow.automation_run"].search([("automation_id", "=", automation.ids[0])], order="id")
    return [(r.res_id, r.status) for r in found]


def test_on_create_update_with_expression(env: Any) -> None:
    a = auto(
        env,
        trigger="on_create",
        filter_domain=[["name", "ilike", "auto-"]],
        actions=[{"type": "update", "values": {"page_count": "len(record.name) * 10"}}],
    )
    book = env["ktest.book"].create({"name": "auto-123"})
    assert book.page_count == 80
    other = env["ktest.book"].create({"name": "manual"})
    assert other.page_count == 0
    assert runs(env, a) == [(book.ids[0], "done")]


def test_on_write_watched_fields_pre_filter_and_no_recursion(env: Any) -> None:
    a = auto(
        env,
        trigger="on_write",
        watched_fields=["page_count"],
        pre_filter_domain=[["page_count", "<", 100]],
        filter_domain=[["page_count", ">=", 100]],
        actions=[
            {"type": "update", "values": {"page_count": "record.page_count + 1"}},  # writes the watched field again
            {"type": "post_message", "body": "'Crossed 100 pages: ' + str(record.page_count)"},
        ],
    )
    book = env["ktest.book"].create({"name": "Thick"})
    book.write({"name": "Thick!"})  # not watched
    book.write({"page_count": 50})  # watched but filter fails
    assert runs(env, a) == []
    book.write({"page_count": 150})  # crosses the threshold
    assert book.page_count == 151  # ran once: the automation does not re-trigger itself
    assert runs(env, a) == [(book.ids[0], "done")]
    note = (
        env["chatter.message"]
        .elevated()
        .search([("model", "=", "ktest.book"), ("res_id", "=", book.ids[0]), ("message_type", "=", "note")])
    )
    assert "Crossed 100 pages: 151" in note.body
    book.write({"page_count": 200})  # pre-filter: it was already >= 100 before
    assert len(runs(env, a)) == 1


def test_condition_and_error_policy(env: Any) -> None:
    auto(
        env,
        trigger="on_create",
        condition="record.page_count > 10",
        actions=[{"type": "update", "values": {"name": "upper(record.name)"}}],
    )
    small = env["ktest.book"].create({"name": "small", "page_count": 1})
    big = env["ktest.book"].create({"name": "big", "page_count": 20})
    assert (small.name, big.name) == ("small", "BIG")
    failing = auto(
        env, trigger="on_create", actions=[{"type": "update", "values": {"page_count": "1 / record.page_count"}}]
    )
    book = env["ktest.book"].create({"name": "zero"})  # logged, the create still succeeds
    assert runs(env, failing)[0][1] == "error"
    failing.write({"on_error": "raise"})
    with pytest.raises(ValidationError, match="Division by zero"):
        env["ktest.book"].create({"name": "zero again"})
    assert book.exists()


def test_activity_notify_create_and_method_actions(env: Any, env_for: Any) -> None:
    demo = env_for("demo").uid
    auto(
        env,
        trigger="on_create",
        actions=[
            {"type": "schedule_activity", "summary": "'Review ' + record.name", "days": 2, "user": str(demo)},
            {"type": "notify", "title": "'New book ' + record.name", "users": f"[{demo}]"},
            {"type": "create_record", "model": "ktest.tag", "values": {"name": "'tag-' + record.name"}},
            {"type": "call_method", "method": "action_publish"},
        ],
    )
    book = env["ktest.book"].create({"name": "Chain"})
    act = env["chatter.activity"].search([("model", "=", "ktest.book"), ("res_id", "=", book.ids[0])])
    assert act.summary == "Review Chain" and act.user_id.ids == [demo]
    assert act.date_deadline == env.today() + dt.timedelta(days=2)
    assert env["core.notification"].elevated().search([("title", "=", "New book Chain"), ("user_id", "=", demo)])
    assert env["ktest.tag"].search([("name", "=", "tag-Chain")])
    assert book.status == "published"


def test_webhook_action_enqueues_job(env: Any) -> None:
    auto(
        env,
        trigger="on_create",
        actions=[{"type": "call_webhook", "url": "https://hooks.example.test/x", "fields": ["name"]}],
    )
    book = env["ktest.book"].create({"name": "Hooked"})
    job = env["core.job"].search([("name", "=", "workflow.call_webhook")], order="id desc", limit=1)
    assert job.kwargs["payload"]["id"] == book.ids[0] and job.kwargs["payload"]["data"]["name"] == "Hooked"


def test_on_delete(env: Any) -> None:
    a = auto(
        env,
        trigger="on_delete",
        actions=[{"type": "create_record", "model": "ktest.tag", "values": {"name": "'deleted ' + record.name"}}],
    )
    book = env["ktest.book"].create({"name": "Gone"})
    book.unlink()
    assert env["ktest.tag"].search([("name", "=", "deleted Gone")]) and len(runs(env, a)) == 1


def test_definition_validation(env: Any) -> None:
    with pytest.raises(ValidationError, match="unknown field"):
        auto(env, actions=[{"type": "update", "values": {"nope": "1"}}])
    with pytest.raises(ValidationError, match="not allowed"):
        auto(env, actions=[{"type": "update", "values": {"name": "record.__class__"}}])
    with pytest.raises(ValidationError, match="unknown type"):
        auto(env, actions=[{"type": "rm -rf"}])
    with pytest.raises(ValidationError, match="not a callable method"):
        auto(env, actions=[{"type": "call_method", "method": "unlink"}])
    with pytest.raises(ValidationError, match="date"):
        auto(env, trigger="on_time", date_field="name")


def test_on_time_fires_once_per_record(env: Any) -> None:
    """1.3-h: three days before the publication date, exactly once, with a mocked clock."""
    t0 = dt.datetime(2026, 3, 1, 9, 0, tzinfo=dt.UTC)
    a = auto(
        env,
        trigger="on_time",
        date_field="published",
        offset_days=-3,
        last_time_check=t0,
        actions=[{"type": "create_record", "model": "ktest.tag", "values": {"name": "'remind ' + record.name"}}],
    )
    soon = env["ktest.book"].create({"name": "Soon", "published": "2026-03-05"})
    later = env["ktest.book"].create({"name": "Later", "published": "2026-03-20"})
    past = env["ktest.book"].create({"name": "Past", "published": "2026-02-01"})  # moment before activation: never
    assert run_time_automations(env, now=t0 + dt.timedelta(hours=1))[a.ids[0]] == 0  # Mar 1: nothing due yet
    assert run_time_automations(env, now=t0 + dt.timedelta(days=1))[a.ids[0]] == 1  # Mar 2 = 5 Mar - 3 days
    fired = run_time_automations(env, now=t0 + dt.timedelta(days=1, hours=1))
    assert fired[a.ids[0]] == 0  # a second check the same day does not fire again
    reminders = env["ktest.tag"].search([("name", "like", "remind ")]).mapped("name")
    assert reminders == ["remind Soon"]
    for day in range(2, 30):  # many later checks, overlapping windows
        run_time_automations(env, now=t0 + dt.timedelta(days=day))
        a.write({"last_time_check": t0})  # even if the cursor is reset, the run key prevents a second fire
    names = sorted(env["ktest.tag"].search([("name", "like", "remind ")]).mapped("name"))
    assert names == ["remind Later", "remind Soon"]
    assert {r for r, _ in runs(env, a)} == {soon.ids[0], later.ids[0]} and past.ids[0] not in {
        r for r, _ in runs(env, a)
    }


def test_on_transition_automation(env: Any) -> None:
    w = make_workflow(env)
    auto(
        env,
        trigger="on_transition",
        transition_id=w.submit.ids[0],
        actions=[
            {"type": "post_message", "body": "'Moved from ' + old_state + ' via ' + transition", "internal": True}
        ],
    )
    book = env["ktest.book"].create({"name": "Transit", "page_count": 3})
    w.submit.apply(book)
    note = (
        env["chatter.message"]
        .elevated()
        .search([("model", "=", "ktest.book"), ("res_id", "=", book.ids[0]), ("message_type", "=", "note")])
    )
    assert "Moved from draft via Submit" in note.body
