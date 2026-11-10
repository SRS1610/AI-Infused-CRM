import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, request } from "../../kernel/api/client";
import { chatter } from "../../kernel/api/platform";
import { Composer, MessageItem } from "../../kernel/chatter/Chatter";
import { registerClientAction } from "../../kernel/extensions";
import { useRealtime } from "../../kernel/realtime";
import { useSession } from "../../kernel/session";
import {
  CatchUpButton,
  CatchUpPanel,
  loadSummaryRun,
  requestSummary,
  SummaryButton,
  SummaryPanel,
  TranslateControl,
  TranslateLanguage,
  type SummaryState,
} from "./DiscussAi";

interface ChannelInfo {
  id: number;
  name: string;
  description: string | null;
  type: "channel" | "group" | "dm";
  privacy: string;
  member: boolean;
  member_count: number;
  notify: string | null;
  unread: number;
  partner_user_id: number | null;
}

/** Team chat: channels and direct messages with unread counters, live updates and presence. */
export function DiscussApp() {
  const { session } = useSession();
  const queryClient = useQueryClient();
  const channels = useQuery({
    queryKey: ["discuss", "channels"],
    queryFn: () => request<ChannelInfo[]>("GET", "/discuss/channels"),
  });
  const presence = useQuery({
    queryKey: ["discuss", "presence"],
    queryFn: () => request<{ online: number[] }>("GET", "/discuss/presence"),
  });
  const [online, setOnline] = useState<Set<number>>(new Set());
  const [params, setParams] = useSearchParams();
  const linked = Number(params.get("channel")) || null; // from a notification link
  const [active, setActive] = useState<number | null>(linked);
  const [panel, setPanel] = useState<"browse" | "new" | "dm" | "catchup" | null>(null);
  const [focus, setFocus] = useState<number | null>(null);
  const summaryRun = Number(params.get("summary")) || null;

  useEffect(() => setOnline(new Set(presence.data?.online ?? [])), [presence.data]);
  useEffect(() => {
    if (active === null && channels.data?.length) setActive(channels.data[0].id);
  }, [channels.data, active]);
  useRealtime(session ? `user:${session.user.id}` : null, (event) => {
    if (event.event === "discuss") void queryClient.invalidateQueries({ queryKey: ["discuss", "channels"] });
  });
  useRealtime("presence", (event) => {
    const { uid, online: isOnline } = event.data as { uid: number; online: boolean };
    setOnline((prev) => {
      const next = new Set(prev);
      if (isOnline) next.add(uid);
      else next.delete(uid);
      return next;
    });
  });

  const list = channels.data ?? [];
  const current = list.find((c) => c.id === active) ?? null;
  const jump = (channelId: number, messageId: number) => {
    setPanel(null);
    setActive(channelId);
    setFocus(messageId);
  };
  const section = (title: string, items: ChannelInfo[]) => (
    <>
      <h2 className="discuss-section">{title}</h2>
      <ul className="discuss-list">
        {items.map((c) => (
          <li key={c.id}>
            <button
              type="button"
              className={`discuss-item${c.id === active ? " active" : ""}`}
              aria-current={c.id === active ? "true" : undefined}
              onClick={() => {
                setActive(c.id);
                setFocus(null);
              }}
            >
              {c.type === "dm" ? (
                <span
                  className={`presence-dot${c.partner_user_id && online.has(c.partner_user_id) ? " online" : ""}`}
                  aria-label={c.partner_user_id && online.has(c.partner_user_id) ? "online" : "offline"}
                />
              ) : (
                <span aria-hidden="true">#</span>
              )}
              <span className="discuss-name">{c.name}</span>
              {c.unread > 0 && (
                <span className="badge count-inline" aria-label={`${c.unread} unread`}>
                  {c.unread}
                </span>
              )}
            </button>
          </li>
        ))}
      </ul>
    </>
  );

  return (
    <div className="discuss">
      <nav className="discuss-sidebar" aria-label="Conversations">
        <CatchUpButton onClick={() => setPanel("catchup")} />
        {section(
          "Channels",
          list.filter((c) => c.type === "channel"),
        )}
        <div className="button-row">
          <button type="button" className="btn small ghost" onClick={() => setPanel("browse")}>
            Browse
          </button>
          <button type="button" className="btn small ghost" onClick={() => setPanel("new")}>
            New channel
          </button>
        </div>
        {section(
          "Direct messages",
          list.filter((c) => c.type !== "channel"),
        )}
        <button type="button" className="btn small ghost" onClick={() => setPanel("dm")}>
          Message someone
        </button>
      </nav>
      <section className="discuss-main" aria-label={current ? `Conversation ${current.name}` : "Conversation"}>
        {panel === "catchup" && <CatchUpPanel onClose={() => setPanel(null)} onJump={jump} />}
        {panel && panel !== "catchup" && (
          <ChannelPicker
            mode={panel}
            onClose={() => setPanel(null)}
            onOpen={(id) => {
              setPanel(null);
              setActive(id);
              void queryClient.invalidateQueries({ queryKey: ["discuss", "channels"] });
            }}
          />
        )}
        {!panel && current && (
          <Conversation
            key={current.id}
            channel={current}
            focus={focus}
            summaryRun={current.id === linked ? summaryRun : null}
            onSummaryClosed={() => {
              if (summaryRun) {
                params.delete("summary");
                setParams(params, { replace: true });
              }
            }}
            onJump={jump}
          />
        )}
        {!panel && !current && <p className="muted">Join a channel or start a conversation.</p>}
      </section>
    </div>
  );
}

