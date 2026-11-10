// Calendar AI (Phase 1.6): meeting brief and suggested times beside the meeting form, and
// natural-language scheduling in the calendar's search bar. Nothing is booked without the user: the
// scheduling request becomes a proposal reviewed with ProposalCard, and "Move here" is a plain edit.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { request } from "../../kernel/api/client";
import { ai, aiErrorMessage, type Citation, type Proposal } from "../../kernel/ai/api";
import { AiLabel, AiUnavailable, Citations, Feedback, ProposalCard, useAiFeature } from "../../kernel/ai/components";
import { registerFormPanel, registerSearchAddon } from "../../kernel/extensions";

const EVENT = "calendar.event";

/** Lets the form panel disappear when the feature is switched off (see the calendar block in styles.css). */
const Hidden = () => <span className="ai-panel-hidden" hidden />;

const when = (iso: string) =>
  new Date(iso).toLocaleString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });

// ------------------------------------------------------------------------------------------ brief

interface Brief {
  run_id: number;
  text: string;
  citations: Citation[];
  created?: string | null;
}

/** Strip the machine citation markers; the sources are listed below the brief. */
const plain = (text: string) => text.replace(/\s*\[source:[^\]]+\]/g, "");

export function MeetingBrief({ resId }: { resId: number }) {
  const feature = useAiFeature("calendar.meeting_brief");
  const [fresh, setFresh] = useState<Brief | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const saved = useQuery({
    queryKey: ["calendar-brief", resId],
    queryFn: () => request<{ brief: Brief | null }>("GET", `/calendar/events/${resId}/brief`),
    enabled: feature.enabled && feature.available,
    retry: false,
  });
  if (!feature.enabled) return <Hidden />;
  if (!feature.available) return <AiUnavailable />;

  const prepare = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await ai.run("calendar.meeting_brief", { res_model: EVENT, res_id: resId });
      setFresh({ run_id: r.run_id, text: r.text, citations: r.citations });
    } catch (e) {
      setError(aiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  const brief = fresh ?? saved.data?.brief ?? null;

  return (
    <div className="cal-ai">
      {brief ? (
        <article className="cal-brief" aria-label="Meeting brief">
          <AiLabel text="AI-generated brief" />
          <div className="cal-brief-text">{plain(brief.text)}</div>
          <Citations items={brief.citations} />
          <div className="cal-ai-row">
            <Feedback runId={brief.run_id} />
            <button type="button" className="btn ghost small" disabled={busy} onClick={() => void prepare()}>
              {busy ? "Preparing…" : "Refresh"}
            </button>
          </div>
        </article>
      ) : (
        <>
          <p className="muted small">
            A brief from this meeting, its attendees, open deals and earlier meetings you can access. It is also
            prepared for you about an hour before the meeting.
          </p>
          <button type="button" className="btn small" disabled={busy} onClick={() => void prepare()}>
            {busy ? "Preparing…" : "Prepare brief"}
          </button>
        </>
      )}
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------------------------------ suggested times

interface Slot {
  start: string;
  stop: string;
  score: number;
  reasons: string[];
}

export function SuggestedTimes({ resId, reload }: { resId: number; reload: () => void }) {
  const feature = useAiFeature("calendar.suggest_times");
  const qc = useQueryClient();
  const [prefer, setPrefer] = useState<"" | "morning" | "afternoon">("");
  const [slots, setSlots] = useState<Slot[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [moved, setMoved] = useState<string | null>(null);
  if (!feature.enabled) return <Hidden />;
  if (!feature.available) return <AiUnavailable />;

  const find = async () => {
    setBusy(true);
    setError(null);
    setMoved(null);
    try {
      const r = await request<{ slots: Slot[] }>("POST", "/calendar/ai/suggest-times", {
        body: { event_id: resId, ...(prefer ? { prefer } : {}) },
      });
      setSlots(r.slots);
    } catch (e) {
      setError(aiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  // booking stays the user's action: moving the meeting is an ordinary edit they click
  const move = async (slot: Slot) => {
    setError(null);
    try {
      await request("PATCH", `/models/${EVENT}/${resId}`, { body: { values: { start: slot.start, stop: slot.stop } } });
      setMoved(slot.start);
      void qc.invalidateQueries({ queryKey: ["record", EVENT] });
      reload();
    } catch (e) {
      setError(aiErrorMessage(e));
    }
  };

  return (
    <div className="cal-ai">
      <div className="cal-ai-row">
        <label className="small">
          <span className="sr-only">Preferred time of day</span>
          <select value={prefer} onChange={(e) => setPrefer(e.target.value as typeof prefer)}>
            <option value="">Any time</option>
            <option value="morning">Mornings</option>
            <option value="afternoon">Afternoons</option>
          </select>
        </label>
        <button type="button" className="btn small" disabled={busy} onClick={() => void find()}>
          {busy ? "Searching…" : "Find times"}
        </button>
      </div>
      {slots && slots.length === 0 && <p className="muted small">No time works for everyone in the next week.</p>}
      {slots && slots.length > 0 && (
        <ol className="cal-slots" aria-label="Suggested times">
          {slots.map((s) => (
            <li key={s.start}>
              <div>
                <strong>{when(s.start)}</strong>
                <span className="muted small"> {s.reasons.join(" · ")}</span>
              </div>
              <button
                type="button"
                className="btn ghost small"
                onClick={() => void move(s)}
                disabled={moved === s.start}
                aria-label={`Move the meeting to ${when(s.start)}`}
              >
                {moved === s.start ? "Moved" : "Move here"}
              </button>
            </li>
          ))}
        </ol>
      )}
      {slots && (
        <p className="muted small">Computed from everyone&apos;s calendar; nothing is booked until you choose.</p>
      )}
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------------------------------ NL scheduling

interface Draft {
  runId: number;
  proposal: Proposal | null;
  resolved: { id: number; name: string }[];
  unresolved: { name: string; candidates: { id: number; name: string }[] }[];
  problems: string[];
}

export function ScheduleWithAi() {
  const feature = useAiFeature("calendar.nl_schedule");
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  if (!feature.enabled) return <></>;
  if (!feature.available) return <AiUnavailable />;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (text.trim().length < 3) return;
    setBusy(true);
    setError(null);
    setDraft(null);
    try {
      const r = await ai.run("calendar.nl_schedule", { inputs: { text } });
      const data = (r.data ?? {}) as Partial<Draft> & { problems?: string[] };
      setDraft({
        runId: r.run_id,
        proposal: r.proposals[0] ?? null,
        resolved: data.resolved ?? [],
        unresolved: data.unresolved ?? [],
        problems: data.problems ?? [],
      });
    } catch (err) {
      setError(aiErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  const payload = draft?.proposal?.payload as { name?: string; start?: string; allday?: boolean; location?: string };

  return (
    <span className="cal-nl">
      <button type="button" className="btn ghost small" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        ✦ Schedule with AI
      </button>
      {open && (
        <div className="search-panel cal-nl-panel" role="dialog" aria-label="Schedule with AI">
          <form onSubmit={submit} className="cal-ai-row">
            <label className="cal-nl-input">
              <span className="sr-only">Describe the meeting</span>
              <input
                className="input"
                value={text}
                placeholder="Lunch with Dana next Tuesday 12:30"
                onChange={(e) => setText(e.target.value)}
              />
            </label>
            <button type="submit" className="btn primary small" disabled={busy || text.trim().length < 3}>
              {busy ? "Drafting…" : "Draft"}
            </button>
          </form>
          {error && (
            <div className="alert error" role="alert">
              {error}
            </div>
          )}
          {draft && !draft.proposal && (
            <div className="alert error" role="alert">
              {draft.problems[0] ?? "The request could not be turned into a meeting."}
            </div>
          )}
          {draft?.proposal && (
            <div className="cal-nl-result">
              <p className="small">
                <strong>{payload.name}</strong>
                {payload.start && (
                  <> · {payload.allday ? new Date(payload.start).toLocaleDateString() : when(payload.start)}</>
                )}
                {payload.location && <> · {payload.location}</>}
              </p>
              {draft.resolved.length > 0 && (
                <p className="small">Invites: {draft.resolved.map((p) => p.name).join(", ")}</p>
              )}
              {draft.unresolved.map((u) => (
                <p key={u.name} className="alert warning small" role="status">
                  “{u.name}” was not added:{" "}
                  {u.candidates.length
                    ? `several contacts match (${u.candidates.map((c) => c.name).join(", ")}); add the right one yourself.`
                    : "no contact you can see matches."}
                </p>
              ))}
              <ProposalCard proposal={draft.proposal} onDecided={() => void qc.invalidateQueries()} />
              <Feedback runId={draft.runId} />
            </div>
          )}
        </div>
      )}
    </span>
  );
}

registerFormPanel(EVENT, { title: "Meeting brief", render: ({ resId }) => <MeetingBrief resId={resId} /> });
registerFormPanel(EVENT, {
  title: "Suggested times",
  render: ({ resId, reload }) => <SuggestedTimes resId={resId} reload={reload} />,
});
registerSearchAddon(EVENT, { key: "calendar.nl_schedule", render: () => <ScheduleWithAi /> });
