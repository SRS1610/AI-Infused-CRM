"""Google Calendar API v3 (incremental sync with syncToken). Remote id = Google event id."""

from __future__ import annotations

import datetime as dt
import urllib.parse
from typing import Any

import httpx

from modules.calendar.ics import RemoteEvent
from modules.calendar.sync.base import SyncConflict

API = "https://www.googleapis.com/calendar/v3"


def _parse_time(value: dict[str, Any]) -> tuple[dt.datetime, bool]:
    if "date" in value:
        return dt.datetime.combine(dt.date.fromisoformat(value["date"]), dt.time(0), dt.UTC), True
    return dt.datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00")).astimezone(dt.UTC), False


def to_remote(item: dict[str, Any]) -> RemoteEvent:
    start, allday = _parse_time(item["start"])
    end, _ = _parse_time(item["end"])
    rrule, exdates = None, []
    for line in item.get("recurrence", []):
        if line.startswith("RRULE:"):
            rrule = line[6:]
        elif line.startswith("EXDATE"):
            for value in line.split(":", 1)[1].split(","):
                exdates.append(dt.datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S").replace(tzinfo=dt.UTC))
    return RemoteEvent(
        uid=item.get("iCalUID") or item["id"],
        summary=item.get("summary", ""),
        start=start,
        end=end,
        allday=allday,
        location=item.get("location"),
        description=item.get("description"),
        rrule=rrule,
        exdates=exdates,
        attendees=[a["email"] for a in item.get("attendees", []) if a.get("email")],
        etag=item.get("etag"),
        href=item["id"],
    )


def to_body(event: RemoteEvent) -> dict[str, Any]:
    def when(value: dt.datetime) -> dict[str, str]:
        return (
            {"date": value.date().isoformat()}
            if event.allday
            else {"dateTime": value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z"), "timeZone": "UTC"}
        )

    body: dict[str, Any] = {
        "summary": event.summary,
        "start": when(event.start),
        "end": when(event.end),
        "iCalUID": event.uid,
    }
    if event.location:
        body["location"] = event.location
    if event.description:
        body["description"] = event.description
    recurrence = []
    if event.rrule:
        recurrence.append(f"RRULE:{event.rrule}")
    if event.exdates:
        recurrence.append("EXDATE:" + ",".join(e.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ") for e in event.exdates))
    if recurrence:
        body["recurrence"] = recurrence
    if event.attendees:
        body["attendees"] = [{"email": e} for e in event.attendees]
    return body


class GoogleAdapter:
    def __init__(self, token: str, calendar_id: str = "primary", client: httpx.Client | None = None) -> None:
        self.base = f"{API}/calendars/{urllib.parse.quote(calendar_id, safe='')}/events"
        self.client = client or httpx.Client(timeout=20)
        self.headers = {"Authorization": f"Bearer {token}"}

    def changes(self, sync_token: str | None) -> tuple[list[RemoteEvent], list[str], str | None]:
        changed: list[RemoteEvent] = []
        deleted: list[str] = []
        params: dict[str, Any] = {"showDeleted": "true", "maxResults": 250}
        if sync_token:
            params["syncToken"] = sync_token
        while True:
            resp = self.client.get(self.base, params=params, headers=self.headers)
            if resp.status_code == 410:  # token invalidated: full resync
                return self.changes(None)
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("items", []):
                if item.get("status") == "cancelled":
                    deleted.append(item["id"])
                else:
                    changed.append(to_remote(item))
            if data.get("nextPageToken"):
                params["pageToken"] = data["nextPageToken"]
                continue
            return changed, deleted, data.get("nextSyncToken")

    def put(self, event: RemoteEvent, remote_id: str | None, etag: str | None) -> tuple[str, str | None]:
        headers = dict(self.headers)
        if remote_id:
            if etag:
                headers["If-Match"] = etag
            resp = self.client.put(f"{self.base}/{remote_id}", json=to_body(event), headers=headers)
        else:
            resp = self.client.post(self.base, json=to_body(event), headers=headers)
        if resp.status_code == 412:
            raise SyncConflict(remote_id)
        resp.raise_for_status()
        data = resp.json()
        return data["id"], data.get("etag")

    def delete(self, remote_id: str, etag: str | None) -> None:
        resp = self.client.delete(f"{self.base}/{remote_id}", headers=self.headers)
        if resp.status_code not in (200, 204, 404, 410):
            resp.raise_for_status()
