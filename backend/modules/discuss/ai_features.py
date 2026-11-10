"""Discuss AI features: channel/thread summary, "catch me up", reply drafts and translation.

Every context is built **as the requesting user**: a channel is read only after its record rules
allowed it (``check_access("read")`` / ``search`` as the user), so a channel the user cannot open
contributes no message, no name and no citation. Messages become ``chatter.message:<id>`` sources
(answers cite them; the runtime rejects any other id) and their authors ``core.user:<id>`` sources,
so the gateway's redaction knows their names (D-049 redaction policy ``redact``).

Nothing here writes: summaries, digests and translations are text; a reply draft becomes a
``message`` proposal that only the user's accept posts.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from typing import Any

from tessera.ai import Source
from tessera.ai.prompts import CITATION, html_to_text
from tessera.ai.runtime import handler, run_feature
from tessera.ai.tools import PendingProposal, ToolContext
from tessera.exceptions import AccessError, MissingError, ValidationError
from tessera.jobs import enqueue, job

MESSAGE_TYPES = ["comment", "email"]
INTERACTIVE_MAX = 50  # above this many messages a summary runs as a background job (BUILD_PLAN §2.6)
SUMMARY_DEFAULT_LIMIT = 100
SUMMARY_MAX = 300
CATCH_UP_PER_CHANNEL = 40
CATCH_UP_MAX = 150
REPLY_CONTEXT = 20
TONE_SAMPLES = 6
MAX_MESSAGE_CHARS = 2000


def _text(body: str | None) -> str:
    return re.sub(r"[ \t]+", " ", html_to_text(body or "")).strip()[:MAX_MESSAGE_CHARS]


def _author(msg: Any) -> str:
    return msg.author_id.name if msg.author_id else (msg.email_from or "Someone")


def _stamp(msg: Any) -> str:
    return msg.date.strftime("%Y-%m-%d %H:%M") if msg.date else ""


def message_source(msg: Any, *, prefix: str = "", note: str = "") -> Source:
    reply = f" (reply to message {msg.parent_id.ids[0]})" if msg.parent_id else ""
    title = f"{prefix}{_author(msg)} · {_stamp(msg)}"
    return Source(f"chatter.message:{msg.ids[0]}", title, f"{note}{_author(msg)}{reply}: {_text(msg.body)}")


def participant_sources(messages: Any, channel_name: str) -> list[Source]:
    """One ``core.user`` source per author: lets the gateway redact (and restore) their names."""
    seen: dict[int, str] = {}
    for m in messages:
        if m.author_id and m.author_id.ids[0] not in seen:
            seen[m.author_id.ids[0]] = m.author_id.name
    return [Source(f"core.user:{uid}", name, f"Participant in {channel_name}") for uid, name in seen.items()]


def readable_channel(env: Any, channel_id: int) -> Any:
    """The channel as the user sees it; AccessError / MissingError when record rules hide it."""
    channel = env["discuss.channel"].browse([int(channel_id)])
    if not channel.exists():
        raise MissingError("This channel does not exist")
    channel.check_access("read")
    return channel


def _label(channel: Any, uid: int) -> str:
    ch = channel.elevated()
    if ch.channel_type == "channel":
        return f"#{ch.name}"
    others = [m.user_id.name for m in ch.member_ids if m.user_id.ids != [uid]]
    return "Direct message with " + ", ".join(others) if others else ch.name


def _parse_when(value: Any, name: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        when = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"'{name}' must be an ISO date or date-time", field=name) from exc
    return when if when.tzinfo else when.replace(tzinfo=dt.UTC)


def summary_domain(channel: Any, inputs: dict[str, Any]) -> list[Any]:
    domain: list[Any] = [
        ("model", "=", "discuss.channel"),
        ("res_id", "=", channel.ids[0]),
        ("message_type", "in", MESSAGE_TYPES),
    ]
    if inputs.get("thread_id"):
        root = int(inputs["thread_id"])
        domain.append({"any": [["id", "=", root], ["parent_id", "=", root]]})
    since, until = _parse_when(inputs.get("since"), "since"), _parse_when(inputs.get("until"), "until")
    if since:
        domain.append(("date", ">=", since))
    if until:
        domain.append(("date", "<=", until))
    return domain


def summary_messages(env: Any, channel: Any, inputs: dict[str, Any]) -> Any:
    """Messages in range, oldest first (the newest ``limit`` when the range holds more)."""
    limit = min(int(inputs.get("limit") or (SUMMARY_MAX if inputs.get("since") else SUMMARY_DEFAULT_LIMIT)), SUMMARY_MAX)
    found = env["chatter.message"].elevated().search(summary_domain(channel, inputs), order="id desc", limit=limit)
    return found.browse(list(reversed(found.ids)))


def count_in_range(env: Any, channel: Any, inputs: dict[str, Any]) -> int:
    return int(env["chatter.message"].elevated().search_count(summary_domain(channel, inputs)))


# ----------------------------------------------------------------------------- summary


@handler("discuss.summary")
def summary_handler(env: Any, record: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    if record is None or record._name != "discuss.channel":
        raise ValidationError("Pick the channel to summarise")
    channel = readable_channel(env, record.ids[0])
    msgs = summary_messages(env, channel, inputs)
    label = _label(channel, env.uid)
    return {"sources": [*participant_sources(msgs, label), *(message_source(m) for m in msgs)]}


def summarize(env: Any, channel_id: int, inputs: dict[str, Any]) -> dict[str, Any]:
    """Interactive when the range holds ≤ 50 messages; otherwise a background job that notifies the user."""
    channel = readable_channel(env, channel_id)
    count = count_in_range(env, channel, inputs)
    if count == 0:
        return {"mode": "empty", "count": 0}
    if count > INTERACTIVE_MAX:
        queued = enqueue(
            env,
            "discuss.summarize",
            {"channel_id": channel.ids[0], "inputs": inputs},
            queue="ai",
            description=f"AI summary of {_label(channel, env.uid)}",
        )
        return {"mode": "background", "count": count, "job_id": queued.ids[0]}
    result = run_feature(env, "discuss.summary", inputs, record=channel, **summary_handler(env, channel, inputs))
    return {"mode": "interactive", "count": count, "result": result.to_dict(), "links": links_for(env, result)}


def summary_url(channel_id: int, run_id: int) -> str:
    return f"/a/discuss.action_discuss?channel={channel_id}&summary={run_id}"


@job("discuss.summarize", queue="ai", max_attempts=1, timeout_seconds=900)
def summarize_job(env: Any, channel_id: int, inputs: dict[str, Any]) -> dict[str, Any]:
    """Background summary (runs as the user who asked, so record rules apply again), then a notification."""
    channel = readable_channel(env, channel_id)
    label = _label(channel, env.uid)
    result = run_feature(
        env,
        "discuss.summary",
        inputs,
        record=channel,
        raise_errors=False,
        **summary_handler(env, channel, inputs),
    )
    ok = result.run.status == "ok"
    env["core.notification"].elevated("ai summary ready").create(
        {
            "user_id": env.uid,
            "title": f"Summary of {label} is ready" if ok else f"The summary of {label} could not be made",
            "body": (result.text[:240] if ok else "AI is unavailable right now; try again later."),
            "kind": "job",
            "model": "discuss.channel",
            "res_id": channel.ids[0],
            "url": summary_url(channel.ids[0], result.run.ids[0]),
        }
    )
    return {"run_id": result.run.ids[0], "status": result.run.status}


# ----------------------------------------------------------------------------- catch me up


def unread_messages(env: Any, max_messages: int = CATCH_UP_MAX) -> list[tuple[Any, Any]]:
    """``[(channel, messages)]`` of the user's channels with unread messages from others, the
    channels filtered by record rules as the user (a stale membership never leaks a channel)."""
    uid = env.uid
    members = env["discuss.member"].elevated().search([("user_id", "=", uid)])
    seen = {m.channel_id.ids[0]: int(m.last_seen_message_id or 0) for m in members if m.channel_id}
    if not seen:
        return []
    channels = env["discuss.channel"].search([("id", "in", sorted(seen))])
    out: list[tuple[Any, Any]] = []
    messages = env["chatter.message"].elevated()
    for channel in channels:
        found = messages.search(
            [
                ("model", "=", "discuss.channel"),
                ("res_id", "=", channel.ids[0]),
                ("id", ">", seen[channel.ids[0]]),
                ("message_type", "in", MESSAGE_TYPES),
                {"any": [["author_id", "is_not_set"], ["author_id", "!=", uid]]},
            ],
            order="id desc",
            limit=CATCH_UP_PER_CHANNEL,
        )
        if found:
            out.append((channel, found.browse(list(reversed(found.ids)))))

    def rank(item: tuple[Any, Any]) -> tuple[int, int, int]:
        channel, msgs = item
        mentions = sum(1 for m in msgs if uid in m.mention_user_ids.ids)
        return (0 if channel.elevated().channel_type != "channel" else 1, -mentions, -msgs.ids[-1])

    out.sort(key=rank)
    budget, trimmed = max_messages, []
    for channel, msgs in out:
        if budget <= 0:
            break
        keep = msgs.browse(msgs.ids[-budget:])
        trimmed.append((channel, keep))
        budget -= len(keep)
    return trimmed


@handler("discuss.catch_up")
def catch_up_handler(env: Any, record: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    uid = env.uid
    groups = unread_messages(env, int(inputs.get("max_messages") or CATCH_UP_MAX))
    sources: list[Source] = []
    all_msgs = env["chatter.message"].elevated()
    for channel, msgs in groups:
        all_msgs |= msgs
    sources += participant_sources(all_msgs, "your channels")
    for channel, msgs in groups:
        label = _label(channel, uid)
        mentions = sum(1 for m in msgs if uid in m.mention_user_ids.ids)
        kind = "direct conversation" if channel.elevated().channel_type != "channel" else "channel"
        sources.append(
            Source(
                f"discuss.channel:{channel.ids[0]}",
                label,
                f"{label} ({kind}): {len(msgs)} unread message(s), {mentions} mentioning you.",
            )
        )
        for m in msgs:
            note = "[mentions you] " if uid in m.mention_user_ids.ids else ""
            sources.append(message_source(m, prefix=f"{label} · ", note=note))
    return {"sources": sources}


def catch_up(env: Any, inputs: dict[str, Any] | None = None) -> dict[str, Any]:
    inputs = inputs or {}
    kwargs = catch_up_handler(env, None, inputs)
    unread = sum(1 for s in kwargs["sources"] if s.model == "chatter.message")
    if not unread:  # nothing to send to a model
        return {"unread": 0, "result": None, "links": {}}
    result = run_feature(env, "discuss.catch_up", inputs, **kwargs)
    return {"unread": unread, "result": result.to_dict(), "links": links_for(env, result)}


def links_for(env: Any, result: Any) -> dict[str, dict[str, Any]]:
    """Cited message id → the channel to open (only messages that were in the user's context)."""
    ids = [c["res_id"] for c in result.citations if c["res_model"] == "chatter.message"]
    out: dict[str, dict[str, Any]] = {}
    for m in env["chatter.message"].elevated().browse(ids).exists():
        if m.model == "discuss.channel":
            out[str(m.ids[0])] = {"channel_id": m.res_id, "author": _author(m), "date": _stamp(m)}
    return out


# ----------------------------------------------------------------------------- reply draft


def _strip_markers(text: str) -> str:
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"\s*" + CITATION.pattern, "", text or "")).strip()


