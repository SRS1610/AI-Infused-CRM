// E-invoice error explanations (sub-phase 2.6) on the e-invoice form: for each validation rule the document failed,
// the field to fix (decided by Tessera, with a link to the customer, company, invoice, line or tax) and the model's
// plain-language explanation and fix hint. Nothing is changed.
import { useState } from "react";
import { Link } from "react-router-dom";
import { aiErrorMessage } from "../../kernel/ai/api";
import { AiLabel, AiUnavailable, Feedback, useAiFeature } from "../../kernel/ai/components";
import { request } from "../../kernel/api/client";
import { registerFormPanel } from "../../kernel/extensions";
import { t } from "../../kernel/i18n";

export const ERROR_EXPLAIN = "account_einvoice.error_explain";

export interface ExplainedFinding {
  rule_id: string;
  flag: string;
  count: number;
  label: string;
  message: string;
  explanation: string;
  fix: string;
  explained_by: "model" | "catalogue";
  link: { model: string; res_id: number; field: string; move_id?: number } | null;
}

export const einvoiceAi = {
  explain: (docId: number) =>
    request<{ run_id: number; items: ExplainedFinding[]; removed_numbers: string[] }>(
      "POST",
      `/account_einvoice/ai/documents/${docId}/explain`,
    ),
};

function linkTo(link: NonNullable<ExplainedFinding["link"]>): string {
  // invoice lines are edited on their invoice
  return link.model === "account.move_line" && link.move_id
    ? `/m/account.move/${link.move_id}`
    : `/m/${link.model}/${link.res_id}`;
}

export function EinvoiceErrors({ resId }: { resId: number }) {
  const feature = useAiFeature(ERROR_EXPLAIN);
  const [result, setResult] = useState<{ runId: number; items: ExplainedFinding[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (!feature.enabled) return <span className="ai-panel-hidden" hidden />;
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      const out = await einvoiceAi.explain(resId);
      setResult({ runId: out.run_id, items: out.items });
    } catch (e) {
      setError(aiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="ai-einvoice-errors">
      <button type="button" className="btn small" disabled={busy} onClick={() => void run()}>
        {busy ? t("Explaining…") : t("Explain the errors")}
      </button>
      {!feature.available && <AiUnavailable />}
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
      {result && result.items.length === 0 && <p className="muted small">{t("This e-invoice has no error.")}</p>}
      {result && result.items.length > 0 && (
        <>
          <ul aria-label={t("Validation errors explained")}>
            {result.items.map((i) => (
              <li
                key={`${i.rule_id}-${i.label}`}
                className={`anomaly severity-${i.flag === "fatal" ? "error" : "warning"}`}
              >
                <strong>{i.label}</strong> <code>{i.rule_id}</code>
                {i.count > 1 && <span className="muted small"> ×{i.count}</span>}{" "}
                {i.explained_by === "model" && <AiLabel text={t("AI explanation")} />}
                <div>{i.explanation}</div>
                <div>
                  <strong>{t("Fix:")}</strong> {i.fix}{" "}
                  {i.link && <Link to={linkTo(i.link)}>{t("Open the record to fix")}</Link>}
                </div>
              </li>
            ))}
          </ul>
          <Feedback runId={result.runId} />
        </>
      )}
    </div>
  );
}

registerFormPanel("account.edi_document", {
  get title() {
    return t("Explain e-invoice errors (AI)");
  },
  render: ({ resId, record }) =>
    record.state === "error" || record.state === "rejected" ? (
      <EinvoiceErrors resId={resId} />
    ) : (
      <span className="ai-panel-hidden" hidden />
    ),
});
