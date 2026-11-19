"""Microsoft 365 through Microsoft Graph. Incremental sync uses the events delta query on the calendar
(series masters, not expanded instances); remote id = Graph event id, etag = changeKey."""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx

from modules.calendar.ics import RemoteEvent
from modules.calendar.sync.base import SyncConflict
from modules.calendar.sync.oauth import BearerAuth, TokenSource

GRAPH = "https://graph.microsoft.com"
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
RRULE_DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def rrule_to_pattern(rule: str, start: dt.datetime) -> dict[str, Any]:
    parts = dict(p.split("=", 1) for p in rule.split(";") if "=" in p)
    freq = parts["FREQ"].upper()
    interval = int(parts.get("INTERVAL", 1))
    pattern: dict[str, Any] = {"interval": interval}
    if freq == "DAILY":
        pattern["type"] = "daily"
    elif freq == "WEEKLY":
        pattern["type"] = "weekly"
        days = parts.get("BYDAY", RRULE_DAYS[start.weekday()]).split(",")
        pattern["daysOfWeek"] = [DAYS[RRULE_DAYS.index(d[-2:])] for d in days]
        pattern["firstDayOfWeek"] = "monday"
    elif freq == "MONTHLY":
        pattern.update({"type": "absoluteMonthly", "dayOfMonth": int(parts.get("BYMONTHDAY", start.day))})
    else:
        pattern.update({"type": "absoluteYearly", "dayOfMonth": start.day, "month": start.month})
    rng: dict[str, Any] = {"startDate": start.date().isoformat(), "recurrenceTimeZone": "UTC"}
    if "COUNT" in parts:
        rng.update({"type": "numbered", "numberOfOccurrences": int(parts["COUNT"])})
    elif "UNTIL" in parts:
        rng.update(
            {"type": "endDate", "endDate": dt.datetime.strptime(parts["UNTIL"][:8], "%Y%m%d").date().isoformat()}
        )
    else:
        rng["type"] = "noEnd"
    return {"pattern": pattern, "range": rng}


def pattern_to_rrule(recurrence: dict[str, Any]) -> str:
    pattern, rng = recurrence["pattern"], recurrence["range"]
    kind = pattern["type"]
    freq = {"daily": "DAILY", "weekly": "WEEKLY", "absoluteMonthly": "MONTHLY", "absoluteYearly": "YEARLY"}[kind]
    parts = [f"FREQ={freq}"]
    if pattern.get("interval", 1) != 1:
        parts.append(f"INTERVAL={pattern['interval']}")
    if kind == "weekly" and pattern.get("daysOfWeek"):
        parts.append("BYDAY=" + ",".join(RRULE_DAYS[DAYS.index(d)] for d in pattern["daysOfWeek"]))
    if kind == "absoluteMonthly" and pattern.get("dayOfMonth"):
        parts.append(f"BYMONTHDAY={pattern['dayOfMonth']}")
    if rng["type"] == "numbered":
        parts.append(f"COUNT={rng['numberOfOccurrences']}")
    elif rng["type"] == "endDate":
        parts.append(f"UNTIL={rng['endDate'].replace('-', '')}T235959Z")
    return ";".join(parts)


def _time(value: dict[str, str]) -> dt.datetime:
    return dt.datetime.fromisoformat(value["dateTime"][:26]).replace(tzinfo=dt.UTC)


def to_remote(item: dict[str, Any]) -> RemoteEvent:
    return RemoteEvent(
        uid=item.get("iCalUId") or item["id"],
        summary=item.get("subject", ""),
        start=_time(item["start"]),
        end=_time(item["end"]),
        allday=bool(item.get("isAllDay")),
        location=(item.get("location") or {}).get("displayName") or None,
        description=(item.get("body") or {}).get("content") or None,
        rrule=pattern_to_rrule(item["recurrence"]) if item.get("recurrence") else None,
        attendees=[a["emailAddress"]["address"] for a in item.get("attendees", []) if a.get("emailAddress")],
        etag=item.get("changeKey"),
        href=item["id"],
    )


def to_body(event: RemoteEvent) -> dict[str, Any]:
    body: dict[str, Any] = {
        "subject": event.summary,
        "start": {"dateTime": event.start.astimezone(dt.UTC).replace(tzinfo=None).isoformat(), "timeZone": "UTC"},
        "end": {"dateTime": event.end.astimezone(dt.UTC).replace(tzinfo=None).isoformat(), "timeZone": "UTC"},
        "isAllDay": event.allday,
        "transactionId": event.uid,
    }
    if event.location:
        body["location"] = {"displayName": event.location}
    if event.description:
        body["body"] = {"contentType": "html", "content": event.description}
    body["recurrence"] = rrule_to_pattern(event.rrule, event.start) if event.rrule else None
    if event.attendees:
        body["attendees"] = [{"emailAddress": {"address": e}, "type": "required"} for e in event.attendees]
    return body


class MicrosoftAdapter:
    def __init__(
        self, token: str | TokenSource, calendar_id: str | None = None, client: httpx.Client | None = None
    ) -> None:
        cal = f"calendars/{calendar_id}" if calendar_id and calendar_id != "primary" else "calendar"
        self.events = f"{GRAPH}/v1.0/me/{cal}/events"
        self.delta = f"{GRAPH}/beta/me/{cal}/events/delta"
        self.client = client or httpx.Client(timeout=20)
        self.auth = BearerAuth(token)  # refreshes an expired OAuth token once on 401
        self.headers = {"Prefer": 'outlook.timezone="UTC"'}

    def changes(self, sync_token: str | None) -> tuple[list[RemoteEvent], list[str], str | None]:
        url = sync_token or self.delta
        changed: list[RemoteEvent] = []
        deleted: list[str] = []
        while True:
            resp = self.client.get(url, headers=self.headers, auth=self.auth)
            if resp.status_code == 410 and sync_token:
                return self.changes(None)
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("value", []):
                if "@removed" in item:
                    deleted.append(item["id"])
                elif item.get("type", "singleInstance") in ("singleInstance", "seriesMaster"):
                    changed.append(to_remote(item))
            if data.get("@odata.nextLink"):
                url = data["@odata.nextLink"]
                continue
            return changed, deleted, data.get("@odata.deltaLink")

    def put(self, event: RemoteEvent, remote_id: str | None, etag: str | None) -> tuple[str, str | None]:
        headers = dict(self.headers)
        if remote_id:
            if etag:
                headers["If-Match"] = etag
            resp = self.client.patch(f"{self.events}/{remote_id}", json=to_body(event), headers=headers, auth=self.auth)
        else:
            resp = self.client.post(self.events, json=to_body(event), headers=headers, auth=self.auth)
        if resp.status_code == 412:
            raise SyncConflict(remote_id)
        resp.raise_for_status()
        data = resp.json()
        return data["id"], data.get("changeKey")

    def delete(self, remote_id: str, etag: str | None) -> None:
        resp = self.client.delete(f"{self.events}/{remote_id}", headers=self.headers, auth=self.auth)
        if resp.status_code not in (200, 204, 404):
            resp.raise_for_status()
