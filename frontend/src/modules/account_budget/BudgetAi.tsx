// Budget variance explanations (sub-phase 2.6, account_budget.variance_explain) beside a budget: a few plain sentences
// on what is over or under plan and whether that is ahead of or behind the time elapsed. Every figure must be one of
// the budget-vs-actual report's (sentences with other numbers are removed server-side). Nothing is written.
import { useState } from "react";
import { ai, aiErrorMessage, type RunResult } from "../../kernel/ai/api";
import { AiLabel, AiUnavailable, Feedback, useAiFeature } from "../../kernel/ai/components";
import { registerFormPanel } from "../../kernel/extensions";
import { t } from "../../kernel/i18n";

export const VARIANCE = "account_budget.variance_explain";

interface VarianceData {
  explanation: string;
  removed_numbers: string[];
  as_of: string;
}

export function VarianceExplanation({ resId }: { resId: number }) {
  const feature = useAiFeature(VARIANCE);
  const [focus, setFocus] = useState("");
  const [result, setResult] = useState<RunResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (!feature.enabled) return <span className="ai-panel-hidden" hidden />;
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      const inputs = focus.trim() ? { focus: focus.trim() } : {};
      setResult(await ai.run(VARIANCE, { res_model: "account.budget", res_id: resId, inputs }));
    } catch (e) {
      setError(aiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  const data = result?.data as VarianceData | undefined;
  return (
    <div className="ai-budget-variance">
      <label className="small">
        {t("Focus (optional)")}{" "}
        <input
          className="input"
          value={focus}
          maxLength={200}
          placeholder={t("e.g. travel costs")}
          onChange={(e) => setFocus(e.target.value)}
        />
      </label>{" "}
      <button type="button" className="btn small" disabled={busy || !feature.available} onClick={() => void run()}>
        {busy ? t("Explaining…") : t("Explain variance")}
      </button>
      {!feature.available && <AiUnavailable />}
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
      {result && data && (
        <div className="ai-explanation" aria-live="polite">
          <AiLabel text={t("AI explanation")} />{" "}
          <span className="muted small">{t("as of {date}", { date: data.as_of })}</span>
          <p>{data.explanation}</p>
          {data.removed_numbers.length > 0 && (
            <p className="muted small" role="note">
              {t("Sentences with figures that are not in the report were removed: {numbers}", {
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

registerFormPanel("account.budget", {
  get title() {
    return t("Explain variance (AI)");
  },
  render: ({ resId }) => <VarianceExplanation resId={resId} />,
});
