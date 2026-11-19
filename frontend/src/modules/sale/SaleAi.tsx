// Sales AI (sub-phase 2.6) beside the quotation form: products to add (upsell), discounts inside the
// approved bands, lines from a pasted request and pricing-policy flags. Every change is a proposal the user
// accepts, edits or rejects; panels disappear when their feature is switched off.
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { request } from "../../kernel/api/client";
import { ai, aiErrorMessage, type Proposal } from "../../kernel/ai/api";
import { AiLabel, AiUnavailable, Feedback, ProposalCard, useAiFeature } from "../../kernel/ai/components";
import { registerFormPanel } from "../../kernel/extensions";
import { t } from "../../kernel/i18n";

const QUOTATION = "sale.quotation";
const Hidden = () => <span className="ai-panel-hidden" hidden />;
const ErrorNote = ({ message }: { message: string | null }) =>
  message ? (
    <div className="alert error" role="alert">
      {message}
    </div>
  ) : null;

// ------------------------------------------------------------------------------------------ upsell

interface Suggestion {
  product_id: number;
  display_name: string;
  score: number;
  price_unit: number | null;
  currency: string;
  source: "history" | "catalogue" | "popular";
  reasons: string[];
}

export function UpsellSuggestions({ resId, reload }: { resId: number; reload: () => void }) {
  const feature = useAiFeature("sale.upsell");
  const [proposals, setProposals] = useState<Record<number, Proposal>>({});
  const [error, setError] = useState<string | null>(null);
  const found = useQuery({
    queryKey: ["sale-upsell", resId],
    queryFn: () =>
      request<{ suggestions: Suggestion[]; run_id: number; orders_learnt: number }>(
        "GET",
        `/sale/ai/quotations/${resId}/upsell`,
      ),
    enabled: feature.enabled,
    retry: false,
  });
  if (!feature.enabled) return <Hidden />;
  if (found.isLoading) return <p className="muted small">{t("Looking at past orders…")}</p>;
  if (found.isError) return <ErrorNote message={aiErrorMessage(found.error)} />;
  const rows = found.data?.suggestions ?? [];
  if (!rows.length) return <p className="muted small">{t("No product to suggest yet.")}</p>;

  const propose = async (s: Suggestion) => {
    setError(null);
    try {
      const p = await request<Proposal>("POST", `/sale/ai/quotations/${resId}/upsell/propose`, {
        body: { product_id: s.product_id, quantity: 1, run_id: found.data?.run_id },
      });
      setProposals((all) => ({ ...all, [s.product_id]: p }));
    } catch (e) {
      setError(aiErrorMessage(e));
    }
  };

  return (
    <div className="sale-ai">
      <AiLabel text={t("AI suggestion")} />
      <ul className="sale-ai-list" aria-label={t("Suggested products")}>
        {rows.map((s) => (
          <li key={s.product_id}>
            <strong>{s.display_name}</strong>
            {s.price_unit !== null && (
              <span className="muted small">
                {" "}
                · {s.price_unit.toFixed(2)} {s.currency}
              </span>
            )}
            <div className="muted small">{s.reasons.join(" · ")}</div>
            {proposals[s.product_id] ? (
              <ProposalCard
                proposal={proposals[s.product_id]}
                onDecided={() => {
                  reload();
                  void found.refetch();
                }}
              />
            ) : (
              <button
                type="button"
                className="btn small"
                onClick={() => void propose(s)}
                aria-label={t("Add {product}", { product: s.display_name })}
              >
                {t("Add")}
              </button>
            )}
          </li>
        ))}
      </ul>
      {found.data?.run_id && <Feedback runId={found.data.run_id} />}
      <ErrorNote message={error} />
    </div>
  );
}

// ------------------------------------------------------------------------------------------ discounts

interface DiscountLine {
  line_id: number;
  product: string;
  quantity: number;
  current_discount: number;
  status: "suggested" | "unchanged" | "no_band";
  suggested_discount?: number;
  band?: { id: number; name: string; min: number; max: number };
  reasons: string[];
  proposal?: Proposal;
}

