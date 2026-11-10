"""OAuth access tokens of Google and Microsoft calendar accounts.

An account stores a refresh token, the current access token and its expiry (tokens encrypted, visible to
administrators only). Before a sync the access token is refreshed when it expires within
:data:`REFRESH_MARGIN`; a provider answer of 401 during the sync forces one refresh and the request is
retried once (:class:`BearerAuth`).

The OAuth client (id and secret) comes from the account owner's company setting ``calendar.oauth_clients``
(``{"google": {"client_id": …, "client_secret": …}, "microsoft": {"client_id": …, "client_secret": …,
"tenant": …}}``; secrets may be stored encrypted with ``tessera.security.crypto``), else from the process
configuration (``TESSERA_GOOGLE_OAUTH_CLIENT_ID`` …).

A refresh the provider refuses (``invalid_grant``: the user revoked access, the refresh token expired, the
client changed) cannot succeed by retrying: :class:`ReconnectRequired` is raised and the sync engine marks
the account *Needs reconnection* and notifies its owner. Network errors and provider outages raise
:class:`TokenRefreshError` and are retried at the next sync.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Generator
from typing import Any, Protocol

import httpx

from tessera.config import get_settings
from tessera.i18n import _
from tessera.logging import get_logger
from tessera.security import crypto

log = get_logger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - an endpoint, not a secret
MICROSOFT_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"  # noqa: S105
MICROSOFT_SCOPE = "offline_access https://graph.microsoft.com/Calendars.ReadWrite"
REFRESH_MARGIN = dt.timedelta(minutes=5)
CLIENTS_SETTING = "calendar.oauth_clients"


class TokenRefreshError(Exception):
    """The access token could not be refreshed this time (network, provider outage): retry later."""


class ReconnectRequired(TokenRefreshError):
    """The provider refused the refresh token (or there is none): the owner must connect the account again."""


class TokenSource(Protocol):
    can_refresh: bool

    def access_token(self) -> str: ...

    def refresh(self) -> str: ...


class StaticToken:
    """A fixed bearer token (tests, or an account connected with an access token only)."""

    can_refresh = False

    def __init__(self, token: str) -> None:
        self.token = token

    def access_token(self) -> str:
        return self.token

    def refresh(self) -> str:
        raise ReconnectRequired(_("The access token was refused and there is no refresh token"))


class BearerAuth(httpx.Auth):
    """Adds ``Authorization: Bearer …``; on a 401 refreshes the token once and replays the request."""

    def __init__(self, tokens: TokenSource | str) -> None:
        self.tokens: TokenSource = StaticToken(tokens) if isinstance(tokens, str) else tokens

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self.tokens.access_token()}"
        response = yield request
        if response.status_code == 401 and self.tokens.can_refresh:
            request.headers["Authorization"] = f"Bearer {self.tokens.refresh()}"
            yield request


def client_credentials(account: Any) -> tuple[str, str, str]:
    """(client id, client secret, token endpoint) for the account's provider."""
    provider = account.provider
    settings = get_settings()
    company = account.user_id.company_id if account.user_id else None
    clients = company.get_setting(CLIENTS_SETTING) if company else None
    own = clients.get(provider) if isinstance(clients, dict) else None

    def setting(key: str) -> str | None:
        value = own.get(key) if isinstance(own, dict) else None
        return crypto.decrypt(str(value)) if value else None

    if provider == "google":
        cid = setting("client_id") or settings.google_oauth_client_id
        secret = setting("client_secret") or settings.google_oauth_client_secret
        url = GOOGLE_TOKEN_URL
    elif provider == "microsoft":
        cid = setting("client_id") or settings.microsoft_oauth_client_id
        secret = setting("client_secret") or settings.microsoft_oauth_client_secret
        tenant = setting("tenant") or settings.microsoft_oauth_tenant or "common"
        url = MICROSOFT_TOKEN_URL.format(tenant=tenant)
    else:
        raise ReconnectRequired(_("{provider} accounts do not use OAuth", provider=provider))
    if not cid or not secret:
        raise ReconnectRequired(
            _(
                "No OAuth client is configured for {provider}: set it in the company setting {setting} "
                "or in the server configuration",
                provider=provider,
                setting=CLIENTS_SETTING,
            )
        )
    return cid, secret, url


class AccountTokens:
    """Access token of a ``calendar.sync_account`` (an elevated record), refreshed when needed.

    Refreshed tokens are written to the account at once, in the caller's transaction."""

    def __init__(self, account: Any, client: httpx.Client | None = None) -> None:
        self.account = account
        self.client = client
        self.can_refresh = bool(account.refresh_token)

    def access_token(self) -> str:
        acc = self.account
        token = crypto.decrypt(acc.access_token) if acc.access_token else None
        token = token or (crypto.decrypt(acc.secret) if acc.secret else None)  # accounts connected before 1.1
        expiring = acc.token_expiry is not None and acc.token_expiry - REFRESH_MARGIN <= acc.env.now()
        if self.can_refresh and (not token or expiring):
            return self.refresh()
        if not token:
            raise ReconnectRequired(_("The account has no access token"))
        return token

    def refresh(self) -> str:
        acc = self.account
        refresh_token = crypto.decrypt(acc.refresh_token) if acc.refresh_token else None
        if not refresh_token:
            raise ReconnectRequired(_("The access token was refused and there is no refresh token"))
        client_id, client_secret, url = client_credentials(acc)
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if acc.provider == "microsoft":
            form["scope"] = MICROSOFT_SCOPE
        http = self.client or httpx.Client(timeout=20)
        try:
            resp = http.post(url, data=form, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise TokenRefreshError(_("Token endpoint unreachable: {exc}", exc=exc)) from exc
        finally:
            if self.client is None:
                http.close()
        try:
            data: dict[str, Any] = resp.json()
        except ValueError:
            data = {}
        if resp.status_code in (400, 401) and data.get("error") in (
            "invalid_grant",
            "invalid_client",
            "unauthorized_client",
            "invalid_request",
            "interaction_required",
        ):
            detail = data.get("error_description") or data["error"]
            raise ReconnectRequired(_("The provider refused the refresh token ({detail})", detail=str(detail)[:300]))
        if resp.status_code >= 400 or not data.get("access_token"):
            raise TokenRefreshError(_("Token refresh failed with HTTP {status_code}", status_code=resp.status_code))
        access = str(data["access_token"])
        vals: dict[str, Any] = {"access_token": access}
        expires_in = data.get("expires_in")
        vals["token_expiry"] = (
            acc.env.now() + dt.timedelta(seconds=int(str(expires_in))) if str(expires_in or "").isdigit() else None
        )
        if data.get("refresh_token"):  # Microsoft rotates refresh tokens; Google keeps the same one
            vals["refresh_token"] = str(data["refresh_token"])
        acc.write(vals)
        log.info("calendar access token refreshed", account=acc.ids[0], provider=acc.provider)
        return access
