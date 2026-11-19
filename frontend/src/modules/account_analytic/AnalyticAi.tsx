// Analytic allocation suggestions (sub-phase 2.6) on invoices and bills: for each line, the distribution models'
// plans plus the distribution most used by similar earlier lines, as a proposal the user accepts or rejects.
import { useState } from "react";
import { aiErrorMessage, type Proposal } from "../../kernel/ai/api";
import { AiLabel, AiUnavailable, ProposalCard, useAiFeature } from "../../kernel/ai/components";
import { request } from "../../kernel/api/client";
import { registerFormPanel } from "../../kernel/extensions";
import { t } from "../../kernel/i18n";

export const ALLOCATION = "account_analytic.allocation_suggest";

export interface AllocationRow {
  line_id: number;
  label?: string;
  suggestion?: Record<string, number>;
  accounts?: { id: number; name: string; plan: string; percent: number }[];
  source?: "models" | "history" | "models+history" | null;
  proposal?: Proposal | null;
  error?: string;
}

export const analyticAi = {
  suggestMove: (moveId: number) =>
    request<{ rows: AllocationRow[] }>("POST", `/account_analytic/ai/moves/${moveId}/suggest`),
};

const INVOICE_TYPES = ["out_invoice", "out_refund", "in_invoice", "in_refund"];

function sourceLabel(source: AllocationRow["source"]): string {
  if (source === "models") return t("from the distribution models");
  if (source === "history") return t("from similar earlier lines");
  if (source === "models+history") return t("distribution models and similar earlier lines");
  return "";
}

export function AnalyticSuggestions({ resId }: { resId: number }) {
  const feature = useAiFeature(ALLOCATION);
  const [rows, setRows] = useState<AllocationRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (!feature.enabled) return <span className="ai-panel-hidden" hidden />;
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      setRows((await analyticAi.suggestMove(resId)).rows);
    } catch (e) {
      setError(aiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="ai-analytic">
      <button type="button" className="btn small" disabled={busy} onClick={() => void run()}>
        {busy ? t("Looking at similar lines…") : t("Suggest analytic distributions")}
      </button>
      {!feature.available && <AiUnavailable />}
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
      {rows && (
        <ul aria-label={t("Analytic suggestions")}>
          {rows.map((r) => (
            <li key={r.line_id}>
              <strong>{r.label}</strong>{" "}
              {r.error ? (
                <span className="muted">{r.error}</span>
              ) : r.accounts && r.accounts.length > 0 ? (
                <>
                  → {r.accounts.map((a) => `${a.name} ${a.percent}%`).join(", ")}{" "}
                  <span className="muted small">({sourceLabel(r.source)})</span> <AiLabel text={t("AI suggestion")} />
                  {r.proposal ? (
                    <ProposalCard proposal={r.proposal} />
                  ) : (
                    <span className="muted small"> {t("Already allocated this way.")}</span>
                  )}
                </>
              ) : (
                <span className="muted">{t("No similar line to learn from yet.")}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

registerFormPanel("account.move", {
  get title() {
    return t("Analytic suggestions (AI)");
  },
  render: ({ resId, record }) =>
    INVOICE_TYPES.includes(String(record.move_type)) ? (
      <AnalyticSuggestions resId={resId} />
    ) : (
      <span className="ai-panel-hidden" hidden />
    ),
});
