// CRM AI assist (Phase 1.6): follow-up drafts in the chatter composer, natural-language search in the
// search bar, and three panels on the lead form — extracted lead details to review, call-notes summary with
// field suggestions (background job, audit trail) and objection handling from past deals.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { request } from "../../kernel/api/client";
import { ai, aiErrorMessage, type Citation, type Proposal } from "../../kernel/ai/api";
import { AiLabel, AiUnavailable, Citations, Feedback, ProposalCard, useAiFeature } from "../../kernel/ai/components";
import { registerAiDraft, registerFormPanel, registerSearchAddon } from "../../kernel/extensions";

const LEAD = "crm.lead";

/** Lets FormView's panel section disappear when the feature is off (see the crm-assist block in styles.css). */
const Hidden = () => <span className="ai-panel-hidden" hidden />;

// ------------------------------------------------------------------------------------------ NL search

type Operator =
  "=" | "!=" | ">" | ">=" | "<" | "<=" | "in" | "not_in" | "ilike" | "not_ilike" | "is_set" | "is_not_set";

export interface Condition {
  field: string;
  operator: Operator;
  value: unknown;
}

interface FieldOption {
  label: string;
  type: string;
  selection?: [string, string][];
  values?: string[];
}

interface SearchResult {
  match: "all" | "any";
  conditions: Condition[];
  explanation: string;
  domain: unknown;
  labels: string[];
  fields: Record<string, FieldOption>;
}

const OPERATORS: [Operator, string][] = [
  ["=", "is"],
  ["!=", "is not"],
  [">", ">"],
  [">=", "≥"],
  ["<", "<"],
  ["<=", "≤"],
  ["in", "is one of"],
  ["not_in", "is none of"],
  ["ilike", "contains"],
  ["not_ilike", "does not contain"],
  ["is_set", "is set"],
  ["is_not_set", "is not set"],
];
const NUMERIC = ["integer", "float", "monetary"];

export function showValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) return value.join(", ");
  return String(value);
}

/** Text typed in the editor → the value the domain needs for that field and operator. */
export function parseValue(raw: string, operator: Operator, type: string | undefined): unknown {
  if (operator === "is_set" || operator === "is_not_set") return null;
  const one = (s: string): unknown => {
    const t = s.trim();
    if (t.startsWith("$")) return t;
    if (type && NUMERIC.includes(type) && t !== "" && !Number.isNaN(Number(t))) return Number(t);
    if (type === "boolean") return t === "true";
    return t;
  };
  return operator === "in" || operator === "not_in"
    ? raw
        .split(",")
        .map(one)
        .filter((v) => v !== "")
    : one(raw);
}

/** Conditions → domain DSL (``[[path, op, value], …]`` or ``{"any": [...]}``), as the server builds it. */
export function toDomain(conditions: Condition[], match: "all" | "any"): unknown {
  const leaves = conditions.map((c) =>
    c.operator === "is_set" || c.operator === "is_not_set" ? [c.field, c.operator] : [c.field, c.operator, c.value],
  );
  return match === "any" && leaves.length > 1 ? { any: leaves } : leaves;
}

