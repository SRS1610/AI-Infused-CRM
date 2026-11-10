"""In-memory stand-ins for the Google Calendar and Microsoft Graph event APIs, with the semantics the
adapters rely on (incremental tokens, etags/changeKeys, 412 on stale etags, deletions in deltas).
Payload shapes follow the providers' documented responses."""

from __future__ import annotations

import itertools
import json
from typing import Any

import httpx


class FakeGoogle:
    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.log: list[tuple[int, str]] = []  # (sequence, event id)
        self.seq = itertools.count(1)
        self.ids = itertools.count(1)

    def _token(self) -> str:
        return f"sync-{len(self.log)}"

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert request.headers.get("authorization", "").startswith("Bearer ")
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
    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.removed: set[str] = set()
        self.log: list[str] = []
        self.ids = itertools.count(1)
        self.keys = itertools.count(1)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
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
