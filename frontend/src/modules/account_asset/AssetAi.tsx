// Asset AI (sub-phase 2.6). On a vendor bill: "Suggest asset models" ranks the company's asset models for each line
// and proposes one (accepting creates the draft asset, or sets the model on the line's draft asset). On an asset:
// depreciation checks (flags, nothing is changed) with an optional plain-language explanation.
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { aiErrorMessage, type Proposal } from "../../kernel/ai/api";
import { AiLabel, AiUnavailable, Feedback, ProposalCard, useAiFeature } from "../../kernel/ai/components";
import { request } from "../../kernel/api/client";
import { registerFormPanel } from "../../kernel/extensions";
import { t } from "../../kernel/i18n";

export const CLASSIFY = "account_asset.classify";
export const CHECK = "account_asset.depreciation_check";

export interface ModelSuggestion {
  model_id: number;
  name: string;
  score: number;
}

export interface LineSuggestion {
  line_id: number;
  text?: string;
  suggestion?: ModelSuggestion | null;
  used_model?: boolean;
  proposal?: Proposal | null;
  error?: string;
}

export interface AssetIssue {
  code: string;
  message: string;
  severity: "error" | "warning";
}

export const assetAi = {
  classifyBill: (moveId: number) =>
    request<{ rows: LineSuggestion[] }>("POST", `/account_asset/ai/bills/${moveId}/classify`),
  check: (assetId: number) =>
    request<{ run_id: number; flagged: boolean; issues: AssetIssue[] }>(
      "GET",
      `/account_asset/ai/assets/${assetId}/check`,
    ),
  explain: (assetId: number) =>
    request<{ run_id: number; text: string; removed_numbers: string[] }>(
      "POST",
      `/account_asset/ai/assets/${assetId}/explain`,
    ),
};

const Hidden = () => <span className="ai-panel-hidden" hidden />;

export function BillAssetSuggestions({ resId }: { resId: number }) {
  const feature = useAiFeature(CLASSIFY);
  const [rows, setRows] = useState<LineSuggestion[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (!feature.enabled) return <Hidden />;
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      setRows((await assetAi.classifyBill(resId)).rows);
    } catch (e) {
      setError(aiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="ai-asset-classify">
      <p className="muted small">
        {t("Suggests the asset model of each line; accepting creates the draft asset with that model.")}
      </p>
      <button type="button" className="btn small" disabled={busy} onClick={() => void run()}>
        {busy ? t("Classifying…") : t("Suggest asset models")}
      </button>
      {!feature.available && <AiUnavailable />}
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
      {rows && rows.length === 0 && <p className="muted small">{t("Every line already has an asset.")}</p>}
      {rows && rows.length > 0 && (
        <ul aria-label={t("Asset model suggestions")}>
          {rows.map((r) => (
            <li key={r.line_id}>
              <strong>{r.text}</strong>{" "}
              {r.error ? (
                <span className="muted">{r.error}</span>
              ) : r.suggestion ? (
                <>
                  → {r.suggestion.name} <AiLabel text={t("AI suggestion")} />
                </>
              ) : (
                <span className="muted">{t("No asset model fits this line.")}</span>
              )}
              {r.proposal && <ProposalCard proposal={r.proposal} />}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function AssetChecks({ resId }: { resId: number }) {
  const feature = useAiFeature(CHECK);
  const [explanation, setExplanation] = useState<{ text: string; runId: number; removed: string[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const checks = useQuery({
    queryKey: ["asset-checks", resId],
    queryFn: () => assetAi.check(resId),
    enabled: feature.enabled,
    retry: false,
  });
  if (!feature.enabled) return <Hidden />;
  const explain = async () => {
    setError(null);
    try {
      const out = await assetAi.explain(resId);
      setExplanation({ text: out.text, runId: out.run_id, removed: out.removed_numbers });
    } catch (e) {
      setError(aiErrorMessage(e));
    }
  };
  const issues = checks.data?.issues ?? [];
  return (
    <div className="ai-asset-checks">
      {checks.isLoading && <p className="muted small">{t("Checking…")}</p>}
      {checks.data && !issues.length && <p className="muted small">{t("No depreciation anomaly found.")}</p>}
      {issues.length > 0 && (
        <ul aria-label={t("Depreciation findings")}>
          {issues.map((i, n) => (
            <li key={`${i.code}-${n}`} className={`anomaly severity-${i.severity}`}>
              <span className="badge">{i.severity === "error" ? t("Error") : t("Warning")}</span> {i.message}{" "}
              <AiLabel text={t("AI check")} />
            </li>
          ))}
        </ul>
      )}
      <div className="ai-actions">
        <button type="button" className="btn ghost small" onClick={() => void checks.refetch()}>
          {t("Check again")}
        </button>
        {issues.length > 0 && feature.available && (
          <button type="button" className="btn ghost small" onClick={() => void explain()}>
            {t("Explain")}
          </button>
        )}
        {!feature.available && <AiUnavailable />}
      </div>
      {explanation && (
        <p className="ai-text">
          <AiLabel /> {explanation.text} <Feedback runId={explanation.runId} />
        </p>
      )}
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
    </div>
  );
}

registerFormPanel("account.move", {
  get title() {
    return t("Asset classification (AI)");
  },
  render: ({ resId, record }) =>
    record.move_type === "in_invoice" ? <BillAssetSuggestions resId={resId} /> : <Hidden />,
});

registerFormPanel("account.asset", {
  get title() {
    return t("Depreciation checks (AI)");
  },
  render: ({ resId, record }) => (record.state === "model" ? <Hidden /> : <AssetChecks resId={resId} />),
});