export function NlSearch({ apply }: { apply: (domain: unknown) => void }) {
  const feature = useAiFeature("crm.nl_search");
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{ runId: number; data: SearchResult } | null>(null);
  const [rows, setRows] = useState<Condition[]>([]);
  const [drafts, setDrafts] = useState<string[]>([]);
  if (!feature.enabled) return <></>;
  if (!feature.available) return <AiUnavailable />;

  const edited = result !== null && JSON.stringify(rows) !== JSON.stringify(result.data.conditions);
  const generate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!query.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const r = await ai.run("crm.nl_search", { inputs: { query } });
      const data = r.data as SearchResult;
      setResult({ runId: r.run_id, data });
      setRows(data.conditions);
      setDrafts(data.conditions.map((c) => showValue(c.value)));
    } catch (err) {
      setError(aiErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  const update = (i: number, patch: Partial<Condition>, draft?: string) => {
    const next = rows.map((r, j) => (j === i ? { ...r, ...patch } : r));
    const field = result?.data.fields[next[i].field];
    if (draft !== undefined || patch.operator) {
      const text = draft ?? drafts[i];
      next[i] = { ...next[i], value: parseValue(text, next[i].operator, field?.type) };
      setDrafts(drafts.map((d, j) => (j === i ? text : d)));
    }
    setRows(next);
  };
  const remove = (i: number) => {
    setRows(rows.filter((_, j) => j !== i));
    setDrafts(drafts.filter((_, j) => j !== i));
  };
  const onApply = () => {
    if (!result) return;
    apply(toDomain(rows, result.data.match));
    setOpen(false);
    setResult(null);
    setQuery("");
  };
  const fields = result ? Object.entries(result.data.fields) : [];

  return (
    <span className="crm-nl-search">
      <button type="button" className="btn ghost small" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        ✦ Ask AI
      </button>
      {open && (
        <div className="search-panel crm-nl-panel" role="dialog" aria-label="Search with AI">
          <form onSubmit={generate} className="crm-nl-form">
            <input
              className="input"
              aria-label="Describe what you are looking for"
              placeholder="e.g. open deals over 10k in EMEA closing this quarter owned by me"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
            <button type="submit" className="btn small" disabled={busy || !query.trim()}>
              {busy ? "Thinking…" : "Generate filter"}
            </button>
          </form>
          {error && (
            <div className="alert error" role="alert">
              {error}
            </div>
          )}
          {result && (
            <div className="crm-nl-review" aria-label="Suggested filter">
              <p className="muted small">
                {!edited && <AiLabel text="AI-suggested" />} {result.data.explanation}
              </p>
              {result.data.match === "any" && rows.length > 1 && <p className="small">Any of these conditions:</p>}
              {rows.length === 0 && <p className="muted small">No condition — this would show everything.</p>}
              {rows.map((row, i) => (
                <div key={i} className="crm-nl-row">
                  <select
                    className="input small"
                    aria-label={`Field of condition ${i + 1}`}
                    value={row.field}
                    onChange={(e) => update(i, { field: e.target.value }, drafts[i])}
                  >
                    {!result.data.fields[row.field] && <option value={row.field}>{row.field}</option>}
                    {fields.map(([path, f]) => (
                      <option key={path} value={path}>
                        {f.label}
                      </option>
                    ))}
                  </select>
                  <select
                    className="input small"
                    aria-label={`Operator of condition ${i + 1}`}
                    value={row.operator}
                    onChange={(e) => update(i, { operator: e.target.value as Operator })}
                  >
                    {OPERATORS.map(([op, label]) => (
                      <option key={op} value={op}>
                        {label}
                      </option>
                    ))}
                  </select>
                  {row.operator !== "is_set" && row.operator !== "is_not_set" && (
                    <input
                      className="input small"
                      aria-label={`Value of condition ${i + 1}`}
                      value={drafts[i] ?? ""}
                      onChange={(e) => update(i, {}, e.target.value)}
                    />
                  )}
                  <button
                    type="button"
                    className="btn ghost small"
                    aria-label={`Remove condition ${i + 1}`}
                    onClick={() => remove(i)}
                  >
                    ×
                  </button>
                </div>
              ))}
              <div className="ai-actions">
                <button type="button" className="btn primary small" onClick={onApply}>
                  Apply filter
                </button>
                <Feedback runId={result.runId} />
              </div>
            </div>
          )}
        </div>
      )}
    </span>
  );
}

// ------------------------------------------------------------------------------------------ extracted details

export function LeadSuggestions({ resId }: { resId: number }) {
  const feature = useAiFeature("crm.lead_extract");
  const qc = useQueryClient();
  const pending = useQuery({
    queryKey: ["ai", "proposals", LEAD, resId, "crm.lead_extract"],
    queryFn: () => ai.proposals({ res_model: LEAD, res_id: resId }),
    enabled: feature.enabled,
  });
  const items = (pending.data ?? []).filter((p) => p.feature === "crm.lead_extract");
  if (!feature.enabled || !items.length) return <Hidden />;
  return (
    <div className="crm-assist">
      <p className="muted small">Details read by AI from the e-mail or form that created this lead.</p>
      {items.map((p) => (
        <ProposalCard key={p.id} proposal={p} onDecided={() => void qc.invalidateQueries({ queryKey: ["record"] })} />
      ))}
    </div>
  );
}

// ------------------------------------------------------------------------------------------ call summary

interface RunDetail {
  id: number;
  feature: string;
  prompt_version: string;
  provider: string | null;
  model: string | null;
  status: string;
  error_code: string | null;
  decision: string;
  create_date: string | null;
  user: string | null;
  output_data: {
    summary?: string;
    key_points?: string[];
    next_step?: { summary: string; due_date: string | null } | null;
  } | null;
  tool_calls: { step: number; name: string; allowed: boolean; error: string | null }[];
  citations: Citation[];
  proposals: Proposal[];
}

interface JobState {
  id: number;
  state: string;
  run_id: number | null;
  status: string | null;
  message: string | null;
}

const POLL_MS = 1500;

export function CallNotes({ resId, reload }: { resId: number; reload: () => void }) {
  const feature = useAiFeature("crm.call_summary");
  const [notes, setNotes] = useState("");
  const [job, setJob] = useState<JobState | null>(null);
  const [run, setRun] = useState<RunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const loadRun = useCallback(async (runId: number) => {
    setRun(await request<RunDetail>("GET", `/ai/runs/${runId}`));
  }, []);

  // reopening the lead (e.g. from the notification) shows the latest summary still waiting for decisions
  useEffect(() => {
    if (!feature.enabled) return;
    let alive = true;
    request<{ id: number; feature: string; decision: string; status: string }[]>("GET", "/ai/runs", {
      query: { res_model: LEAD, res_id: resId },
    })
      .then((runs) => {
        const last = runs.find(
          (r) => r.feature === "crm.call_summary" && r.status === "ok" && r.decision === "pending",
        );
        if (alive && last) void loadRun(last.id);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [feature.enabled, resId, loadRun]);

  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );

  if (!feature.enabled) return <Hidden />;
  if (!feature.available) return <AiUnavailable />;

  const poll = async (jobId: number) => {
    try {
      const state = await request<JobState>("GET", `/crm/ai/jobs/${jobId}`);
      setJob(state);
      if (state.state === "done") {
        if (state.run_id) await loadRun(state.run_id);
        if (state.status && state.status !== "ok") setError(state.message ?? "The summary could not be made.");
        return;
      }
      if (state.state === "dead" || state.state === "cancelled") {
        setError("The summary could not be made. You can try again.");
        return;
      }
      timer.current = setTimeout(() => void poll(jobId), POLL_MS);
    } catch (e) {
      setError(aiErrorMessage(e));
    }
  };
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setRun(null);
    try {
      const queued = await request<{ job_id: number; state: string }>("POST", `/crm/ai/leads/${resId}/call-summary`, {
        body: { notes },
      });
      setJob({ id: queued.job_id, state: queued.state, run_id: null, status: null, message: null });
      void poll(queued.job_id);
    } catch (err) {
      setError(aiErrorMessage(err));
    }
  };
  const working = job !== null && !["done", "dead", "cancelled"].includes(job.state);
  const out = run?.output_data;
  const decided = (run?.proposals ?? []).filter((p) => p.state !== "pending").length;

  return (
    <div className="crm-assist">
      <form onSubmit={submit}>
        <label className="field">
          <span>Call or meeting notes</span>
          <textarea
            rows={5}
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="Paste the transcript or your notes"
          />
        </label>
        <button type="submit" className="btn small" disabled={working || notes.trim().length < 10}>
          {working ? "Summarising…" : "Summarise"}
        </button>
      </form>
      {working && (
        <p className="muted small" role="status">
          Working on it in the background — you will be notified when it is ready.
        </p>
      )}
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
      {run && out && (
        <div className="crm-call-summary">
          <h3 className="small">Summary {run.decision === "pending" && <AiLabel />}</h3>
          <p>{out.summary}</p>
          {!!out.key_points?.length && (
            <ul>
              {out.key_points.map((k) => (
                <li key={k}>{k}</li>
              ))}
            </ul>
          )}
          <Feedback runId={run.id} />
          {run.proposals.map((p) => (
            <ProposalCard
              key={p.id}
              proposal={p}
              onDecided={() => {
                reload();
                void loadRun(run.id);
              }}
            />
          ))}
          <details className="crm-audit">
            <summary>Audit trail</summary>
            <dl aria-label="AI run audit">
              <dt>Run</dt>
              <dd>
                #{run.id} · {run.create_date?.slice(0, 16).replace("T", " ")} · {run.user}
              </dd>
              <dt>Prompt</dt>
              <dd>{run.prompt_version}</dd>
              <dt>Model</dt>
              <dd>{[run.provider, run.model].filter(Boolean).join(" · ") || "—"}</dd>
              <dt>Status</dt>
              <dd>{run.status}</dd>
              <dt>Decision</dt>
              <dd>
                {run.decision} ({decided}/{run.proposals.length} decided)
              </dd>
              <dt>Tool calls</dt>
              <dd>{run.tool_calls.length ? run.tool_calls.map((c) => c.name).join(", ") : "none"}</dd>
            </dl>
          </details>
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------------------------------ objections

interface ObjectionResult {
  responses: { approach: string; response: string; citations: string[] }[];
  citations: string[];
  insufficient_evidence: boolean;
}

function SuggestedResponse({ item, citations }: { item: ObjectionResult["responses"][number]; citations: Citation[] }) {
  const [text, setText] = useState(item.response);
  const cited = citations.filter((c) => item.citations.includes(c.id));
  return (
    <li className="crm-objection">
      <strong>{item.approach}</strong> {text === item.response && <AiLabel text="AI-suggested" />}
      <textarea
        aria-label={`Response: ${item.approach}`}
        rows={3}
        value={text}
        onChange={(e) => setText(e.target.value)}
      />
      {cited.length > 0 && (
        <>
          <span className="muted small">Based on:</span>
          <Citations items={cited} />
        </>
      )}
    </li>
  );
}

export function Objections({ resId }: { resId: number }) {
  const feature = useAiFeature("crm.objections");
  const [objection, setObjection] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{ runId: number; data: ObjectionResult; citations: Citation[] } | null>(null);
  if (!feature.enabled) return <Hidden />;
  if (!feature.available) return <AiUnavailable />;
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await ai.run("crm.objections", { inputs: { objection }, res_model: LEAD, res_id: resId });
      setResult({ runId: r.run_id, data: r.data as ObjectionResult, citations: r.citations });
    } catch (err) {
      setError(aiErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="crm-assist">
      <form onSubmit={submit}>
        <label className="field">
          <span>What did the customer object?</span>
          <input
            className="input"
            value={objection}
            onChange={(e) => setObjection(e.target.value)}
            placeholder="e.g. Your price is above our budget"
          />
        </label>
        <button type="submit" className="btn small" disabled={busy || objection.trim().length < 3}>
          {busy ? "Looking at past deals…" : "Suggest responses"}
        </button>
      </form>
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
      {result && (
        <div aria-label="Suggested responses">
          {result.data.insufficient_evidence && (
            <p className="muted small">No similar past deal was found — general advice only.</p>
          )}
          <ul className="crm-objections">
            {result.data.responses.map((item, i) => (
              <SuggestedResponse key={`${result.runId}-${i}`} item={item} citations={result.citations} />
            ))}
          </ul>
          <Feedback runId={result.runId} />
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------------------------------ registration

registerAiDraft(LEAD, "crm.followup_draft");
registerSearchAddon(LEAD, { key: "crm.nl_search", render: ({ apply }) => <NlSearch apply={apply} /> });
registerFormPanel(LEAD, { title: "AI suggestions", render: ({ resId }) => <LeadSuggestions resId={resId} /> });
registerFormPanel(LEAD, {
  title: "Summarise call notes",
  render: ({ resId, reload }) => <CallNotes resId={resId} reload={reload} />,
});
registerFormPanel(LEAD, { title: "Objection handling", render: ({ resId }) => <Objections resId={resId} /> });
