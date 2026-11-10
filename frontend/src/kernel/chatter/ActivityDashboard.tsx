import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { aiErrorMessage } from "../ai/api";
import { AiLabel, Feedback, useAiFeature } from "../ai/components";
import { request } from "../api/client";
import { chatter } from "../api/platform";
import { formatDate } from "../format";
import { ActivityRow } from "./Chatter";

const STATES = [
  { key: "overdue", label: "Overdue" },
  { key: "today", label: "Today" },
  { key: "planned", label: "Planned" },
] as const;

const DAILY_TODO = "chatter.daily_todo";

interface RankedItem {
  activity_id: number;
  rank: number;
  summary: string;
  type: string;
  model: string;
  res_id: number;
  res_name: string | null;
  date_deadline: string;
  reasons: string[];
}

interface RankedTodo {
  run_id: number;
  items: RankedItem[];
}

/** The "My activities" client action: counts per model and bucket, then the activities themselves. */
export function ActivityDashboard() {
  const [state, setState] = useState<string | undefined>(undefined);
  const [params] = useSearchParams();
  // the morning notification links here with ?ranked=1
  const linked = params.get("ranked") === "1";
  const [ranked, setRanked] = useState(linked);
  useEffect(() => {
    if (linked) setRanked(true);
  }, [linked]);
  const todo = useAiFeature(DAILY_TODO);
  const data = useQuery({ queryKey: ["activities", "mine", state], queryFn: () => chatter.mine(state) });
  const summary = data.data?.summary ?? [];
  const totals = STATES.map((s) => ({ ...s, count: summary.reduce((n, m) => n + m[s.key], 0) }));
  return (
    <div className="activity-dashboard">
      <div className="activity-dashboard-head">
        <h1 className="page-title">My activities</h1>
        {/* ranked by rules on the server (no AI provider), so it works while the AI gateway is down;
            hidden when the feature is turned off for the company */}
        {todo.enabled && (
          <button
            type="button"
            className={`btn small${ranked ? " active" : " ghost"}`}
            aria-pressed={ranked}
            onClick={() => setRanked((r) => !r)}
          >
            ✦ Rank my day
          </button>
        )}
      </div>
      {todo.enabled && ranked && <RankedTodoPanel onChange={() => void data.refetch()} />}
      <div className="activity-buckets" role="tablist" aria-label="Due">
        <button
          type="button"
          role="tab"
          aria-selected={!state}
          className={`btn small${!state ? " active" : " ghost"}`}
          onClick={() => setState(undefined)}
        >
          All
        </button>
        {totals.map((s) => (
          <button
            key={s.key}
            type="button"
            role="tab"
            aria-selected={state === s.key}
            className={`btn small bucket ${s.key}${state === s.key ? " active" : " ghost"}`}
            onClick={() => setState(s.key)}
          >
            {s.label} <span className="badge">{s.count}</span>
          </button>
        ))}
      </div>
      {summary.length > 0 && (
        <table className="activity-summary">
          <thead>
            <tr>
              <th scope="col">Document</th>
              {STATES.map((s) => (
                <th key={s.key} scope="col" className="num">
                  {s.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {summary.map((m) => (
              <tr key={m.model}>
                <th scope="row">{m.label}</th>
                {STATES.map((s) => (
                  <td key={s.key} className={`num ${m[s.key] ? s.key : ""}`}>
                    {m[s.key]}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="activity-list">
        {data.isLoading && <div className="loading">Loading…</div>}
        {(data.data?.items ?? []).map((a) => (
          <ActivityRow key={a.id} activity={a} showRecord onChange={() => void data.refetch()} />
        ))}
        {!data.isLoading && (data.data?.items.length ?? 0) === 0 && <p className="muted">Nothing to do. 🎉</p>}
      </div>
    </div>
  );
}

/** Today's activities in the order the ranking suggests, each with the reasons behind its place. */
function RankedTodoPanel({ onChange }: { onChange: () => void }) {
  const queryClient = useQueryClient();
  const ranked = useQuery({
    queryKey: ["activities", "ranked"],
    queryFn: () => request<RankedTodo>("GET", "/chatter/ai/todo"),
    retry: false,
  });
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["activities"] });
    onChange();
  };
  const items = ranked.data?.items ?? [];
  return (
    <section className="ai-todo" aria-label="AI-ranked to-do">
      <header>
        <h2>Suggested order for today</h2>
        {ranked.data && <AiLabel text="AI-ranked" />}
        <span className="spacer" />
        <button type="button" className="btn small ghost" onClick={() => void ranked.refetch()}>
          Re-rank
        </button>
      </header>
      {ranked.isLoading && (
        <p className="muted" role="status">
          Ranking your activities…
        </p>
      )}
      {ranked.isError && (
        <div className="alert error" role="alert">
          {aiErrorMessage(ranked.error)}
        </div>
      )}
      {ranked.data && items.length === 0 && <p className="muted">Nothing to rank. 🎉</p>}
      {items.length > 0 && (
        <ol className="ai-todo-list">
          {items.map((i) => (
            <li key={i.activity_id}>
              <div className="ai-todo-main">
                <span className="ai-todo-rank" aria-hidden="true">
                  {i.rank}
                </span>
                <div>
                  <strong>{i.type}</strong> — <span>{i.summary}</span>
                  {i.res_name && (
                    <>
                      {" · "}
                      <Link to={`/m/${i.model}/${i.res_id}`}>{i.res_name}</Link>
                    </>
                  )}
                  <div className="muted small">due {formatDate(i.date_deadline)}</div>
                  <ul className="ai-todo-reasons" aria-label="Why">
                    {i.reasons.map((r) => (
                      <li key={r}>{r}</li>
                    ))}
                  </ul>
                </div>
              </div>
              <button
                type="button"
                className="btn small"
                aria-label={`Done: ${i.summary}`}
                onClick={async () => {
                  await chatter.done(i.activity_id);
                  refresh();
                }}
              >
                Done
              </button>
            </li>
          ))}
        </ol>
      )}
      {ranked.data && <Feedback runId={ranked.data.run_id} />}
    </section>
  );
}