export function DiscountSuggestions({ resId, reload }: { resId: number; reload: () => void }) {
  const feature = useAiFeature("sale.discount_suggest");
  const [result, setResult] = useState<{ lines: DiscountLine[]; run_id: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!feature.enabled) return <Hidden />;

  const suggest = async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(
        await request<{ lines: DiscountLine[]; run_id: number }>("POST", `/sale/ai/quotations/${resId}/discounts`),
      );
    } catch (e) {
      setError(aiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="sale-ai">
      <button type="button" className="btn small" disabled={busy} onClick={() => void suggest()}>
        {busy ? t("Thinking…") : result ? t("Suggest again") : t("Suggest discounts")}
      </button>
      {result && (
        <>
          <AiLabel text={t("AI suggestion")} />
          <ul className="sale-ai-list" aria-label={t("Discount suggestions")}>
            {result.lines.map((line) => (
              <li key={line.line_id}>
                <strong>{line.product}</strong>{" "}
                {line.status === "no_band" ? (
                  <span className="muted small">{t("No approved band")}</span>
                ) : (
                  <span className="badge">
                    {t("{discount} % (band {min}–{max} %)", {
                      discount: line.suggested_discount ?? 0,
                      min: line.band?.min ?? 0,
                      max: line.band?.max ?? 0,
                    })}
                  </span>
                )}
                <div className="muted small">{line.reasons.join(" · ")}</div>
                {line.proposal && <ProposalCard proposal={line.proposal} onDecided={reload} />}
              </li>
            ))}
          </ul>
          <Feedback runId={result.run_id} />
        </>
      )}
      <ErrorNote message={error} />
    </div>
  );
}

// ------------------------------------------------------------------------------------------ lines from text

interface MappedLine {
  index: number;
  description: string;
  product_id: number;
  display_name: string;
  quantity: number;
  method: string;
  notes: string[];
}

interface Flagged {
  index: number;
  description: string;
  reason: "no_match" | "ambiguous";
  candidates: { product_id: number; display_name: string; score: number }[];
}

export function LinesFromText({ resId, reload }: { resId: number; reload: () => void }) {
  const feature = useAiFeature("sale.quote_from_text");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{
    runId: number;
    lines: MappedLine[];
    flagged: Flagged[];
    proposal: Proposal | null;
  } | null>(null);
  if (!feature.enabled) return <Hidden />;
  if (!feature.available) return <AiUnavailable />;

  const read = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await ai.run("sale.quote_from_text", { res_model: QUOTATION, res_id: resId, inputs: { text } });
      const data = (r.data ?? {}) as { lines?: MappedLine[]; flagged?: Flagged[] };
      setResult({
        runId: r.run_id,
        lines: data.lines ?? [],
        flagged: data.flagged ?? [],
        proposal: r.proposals[0] ?? null,
      });
    } catch (e) {
      setError(aiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="sale-ai">
      <label className="field">
        <span>{t("Customer request (e-mail, RFP, list)")}</span>
        <textarea rows={5} value={text} onChange={(e) => setText(e.target.value)} />
      </label>
      <button type="button" className="btn small" disabled={busy || !text.trim()} onClick={() => void read()}>
        {busy ? t("Reading…") : t("Find the products")}
      </button>
      {result && (
        <>
          <AiLabel />
          {result.lines.length > 0 && (
            <ul className="sale-ai-list" aria-label={t("Matched lines")}>
              {result.lines.map((line) => (
                <li key={line.index}>
                  {line.quantity} × <strong>{line.display_name}</strong>{" "}
                  <span className="muted small">“{line.description}”</span>
                  {line.notes.map((n) => (
                    <div key={n} className="muted small">
                      {n}
                    </div>
                  ))}
                </li>
              ))}
            </ul>
          )}
          {result.flagged.length > 0 && (
            <ul className="sale-ai-list flagged" aria-label={t("Not matched")}>
              {result.flagged.map((f) => (
                <li key={f.index}>
                  <span className="badge warning">
                    {f.reason === "ambiguous" ? t("Several products fit") : t("Not in the catalogue")}
                  </span>{" "}
                  “{f.description}”
                  {f.reason === "ambiguous" && (
                    <div className="muted small">{f.candidates.map((c) => c.display_name).join(" · ")}</div>
                  )}
                </li>
              ))}
            </ul>
          )}
          {result.proposal && <ProposalCard proposal={result.proposal} onDecided={reload} />}
          {!result.proposal && !result.lines.length && (
            <p className="muted small">{t("No catalogue product was recognised.")}</p>
          )}
          <Feedback runId={result.runId} />
        </>
      )}
      <ErrorNote message={error} />
    </div>
  );
}