function Conversation({
  channel,
  focus,
  summaryRun,
  onSummaryClosed,
  onJump,
}: {
  channel: ChannelInfo;
  focus: number | null;
  summaryRun: number | null;
  onSummaryClosed: () => void;
  onJump: (channelId: number, messageId: number) => void;
}) {
  const queryClient = useQueryClient();
  const { session } = useSession();
  const [lang, setLang] = useState(() => (session?.user.lang ?? "en").slice(0, 2));
  const [summary, setSummary] = useState<SummaryState | null>(null);
  const [summarising, setSummarising] = useState(false);
  useEffect(() => {
    if (summaryRun) void loadSummaryRun(summaryRun, channel.id).then(setSummary);
  }, [summaryRun, channel.id]);
  const messages = useQuery({
    queryKey: ["chatter", "discuss.channel", channel.id, "messages"],
    queryFn: () => chatter.messages("discuss.channel", channel.id),
  });
  const bottom = useRef<HTMLDivElement>(null);
  const ordered = useMemo(() => [...(messages.data?.items ?? [])].reverse(), [messages.data]);
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["chatter", "discuss.channel", channel.id] });
  useRealtime(`record:discuss.channel:${channel.id}`, (event) => {
    if (event.event === "chatter") refresh();
  });
  useEffect(() => {
    if (focus && messages.data) {
      document.getElementById(`discuss-msg-${focus}`)?.scrollIntoView?.({ block: "center" });
      return;
    }
    bottom.current?.scrollIntoView?.({ block: "end" });
    const newest = messages.data?.items[0];
    if (newest && channel.member) {
      void request("POST", `/discuss/channels/${channel.id}/seen`, { body: { message_id: newest.id } }).then(() =>
        queryClient.invalidateQueries({ queryKey: ["discuss", "channels"] }),
      );
    }
  }, [messages.data, channel.id, channel.member, queryClient, focus]);
  return (
    <>
      <header className="discuss-header">
        <h1>
          {channel.type === "channel" ? "#" : ""}
          {channel.name}
        </h1>
        {channel.description && <span className="muted">{channel.description}</span>}
        <span className="muted small">{channel.member_count} members</span>
        <SummaryButton
          busy={summarising}
          onClick={async () => {
            setSummarising(true);
            setSummary(await requestSummary(channel.id));
            setSummarising(false);
          }}
        />
        <TranslateLanguage lang={lang} onChange={setLang} />
        {channel.member && (
          <label className="small">
            Notify me{" "}
            <select
              className="input small"
              defaultValue={channel.notify ?? "mentions"}
              onChange={(e) =>
                void request("POST", `/discuss/channels/${channel.id}/notify`, { body: { notify: e.target.value } })
              }
            >
              <option value="all">for every message</option>
              <option value="mentions">when mentioned</option>
              <option value="none">never</option>
            </select>
          </label>
        )}
      </header>
      {summary && (
        <SummaryPanel
          state={summary}
          onJump={onJump}
          onClose={() => {
            setSummary(null);
            onSummaryClosed();
          }}
        />
      )}
      <ol className="chatter-messages discuss-messages" aria-live="polite">
        {ordered.map((m) => (
          <Fragment key={m.id}>
            <MessageItem message={m} onChange={refresh} />
            <li
              id={`discuss-msg-${m.id}`}
              className={`discuss-ai-row${focus === m.id ? " focused" : ""}`}
              aria-label={focus === m.id ? "Linked message" : undefined}
            >
              {m.body && m.author?.id !== session?.user.id && <TranslateControl messageId={m.id} lang={lang} />}
            </li>
          </Fragment>
        ))}
      </ol>
      <div ref={bottom} />
      {channel.member ? (
        <Composer model="discuss.channel" resId={channel.id} internal={false} onPosted={refresh} />
      ) : (
        <button
          type="button"
          className="btn primary"
          onClick={async () => {
            await request("POST", `/discuss/channels/${channel.id}/join`);
            void queryClient.invalidateQueries({ queryKey: ["discuss", "channels"] });
          }}
        >
          Join #{channel.name}
        </button>
      )}
    </>
  );
}

