// Subscription AI (sub-phase 2.6) beside the subscription form: the churn score with its top factors ("Score now"
// recomputes it in-process) and "Draft renewal", a renewal e-mail proposed as a message the user reviews and sends.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ai, aiErrorMessage, type RunResult } from "../../kernel/ai/api";
import { AiLabel, AiUnavailable, Feedback, ProposalCard, useAiFeature } from "../../kernel/ai/components";
import { request } from "../../kernel/api/client";
import { registerFormPanel } from "../../kernel/extensions";
import { t } from "../../kernel/i18n";

export const CHURN = "sale_subscription.churn_predict";
export const RENEWAL = "sale_subscription.renewal_draft";

export interface ChurnFactor {
  feature: string;
  label: string;
  direction: "raises" | "lowers";
  weight: number;
  value: number;
}

export interface ChurnView {
  subscription_id: number;
  is_subscription: boolean;
  running: boolean;
  score: number | null;
  probability: number | null;
  risk: "low" | "medium" | "high" | null;
  factors: ChurnFactor[];
  scored_at: string | null;
  model: { source: "company" | "prior"; trained_at: string | null; rows: number | null; holdout_auc: number | null };
  excluded_inputs: string;
  run_id?: number;
}

const churnApi = {
  get: (id: number) => request<ChurnView>("GET", `/sale_subscription/ai/subscriptions/${id}/churn`),
  rescore: (id: number) => request<ChurnView>("POST", `/sale_subscription/ai/subscriptions/${id}/churn`),
};

const Hidden = () => <span className="ai-panel-hidden" hidden />;

export function riskLabel(risk: ChurnView["risk"]): string {
  if (risk === "high") return t("High churn risk");
  if (risk === "medium") return t("At risk");
  if (risk === "low") return t("Low churn risk");
  return t("Not scored yet");
}

/** The churn score, its risk level and the factors that raise or lower it. */
export function ChurnBlock({ resId }: { resId: number }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const data = useQuery({ queryKey: ["churn", resId], queryFn: () => churnApi.get(resId), retry: false });
  const rescore = useMutation({
    mutationFn: () => churnApi.rescore(resId),
    onSuccess: (view) => qc.setQueryData(["churn", resId], view),
  });
  if (data.isLoading) return <p className="muted small">{t("Loading the churn score…")}</p>;
  if (data.isError) return <div className="alert error">{aiErrorMessage(data.error)}</div>;
  const view = data.data!;
  if (!view.is_subscription) return null;
  const maxWeight = Math.max(0.01, ...view.factors.map((f) => Math.abs(f.weight)));
  return (
    <div className="ai-churn">
      <div className="ai-score-head">
        {view.score !== null && (
          <span className="ai-score-value" aria-label={t("Churn score {score} out of 100", { score: view.score })}>
            {view.score}
          </span>
        )}
        <span className="ai-score-meta">
          <span className={`badge risk-${view.risk ?? "none"}`}>{riskLabel(view.risk)}</span>{" "}
          <AiLabel text={t("AI score")} />
          <span className="muted small">
            {view.model.source === "company"
              ? t("Company model trained on {rows} subscriptions", { rows: view.model.rows ?? "?" })
              : t("Starting weights until the company has enough history")}
          </span>
        </span>
      </div>
      <div className="ai-actions">
        {view.running && (
          <button type="button" className="btn small" disabled={rescore.isPending} onClick={() => rescore.mutate()}>
            {view.score === null ? t("Score now") : t("Score again")}
          </button>
        )}
        {view.factors.length > 0 && (
          <button
            type="button"
            className="btn ghost small"
            aria-expanded={open}
            aria-controls={`churn-why-${resId}`}
            onClick={() => setOpen((o) => !o)}
          >
            {t("Why this score?")}
          </button>
        )}
      </div>
      {rescore.isError && <div className="alert error">{aiErrorMessage(rescore.error)}</div>}
      {open && (
        <div id={`churn-why-${resId}`} className="ai-why" role="region" aria-label={t("Why this score")}>
          <ul className="ai-factors">
            {view.factors.map((f) => (
              <li key={f.feature} className={`factor ${f.direction === "raises" ? "negative" : "positive"}`}>
                <span aria-hidden="true">{f.direction === "raises" ? "▲" : "▼"}</span>{" "}
                <span className="sr-only">
                  {f.direction === "raises" ? t("Raises the risk:") : t("Lowers the risk:")}
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
          <p className="muted small">{view.excluded_inputs}.</p>
          {view.run_id && <Feedback runId={view.run_id} />}
        </div>
      )}
    </div>
  );
}

interface RenewalData {
  subject: string;
  body: string;
  removed_numbers: string[];
  tone: string;
}

/** "Draft renewal": a renewal e-mail written from the subscription's records, proposed as a message. */
export function RenewalDraft({ resId, reload }: { resId: number; reload: () => void }) {
  const feature = useAiFeature(RENEWAL);
  const [result, setResult] = useState<RunResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (!feature.enabled) return null;
  if (!feature.available) return <AiUnavailable />;
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(await ai.run(RENEWAL, { res_model: "sale.quotation", res_id: resId }));
    } catch (e) {
      setError(aiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  const data = result?.data as RenewalData | undefined;
  return (
    <div className="ai-renewal">
      <button type="button" className="btn small" disabled={busy} onClick={run}>
        {busy ? t("Drafting…") : result ? t("Draft again") : t("Draft renewal")}
      </button>
      {error && <div className="alert error">{error}</div>}
      {result && data && (
        <div className="ai-renewal-result">
          {data.subject && (
            <p className="small">
              <strong>{t("Subject")}:</strong> {data.subject}
            </p>
          )}
          {data.removed_numbers.length > 0 && (
            <p className="muted small" role="note">
              {t("Sentences with figures not found in the records were removed: {numbers}", {
                numbers: data.removed_numbers.join(", "),
              })}
            </p>
          )}
          {result.proposals.map((p) => (
            <ProposalCard key={p.id} proposal={p} onDecided={() => reload()} />
          ))}
          <Feedback runId={result.run_id} />
        </div>
      )}
    </div>
  );
}

export function SubscriptionAiPanel({
  resId,
  record,
  reload,
}: {
  resId: number;
  record: Record<string, unknown>;
  reload: () => void;
}) {
  const churn = useAiFeature(CHURN);
  const renewal = useAiFeature(RENEWAL);
  const state = record.subscription_state as string | undefined;
  const isSubscription = !!record.plan_id && !!state && state !== "draft";
  if (!isSubscription || (!churn.enabled && !renewal.enabled)) return <Hidden />;
  const running = state === "progress" || state === "paused";
  return (
    <div className="ai-subscription">
      {churn.enabled && <ChurnBlock resId={resId} />}
      {renewal.enabled && running && (
        <section aria-label={t("Renewal outreach")}>
          <h3 className="small">{t("Renewal outreach")}</h3>
          <RenewalDraft resId={resId} reload={reload} />
        </section>
      )}
    </div>
  );
}

registerFormPanel("sale.quotation", {
  get title() {
    return t("Churn and renewal (AI)");
  },
  render: ({ resId, record, reload }) => <SubscriptionAiPanel resId={resId} record={record} reload={reload} />,
});
