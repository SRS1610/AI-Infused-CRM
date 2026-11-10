"""CalDAV (RFC 4791) with WebDAV collection sync (RFC 6578). Resources we create are named <uid>.ics, so a
remote id is the resource href."""

from __future__ import annotations

import urllib.parse
from xml.etree import ElementTree as ET

import httpx

from modules.calendar.ics import RemoteEvent, from_ics, to_ics
from modules.calendar.sync.base import SyncConflict

NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav"}
SYNC_BODY = """<?xml version="1.0" encoding="utf-8"?>
<d:sync-collection xmlns:d="DAV:"><d:sync-token>{token}</d:sync-token><d:sync-level>1</d:sync-level>
<d:prop><d:getetag/></d:prop></d:sync-collection>"""


class CalDavAdapter:
    def __init__(
        self, url: str, username: str | None, password: str | None, client: httpx.Client | None = None
    ) -> None:
        self.url = url if url.endswith("/") else url + "/"
        self.client = client or httpx.Client(auth=(username, password or "") if username else None, timeout=20)

    def _abs(self, href: str) -> str:
        return urllib.parse.urljoin(self.url, href)

    def changes(self, sync_token: str | None) -> tuple[list[RemoteEvent], list[str], str | None]:
        body = SYNC_BODY.format(token=sync_token or "")
        resp = self.client.request(
            "REPORT", self.url, content=body, headers={"Depth": "1", "Content-Type": "application/xml"}
        )
        if resp.status_code in (403, 409) and sync_token:  # token expired: start over
            return self.changes(None)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)  # noqa: S314
        changed: list[RemoteEvent] = []
        deleted: list[str] = []
        base_path = urllib.parse.urlparse(self.url).path
        for response in root.findall("d:response", NS):
            href = response.findtext("d:href", default="", namespaces=NS)
            if not href or urllib.parse.urlparse(self._abs(href)).path.rstrip("/") == base_path.rstrip("/"):
                continue
            status = response.findtext("d:status", default="", namespaces=NS)
            if "404" in status:
                deleted.append(urllib.parse.urlparse(self._abs(href)).path)
                continue
            got = self.client.get(self._abs(href))
            if got.status_code == 404:
                deleted.append(urllib.parse.urlparse(self._abs(href)).path)
                continue
            got.raise_for_status()
            for ev in from_ics(
                got.text, etag=got.headers.get("ETag"), href=urllib.parse.urlparse(self._abs(href)).path
            ):
                changed.append(ev)
        token = root.findtext("d:sync-token", default=None, namespaces=NS)
        return changed, deleted, token

    def put(self, event: RemoteEvent, remote_id: str | None, etag: str | None) -> tuple[str, str | None]:
        href = remote_id or urllib.parse.urlparse(self.url + urllib.parse.quote(event.uid, safe="") + ".ics").path
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if etag:
            headers["If-Match"] = etag
        elif remote_id is None:
            headers["If-None-Match"] = "*"
        resp = self.client.put(self._abs(href), content=to_ics(event).encode(), headers=headers)
        if resp.status_code == 412:
            raise SyncConflict(href)
        resp.raise_for_status()
        new_etag = resp.headers.get("ETag")
        if not new_etag:
            new_etag = self.client.head(self._abs(href)).headers.get("ETag")
        return href, new_etag

    def delete(self, remote_id: str, etag: str | None) -> None:
        resp = self.client.delete(self._abs(remote_id), headers={"If-Match": etag} if etag else {})
        if resp.status_code not in (200, 204, 404, 412):
            resp.raise_for_status()
