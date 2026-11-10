"""Mail queue, job sweeper and notifications (sub-phase 1.3)."""

from __future__ import annotations

import datetime as dt
from email.message import EmailMessage
from typing import Any

import pytest

from tessera.jobs import run_now
from tessera.orm.environment import ADMIN_UID, SYSTEM_UID, environment_for

pytestmark = pytest.mark.db


class FakeSMTP:
    instances: list[FakeSMTP] = []
    fail_next = 0

    def __init__(self, host: str, port: int, timeout: float = 30, **kw: Any) -> None:
        self.host, self.port = host, port
        self.started_tls = False
        self.logged_in: tuple[str, str] | None = None
        self.sent: list[EmailMessage] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def starttls(self, context: Any = None) -> None:
        self.started_tls = True

    def login(self, user: str, password: str) -> None:
        self.logged_in = (user, password)

    def send_message(self, msg: EmailMessage) -> None:
        if FakeSMTP.fail_next:
            FakeSMTP.fail_next -= 1
            raise ConnectionRefusedError("smtp down")
        self.sent.append(msg)


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSMTP]:
    import smtplib

    FakeSMTP.instances = []
    FakeSMTP.fail_next = 0
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


@pytest.fixture
def db(registry: Any, database_url: str) -> Any:
    yield database_url
    with environment_for(database_url, SYSTEM_UID, su=True) as env:
        env.cr.execute("DELETE FROM core_job WHERE name IN ('core.send_mail', 'core.jobs_sweep') OR name LIKE 'test.%'")
        env.cr.execute("DELETE FROM core_mail_queue WHERE subject LIKE 'T-%'")
        env.cr.execute("DELETE FROM core_mail_server WHERE name LIKE 'T-%'")


def _mail_jobs(db: str, mail_id: int) -> list[int]:
    with environment_for(db, SYSTEM_UID, su=True) as env:
        return env["core.job"].search([("idempotency_key", "=", f"mail:{mail_id}")]).ids


def test_mail_queue_sends_html_and_text_via_job(db: str, smtp: type[FakeSMTP]) -> None:
    with environment_for(db, ADMIN_UID) as env:
        mail = env["core.mail_queue"].queue(
            {
                "email_to": "ana@example.test",
                "subject": "T-hello",
                "body_html": "<p>Hello <b>Ana</b></p><p>Bye</p>",
                "in_reply_to": "<x@y>",
            }
        )
        mail_id = mail.ids[0]
    assert smtp.instances == []  # nothing sent before the job runs
    jobs = _mail_jobs(db, mail_id)
    assert run_now(db, jobs) == {jobs[0]: "done"}
    msg = smtp.instances[0].sent[0]
    assert (
        msg["To"] == "ana@example.test"
        and msg["In-Reply-To"] == "<x@y>"
        and msg["Message-ID"].endswith("@tessera.local>")
    )
    parts = {p.get_content_type(): p.get_content() for p in msg.iter_parts()}
    assert "Hello Ana" in parts["text/plain"] and "<b>Ana</b>" in parts["text/html"]
    with environment_for(db, SYSTEM_UID, su=True) as env:
        rec = env["core.mail_queue"].browse([mail_id])
        assert rec.state == "sent" and rec.attempts == 1


def test_mail_failure_is_recorded_and_retried(db: str, smtp: type[FakeSMTP]) -> None:
    smtp.fail_next = 1
    with environment_for(db, ADMIN_UID) as env:
        mail_id = (
            env["core.mail_queue"].queue({"email_to": "b@example.test", "subject": "T-retry", "body_text": "x"}).ids[0]
        )
    jobs = _mail_jobs(db, mail_id)
    assert run_now(db, jobs) == {jobs[0]: "retry"}
    with environment_for(db, SYSTEM_UID, su=True) as env:
        rec = env["core.mail_queue"].browse([mail_id])
        assert rec.state == "failed" and "smtp down" in rec.error and rec.attempts == 1
    assert run_now(db, jobs) == {jobs[0]: "done"}
    with environment_for(db, SYSTEM_UID, su=True) as env:
        assert env["core.mail_queue"].browse([mail_id]).state == "sent"


