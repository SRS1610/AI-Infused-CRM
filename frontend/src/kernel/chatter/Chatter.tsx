import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useId, useRef, useState } from "react";
import { ai, aiCodeMessage, aiErrorMessage, streamFeature } from "../ai/api";
import { AiLabel, AiUnavailable, Feedback, useAiFeature } from "../ai/components";
import { chatter, type Activity, type ChatterMessage } from "../api/platform";
import { aiDraftFeature } from "../extensions";
import { formatDate, formatDatetime } from "../format";
import { useRealtime } from "../realtime";

const QUICK_REACTIONS = ["👍", "🎉", "❤️", "👀"];

function relative(date: string | null): string {
  return date ? formatDatetime(date) : "";
}

/** Message, note, follower and activity panel shown beside forms of models with a thread. */
export function Chatter({ model, resId, canWrite = true }: { model: string; resId: number; canWrite?: boolean }) {
  const queryClient = useQueryClient();
  const key = ["chatter", model, resId];
  const messages = useQuery({ queryKey: [...key, "messages"], queryFn: () => chatter.messages(model, resId) });
  const followers = useQuery({ queryKey: [...key, "followers"], queryFn: () => chatter.followers(model, resId) });
  const activities = useQuery({ queryKey: [...key, "activities"], queryFn: () => chatter.activities(model, resId) });
  const [composer, setComposer] = useState<"message" | "note" | "activity" | null>(null);
  const [older, setOlder] = useState<ChatterMessage[]>([]);
  const [hasMoreOlder, setHasMoreOlder] = useState<boolean | null>(null);

  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: key });
  }, [queryClient, model, resId]); // eslint-disable-line react-hooks/exhaustive-deps
  useRealtime(`record:${model}:${resId}`, (event) => {
    if (event.event === "chatter" || event.event === "write") refresh();
  });

  const all = [...(messages.data?.items ?? []), ...older];
  const hasMore = hasMoreOlder ?? messages.data?.has_more ?? false;
  const loadOlder = async () => {
    const last = all[all.length - 1];
    if (!last) return;
    const page = await chatter.messages(model, resId, last.id);
    setOlder((o) => [...o, ...page.items]);
    setHasMoreOlder(page.has_more);
  };

  return (
    <aside className="chatter" aria-label="Discussion">
      <div className="chatter-toolbar">
        <button
          type="button"
          className={`btn small${composer === "message" ? " active" : ""}`}
          onClick={() => setComposer(composer === "message" ? null : "message")}
        >
          Send message
        </button>
        <button
          type="button"
          className={`btn small${composer === "note" ? " active" : ""}`}
          onClick={() => setComposer(composer === "note" ? null : "note")}
        >
          Log note
        </button>
        {canWrite && (
          <button
            type="button"
            className={`btn small${composer === "activity" ? " active" : ""}`}
            onClick={() => setComposer(composer === "activity" ? null : "activity")}
          >
            Activity
          </button>
        )}
        <div className="spacer" />
        {followers.data && (
          <button
            type="button"
            className="btn small ghost"
            aria-pressed={followers.data.following}
            onClick={async () => {
              await (followers.data!.following ? chatter.unfollow(model, resId) : chatter.follow(model, resId));
              refresh();
            }}
            title={followers.data.items.map((f) => f.name).join(", ")}
          >
            {followers.data.following ? "Following" : "Follow"} · {followers.data.items.length}
          </button>
        )}
      </div>
      {(composer === "message" || composer === "note") && (
        <Composer
          model={model}
          resId={resId}
          internal={composer === "note"}
          onPosted={() => {
            setComposer(null);
            refresh();
          }}
        />
      )}
      {composer === "activity" && (
        <ActivityForm
          model={model}
          resId={resId}
          onDone={() => {
            setComposer(null);
            refresh();
          }}
        />
      )}
      {(activities.data?.length ?? 0) > 0 && (
        <section className="chatter-activities" aria-label="Planned activities">
          <h3 className="chatter-heading">Planned activities</h3>
          {activities.data!.map((a) => (
            <ActivityRow key={a.id} activity={a} onChange={refresh} />
          ))}
        </section>
      )}
      <ol className="chatter-messages" aria-live="polite">
        {messages.isLoading && <li className="muted">Loading…</li>}
        {all.map((m) => (
          <MessageItem key={m.id} message={m} onChange={refresh} />
        ))}
        {!messages.isLoading && all.length === 0 && <li className="muted">No messages yet.</li>}
      </ol>
      {hasMore && (
        <button type="button" className="btn small ghost block" onClick={() => void loadOlder()}>
          Load older messages
        </button>
      )}
    </aside>
  );
}

