// Commission AI (sub-phase 2.6) beside the commission statement form: "Explain" writes a plain-language explanation of
// the statement that may only use the statement's own numbers (sentences with other figures are removed server-side).
import { useState } from "react";
import { ai, aiErrorMessage, type RunResult } from "../../kernel/ai/api";
import { AiLabel, AiUnavailable, Feedback, useAiFeature } from "../../kernel/ai/components";
import { registerFormPanel } from "../../kernel/extensions";
import { t } from "../../kernel/i18n";

export const EXPLAIN = "sale_commission.explain";

interface ExplainData {
  explanation: string;
  removed_numbers: string[];
  total: number;
  currency: string;
}

const Hidden = () => <span className="ai-panel-hidden" hidden />;

export function StatementExplanation({ resId }: { resId: number }) {
  const feature = useAiFeature(EXPLAIN);
  const [result, setResult] = useState<RunResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (!feature.enabled) return <Hidden />;
  if (!feature.available) return <AiUnavailable />;
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(await ai.run(EXPLAIN, { res_model: "sale.commission_statement", res_id: resId }));
    } catch (e) {
      setError(aiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  const data = result?.data as ExplainData | undefined;
  return (
    <div className="ai-commission-explain">
      <p className="muted small">{t("A plain-language explanation that only uses this statement's figures.")}</p>
      <button type="button" className="btn small" disabled={busy} onClick={run}>
        {busy ? t("Explaining…") : result ? t("Explain again") : t("Explain")}
      </button>
      {error && <div className="alert error">{error}</div>}
      {result && data && (
        <div className="ai-explanation" aria-live="polite">
          <AiLabel text={t("AI explanation")} />
          <p>{data.explanation}</p>
          {data.removed_numbers.length > 0 && (
            <p className="muted small" role="note">
              {t("Sentences with figures that are not on the statement were removed: {numbers}", {
                numbers: data.removed_numbers.join(", "),
              })}
            </p>
          )}
          <Feedback runId={result.run_id} />
        </div>
      )}
    </div>
  );
}

registerFormPanel("sale.commission_statement", {
  get title() {
    return t("Explain this statement (AI)");
  },
  render: ({ resId }) => <StatementExplanation resId={resId} />,
});