def _reply_proposal(channel_id: int, title: str) -> Any:
    def hook(ctx: ToolContext, text: str, data: Any) -> None:
        ctx.proposals.append(
            PendingProposal("message", "discuss.channel", channel_id, {"body": _strip_markers(text)}, title)
        )

    return hook


@handler("discuss.reply_draft")
def reply_draft_handler(env: Any, record: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    if record is None or record._name != "discuss.channel":
        raise ValidationError("Pick the conversation to reply in")
    channel = readable_channel(env, record.ids[0])
    domain: list[Any] = [
        ("model", "=", "discuss.channel"),
        ("res_id", "=", channel.ids[0]),
        ("message_type", "in", MESSAGE_TYPES),
    ]
    if inputs.get("parent_id"):
        root = int(inputs["parent_id"])
        domain.append({"any": [["id", "=", root], ["parent_id", "=", root]]})
    messages = env["chatter.message"].elevated()
    found = messages.search(domain, order="id desc", limit=REPLY_CONTEXT)
    context = found.browse(list(reversed(found.ids)))
    # tone profile: the user's own recent messages, only from channels the user can still read
    mine = messages.search(
        [("model", "=", "discuss.channel"), ("author_id", "=", env.uid), ("message_type", "=", "comment")],
        order="id desc",
        limit=40,
    )
    readable = set(env["discuss.channel"].search([("id", "in", sorted({m.res_id for m in mine}))]).ids)
    tone = [m for m in mine if m.res_id in readable and m.ids[0] not in context.ids][:TONE_SAMPLES]
    label = _label(channel, env.uid)
    sources = [
        *participant_sources(context, label),
        *(message_source(m) for m in context),
        *(message_source(m, prefix="Tone sample · ") for m in tone),
    ]
    return {"sources": sources, "on_output": _reply_proposal(channel.ids[0], f"Reply in {label}")}


# ----------------------------------------------------------------------------- translation


@handler("discuss.translate")
def translate_handler(env: Any, record: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    from modules.chatter.thread import check_thread_access, is_internal_user

    msg = env["chatter.message"].elevated().browse([int(inputs.get("message_id") or 0)]).exists()
    if not msg or msg.message_type not in (*MESSAGE_TYPES, "note"):
        raise MissingError("This message does not exist")
    check_thread_access(env, msg.model, msg.res_id, "read")  # the thread's record rules decide
    if msg.is_internal and not is_internal_user(env):
        raise AccessError("Not allowed")
    return {"sources": [Source(f"chatter.message:{msg.ids[0]}", f"Message from {_author(msg)}", _text(msg.body))]}


# chrF (Popović 2015) as computed by sacreBLEU 2.x: character n-grams for n = 1..6 with whitespace
# removed, an F-beta score (beta = 2, recall weighted higher) per order, averaged over the orders both
# strings are long enough for. Reported on a 0..1 scale (sacreBLEU prints 0..100).
#
# Why 0.6: on short business chat messages with a single reference, faithful translations worded
# differently from the reference typically land between ~0.6 and ~0.9 chrF, while an untranslated
# message or one in the wrong language falls to ~0.1-0.35 and a translation that drops or changes a
# clause falls to ~0.45-0.55. 0.6 therefore separates "faithful, worded differently" from "broken",
# per language pair. COMET would be a better quality signal but needs a neural model the replay CI
# cannot run; the nightly live run can add it later.
CHRF_ORDER = 6
CHRF_BETA = 2.0


def _char_ngrams(text: str, n: int) -> Counter[str]:
    s = re.sub(r"\s+", "", text)
    return Counter(s[i : i + n] for i in range(len(s) - n + 1))


def chrf(hypothesis: str, reference: str, order: int = CHRF_ORDER, beta: float = CHRF_BETA) -> float:
    """Sentence-level chrF in 0..1."""
    b2 = beta * beta
    total, effective = 0.0, 0
    for n in range(1, order + 1):
        hyp, ref = _char_ngrams(hypothesis, n), _char_ngrams(reference, n)
        if not hyp or not ref:
            continue  # a string shorter than n characters: that order does not count
        effective += 1
        overlap = sum((hyp & ref).values())
        p, r = overlap / sum(hyp.values()), overlap / sum(ref.values())
        total += (1 + b2) * p * r / (b2 * p + r) if p + r else 0.0
    if not effective:
        return 1.0 if hypothesis.strip() == reference.strip() else 0.0
    return total / effective


def chrf_scorer(results: list[dict[str, Any]]) -> dict[str, float]:
    """Aggregate eval scorer: mean sentence chrF overall and per language pair (``expected.pair``)."""
    per_pair: dict[str, list[float]] = {}
    for r in results:
        exp = r["_case"].get("expected", {})
        if "reference" not in exp:
            continue
        score = chrf(r.get("text") or "", exp["reference"]) if r.get("status") == "ok" else 0.0
        per_pair.setdefault(str(exp.get("pair", "all")), []).append(score)
    out = {f"chrf_{pair}": sum(v) / len(v) for pair, v in per_pair.items()}
    scores = [s for v in per_pair.values() for s in v]
    if scores:
        out["chrf"] = sum(scores) / len(scores)
    return out
