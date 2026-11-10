// CRM insights (Phase 1.6): AI score with a "why" panel, deal risk badge, next-best-action and routing on
// the lead form; bulk routing review for selected leads; forecast with confidence ranges for the reports.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ai, aiErrorMessage, type Proposal, type RunResult } from "../../kernel/ai/api";
import {
  AiLabel,
  AiUnavailable,
  Feedback,
  ProposalCard,
  proposalStateLabel,
  useAiFeature,
} from "../../kernel/ai/components";
import { request } from "../../kernel/api/client";
import { registerFormPanel, registerSelectionAction } from "../../kernel/extensions";
import { formatNumber } from "../../kernel/format";
import { t } from "../../kernel/i18n";

export interface ScoreFactor {
  feature: string;
  label: string;
  value: number;
  direction: "positive" | "negative";
  weight: number;
}

export interface LeadInsights {
  lead_id: number;
  score?: {
    score: number | null;
    probability: number | null;
    factors: ScoreFactor[];
    source: "model" | "rules" | null;
    scored_at: string | null;
    model: { version: number; trained_at: string | null; rows: number | null; holdout_auc: number | null } | null;
    excluded_inputs: string;
    run_id: number | null;
  };
  risk?: {
    level: "none" | "low" | "medium" | "high" | null;
    score: number | null;
    reasons: { code: string; label: string; weight: number }[];
    checked_at: string | null;
    run_id: number | null;
  };
}

export interface RoutingRow {
  lead_id: number;
  lead: string;
  user_id: number | null;
  user: string | null;
  team_id: number | null;
  team: string | null;
  reasons: string[];
  confidence: number;
  status: "proposed" | "applied" | "already_assigned" | "no_candidate";
  proposal: Proposal | null;
}

interface RoutingResult {
  run_id: number;
  rows: RoutingRow[];
}

interface NbaAction {
  title: string;
  kind: string;
  rationale: string;
  signals: string[];
  due_in_days: number;
}

export interface ForecastPeriod {
  period: number;
  label: string;
  p10: number;
  p50: number;
  p90: number;
  explanation: string;
}

export interface ForecastResponse {
  enabled: boolean;
  forecast: {
    id: number;
    name: string;
    periods: ForecastPeriod[];
    method: string;
    narrative: string | null;
    run_id: number | null;
    generated_at: string | null;
  } | null;
}