// ------------------------------------------------------------------------------------------ pricing flags

interface Finding {
  line_id: number;
  product: string;
  code: string;
  severity: "warning" | "breach";
  message: string;
}

interface StoredFlag {
  id: number;
  product: string | null;
  code: string;
  severity: "warning" | "breach";
  message: string;
  state: "open" | "approved" | "resolved";
  approved_by: string | null;
}

export function PricingFlags({ resId }: { resId: number }) {
  const feature = useAiFeature("sale.pricing_breach");
  const [error, setError] = useState<string | null>(null);
  const found = useQuery({
    queryKey: ["sale-pricing", resId],
    queryFn: () => request<{ findings: Finding[]; flags: StoredFlag[] }>("GET", `/sale/ai/quotations/${resId}/pricing`),
    enabled: feature.enabled,
    retry: false,
  });
  if (!feature.enabled) return <Hidden />;
  if (found.isLoading) return <p className="muted small">{t("Checking prices…")}</p>;
  if (found.isError) return <ErrorNote message={aiErrorMessage(found.error)} />;
  const findings = found.data?.findings ?? [];
  const flags = found.data?.flags ?? [];

  const act = async (path: string) => {
    setError(null);
    try {
      await request("POST", path);
      await found.refetch();
    } catch (e) {
      setError(aiErrorMessage(e));
    }
  };

  return (
    <div className="sale-ai">
      {findings.length === 0 ? (
        <p className="muted small" role="status">
          {t("Prices follow the policy.")}
        </p>
      ) : (
        <ul className="sale-ai-list" aria-label={t("Pricing findings")}>
          {findings.map((f) => (
            <li key={`${f.line_id}-${f.code}`}>
              <span className={f.severity === "breach" ? "badge danger" : "badge warning"}>
                {f.severity === "breach" ? t("Breach") : t("Warning")}
              </span>{" "}
              <strong>{f.product}</strong>: {f.message}
            </li>
          ))}
        </ul>
      )}
      {flags.length > 0 && (
        <ul className="sale-ai-list" aria-label={t("Recorded flags")}>
          {flags.map((f) => (
            <li key={f.id}>
              {f.product}: {f.message}{" "}
              {f.state === "approved" ? (
                <span className="badge">{t("Approved by {name}", { name: f.approved_by ?? "" })}</span>
              ) : (
                <button
                  type="button"
                  className="btn ghost small"
                  onClick={() => void act(`/sale/ai/pricing-flags/${f.id}/approve`)}
                >
                  {t("Approve")}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {findings.length > 0 && (
        <button
          type="button"
          className="btn small"
          onClick={() => void act(`/sale/ai/quotations/${resId}/pricing/check`)}
        >
          {t("Record and notify managers")}
        </button>
      )}
      <ErrorNote message={error} />
    </div>
  );
}

registerFormPanel(QUOTATION, {
  get title() {
    return t("Pricing check");
  },
  render: ({ resId }) => <PricingFlags resId={resId} />,
});
registerFormPanel(QUOTATION, {
  get title() {
    return t("Suggested products");
  },
  render: ({ resId, reload }) => <UpsellSuggestions resId={resId} reload={reload} />,
});
registerFormPanel(QUOTATION, {
  get title() {
    return t("Discount suggestions");
  },
  render: ({ resId, reload }) => <DiscountSuggestions resId={resId} reload={reload} />,
});
registerFormPanel(QUOTATION, {
  get title() {
    return t("Lines from a request");
  },
  render: ({ resId, reload }) => <LinesFromText resId={resId} reload={reload} />,
});
