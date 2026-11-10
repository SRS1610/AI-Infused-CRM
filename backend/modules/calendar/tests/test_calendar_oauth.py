"""OAuth token refresh of Google and Microsoft calendar accounts, against the fake provider servers."""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest

from modules.calendar.sync.engine import make_adapter, sync_account, sync_all_job
from modules.calendar.sync.oauth import (
    GOOGLE_TOKEN_URL,
    AccountTokens,
    BearerAuth,
    ReconnectRequired,
    StaticToken,
    TokenRefreshError,
    client_credentials,
)
from modules.calendar.tests.fakes import FakeGoogle, FakeGraph, FakeTokenEndpoint
from tessera.config import get_settings
from tessera.exceptions import AccessError
from tessera.security import crypto

pytestmark = pytest.mark.db

TOKEN_HOSTS = ("oauth2.googleapis.com", "login.microsoftonline.com")


def routed(token: FakeTokenEndpoint, api: FakeGoogle | FakeGraph) -> httpx.Client:
    """One client for both the provider API and its token endpoint, as in production."""

    def handle(request: httpx.Request) -> httpx.Response:
        return token.handler(request) if request.url.host in TOKEN_HOSTS else api.handler(request)

    return httpx.Client(transport=httpx.MockTransport(handle))


@pytest.fixture
def google_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "google_oauth_client_id", "g-client")
    monkeypatch.setattr(get_settings(), "google_oauth_client_secret", "g-secret")


def _account(env: Any, provider: str, **vals: Any) -> Any:
    demo = env.ref("core.user_demo")  # the record rule limits accounts to their owner: work as the system
    return (
        env["calendar.sync_account"]
        .elevated()
        .create({"provider": provider, "name": f"{provider} calendar", "user_id": demo.ids[0], **vals})
    )


def _event(env: Any, name: str) -> Any:
    start = dt.datetime(2026, 12, 1, 9, tzinfo=dt.UTC)
    return (
        env["calendar.event"]
        .elevated()
        .create(
            {
                "name": name,
                "start": start,
                "stop": start + dt.timedelta(hours=1),
                "user_id": env.ref("core.user_demo").ids[0],
            }
        )
    )


def _notes(env: Any, account: Any) -> Any:
    return (
        env["core.notification"]
        .elevated()
        .search([("model", "=", "calendar.sync_account"), ("res_id", "=", account.ids[0])])
    )


def test_tokens_are_stored_encrypted_and_visible_to_administrators_only(env: Any, env_for: Any) -> None:
    account = _account(env, "google", refresh_token="r-1", access_token="a-1")
    row = env.cr.execute(
        "SELECT refresh_token, access_token FROM calendar_sync_account WHERE id = :id", {"id": account.ids[0]}
    ).one()
    assert all(crypto.is_encrypted(v) for v in row) and "r-1" not in "".join(row)
    assert crypto.decrypt(account.refresh_token) == "r-1"
    demo = env_for("demo")
    mine = demo["calendar.sync_account"].browse(account.ids)
    assert mine.name == "google calendar"  # the owner sees the account …
    with pytest.raises(AccessError):  # … but never its tokens
        mine.read(["name", "refresh_token"])


def test_google_token_is_refreshed_before_it_expires(env: Any, google_client: None) -> None:
    token = FakeTokenEndpoint("g-client", "g-secret", "r-google")
    api = FakeGoogle(valid_tokens=token.valid)
    expired = env.now() - dt.timedelta(minutes=1)
    account = _account(env, "google", refresh_token="r-google", access_token="stale", token_expiry=expired)
    _event(env, "Planning")
    stats = sync_account(account, make_adapter(account, routed(token, api)))
    assert stats["pushed"] == 1 and len(api.events) == 1
    (call,) = token.calls  # refreshed once, before the first API request
    assert call == {"grant_type": "refresh_token", "refresh_token": "r-google", "client_id": "g-client",
                    "client_secret": "g-secret"}  # fmt: skip
    assert crypto.decrypt(account.access_token) == "access-1" and crypto.decrypt(account.refresh_token) == "r-google"
    assert account.token_expiry > env.now() + dt.timedelta(minutes=55) and account.state == "active"
    # still valid: the next sync uses it without calling the token endpoint
    sync_account(account, make_adapter(account, routed(token, api)))
    assert len(token.calls) == 1


def test_google_refreshes_once_on_401_and_retries(env: Any, google_client: None) -> None:
    token = FakeTokenEndpoint("g-client", "g-secret", "r-google")
    api = FakeGoogle(valid_tokens=token.valid)
    later = env.now() + dt.timedelta(hours=1)
    account = _account(env, "google", refresh_token="r-google", access_token="revoked-early", token_expiry=later)
    _event(env, "Review")
    assert sync_account(account, make_adapter(account, routed(token, api)))["pushed"] == 1
    assert len(token.calls) == 1 and crypto.decrypt(account.access_token) == "access-1"
    token.expire_all()  # the provider drops the token before its announced expiry
    _event(env, "Second review")
    assert sync_account(account, make_adapter(account, routed(token, api)))["pushed"] == 1
    assert len(token.calls) == 2 and len(api.events) == 2


