// Anomaly flags on a bill (account.anomaly): possible duplicates, unusual amounts, unexpected vendors and
// changed bank accounts, each with its explanation. Flags never block posting; the person dismisses or
// confirms them. "Explain" asks the model for a plain-words summary (numbers checked against the flags).
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { ai, aiErrorMessage } from "../../kernel/ai/api";
import { AiLabel, AiUnavailable, Feedback, useAiFeature } from "../../kernel/ai/components";
import { registerFormPanel } from "../../kernel/extensions";
import { t } from "../../kernel/i18n";
import { accountAi, type AnomalyFlag } from "./api";

export const ANOMALY = "account.anomaly";
const MOVE = "account.move";

function kindLabel(kind: string): string {
  const labels: Record<string, string> = {
    duplicate_number: t("Same invoice number"),
    duplicate_amount: t("Same amount and date"),
    near_duplicate_text: t("Nearly identical document"),
    unusual_amount: t("Unusual amount for this vendor"),
    new_vendor_large: t("First bill of a new vendor, large amount"),
    bank_account_changed: t("Bank account changed recently"),
  };
  return labels[kind] ?? kind;
}

function stateLabel(state: AnomalyFlag["state"]): string {
  return { open: t("open"), dismissed: t("dismissed"), confirmed: t("confirmed") }[state] ?? state;
}

export function BillAnomalies({ resId }: { resId: number }) {
  const feature = useAiFeature(ANOMALY);
  const [error, setError] = useState<string | null>(null);
  const [explanation, setExplanation] = useState<{ text: string; runId: number } | null>(null);
  const flags = useQuery({
    queryKey: ["account-anomalies", resId],
    queryFn: () => accountAi.anomalies(resId),
    enabled: feature.enabled,
    retry: false,
  });
  if (!feature.enabled) return <span className="ai-panel-hidden" hidden />;
  const act = async (fn: () => Promise<unknown>) => {
    setError(null);
    try {
      await fn();
      await flags.refetch();
    } catch (e) {
      setError(aiErrorMessage(e));
    }
  };
  const explain = async () => {
    setError(null);
    try {
      const run = await ai.run(ANOMALY, { res_model: MOVE, res_id: resId });
      const data = (run.data ?? {}) as { narrative?: string };
      setExplanation({ text: data.narrative ?? run.text, runId: run.run_id });
    } catch (e) {
      setError(aiErrorMessage(e));
    }
  };
  const items = flags.data?.flags ?? [];
  const open = items.filter((f) => f.state === "open");
  return (
    <div className="bill-anomalies">
      {flags.isLoading && <p className="muted small">{t("Checking…")}</p>}
      {!flags.isLoading && !items.length && <p className="muted small">{t("No anomaly found on this document.")}</p>}
      {items.length > 0 && (
        <ul aria-label={t("Anomaly flags")}>
          {items.map((f) => (
            <li key={f.id} className={`anomaly severity-${f.severity}`}>
              <strong>{kindLabel(f.kind)}</strong> <span className="badge">{stateLabel(f.state)}</span> <AiLabel />
              <div>{f.explanation}</div>
              {f.related && <Link to={`/m/${MOVE}/${f.related.id}`}>{f.related.name}</Link>}
              {f.state === "open" && (
                <div className="ai-actions">
                  <button
                    type="button"
                    className="btn small"
                    onClick={() => void act(() => accountAi.decide(f.id, "dismiss"))}
                  >
                    {t("Not an anomaly")}
                  </button>
                  <button
                    type="button"
                    className="btn small"
                    onClick={() => void act(() => accountAi.decide(f.id, "confirm"))}
                  >
                    {t("Confirm the problem")}
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
      <div className="ai-actions">
        <button type="button" className="btn ghost small" onClick={() => void act(() => accountAi.check(resId))}>
          {t("Check again")}
        </button>
        {open.length > 0 && feature.available && (
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

registerFormPanel(MOVE, {
  get title() {
    return t("Anomaly checks");
  },
  render: ({ resId, record }) =>
    String(record.move_type).startsWith("in_") ? (
      <BillAnomalies resId={resId} />
    ) : (
      <span className="ai-panel-hidden" hidden />
    ),
});
