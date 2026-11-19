// Product AI (sub-phase 2.6) beside the product form: a sales description drafted from the catalogue facts.
// Sentences stating specifications the catalogue does not have are removed before the proposal is made;
// nothing is written until the user accepts it.
import { useState } from "react";
import { ai, aiErrorMessage, type Proposal } from "../../kernel/ai/api";
import { AiLabel, AiUnavailable, Feedback, ProposalCard, useAiFeature } from "../../kernel/ai/components";
import { registerFormPanel } from "../../kernel/extensions";
import { t } from "../../kernel/i18n";

const PRODUCT = "product.product";

interface DraftData {
  description: string;
  removed: { sentence: string; unsupported: string[] }[];
}

export function DescriptionDraft({ resId, reload }: { resId: number; reload: () => void }) {
  const feature = useAiFeature("product.description_draft");
  const [result, setResult] = useState<{ runId: number; data: DraftData; proposal: Proposal | null } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!feature.enabled) return <span className="ai-panel-hidden" hidden />;
  if (!feature.available) return <AiUnavailable />;

  const draft = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await ai.run("product.description_draft", { res_model: PRODUCT, res_id: resId });
      setResult({ runId: r.run_id, data: r.data as DraftData, proposal: r.proposals[0] ?? null });
    } catch (e) {
      setError(aiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="sale-ai">
      <button type="button" className="btn small" disabled={busy} onClick={() => void draft()}>
        {busy ? t("Writing…") : result ? t("Draft again") : t("Draft a sales description")}
      </button>
      {result && (
        <>
          {result.proposal ? (
            <ProposalCard proposal={result.proposal} onDecided={reload} />
          ) : (
            <p>
              {result.data.description} <AiLabel />
            </p>
          )}
          {result.data.removed.length > 0 && (
            <div className="muted small" role="note">
              {t("Removed because the catalogue does not state it:")}
              <ul aria-label={t("Removed sentences")}>
                {result.data.removed.map((r) => (
                  <li key={r.sentence}>
                    “{r.sentence}” ({r.unsupported.join(", ")})
                  </li>
                ))}
              </ul>
            </div>
          )}
          <Feedback runId={result.runId} />
        </>
      )}
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
    </div>
  );
}

registerFormPanel(PRODUCT, {
  get title() {
    return t("Sales description draft");
  },
  render: ({ resId, reload }) => <DescriptionDraft resId={resId} reload={reload} />,
});
