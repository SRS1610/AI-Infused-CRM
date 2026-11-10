"""New channel messages: bump the channel, tell members' clients (unread badges), notify by preference."""

from __future__ import annotations

import re
from typing import Any

from tessera.orm import Model
from tessera.orm.hooks import hook
from tessera.realtime import publish_after_commit


@hook("after_create")
def channel_message_posted(records: Model, vals_list: list[dict[str, Any]]) -> None:
    if records._name != "chatter.message":
        return
    msgs = records.elevated().filtered(
        lambda m: m.model == "discuss.channel" and m.message_type in ("comment", "email")
    )
    if not msgs:
        return
    env = records.env
    channels = env["discuss.channel"].elevated("channel activity")
    members = env["discuss.member"].elevated()
    notifications = env["core.notification"].elevated("channel notify")
    for msg in msgs:
        channel = channels.browse([msg.res_id])
        channel.write({"last_message_at": msg.date or env.now()})
        author = msg.author_id.ids[0] if msg.author_id else None
        mentioned = set(msg.mention_user_ids.ids)
        preview = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", msg.body or "")).strip()[:200]
        for member in members.search([("channel_id", "=", channel.ids[0])]):
            uid = member.user_id.ids[0]
            if uid == author:
                member.write({"last_seen_message_id": msg.ids[0]})  # your own message is read
                continue
            publish_after_commit(
                env.cr,
                f"user:{uid}",
                "discuss",
                {"channel_id": channel.ids[0], "message_id": msg.ids[0], "author_id": author},
            )
            if member.notify == "all" and uid not in mentioned:  # mentions are notified by chatter already
                who = msg.author_id.name if msg.author_id else "Someone"
                title = f"{who} in #{channel.name}" if channel.channel_type == "channel" else f"{who}"
                notifications.create(
                    {
                        "user_id": uid,
                        "title": title,
                        "body": preview,
                        "kind": "message",
                        "model": "discuss.channel",
                        "res_id": channel.ids[0],
                    }
                )
