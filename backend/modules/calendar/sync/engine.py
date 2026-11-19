"""Two-way sync of one account: pull remote changes, then push local ones.

* Echo suppression: a pulled event whose etag equals the link's etag is our own write coming back.
* Conflicts: if both sides changed since the last sync, the most recently written side wins (remote
  wins ties); the losing version is recorded in the event's chatter.
* Deletions propagate both ways (local deletions are archived events with a link).
* Modified single occurrences (RECURRENCE-ID / exceptions) are not synchronised yet; cancelled
  occurrences (EXDATE) are.
* Google and Microsoft accounts refresh their OAuth access token before it expires or when the provider
  answers 401 (``sync/oauth.py``). When the provider refuses the refresh token the account is set to
  *Needs reconnection*, skipped by the scheduled sync, and its owner is notified once.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx

from modules.calendar.ics import RemoteEvent, content_hash, from_event
from modules.calendar.sync.base import CalendarAdapter, SyncConflict
from modules.calendar.sync.oauth import AccountTokens, ReconnectRequired
from tessera.i18n import _
from tessera.jobs import job
from tessera.logging import get_logger
from tessera.security import crypto

log = get_logger(__name__)


def make_adapter(account: Any, client: httpx.Client | None = None) -> CalendarAdapter:
    """The provider adapter of ``account``; ``client`` (tests) carries both API and token requests."""
    acc = account.elevated()
    if acc.provider == "caldav":
        from modules.calendar.sync.caldav import CalDavAdapter

        secret = crypto.decrypt(acc.secret) if acc.secret else None
        return CalDavAdapter(acc.url, acc.username, secret, client=client)
    tokens = AccountTokens(acc, client)
    if acc.provider == "google":
        from modules.calendar.sync.google import GoogleAdapter

        return GoogleAdapter(tokens, acc.calendar_id or "primary", client=client)
    from modules.calendar.sync.microsoft import MicrosoftAdapter

    return MicrosoftAdapter(tokens, acc.calendar_id, client=client)


def needs_reconnection(account: Any, reason: str) -> None:
    """Stop syncing ``account`` until its owner connects it again, and tell them (once)."""
    acc = account.elevated()
    already = acc.state == "reconnect"
    acc.write({"state": "reconnect", "last_error": reason[:2000]})
    log.warning("calendar account needs reconnection", account=acc.ids[0], reason=reason[:200])
    if already or not acc.user_id:
        return
    acc.env["core.notification"].notify(
        [acc.user_id.ids[0]],
        _("Reconnect your calendar: {name}", name=acc.name),
        _(
            "Tessera can no longer synchronise this calendar because the provider refused its access. "
            "Connect the account again under Calendar → External calendars."
        ),
        kind="system",
        model="calendar.sync_account",
        res_id=acc.ids[0],
        url=f"/m/calendar.sync_account/{acc.ids[0]}",
    )


def _values(remote: RemoteEvent) -> dict[str, Any]:
    return {
        "name": remote.summary or "(no title)",
        "start": remote.start,
        "stop": remote.end if remote.end > remote.start else remote.start,
        "allday": remote.allday,
        "location": remote.location,
        "description": remote.description,
        "rrule": remote.rrule,
        "exdates": sorted(e.astimezone(dt.UTC).isoformat() for e in remote.exdates),
    }


def sync_account(account: Any, adapter: CalendarAdapter | None = None) -> dict[str, Any]:
    env = account.env
    adapter = adapter or make_adapter(account)
    events = env["calendar.event"].with_context(active_test=False, tracking_disable=True)
    links = env["calendar.sync_link"]
    stats = {"pulled": 0, "pushed": 0, "deleted_local": 0, "deleted_remote": 0, "conflicts": 0}
    user = account.user_id
    try:
        changed, deleted, token = adapter.changes(account.sync_token)
        # -- pull --------------------------------------------------------------------------------------
        for remote in changed:
            if remote.extra.get("recurrence_id"):
                continue  # modified occurrence: not synchronised yet
            link = links.search(
                [("account_id", "=", account.ids[0]), ("remote_uid", "=", remote.href or remote.uid)], limit=1
            )
            if link and link.etag and remote.etag and link.etag == remote.etag:
                continue  # our own write echoing back
            if link and link.event_id:
                ev = link.event_id
                local_changed = bool(link.content_hash) and content_hash(from_event(ev)) != link.content_hash
                if local_changed:
                    stats["conflicts"] += 1
                    from modules.chatter.thread import post_message

                    post_message(
                        env,
                        "calendar.event",
                        ev.ids[0],
                        "<p>Changed on both sides; the external calendar's version was kept.</p>",
                        message_type="notification",
                        notify=False,
                    )
                ev.write({**_values(remote), "active": True})
                link.write(
                    {
                        "etag": remote.etag,
                        "synced_write_date": ev.write_date,
                        "content_hash": content_hash(from_event(ev)),
                    }
                )
            else:
                ev = events.create({**_values(remote), "user_id": user.ids[0], "sync_uid": remote.uid})
                links.create(
                    {
                        "account_id": account.ids[0],
                        "event_id": ev.ids[0],
                        "remote_uid": remote.href or remote.uid,
                        "etag": remote.etag,
                        "synced_write_date": ev.write_date,
                        "content_hash": content_hash(from_event(ev)),
                    }
                )
            stats["pulled"] += 1
        for remote_id in deleted:
            link = links.search([("account_id", "=", account.ids[0]), ("remote_uid", "=", remote_id)], limit=1)
            if link:
                if link.event_id and link.event_id.active:
                    link.event_id.write({"active": False})
                    stats["deleted_local"] += 1
                link.unlink()
        # -- push --------------------------------------------------------------------------------------
        mine = events.search([("user_id", "=", user.ids[0]), ("recurrence_id", "=", None)])
        linked = {lk.event_id.ids[0]: lk for lk in links.search([("account_id", "=", account.ids[0])]) if lk.event_id}
        for ev in mine:
            link = linked.get(ev.ids[0])
            if not ev.active:
                if link:
                    adapter.delete(link.remote_uid, link.etag)
                    link.unlink()
                    stats["deleted_remote"] += 1
                continue
            remote = from_event(ev)
            fingerprint = content_hash(remote)
            if link and link.content_hash == fingerprint:
                continue  # nothing we synchronise changed
            try:
                remote_id, etag = adapter.put(remote, link.remote_uid if link else None, link.etag if link else None)
            except SyncConflict:
                stats["conflicts"] += 1
                continue  # the next pull brings the remote version
            if link:
                link.write(
                    {
                        "etag": etag,
                        "synced_write_date": ev.write_date,
                        "remote_uid": remote_id,
                        "content_hash": fingerprint,
                    }
                )
            else:
                links.create(
                    {
                        "account_id": account.ids[0],
                        "event_id": ev.ids[0],
                        "remote_uid": remote_id,
                        "etag": etag,
                        "synced_write_date": ev.write_date,
                        "content_hash": fingerprint,
                    }
                )
            stats["pushed"] += 1
        # our pushes come back in the next pull with the etag we stored, and are skipped there
        account.write({"sync_token": token, "last_sync": env.now(), "last_error": None, "state": "active"})
    except ReconnectRequired as exc:
        needs_reconnection(account, str(exc))
        raise
    except Exception as exc:
        account.write({"last_error": str(exc)[:2000], "state": "error"})
        log.warning("calendar sync failed", account=account.ids[0], error=str(exc)[:200])
        raise
    return stats


@job("calendar.sync", queue="default", max_attempts=1, as_system=True, timeout_seconds=900)
def sync_all_job(env: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for account in env["calendar.sync_account"].search([("state", "in", ["active", "error"])]):
        try:
            out[str(account.ids[0])] = sync_account(account)
        except Exception as exc:
            out[str(account.ids[0])] = {"error": str(exc)[:200]}
    return out