function ChannelPicker({
  mode,
  onClose,
  onOpen,
}: {
  mode: "browse" | "new" | "dm";
  onClose: () => void;
  onOpen: (id: number) => void;
}) {
  const [q, setQ] = useState("");
  const [name, setName] = useState("");
  const [privacy, setPrivacy] = useState("public");
  const browse = useQuery({
    queryKey: ["discuss", "browse", q],
    queryFn: () => request<ChannelInfo[]>("GET", "/discuss/channels/browse", { query: { q } }),
    enabled: mode === "browse",
  });
  const people = useQuery({
    queryKey: ["discuss", "people", q],
    queryFn: () => api.nameSearch("core.user", q, [["share", "=", false]], 10),
    enabled: mode === "dm",
  });
  return (
    <div className="discuss-picker">
      <div className="button-row">
        <h2>{mode === "browse" ? "Browse channels" : mode === "new" ? "New channel" : "Message someone"}</h2>
        <button type="button" className="btn small ghost" onClick={onClose}>
          Close
        </button>
      </div>
      {mode === "new" ? (
        <form
          className="portal-form"
          onSubmit={async (e) => {
            e.preventDefault();
            const ch = await request<ChannelInfo>("POST", "/discuss/channels", { body: { name, privacy } });
            onOpen(ch.id);
          }}
        >
          <label>
            Name
            <input className="input" value={name} onChange={(e) => setName(e.target.value)} required maxLength={80} />
          </label>
          <label>
            Who can join
            <select className="input" value={privacy} onChange={(e) => setPrivacy(e.target.value)}>
              <option value="public">Everyone</option>
              <option value="invite">Invited members only</option>
            </select>
          </label>
          <button type="submit" className="btn primary">
            Create
          </button>
        </form>
      ) : (
        <>
          <input
            className="input"
            placeholder="Search…"
            aria-label="Search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <ul className="discuss-list">
            {mode === "browse" &&
              (browse.data ?? []).map((c) => (
                <li key={c.id}>
                  <button
                    type="button"
                    className="discuss-item"
                    onClick={async () => {
                      if (!c.member) await request("POST", `/discuss/channels/${c.id}/join`);
                      onOpen(c.id);
                    }}
                  >
                    # {c.name} <span className="muted small">{c.member ? "joined" : `${c.member_count} members`}</span>
                  </button>
                </li>
              ))}
            {mode === "dm" &&
              (people.data ?? []).map((u) => (
                <li key={u.id}>
                  <button
                    type="button"
                    className="discuss-item"
                    onClick={async () => {
                      const ch = await request<ChannelInfo>("POST", `/discuss/dm/${u.id}`);
                      onOpen(ch.id);
                    }}
                  >
                    {u.display_name}
                  </button>
                </li>
              ))}
          </ul>
        </>
      )}
    </div>
  );
}

registerClientAction("discuss", () => <DiscussApp />);
