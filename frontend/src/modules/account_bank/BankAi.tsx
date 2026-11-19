// AI reconciliation suggestions (sub-phase 2.6) inside the bank reconciliation screen: for the selected transaction,
// the ranked candidates (amount, reference similarity, partner named or known from history, date) and one match
// proposal for the best — nothing is matched before the user accepts it. The toolbar button proposes matches for
// every transaction the rules left open.
import { useState } from "react";
import { aiErrorMessage, type Proposal } from "../../kernel/ai/api";
import { AiLabel, AiUnavailable, ProposalCard, useAiFeature } from "../../kernel/ai/components";
import { request } from "../../kernel/api/client";
import { formatMonetary } from "../../kernel/format";
import { t } from "../../kernel/i18n";

export const RECONCILE = "account_bank.reconcile_suggest";

export interface AiSuggestion {
  index: number;
  kind: "items" | "writeoff";
  line_ids: number[];
  items: { id: number; move_name: string; partner: string | null; amount_residual: number; currency: string }[];
  amount: number;
  score: number;
  reasons: string[];
  rule: string | null;
}

export interface AiSuggestResult {
  run_id: number;
  suggestions: AiSuggestion[];
  used_model: boolean;
  proposal: Proposal | null;
}

export const bankAi = {
  suggest: (lineId: number) => request<AiSuggestResult>("POST", `/account_bank/ai/lines/${lineId}/suggest`),
  suggestOpen: (journalId: number | null) =>
    request<{ proposed: number; rows: unknown[] }>("POST", "/account_bank/ai/suggest", {
      body: journalId ? { journal_id: journalId } : {},
    }),
};

function reasonLabel(code: string): string {
  const labels: Record<string, string> = {
    amount_exact: t("same amount"),
    amount_close: t("amount close (fees deducted?)"),
    reference: t("reference in the label"),
    reference_similar: t("reference almost in the label"),
    partner: t("same partner"),
    partner_named: t("partner named in the label"),
    partner_history: t("partner known from earlier payments"),
    model_choice: t("chosen by the AI among close candidates"),
  };
  return labels[code] ?? code;
}

export function AiLineSuggestions({ lineId, onDecided }: { lineId: number; onDecided: () => void }) {
  const feature = useAiFeature(RECONCILE);
  const [result, setResult] = useState<AiSuggestResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (!feature.enabled) return null;
  const ask = async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(await bankAi.suggest(lineId));
    } catch (e) {
      setError(aiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="bank-ai-suggestions">
      <h3>
        {t("AI suggestions")} {!feature.available && <AiUnavailable />}
      </h3>
      <button type="button" className="btn small" disabled={busy} onClick={() => void ask()}>
        {busy ? t("Ranking…") : t("Suggest a match")}
      </button>
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
      {result && result.suggestions.length === 0 && <p className="muted">{t("No candidate found.")}</p>}
      {result && result.suggestions.length > 0 && (
        <ol aria-label={t("AI suggestions")}>
          {result.suggestions.map((s) => (
            <li key={s.index}>
              <strong>
                {s.kind === "items" ? s.items.map((i) => i.move_name).join(", ") : (s.rule ?? t("Write-off"))}
              </strong>{" "}
              {formatMonetary(s.amount, s.items[0]?.currency ?? null)}{" "}
              <span className="muted small">
                {t("confidence {score} %", { score: Math.round(s.score * 100) })} ·{" "}
                {s.reasons.map(reasonLabel).join(", ")}
              </span>{" "}
              {s.index === 0 && <AiLabel text={t("AI suggestion")} />}
            </li>
          ))}
        </ol>
      )}
      {result?.proposal && <ProposalCard proposal={result.proposal} onDecided={onDecided} />}
    </div>
  );
}

export function AiSuggestOpenButton({
  journalId,
  onDone,
}: {
  journalId: number | null;
  onDone: (message: string, error?: boolean) => void;
}) {
  const feature = useAiFeature(RECONCILE);
  if (!feature.enabled) return null;
  const run = async () => {
    try {
      const out = await bankAi.suggestOpen(journalId);
      onDone(t("{count} match proposals to review", { count: out.proposed }));
    } catch (e) {
      onDone(aiErrorMessage(e), true);
    }
  };
  return (
    <button type="button" className="btn ghost" disabled={!feature.available} onClick={() => void run()}>
      {t("AI: propose matches")}
    </button>
  );
}