export function MessageItem({ message: m, onChange }: { message: ChatterMessage; onChange: () => void }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(m.body);
  const isLog = m.type === "notification" || m.type === "system";
  return (
    <li className={`chatter-message ${m.type}${m.internal ? " internal" : ""}`}>
      <div className="message-head">
        <span className="avatar" aria-hidden="true">
          {(m.author?.name ?? m.email_from ?? "S").slice(0, 1).toUpperCase()}
        </span>
        <strong>{m.author?.name ?? m.email_from ?? "System"}</strong>
        <span className="muted"> · {relative(m.date)}</span>
        {m.type === "note" && <span className="badge">Note</span>}
        {m.type === "email" && <span className="badge">Email</span>}
        {m.ai_assisted && (
          <span className="badge" title="Drafted with AI and reviewed by the author">
            AI-assisted
          </span>
        )}
      </div>
      {editing ? (
        <div className="message-edit">
          <textarea
            className="input"
            aria-label="Edit message"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
          <button
            type="button"
            className="btn small primary"
            onClick={async () => {
              await chatter.edit(m.id, draft);
              setEditing(false);
              onChange();
            }}
          >
            Save
          </button>
          <button type="button" className="btn small ghost" onClick={() => setEditing(false)}>
            Cancel
          </button>
        </div>
      ) : (
        // bodies are sanitised server-side (nh3) before storage
        !isLog && <div className="message-body" dangerouslySetInnerHTML={{ __html: m.body }} />
      )}
      {isLog && m.body && m.tracking.length === 0 && (
        <div className="message-body muted" dangerouslySetInnerHTML={{ __html: m.body }} />
      )}
      {m.tracking.length > 0 && (
        <ul className="tracking">
          {m.tracking.map((t) => (
            <li key={t.field}>
              {t.label}: <span className="old">{t.old ?? "—"}</span> → <span className="new">{t.new ?? "—"}</span>
            </li>
          ))}
        </ul>
      )}
      {m.attachments.length > 0 && (
        <ul className="attachments">
          {m.attachments.map((a) => (
            <li key={a.id}>
              <a href={`/api/v1/attachments/${a.id}/content`} target="_blank" rel="noreferrer">
                📎 {a.name}
              </a>
            </li>
          ))}
        </ul>
      )}
      {!isLog && (
        <div className="reactions">
          {m.reactions.map((r) => (
            <button
              key={r.emoji}
              type="button"
              className={`reaction${r.mine ? " mine" : ""}`}
              aria-pressed={r.mine}
              title={r.users.join(", ")}
              onClick={async () => {
                await chatter.react(m.id, r.emoji);
                onChange();
              }}
            >
              {r.emoji} {r.count}
            </button>
          ))}
          {QUICK_REACTIONS.filter((e) => !m.reactions.some((r) => r.emoji === e)).map((e) => (
            <button
              key={e}
              type="button"
              className="reaction add"
              aria-label={`React with ${e}`}
              onClick={async () => {
                await chatter.react(m.id, e);
                onChange();
              }}
            >
              {e}
            </button>
          ))}
          {m.editable && (
            <>
              <button type="button" className="btn small ghost" onClick={() => setEditing(true)}>
                Edit
              </button>
              <button
                type="button"
                className="btn small ghost danger"
                onClick={async () => {
                  if (!window.confirm("Delete this message?")) return;
                  await chatter.remove(m.id);
                  onChange();
                }}
              >
                Delete
              </button>
            </>
          )}
        </div>
      )}
    </li>
  );
}

