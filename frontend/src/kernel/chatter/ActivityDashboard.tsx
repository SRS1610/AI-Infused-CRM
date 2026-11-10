import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { chatter } from "../api/platform";
import { ActivityRow } from "./Chatter";

const STATES = [
  { key: "overdue", label: "Overdue" },
  { key: "today", label: "Today" },
  { key: "planned", label: "Planned" },
] as const;

/** The "My activities" client action: counts per model and bucket, then the activities themselves. */
export function ActivityDashboard() {
  const [state, setState] = useState<string | undefined>(undefined);
  const data = useQuery({ queryKey: ["activities", "mine", state], queryFn: () => chatter.mine(state) });
  const summary = data.data?.summary ?? [];
  const totals = STATES.map((s) => ({ ...s, count: summary.reduce((n, m) => n + m[s.key], 0) }));
  return (
    <div className="activity-dashboard">
      <h1 className="page-title">My activities</h1>
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
