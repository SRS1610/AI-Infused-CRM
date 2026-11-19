"""1.3-g over HTTP: a guarded transition is absent from the status bar and returns 409; webhook triggers."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from modules.workflow.tests.test_workflow import make_workflow
from tessera.config import get_settings
from tessera.orm.environment import SYSTEM_UID, environment_for

pytestmark = pytest.mark.db


@pytest.fixture
def committed_workflow(registry: Any) -> Iterator[Any]:
    url = get_settings().database_url
    with environment_for(url, SYSTEM_UID, su=True) as env:
        w = make_workflow(env, strict=True)
        ids = {"wf": w.wf.ids[0], "submit": w.submit.ids[0], "publish": w.publish.ids[0]}
        hook = env["workflow.automation"].create(
            {
                "name": "external",
                "model_name": "ktest.book",
                "trigger": "on_webhook",
                "actions": [{"type": "update", "values": {"page_count": "payload['pages']"}}],
            }
        )
        ids["hook"], ids["token"] = hook.ids[0], hook.webhook_token
    yield ids
    with environment_for(url, SYSTEM_UID, su=True) as env:
        env["workflow.automation"].browse([ids["hook"]]).unlink()
        env["workflow.definition"].browse([ids["wf"]]).unlink()


def test_statusbar_and_conflict_over_http(client, librarian, committed_workflow) -> None:  # type: ignore[no-untyped-def]
    book = librarian.post("/api/v1/models/ktest.book", json={"values": {"name": "http-wf"}}).json()
    base = f"/api/v1/workflow/ktest.book/{book['id']}"
    bar = librarian.get(f"{base}/statusbar").json()
    assert bar["current"] == "draft" and bar["transitions"] == []
    blocked = librarian.post(f"{base}/transitions/{committed_workflow['submit']}")
    assert blocked.status_code == 409 and blocked.json()["error"] == "ConflictError"
    direct = librarian.patch(f"/api/v1/models/ktest.book/{book['id']}", json={"values": {"status": "published"}})
    assert direct.status_code == 409
    # the webhook automation sets the pages, which satisfies the guard
    hook = client.post(
        f"/api/v1/workflow/hooks/{committed_workflow['token']}", json={"record_id": book["id"], "pages": 12}
    )
    assert hook.status_code == 200 and hook.json() == {"ran": 1}
    assert client.post("/api/v1/workflow/hooks/wrong-token", json={"record_id": book["id"]}).status_code == 401
    bar = librarian.get(f"{base}/statusbar").json()
    assert [t["name"] for t in bar["transitions"]] == ["Submit"]
    moved = librarian.post(f"{base}/transitions/{committed_workflow['submit']}").json()
    assert moved["current"] == "review" and [t["name"] for t in moved["transitions"]] == ["Publish"]
    assert librarian.post(f"{base}/transitions/999999").status_code == 404
