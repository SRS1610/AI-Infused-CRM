// Payment failure summary (sub-phase 2.6). On a failed transaction: why it failed (insufficient funds, card
// expired, authentication required, fraud suspected, provider error, other) and what to do next. On a provider:
// the failures of the last 90 days per customer, with an optional AI narrative (figures checked server-side).
// Nothing is retried or sent.
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { aiErrorMessage } from "../../kernel/ai/api";
import { AiLabel, AiUnavailable, Feedback, useAiFeature } from "../../kernel/ai/components";
import { request } from "../../kernel/api/client";
import { registerFormPanel } from "../../kernel/extensions";
import { t } from "../../kernel/i18n";

export const FAILURES = "payment.failure_summary";

export interface FailureInfo {
  run_id: number;
  category: string;
  label: string;
  advice: string;
  code: string | null;
  message: string;
}

export interface CustomerFailures {
  partner_id: number | null;
  customer: string;
  failures: number;
  amounts: Record<string, number>;
  main_label: string;
  advice: string;
  last_failure: string;
}

export interface FailureSummary {
  run_id: number;
  failures: number;
  since: string;
  by_category: Record<string, number>;
  labels: Record<string, string>;
  customers: CustomerFailures[];
}

export const paymentAi = {
  failure: (txId: number) => request<FailureInfo>("GET", `/payment/ai/transactions/${txId}/failure`),
  summary: (providerId?: number, days = 90) =>
    request<FailureSummary>("GET", "/payment/ai/failures", {
      query: providerId ? { days, provider_id: providerId } : { days },
    }),
  narrative: (days = 90) =>
    request<{ run_id: number; narrative: string; removed_numbers: string[] }>(
      "POST",
      "/payment/ai/failures/narrative",
      {
        query: { days },
      },
    ),
};

const Hidden = () => <span className="ai-panel-hidden" hidden />;
const money = (v: number, c: string) =>
  `${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${c}`;

export function TransactionFailure({ resId }: { resId: number }) {
  const feature = useAiFeature(FAILURES);
  const info = useQuery({
    queryKey: ["payment-failure", resId],
    queryFn: () => paymentAi.failure(resId),
    enabled: feature.enabled,
    retry: false,
  });
  if (!feature.enabled) return <Hidden />;
  if (info.isError) return <div className="alert error">{aiErrorMessage(info.error)}</div>;
  if (!info.data) return <p className="muted small">{t("Checking…")}</p>;
  return (
    <div className="ai-payment-failure">
      <p>
        <strong>{info.data.label}</strong> <AiLabel text={t("AI classification")} />
      </p>
      <p>{info.data.advice}</p>
      {info.data.code && <p className="muted small">{t("Provider code: {code}", { code: info.data.code })}</p>}
    </div>
  );
}

export function ProviderFailures({ resId }: { resId: number }) {
  const feature = useAiFeature(FAILURES);
  const [narrative, setNarrative] = useState<{ text: string; runId: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const summary = useQuery({
    queryKey: ["payment-failures", resId],
    queryFn: () => paymentAi.summary(resId),
    enabled: feature.enabled,
    retry: false,
  });
  if (!feature.enabled) return <Hidden />;
  const narrate = async () => {
    setError(null);
    try {
      const out = await paymentAi.narrative();
      setNarrative({ text: out.narrative, runId: out.run_id });
    } catch (e) {
      setError(aiErrorMessage(e));
    }
  };
  const data = summary.data;
  return (
    <div className="ai-payment-failures">
      {summary.isLoading && <p className="muted small">{t("Checking…")}</p>}
      {data && data.failures === 0 && <p className="muted small">{t("No failed payment in the last 90 days.")}</p>}
      {data && data.failures > 0 && (
        <>
          <p>
            {t("{count} failed payments since {date}", { count: data.failures, date: data.since })}:{" "}
            {Object.entries(data.by_category)
              .map(([c, n]) => `${data.labels[c] ?? c} ${n}`)
              .join(", ")}{" "}
            <AiLabel text={t("AI classification")} />
          </p>
          <table className="table small">
            <thead>
              <tr>
                <th scope="col">{t("Customer")}</th>
                <th scope="col">{t("Failures")}</th>
                <th scope="col">{t("Amount")}</th>
                <th scope="col">{t("Main reason")}</th>
                <th scope="col">{t("Next step")}</th>
              </tr>
            </thead>
            <tbody>
              {data.customers.map((r) => (
                <tr key={String(r.partner_id)}>
                  <td>{r.customer}</td>
                  <td>{r.failures}</td>
                  <td>
                    {Object.entries(r.amounts)
                      .map(([c, v]) => money(v, c))
                      .join(", ")}
                  </td>
                  <td>{r.main_label}</td>
                  <td>{r.advice}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="ai-actions">
            {feature.available ? (
              <button type="button" className="btn ghost small" onClick={() => void narrate()}>
                {t("Summarise all failures")}
              </button>
            ) : (
              <AiUnavailable />
            )}
          </div>
        </>
      )}
      {narrative && (
        <p className="ai-text">
          <AiLabel /> {narrative.text} <Feedback runId={narrative.runId} />
        </p>
      )}
      {(error || summary.isError) && (
        <div className="alert error" role="alert">
          {error ?? aiErrorMessage(summary.error)}
        </div>
      )}
    </div>
  );
}

registerFormPanel("payment.transaction", {
  get title() {
    return t("Why did this payment fail? (AI)");
  },
  render: ({ resId, record }) =>
    record.state === "error" || record.state === "cancel" ? <TransactionFailure resId={resId} /> : <Hidden />,
});

registerFormPanel("payment.provider", {
  get title() {
    return t("Payment failures (AI)");
  },
  render: ({ resId }) => <ProviderFailures resId={resId} />,
});
