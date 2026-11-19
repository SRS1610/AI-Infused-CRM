// Personalised payment reminders (sub-phase 2.6, account_dunning.message_draft) on a prepared reminder: the model
// drafts the text with the level's tone and the customer's history; every figure is checked against the ledger
// server-side and the invoice table stays the ledger's. The draft is a proposal: accepting it only replaces the
// reminder's text — sending stays the run's own action.
import { useState } from "react";
import { ai, aiErrorMessage, type RunResult } from "../../kernel/ai/api";
import { AiLabel, AiUnavailable, Feedback, ProposalCard, useAiFeature } from "../../kernel/ai/components";
import { registerFormPanel } from "../../kernel/extensions";
import { t } from "../../kernel/i18n";

export const DUNNING_DRAFT = "account_dunning.message_draft";

interface DraftData {
  subject: string;
  body: string;
  removed_numbers: string[];
  tone: "friendly" | "firm" | "formal";
}

function toneLabel(tone: DraftData["tone"]): string {
  return { friendly: t("friendly"), firm: t("firm"), formal: t("formal") }[tone] ?? tone;
}

export function DunningDraft({ resId, reload }: { resId: number; reload: () => void }) {
  const feature = useAiFeature(DUNNING_DRAFT);
  const [result, setResult] = useState<RunResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (!feature.enabled) return <span className="ai-panel-hidden" hidden />;
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(await ai.run(DUNNING_DRAFT, { res_model: "account.dunning_run_line", res_id: resId }));
    } catch (e) {
      setError(aiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  const data = result?.data as DraftData | undefined;
  return (
    <div className="ai-dunning-draft">
      <p className="muted small">
        {t("The amounts and the invoice table always come from the ledger; nothing is sent until you send the run.")}
      </p>
      <button type="button" className="btn small" disabled={busy || !feature.available} onClick={() => void run()}>
        {busy ? t("Drafting…") : result ? t("Draft again") : t("Draft personalised message")}
      </button>
      {!feature.available && <AiUnavailable />}
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
      {result && data && (
        <div className="ai-explanation" aria-live="polite">
          <AiLabel /> <span className="muted small">{t("Tone: {tone}", { tone: toneLabel(data.tone) })}</span>
          <p>
            <strong>{data.subject}</strong>
          </p>
          <p className="pre-wrap">{data.body}</p>
          {data.removed_numbers.length > 0 && (
            <p className="muted small" role="note">
              {t("Sentences with figures that are not in the ledger were removed: {numbers}", {
                numbers: data.removed_numbers.join(", "),
              })}
            </p>
          )}
          {result.proposals.map((p) => (
            <ProposalCard key={p.id} proposal={p} onDecided={() => reload()} />
          ))}
          <Feedback runId={result.run_id} />
        </div>
      )}
    </div>
  );
}

registerFormPanel("account.dunning_run_line", {
  get title() {
    return t("Personalised message (AI)");
  },
  render: ({ resId, record, reload }) =>
    record.state === "draft" || record.state === "no_email" ? (
      <DunningDraft resId={resId} reload={reload} />
    ) : (
      <span className="ai-panel-hidden" hidden />
    ),
});
