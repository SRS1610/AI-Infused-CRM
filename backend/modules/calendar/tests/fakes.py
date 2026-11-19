"""In-memory stand-ins for the Google Calendar and Microsoft Graph event APIs, with the semantics the
adapters rely on (incremental tokens, etags/changeKeys, 412 on stale etags, deletions in deltas), and for
the providers' OAuth token endpoints (refresh-token grant). Payload shapes follow the providers'
documented responses."""

from __future__ import annotations

import itertools
import json
import urllib.parse
from typing import Any

import httpx


class FakeTokenEndpoint:
    """``POST /token`` with ``grant_type=refresh_token`` (Google) or ``/<tenant>/oauth2/v2.0/token``
    (Microsoft identity platform). Issues access tokens the API fakes accept (``valid`` is shared with
    them); ``rotate`` issues a new refresh token on every refresh, as Microsoft does."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str, *, rotate: bool = False) -> None:
        self.client_id, self.client_secret, self.rotate = client_id, client_secret, rotate
        self.refresh_tokens = {refresh_token}
        self.valid: set[str] = set()
        self.expires_in = 3599
        self.calls: list[dict[str, str]] = []
        self.counter = itertools.count(1)
        self.fail_with: int | None = None  # simulate an outage (e.g. 503)

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        self.calls.append(form)
        if self.fail_with:
            return httpx.Response(self.fail_with, text="unavailable")
        if form.get("client_id") != self.client_id or form.get("client_secret") != self.client_secret:
            return httpx.Response(401, json={"error": "invalid_client", "error_description": "Unauthorized"})
        if form.get("grant_type") != "refresh_token" or form.get("refresh_token") not in self.refresh_tokens:
            return httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."}
            )
        access = f"access-{next(self.counter)}"
        self.valid.add(access)
        body: dict[str, Any] = {"access_token": access, "expires_in": self.expires_in, "token_type": "Bearer"}
        if self.rotate:
            self.refresh_tokens.discard(form["refresh_token"])
            new = f"refresh-{next(self.counter)}"
            self.refresh_tokens.add(new)
            body["refresh_token"] = new
        return httpx.Response(200, json=body)

    def expire_all(self) -> None:
        """Every access token issued so far stops working (the provider answers 401)."""
        self.valid.clear()

    def revoke(self) -> None:
        """The user revoked the app's access: refresh tokens no longer work."""
        self.refresh_tokens.clear()
        self.valid.clear()


def _authorized(request: httpx.Request, valid: set[str] | None) -> bool:
    auth = request.headers.get("authorization", "")
    assert auth.startswith("Bearer ")
    return valid is None or auth[7:] in valid


def _unauthorized() -> httpx.Response:
    return httpx.Response(401, json={"error": {"code": 401, "message": "Request had invalid credentials."}})


class FakeGoogle:
    def __init__(self, valid_tokens: set[str] | None = None) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.log: list[tuple[int, str]] = []  # (sequence, event id)
        self.seq = itertools.count(1)
        self.ids = itertools.count(1)
        self.valid_tokens = valid_tokens  # None: any bearer token is accepted

    def _token(self) -> str:
        return f"sync-{len(self.log)}"

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if not _authorized(request, self.valid_tokens):
            return _unauthorized()
        event_id = path.rsplit("/events", 1)[1].strip("/") or None
        if request.method == "GET":
            token = request.url.params.get("syncToken")
            since = int(token.split("-")[1]) if token else 0
            changed_ids = list(dict.fromkeys(eid for _, eid in self.log[since:])) if token else list(self.events)
            items = [self.events[eid] for eid in changed_ids if eid in self.events]
            return httpx.Response(200, json={"kind": "calendar#events", "items": items, "nextSyncToken": self._token()})
        if request.method == "POST":
            body = json.loads(request.content)
            eid = f"g{next(self.ids)}"
            item = {**body, "id": eid, "status": "confirmed", "etag": f'"{next(self.seq)}"', "kind": "calendar#event"}
            self.events[eid] = item
            self.log.append((len(self.log), eid))
            return httpx.Response(200, json=item)
        current = self.events.get(event_id or "")
        if current is None:
            return httpx.Response(404, json={"error": {"code": 404}})
        if request.method == "PUT":
            if request.headers.get("if-match") and request.headers["if-match"] != current["etag"]:
                return httpx.Response(412, json={"error": {"code": 412, "message": "Precondition Failed"}})
            body = json.loads(request.content)
            item = {**body, "id": event_id, "status": "confirmed", "etag": f'"{next(self.seq)}"'}
            self.events[event_id] = item  # type: ignore[index]
            self.log.append((len(self.log), event_id))  # type: ignore[arg-type]
            return httpx.Response(200, json=item)
        if request.method == "DELETE":
            current["status"] = "cancelled"
            self.log.append((len(self.log), event_id))  # type: ignore[arg-type]
            return httpx.Response(204)
        return httpx.Response(405)


class FakeGraph:
    def __init__(self, valid_tokens: set[str] | None = None) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.removed: set[str] = set()
        self.log: list[str] = []
        self.ids = itertools.count(1)
        self.keys = itertools.count(1)
        self.valid_tokens = valid_tokens

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if not _authorized(request, self.valid_tokens):
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})
        if path.endswith("/events/delta"):
            since = int(request.url.params.get("since", "0"))
            ids = list(dict.fromkeys(self.log[since:])) if "since" in request.url.params else list(self.events)
            value = []
            for eid in ids:
                if eid in self.removed:
                    value.append({"id": eid, "@removed": {"reason": "deleted"}})
                elif eid in self.events:
                    value.append(self.events[eid])
            delta = f"https://graph.microsoft.com/beta/me/calendar/events/delta?since={len(self.log)}"
            return httpx.Response(200, json={"value": value, "@odata.deltaLink": delta})
        if request.method == "POST" and path.endswith("/events"):
            body = json.loads(request.content)
            eid = f"m{next(self.ids)}"
            item = {
                **body,
                "id": eid,
                "type": "seriesMaster" if body.get("recurrence") else "singleInstance",
                "changeKey": f"ck{next(self.keys)}",
            }
            self.events[eid] = item
            self.log.append(eid)
            return httpx.Response(201, json=item)
        eid = path.rsplit("/", 1)[1]
        current = self.events.get(eid)
        if current is None or eid in self.removed:
            return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
        if request.method == "PATCH":
            if request.headers.get("if-match") and request.headers["if-match"] != current["changeKey"]:
                return httpx.Response(412, json={"error": {"code": "ErrorIrresolvableConflict"}})
            current.update(json.loads(request.content))
            current["changeKey"] = f"ck{next(self.keys)}"
            self.log.append(eid)
            return httpx.Response(200, json=current)
        if request.method == "DELETE":
            self.removed.add(eid)
            self.log.append(eid)
            return httpx.Response(204)
        return httpx.Response(405)