def test_microsoft_rotates_refresh_tokens_with_company_client_and_tenant(env: Any) -> None:
    company = env.ref("core.user_demo").company_id
    company.set_setting(
        "calendar.oauth_clients",
        {
            "microsoft": {
                "client_id": "ms-client",
                "client_secret": crypto.encrypt("ms-secret"),
                "tenant": "contoso.example",
            }
        },
    )
    token = FakeTokenEndpoint("ms-client", "ms-secret", "r-ms", rotate=True)
    api = FakeGraph(valid_tokens=token.valid)
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            seen.append(request.url.path)
            return token.handler(request)
        return api.handler(request)

    account = _account(env, "microsoft", refresh_token="r-ms")  # never had an access token
    _event(env, "Board meeting")
    client = httpx.Client(transport=httpx.MockTransport(handle))
    assert sync_account(account, make_adapter(account, client))["pushed"] == 1
    assert seen == ["/contoso.example/oauth2/v2.0/token"]
    assert "offline_access" in token.calls[0]["scope"] and "Calendars.ReadWrite" in token.calls[0]["scope"]
    rotated = crypto.decrypt(account.refresh_token)
    assert rotated != "r-ms" and rotated in token.refresh_tokens
    token.expire_all()
    _event(env, "Board follow-up")
    assert sync_account(account, make_adapter(account, client))["pushed"] == 1
    assert token.calls[1]["refresh_token"] == rotated and crypto.decrypt(account.refresh_token) != rotated


def test_refused_refresh_marks_the_account_and_notifies_its_owner_once(env: Any, google_client: None) -> None:
    token = FakeTokenEndpoint("g-client", "g-secret", "r-google")
    api = FakeGoogle(valid_tokens=token.valid)
    account = _account(env, "google", refresh_token="r-google")
    token.revoke()  # the user removed Tessera's access in their Google account
    with pytest.raises(ReconnectRequired, match=r"invalid_grant|expired or revoked"):
        sync_account(account, make_adapter(account, routed(token, api)))
    assert account.state == "reconnect" and "revoked" in account.last_error
    (note,) = _notes(env, account)
    assert note.user_id == env.ref("core.user_demo") and "Reconnect your calendar" in note.title
    with pytest.raises(ReconnectRequired):
        sync_account(account, make_adapter(account, routed(token, api)))
    assert len(_notes(env, account)) == 1  # not notified again
    # the scheduled sync leaves it alone until the owner connects it again
    assert str(account.ids[0]) not in sync_all_job(env.elevated())
    token.refresh_tokens.add("r-new")
    account.write({"refresh_token": "r-new"})
    assert account.state == "active" and not account.last_error
    assert sync_account(account, make_adapter(account, routed(token, api)))["pulled"] == 0
    assert account.state == "active"


def test_transient_refresh_failure_is_retried_later(env: Any, google_client: None) -> None:
    token = FakeTokenEndpoint("g-client", "g-secret", "r-google")
    api = FakeGoogle(valid_tokens=token.valid)
    account = _account(env, "google", refresh_token="r-google")
    token.fail_with = 503
    with pytest.raises(TokenRefreshError, match="HTTP 503"):
        sync_account(account, make_adapter(account, routed(token, api)))
    assert account.state == "error" and not _notes(env, account)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    tokens = AccountTokens(account.elevated(), httpx.Client(transport=httpx.MockTransport(down)))
    with pytest.raises(TokenRefreshError, match="unreachable"):
        tokens.access_token()
    token.fail_with = None
    sync_account(account, make_adapter(account, routed(token, api)))
    assert account.state == "active"


def test_missing_oauth_client_or_refresh_token_needs_reconnection(env: Any) -> None:
    account = _account(env, "google", refresh_token="r-google")
    with pytest.raises(ReconnectRequired, match="No OAuth client is configured for google"):
        client_credentials(account)
    caldav = _account(env, "caldav", url="https://dav.example/cal/")
    with pytest.raises(ReconnectRequired):
        client_credentials(caldav)
    bare = _account(env, "microsoft")
    with pytest.raises(ReconnectRequired, match="no access token"):
        AccountTokens(bare.elevated()).access_token()
    with pytest.raises(ReconnectRequired, match="no refresh token"):
        AccountTokens(bare.elevated()).refresh()
    with pytest.raises(ReconnectRequired):
        StaticToken("x").refresh()


def test_accounts_connected_with_an_access_token_only_still_sync(env: Any) -> None:
    """Accounts created before refresh support kept their access token in ``secret``."""
    api = FakeGoogle(valid_tokens={"legacy-token"})
    account = _account(env, "google", secret="legacy-token")
    _event(env, "Old style")
    client = httpx.Client(transport=httpx.MockTransport(api.handler))
    assert sync_account(account, make_adapter(account, client))["pushed"] == 1
    api.valid_tokens.clear()  # expired, and nothing to refresh it with
    _event(env, "Old style 2")
    with pytest.raises(httpx.HTTPStatusError):
        sync_account(account, make_adapter(account, client))
    assert account.state == "error"


def test_bearer_auth_accepts_a_plain_token() -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return httpx.Response(401)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert client.get(GOOGLE_TOKEN_URL, auth=BearerAuth("plain")).status_code == 401
    assert seen == ["Bearer plain"]  # no refresh possible: one request only