def test_configured_server_with_auth_and_encrypted_password(db: str, smtp: type[FakeSMTP]) -> None:
    with environment_for(db, ADMIN_UID) as env:
        server = env["core.mail_server"].create(
            {
                "name": "T-relay",
                "host": "smtp.example.test",
                "port": 2525,
                "security": "starttls",
                "username": "u",
                "password": "pw",
            }
        )
        assert server.elevated().password.startswith("enc:v1:")
        mail_id = (
            env["core.mail_queue"].queue({"email_to": "c@example.test", "subject": "T-auth", "body_text": "x"}).ids[0]
        )
    run_now(db, _mail_jobs(db, mail_id))
    client = smtp.instances[0]
    assert (client.host, client.port, client.started_tls, client.logged_in) == (
        "smtp.example.test",
        2525,
        True,
        ("u", "pw"),
    )


@pytest.mark.parametrize("ignored", [None])
def test_sweeper_recovers_stuck_and_prunes_old_jobs(db: str, ignored: None) -> None:
    from tessera.jobs import job

    @job("test.sweep_target", max_attempts=2, timeout_seconds=60)
    def _target(env: Any) -> None:
        return None

    long_ago = dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)
    with environment_for(db, SYSTEM_UID, su=True) as env:
        jobs = env["core.job"]
        stuck = jobs.create(
            {"name": "test.sweep_target", "state": "running", "attempts": 1, "max_attempts": 2, "started_at": long_ago}
        )
        env["core.job_attempt"].create(
            {"job_id": stuck.ids[0], "number": 1, "status": "running", "started_at": long_ago}
        )
        dead = jobs.create(
            {"name": "test.sweep_target", "state": "running", "attempts": 2, "max_attempts": 2, "started_at": long_ago}
        )
        old = jobs.create(
            {"name": "test.sweep_target", "state": "done", "finished_at": long_ago - dt.timedelta(days=40)}
        )
        late = jobs.create({"name": "test.sweep_target", "state": "queued", "next_attempt_at": long_ago})
        ids = (stuck.ids[0], dead.ids[0], old.ids[0], late.ids[0])
        sweep = env["core.job"].create({"name": "core.jobs_sweep", "state": "queued"})
        sweep_id = sweep.ids[0]
    assert run_now(db, [sweep_id]) == {sweep_id: "done"}
    with environment_for(db, SYSTEM_UID, su=True) as env:
        result = env["core.job"].browse([sweep_id]).result
        assert (
            result["stuck"] == 2 and result["requeued"] == 1 and result["pruned"] >= 1 and result["redispatched"] >= 1
        )
        assert env["core.job"].browse([ids[0]]).state == "queued"
        assert env["core.job"].browse([ids[0]]).attempt_ids[0].status == "failed"
        assert env["core.job"].browse([ids[1]]).state == "dead"
        assert not env["core.job"].browse([ids[2]]).exists()


def test_notifications_are_private_and_published(env: Any, env_for: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    published: list[tuple[str, str, dict[str, Any]]] = []
    import modules.core.models.notification as notification_module

    monkeypatch.setattr(
        notification_module,
        "publish_after_commit",
        lambda cr, channel, event, data=None: published.append((channel, event, data)),
    )
    demo_env = env_for("demo")
    demo_uid = demo_env.uid
    env["core.notification"].notify([demo_uid, ADMIN_UID, demo_uid], "Hi", "there", kind="mention")
    assert sorted(c for c, _, _ in published) == sorted([f"user:{demo_uid}", f"user:{ADMIN_UID}"])
    mine = demo_env["core.notification"].search([("title", "=", "Hi")])
    assert len(mine) == 1 and mine.user_id.ids == [demo_uid]
    assert len(env["core.notification"].search([("title", "=", "Hi")])) == 1  # admin sees only their own too
    mine.action_mark_read()
    assert mine.is_read and mine.read_at
