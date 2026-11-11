// "Ask" box of the financial reports (sub-phase 2.6, account_reports.nl_report): a question becomes report
// parameters (report, period, comparison, companies, partners, journals) that the user reviews and applies to the
// filters. The model never produces a figure: the numbers come from the report engine when the report runs.
import { useState } from "react";
import { ai, aiErrorMessage } from "../../kernel/ai/api";
import { AiLabel, AiUnavailable, Feedback, useAiFeature } from "../../kernel/ai/components";
import { t } from "../../kernel/i18n";
import type { ReportFilters } from "./api";

export const NL_REPORT = "account_reports.nl_report";

export interface AskedParams {
  report: string;
  date_from: string | null;
  date_to: string | null;
  comparison: "none" | "previous_period" | "previous_year";
  periods: number;
  partner_ids: number[];
  journal_ids: number[];
  company_ids: number[];
  include_draft: boolean;
  account_type: "receivable" | "payable" | "all";
}

export interface AskResult {
  params: AskedParams | null;
  report_name?: string;
  company_names?: string[];
  partner_names?: string[];
  journal_names?: string[];
  warnings: string[];
}

export interface ScopeLabels {
  partners: string[];
  journals: string[];
}

export function filtersOf(p: AskedParams): ReportFilters {
  return {
    date_from: p.date_from ?? undefined,
    date_to: p.date_to ?? undefined,
    comparison: p.comparison,
    periods: p.periods,
    include_draft: p.include_draft,
    account_type: p.account_type,
    company_ids: p.company_ids,
    partner_ids: p.partner_ids,
    journal_ids: p.journal_ids,
  };
}

function comparisonLabel(c: AskedParams["comparison"], periods: number): string {
  if (c === "previous_period") return t("compared with the {count} previous periods", { count: periods });
  if (c === "previous_year") return t("compared with the previous year");
  return "";
}

export function ReportAsk({
  onApply,
}: {
  onApply: (code: string, filters: ReportFilters, labels: ScopeLabels) => void;
}) {
  const feature = useAiFeature(NL_REPORT);
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<{ data: AskResult; runId: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (!feature.enabled) return null;
  const ask = async () => {
    if (!question.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const run = await ai.run(NL_REPORT, { inputs: { question } });
      setResult({ data: run.data as AskResult, runId: run.run_id });
    } catch (e) {
      setError(aiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  const p = result?.data.params;
  return (
    <div className="report-ask control-panel">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void ask();
        }}
      >
        <label>
          {t("Ask")}{" "}
          <input
            className="input"
            type="search"
            value={question}
            placeholder={t("e.g. P&L last quarter compared with last year")}
            aria-label={t("Question about the accounts")}
            onChange={(e) => setQuestion(e.target.value)}
          />
        </label>{" "}
        <button type="submit" className="btn small" disabled={busy || !feature.available || !question.trim()}>
          {busy ? t("Reading the question…") : t("Fill the filters")}
        </button>
        {!feature.available && <AiUnavailable />}
      </form>
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
      {result && (
        <div className="report-ask-result" role="status">
          {p ? (
            <>
              <AiLabel text={t("AI reading of the question")} /> <strong>{result.data.report_name}</strong>
              {": "}
              {p.date_from ? `${p.date_from} → ${p.date_to}` : t("as of {date}", { date: p.date_to ?? "" })}{" "}
              {comparisonLabel(p.comparison, p.periods)}
              {(result.data.company_names ?? []).length > 0 && ` · ${result.data.company_names!.join(", ")}`}
              {(result.data.partner_names ?? []).length > 0 && ` · ${result.data.partner_names!.join(", ")}`}
              {(result.data.journal_names ?? []).length > 0 && ` · ${result.data.journal_names!.join(", ")}`}{" "}
              <button
                type="button"
                className="btn primary small"
                onClick={() => {
                  onApply(p.report, filtersOf(p), {
                    partners: result.data.partner_names ?? [],
                    journals: result.data.journal_names ?? [],
                  });
                  setResult(null);
                }}
              >
                {t("Apply these filters")}
              </button>{" "}
              <button type="button" className="btn ghost small" onClick={() => setResult(null)}>
                {t("Dismiss")}
              </button>
            </>
          ) : (
            <span>{t("The question could not be turned into report filters.")}</span>
          )}
          {result.data.warnings.length > 0 && (
            <ul className="muted small">
              {result.data.warnings.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          )}
          <Feedback runId={result.runId} />
        </div>
      )}
    </div>
  );
}