const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)} %`);

const insightsApi = {
  get: (id: number) => request<LeadInsights>("GET", `/crm/ai/leads/${id}/insights`),
  rescore: (id: number) => request<LeadInsights>("POST", `/crm/ai/leads/${id}/score`),
  routeOne: (id: number) => request<RoutingResult>("POST", `/crm/ai/leads/${id}/routing`),
  routeMany: (ids: number[]) => request<RoutingResult>("POST", "/crm/ai/routing", { body: { lead_ids: ids } }),
  forecast: () => request<ForecastResponse>("GET", "/crm/ai/forecast"),
  requestForecast: () => request<{ job_id: number }>("POST", "/crm/ai/forecast", { body: {} }),
};

/** The AI score, its probability and a "why" panel listing the top contributing factors. */
export function ScoreBlock({ leadId, score }: { leadId: number; score: NonNullable<LeadInsights["score"]> }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const rescore = useMutation({
    mutationFn: () => insightsApi.rescore(leadId),
    onSuccess: (data) => qc.setQueryData(["crm-ai", "insights", leadId], data),
  });
  if (score.score === null) {
    return (
      <div className="ai-score empty">
        <span className="muted small">{t("Not scored yet.")}</span>{" "}
        <button type="button" className="btn small" disabled={rescore.isPending} onClick={() => rescore.mutate()}>
          {t("Score now")}
        </button>
      </div>
    );
  }
  const maxWeight = Math.max(0.01, ...score.factors.map((f) => Math.abs(f.weight)));
  return (
    <div className="ai-score">
      <div className="ai-score-head">
        <span className="ai-score-value" aria-label={t("AI score {score} out of 100", { score: score.score })}>
          {score.score}
        </span>
        <span className="ai-score-meta">
          <AiLabel text={t("AI score")} />
          <span className="small">{t("Win probability {value}", { value: pct(score.probability) })}</span>
          <span className="muted small">
            {score.source === "model" && score.model
              ? t("Model v{version}, trained on {rows} closed deals", {
                  version: score.model.version,
                  rows: score.model.rows ?? "?",
                })
              : t("Rule-based until there is enough history")}
          </span>
        </span>
      </div>
      <button
        type="button"
        className="btn ghost small"
        aria-expanded={open}
        aria-controls={`why-${leadId}`}
        onClick={() => setOpen((o) => !o)}
      >
        {t("Why this score?")}
      </button>
      {open && (
        <div id={`why-${leadId}`} className="ai-why" role="region" aria-label={t("Why this score")}>
          <ul className="ai-factors">
            {score.factors.map((f) => (
              <li key={f.feature} className={`factor ${f.direction}`}>
                <span aria-hidden="true">{f.direction === "positive" ? "▲" : "▼"}</span>{" "}
                <span className="sr-only">
                  {f.direction === "positive" ? t("Raises the score:") : t("Lowers the score:")}
                </span>
                {f.label}
                <span
                  className="factor-bar"
                  style={{ width: `${Math.round((60 * Math.abs(f.weight)) / maxWeight)}px` }}
                  aria-hidden="true"
                />
              </li>
            ))}
          </ul>
          <p className="muted small">{score.excluded_inputs}.</p>
          {score.run_id && <Feedback runId={score.run_id} />}
        </div>
      )}
    </div>
  );
}

/** Risk badge with its reasons (only when the deal is flagged). */
export function RiskBlock({ risk }: { risk: NonNullable<LeadInsights["risk"]> }) {
  if (risk.level !== "medium" && risk.level !== "high") {
    return risk.checked_at ? <p className="muted small">{t("No deal risk signals.")}</p> : null;
  }
  return (
    <div className={`ai-risk level-${risk.level}`} role="status">
      <span className={`badge risk-${risk.level}`}>{risk.level === "high" ? t("High risk") : t("At risk")}</span>{" "}
      <AiLabel text={t("AI alert")} />
      <ul className="ai-risk-reasons">
        {risk.reasons.map((r) => (
          <li key={r.code}>{r.label}</li>
        ))}
      </ul>
      {risk.run_id && <Feedback runId={risk.run_id} />}
    </div>
  );
}

/** Next-best-action: ranked actions with rationale; scheduling one is a proposal to accept. */
export function NextBestAction({ leadId }: { leadId: number }) {
  const feature = useAiFeature("crm.next_best_action");
  const [result, setResult] = useState<RunResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (!feature.enabled) return null;
  if (!feature.available) return <AiUnavailable />;
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(await ai.run("crm.next_best_action", { res_model: "crm.lead", res_id: leadId }));
    } catch (e) {
      setError(aiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  const actions = ((result?.data as { actions?: NbaAction[] } | null)?.actions ?? []) as NbaAction[];
  return (
    <div className="ai-nba">
      <button type="button" className="btn small" disabled={busy} onClick={run}>
        {busy ? t("Thinking…") : result ? t("Suggest again") : t("Suggest next actions")}
      </button>
      {error && <div className="alert error">{error}</div>}
      {result && (
        <div className="ai-nba-result">
          <AiLabel text={t("AI suggestions")} />
          <ol className="ai-nba-actions">
            {actions.map((a, i) => (
              <li key={i}>
                <strong>{a.title}</strong> <span className="badge">{a.kind}</span>
                <div className="small">{a.rationale}</div>
              </li>
            ))}
          </ol>
          {result.proposals.map((p) => (
            <ProposalCard key={p.id} proposal={p} />
          ))}
          <Feedback runId={result.run_id} />
        </div>
      )}
    </div>
  );
}

/** Suggested salesperson for an unassigned lead, as a proposal. */
export function RoutingSuggestion({ leadId, onDecided }: { leadId: number; onDecided?: () => void }) {
  const feature = useAiFeature("crm.lead_routing");
  const [out, setOut] = useState<RoutingResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  if (!feature.enabled) return null;
  const run = async () => {
    setError(null);
    try {
      setOut(await insightsApi.routeOne(leadId));
    } catch (e) {
      setError(aiErrorMessage(e));
    }
  };
  const row = out?.rows[0];
  return (
    <div className="ai-routing">
      {!out && (
        <button type="button" className="btn small" onClick={run}>
          {t("Suggest salesperson")}
        </button>
      )}
      {error && <div className="alert error">{error}</div>}
      {row && (
        <div>
          <AiLabel text={t("AI suggestion")} />{" "}
          {row.user ? (
            <span>
              <strong>{row.user}</strong>
              {row.team ? ` (${row.team})` : ""}
            </span>
          ) : (
            <span className="muted">{t("No eligible salesperson found.")}</span>
          )}
          <ul className="small">
            {row.reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
          {row.proposal && <ProposalCard proposal={row.proposal} onDecided={() => onDecided?.()} />}
          {row.status === "applied" && (
            <p className="small">{t("Assigned automatically (allowed by an assignment rule).")}</p>
          )}
          <Feedback runId={out!.run_id} />
        </div>
      )}
    </div>
  );
}

export function LeadInsightsPanel({
  resId,
  record,
  reload,
}: {
  resId: number;
  record: Record<string, unknown>;
  reload: () => void;
}) {
  const score = useAiFeature("crm.lead_score");
  const risk = useAiFeature("crm.deal_risk");
  const nba = useAiFeature("crm.next_best_action");
  const routing = useAiFeature("crm.lead_routing");
  const wantsInsights = score.enabled || risk.enabled;
  const insights = useQuery({
    queryKey: ["crm-ai", "insights", resId],
    queryFn: () => insightsApi.get(resId),
    enabled: wantsInsights,
    retry: false,
  });
  if (!score.enabled && !risk.enabled && !nba.enabled && !routing.enabled) {
    return <div className="ai-insights-hidden" aria-hidden="true" />;
  }
  const unassigned = !record.user_id;
  return (
    <div className="ai-insights">
      {insights.error && <div className="alert error">{aiErrorMessage(insights.error)}</div>}
      {insights.data?.risk && <RiskBlock risk={insights.data.risk} />}
      {insights.data?.score && <ScoreBlock leadId={resId} score={insights.data.score} />}
      {nba.enabled && (
        <section aria-label={t("Next best action")}>
          <h3 className="small">{t("Next best action")}</h3>
          <NextBestAction leadId={resId} />
        </section>
      )}
      {routing.enabled && unassigned && (
        <section aria-label={t("Routing")}>
          <h3 className="small">{t("Routing")}</h3>
          <RoutingSuggestion leadId={resId} onDecided={reload} />
        </section>
      )}
    </div>
  );
}

function routingStatusLabel(status: RoutingRow["status"]): string {
  const labels: Record<RoutingRow["status"], string> = {
    proposed: t("proposed"),
    applied: t("applied"),
    already_assigned: t("already assigned"),
    no_candidate: t("no candidate"),
  };
  return labels[status] ?? status;
}

/** A proposal state after a decision, or the error message when the decision failed. */
const decidedLabel = (value: string) => proposalStateLabel(value as Proposal["state"]);

/** Bulk routing: one proposal per selected lead, accepted or rejected row by row. */
export function RoutingReview({ ids, onDone }: { ids: number[]; onDone: () => void }) {
  const feature = useAiFeature("crm.lead_routing");
  const qc = useQueryClient();
  const result = useQuery({
    queryKey: ["crm-ai", "routing", ids.join(",")],
    queryFn: () => insightsApi.routeMany(ids),
    enabled: feature.enabled,
    retry: false,
    staleTime: Infinity,
  });
  const [decided, setDecided] = useState<Record<number, string>>({});
  if (!feature.enabled) return <p className="muted">{t("AI routing is turned off.")}</p>;
  const decide = async (p: Proposal, accept: boolean) => {
    try {
      const out = accept ? await ai.accept(p.id) : await ai.reject(p.id);
      setDecided((d) => ({ ...d, [p.id]: out.state }));
      void qc.invalidateQueries({ queryKey: ["record", "crm.lead"] });
    } catch (e) {
      setDecided((d) => ({ ...d, [p.id]: aiErrorMessage(e) }));
    }
  };
  const pending = (result.data?.rows ?? []).filter((r) => r.proposal && !decided[r.proposal.id]);
  return (
    <div className="ai-routing-review">
      <AiLabel text={t("AI suggestions")} />
      {result.isLoading && <p className="muted">{t("Finding the best salesperson for each lead…")}</p>}
      {result.error && <div className="alert error">{aiErrorMessage(result.error)}</div>}
      {result.data && (
        <table className="data-table">
          <caption className="sr-only">{t("Suggested assignees")}</caption>
          <thead>
            <tr>
              <th scope="col">{t("Lead")}</th>
              <th scope="col">{t("Suggested")}</th>
              <th scope="col">{t("Why")}</th>
              <th scope="col">{t("Decision")}</th>
            </tr>
          </thead>
          <tbody>
            {result.data.rows.map((r) => (
              <tr key={r.lead_id}>
                <th scope="row">{r.lead}</th>
                <td>{r.user ?? "—"}</td>
                <td className="small">{r.reasons.join("; ")}</td>
                <td>
                  {r.proposal && !decided[r.proposal.id] ? (
                    <span className="ai-actions">
                      <button
                        type="button"
                        className="btn primary small"
                        aria-label={t("Accept {user} for {lead}", { user: r.user, lead: r.lead })}
                        onClick={() => decide(r.proposal!, true)}
                      >
                        {t("Accept")}
                      </button>
                      <button
                        type="button"
                        className="btn small"
                        aria-label={t("Reject suggestion for {lead}", { lead: r.lead })}
                        onClick={() => decide(r.proposal!, false)}
                      >
                        {t("Reject")}
                      </button>
                    </span>
                  ) : (
                    <span className="badge">
                      {r.proposal ? decidedLabel(decided[r.proposal.id]) : routingStatusLabel(r.status)}
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="ai-actions">
        {pending.length > 0 && (
          <button
            type="button"
            className="btn small"
            onClick={async () => {
              for (const r of pending) await decide(r.proposal!, true);
            }}
          >
            {t("Accept all remaining ({count})", { count: pending.length })}
          </button>
        )}
        <button type="button" className="btn ghost small" onClick={onDone}>
          {t("Close")}
        </button>
        {result.data && <Feedback runId={result.data.run_id} />}
      </div>
    </div>
  );
}

/** Forecast with P10/P50/P90 per month, for the sales analysis page. */
export function AiForecastSection() {
  const feature = useAiFeature("crm.forecast");
  const qc = useQueryClient();
  const [queued, setQueued] = useState<string | null>(null);
  const data = useQuery({
    queryKey: ["crm-ai", "forecast"],
    queryFn: insightsApi.forecast,
    enabled: feature.enabled,
    retry: false,
  });
  if (!feature.enabled) return null;
  const refresh = async () => {
    try {
      await insightsApi.requestForecast();
      setQueued(t("Forecast requested — you will get a notification when it is ready."));
      void qc.invalidateQueries({ queryKey: ["crm-ai", "forecast"] });
    } catch (e) {
      setQueued(aiErrorMessage(e));
    }
  };
  const fc = data.data?.forecast;
  return (
    <section aria-labelledby="ai-forecast-h" className="ai-forecast">
      <h2 id="ai-forecast-h">
        {t("Forecast with confidence range")} <AiLabel text={t("AI forecast")} />
      </h2>
      {fc ? (
        <>
          <table className="pivot-table">
            <thead>
              <tr>
                <th scope="col">{t("Month")}</th>
                <th scope="col" className="num">
                  {t("Low (P10)")}
                </th>
                <th scope="col" className="num">
                  {t("Likely (P50)")}
                </th>
                <th scope="col" className="num">
                  {t("High (P90)")}
                </th>
              </tr>
            </thead>
            <tbody>
              {fc.periods.map((p) => (
                <tr key={p.period}>
                  <th scope="row">{p.label}</th>
                  <td className="num">{formatNumber(p.p10, 0)}</td>
                  <td className="num">
                    <strong>{formatNumber(p.p50, 0)}</strong>
                  </td>
                  <td className="num">{formatNumber(p.p90, 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <ul className="ai-forecast-explanations small">
            {fc.periods.map((p) => (
              <li key={p.period}>{p.explanation}</li>
            ))}
          </ul>
          {fc.narrative && <p className="ai-forecast-narrative">{fc.narrative}</p>}
          <p className="muted small">
            {fc.method}
            {fc.generated_at ? ` · ${new Date(fc.generated_at).toLocaleString()}` : ""}
          </p>
          {fc.run_id && <Feedback runId={fc.run_id} />}
        </>
      ) : (
        !data.isLoading && <p className="muted">{t("No forecast computed yet.")}</p>
      )}
      <div className="ai-actions">
        <button type="button" className="btn small" onClick={refresh}>
          {fc ? t("Refresh forecast") : t("Compute forecast")}
        </button>
        {queued && (
          <span className="small" role="status">
            {queued}
          </span>
        )}
      </div>
    </section>
  );
}

registerFormPanel("crm.lead", {
  get title() {
    return t("AI insights");
  },
  render: ({ resId, record, reload }) => <LeadInsightsPanel resId={resId} record={record} reload={reload} />,
});

registerSelectionAction("crm.lead", {
  get label() {
    return t("Suggest salespeople (AI)");
  },
  render: ({ ids, onDone }) => <RoutingReview ids={ids} onDone={onDone} />,
});
