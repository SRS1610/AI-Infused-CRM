// Pre-filing checks (sub-phase 2.6) on the tax return form: the deterministic findings and the statistical outliers
// against the previous periods (tax rate never used for a country, grid far from its usual level), ranked, each with
// why it matters and what to do. Flags only: closing is never blocked. "Brief me" asks the model for a short
// briefing whose figures are checked server-side.
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { aiErrorMessage } from "../../kernel/ai/api";
import { AiLabel, AiUnavailable, Feedback, useAiFeature } from "../../kernel/ai/components";
import { request } from "../../kernel/api/client";
import { registerFormPanel } from "../../kernel/extensions";
import { t } from "../../kernel/i18n";

export const PREFILING = "account_tax.prefiling";

export interface PrefilingItem {
  rank: number;
  code: string;
  severity: "error" | "warning" | "info";
  title: string;
  message: string;
  why: string;
  action: string;
  model: string | null;
  res_ids: number[];
  source: "check" | "statistics";
}

export interface PrefilingReview {
  run_id: number;
  net: number;
  items: PrefilingItem[];
}

export const taxAi = {
  review: (returnId: number) => request<PrefilingReview>("GET", `/account_tax/ai/returns/${returnId}/prefiling`),
  narrative: (returnId: number) =>
    request<{ run_id: number; narrative: string; removed_numbers: string[] }>(
      "POST",
      `/account_tax/ai/returns/${returnId}/prefiling/narrative`,
    ),
};

const severityLabel = (s: PrefilingItem["severity"]) =>
  ({ error: t("Error"), warning: t("Warning"), info: t("Information") })[s] ?? s;

export function PrefilingChecks({ resId }: { resId: number }) {
  const feature = useAiFeature(PREFILING);
  const [briefing, setBriefing] = useState<{ text: string; runId: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const review = useQuery({
    queryKey: ["tax-prefiling", resId],
    queryFn: () => taxAi.review(resId),
    enabled: feature.enabled,
    retry: false,
  });
  if (!feature.enabled) return <span className="ai-panel-hidden" hidden />;
  const brief = async () => {
    setError(null);
    try {
      const out = await taxAi.narrative(resId);
      setBriefing({ text: out.narrative, runId: out.run_id });
    } catch (e) {
      setError(aiErrorMessage(e));
    }
  };
  const items = review.data?.items ?? [];
  return (
    <div className="ai-tax-prefiling">
      <p className="muted small">{t("Checks before filing; they never block the closing.")}</p>
      {review.isLoading && <p className="muted small">{t("Checking…")}</p>}
      {review.data && !items.length && <p className="muted small">{t("Nothing to review before filing.")}</p>}
      {items.length > 0 && (
        <ol aria-label={t("Pre-filing findings")}>
          {items.map((i) => (
            <li key={i.code} className={`anomaly severity-${i.severity}`}>
              <strong>{i.title}</strong> <span className="badge">{severityLabel(i.severity)}</span>{" "}
              {i.source === "statistics" && <AiLabel text={t("Compared with previous periods")} />}
              <div>{i.message}</div>
              <div className="muted small">
                {i.why} {i.action}
              </div>
              {i.model === "account.move" && i.res_ids.length > 0 && (
                <div className="small">
                  {i.res_ids.slice(0, 5).map((id) => (
                    <Link key={id} to={`/m/account.move/${id}`} className="chip">
                      #{id}
                    </Link>
                  ))}
                  {i.res_ids.length > 5 && t("+{count} more", { count: i.res_ids.length - 5 })}
                </div>
              )}
            </li>
          ))}
        </ol>
      )}
      <div className="ai-actions">
        <button type="button" className="btn ghost small" onClick={() => void review.refetch()}>
          {t("Check again")}
        </button>
        {items.length > 0 &&
          (feature.available ? (
            <button type="button" className="btn ghost small" onClick={() => void brief()}>
              {t("Brief me")}
            </button>
          ) : (
            <AiUnavailable />
          ))}
      </div>
      {briefing && (
        <p className="ai-text">
          <AiLabel /> {briefing.text} <Feedback runId={briefing.runId} />
        </p>
      )}
      {(error || review.isError) && (
        <div className="alert error" role="alert">
          {error ?? aiErrorMessage(review.error)}
        </div>
      )}
    </div>
  );
}

registerFormPanel("account.tax_return", {
  get title() {
    return t("Pre-filing checks (AI)");
  },
  render: ({ resId, record }) =>
    record.state === "closed" ? <span className="ai-panel-hidden" hidden /> : <PrefilingChecks resId={resId} />,
});