export function Composer({
  model,
  resId,
  internal,
  onPosted,
}: {
  model: string;
  resId: number;
  internal: boolean;
  onPosted: () => void;
}) {
  const [text, setText] = useState("");
  const [mentions, setMentions] = useState<{ id: number; name: string; login: string }[]>([]);
  const [picked, setPicked] = useState<{ id: number; login: string }[]>([]);
  const [files, setFiles] = useState<{ id: number; name: string }[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const area = useRef<HTMLTextAreaElement>(null);
  const id = useId();
  const draftFeature = internal ? undefined : aiDraftFeature(model);
  const aiFeature = useAiFeature(draftFeature);
  // the AI draft currently in the box: its proposal is accepted (as is or edited) when the user sends
  const [aiDraft, setAiDraft] = useState<{ runId: number; proposalId: number | null; text: string } | null>(null);
  const [drafting, setDrafting] = useState(false);
  const aiUntouched = aiDraft !== null && text === aiDraft.text;

  const draftWithAi = async () => {
    if (!draftFeature) return;
    setDrafting(true);
    setError(null);
    setText("");
    let streamed = "";
    try {
      const result = await streamFeature(draftFeature, { res_model: model, res_id: resId }, (ev) => {
        if (ev.event === "delta") {
          streamed += ev.text;
          setText(streamed);
        } else if (ev.event === "error") setError(aiCodeMessage(ev.error.code, ev.error.message));
      });
      if (result) {
        const proposal = result.proposals.find((p) => p.kind === "message");
        setText(result.text);
        setAiDraft({ runId: result.run_id, proposalId: proposal?.id ?? null, text: result.text });
      }
    } catch (e) {
      setError(aiErrorMessage(e));
    } finally {
      setDrafting(false);
    }
  };
  const discardDraft = async () => {
    if (aiDraft?.proposalId) await ai.reject(aiDraft.proposalId, "discarded in composer").catch(() => undefined);
    setAiDraft(null);
    setText("");
  };

  const onInput = async (value: string) => {
    setText(value);
    const match = /(?:^|\s)@([\w.-]{1,30})$/.exec(value);
    if (match) setMentions(await chatter.mentions(match[1]));
    else setMentions([]);
  };
  const pick = (u: { id: number; login: string }) => {
    setText((t) => t.replace(/@([\w.-]{1,30})$/, `@${u.login} `));
    setPicked((p) => [...p, u]);
    setMentions([]);
    area.current?.focus();
  };
  const send = async () => {
    if (!text.trim() && files.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      const escaped = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
      const body = escaped
        .split(/\n{2,}/)
        .map((p) => `<p>${p.replace(/\n/g, "<br/>")}</p>`)
        .join("");
      if (aiDraft?.proposalId && files.length === 0) {
        // the human takes ownership: accepting posts the message (linked to the AI run) as this user
        await ai.accept(aiDraft.proposalId, aiUntouched ? undefined : { body });
      } else {
        if (aiDraft?.proposalId) await ai.reject(aiDraft.proposalId, "sent manually").catch(() => undefined);
        await chatter.post(model, resId, {
          body,
          internal,
          attachment_ids: files.map((f) => f.id),
          mention_user_ids: picked.filter((p) => text.includes(`@${p.login}`)).map((p) => p.id),
        });
      }
      setAiDraft(null);
      setText("");
      setFiles([]);
      setPicked([]);
      onPosted();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className={`composer${internal ? " internal" : ""}`}>
      <label htmlFor={id} className="sr-only">
        {internal ? "Internal note" : "Message to followers"}
      </label>
      {aiUntouched && (
        <div className="composer-ai">
          <AiLabel /> <Feedback runId={aiDraft.runId} />
        </div>
      )}
      <textarea
        id={id}
        ref={area}
        aria-busy={drafting}
        className="input"
        rows={3}
        placeholder={internal ? "Log an internal note… (@ to mention)" : "Send a message to followers… (@ to mention)"}
        value={text}
        onChange={(e) => void onInput(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) void send();
        }}
      />
      {mentions.length > 0 && (
        <ul className="mention-list" role="listbox" aria-label="Mention suggestions">
          {mentions.map((u) => (
            <li key={u.id} role="option" aria-selected="false">
              <button type="button" className="btn ghost small block" onClick={() => pick(u)}>
                {u.name} <span className="muted">@{u.login}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {files.length > 0 && (
        <ul className="attachments">
          {files.map((f) => (
            <li key={f.id}>📎 {f.name}</li>
          ))}
        </ul>
      )}
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
      <div className="composer-actions">
        <button type="button" className="btn primary small" disabled={busy} onClick={() => void send()}>
          {internal ? "Log" : "Send"}
        </button>
        <label className="btn small ghost">
          Attach
          <input
            type="file"
            className="sr-only"
            onChange={async (e) => {
              const file = e.target.files?.[0];
              if (!file) return;
              const uploaded = await chatter.upload(file, model, resId);
              setFiles((f) => [...f, { id: uploaded.id, name: uploaded.name }]);
            }}
          />
        </label>
        {draftFeature &&
          aiFeature.enabled &&
          (aiFeature.available ? (
            <button
              type="button"
              className="btn small ghost"
              disabled={drafting || busy}
              onClick={() => void draftWithAi()}
            >
              {drafting ? "Drafting…" : "✦ Draft with AI"}
            </button>
          ) : (
            <AiUnavailable />
          ))}
        {aiDraft && (
          <button type="button" className="btn small ghost" onClick={() => void discardDraft()}>
            Discard draft
          </button>
        )}
        <span className="muted small">Ctrl+Enter to send</span>
      </div>
    </div>
  );
}

function ActivityForm({ model, resId, onDone }: { model: string; resId: number; onDone: () => void }) {
  const [summary, setSummary] = useState("");
  const [type, setType] = useState("chatter.activity_type_todo");
  const [due, setDue] = useState(() => new Date().toISOString().slice(0, 10));
  const [error, setError] = useState<string | null>(null);
  return (
    <form
      className="activity-form"
      onSubmit={async (e) => {
        e.preventDefault();
        try {
          await chatter.schedule(model, resId, {
            activity_type: type,
            summary: summary || undefined,
            date_deadline: due,
          });
          onDone();
        } catch (err) {
          setError(err instanceof Error ? err.message : String(err));
        }
      }}
    >
      <label>
        Type
        <select className="input small" value={type} onChange={(e) => setType(e.target.value)}>
          <option value="chatter.activity_type_todo">To-do</option>
          <option value="chatter.activity_type_call">Call</option>
          <option value="chatter.activity_type_meeting">Meeting</option>
          <option value="chatter.activity_type_email">Email</option>
        </select>
      </label>
      <label>
        Summary
        <input className="input small" value={summary} onChange={(e) => setSummary(e.target.value)} />
      </label>
      <label>
        Due
        <input className="input small" type="date" value={due} onChange={(e) => setDue(e.target.value)} />
      </label>
      <button type="submit" className="btn primary small">
        Schedule
      </button>
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
    </form>
  );
}

export function ActivityRow({
  activity: a,
  onChange,
  showRecord = false,
}: {
  activity: Activity;
  onChange: () => void;
  showRecord?: boolean;
}) {
  return (
    <div className={`activity-row ${a.state}`}>
      <span className={`state-dot ${a.state}`} aria-label={a.state} />
      <div className="activity-main">
        <strong>{a.type.name}</strong> {a.summary && <span>— {a.summary}</span>}
        {showRecord && a.res_name && (
          <a className="muted" href={`/m/${a.model}/${a.res_id}`}>
            {" "}
            · {a.res_name}
          </a>
        )}
        <div className="muted small">
          {a.user.name} · due {formatDate(a.date_deadline)}
        </div>
      </div>
      <button
        type="button"
        className="btn small"
        onClick={async () => {
          await chatter.done(a.id);
          onChange();
        }}
      >
        Done
      </button>
      <button
        type="button"
        className="btn small ghost"
        onClick={async () => {
          await chatter.snooze(a.id, 1);
          onChange();
        }}
      >
        +1 day
      </button>
    </div>
  );
}
