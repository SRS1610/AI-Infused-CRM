// Shared AI UI: disclosure label, availability hook, feedback, citations and the proposal review card.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { ai, aiErrorMessage, type Citation, type Proposal } from "./api";
import { t } from "../i18n";

export function useAiStatus() {
  return useQuery({ queryKey: ["ai", "status"], queryFn: ai.status, staleTime: 30_000, retry: false });
}

/** ``{ enabled, available }`` for one feature: hidden when disabled, "unavailable" when the gateway is down. */
export function useAiFeature(name: string | undefined) {
  const status = useAiStatus();
  const info = name ? status.data?.features[name] : undefined;
  return { enabled: !!info, available: !!status.data?.available, info, loading: status.isLoading };
}

export function AiLabel({ text = t("AI-drafted") }: { text?: string }) {
  return (
    <span
      className="ai-label"
      role="note"
      aria-label={t("{label}: review before using", { label: text })}
      title={t("Written by AI — review it")}
    >
      ✦ {text}
    </span>
  );
}

export function AiUnavailable() {
  return (
    <span className="ai-unavailable muted small" role="status">
      {t("AI unavailable")}
    </span>
  );
}

export function Feedback({ runId }: { runId: number }) {
  const [sent, setSent] = useState<"up" | "down" | null>(null);
  const send = async (rating: "up" | "down") => {
    setSent(rating);
    try {
      await ai.feedback(runId, { rating });
    } catch {
      setSent(null);
    }
  };
  return (
    <span className="ai-feedback" aria-label={t("Was this helpful?")}>
      <button
        type="button"
        className="btn ghost small"
        aria-pressed={sent === "up"}
        onClick={() => send("up")}
        title={t("Helpful")}
      >
        👍
      </button>
      <button
        type="button"
        className="btn ghost small"
        aria-pressed={sent === "down"}
        onClick={() => send("down")}
        title={t("Not helpful")}
      >
        👎
      </button>
    </span>
  );
}

export function Citations({ items }: { items: Citation[] }) {
  if (!items.length) return null;
  return (
    <ul className="ai-citations" aria-label={t("Sources")}>
      {items.map((c) => (
        <li key={c.id}>
          <Link to={`/m/${c.res_model}/${c.res_id}`}>{c.title || c.id}</Link>
        </li>
      ))}
    </ul>
  );
}

export function proposalStateLabel(state: Proposal["state"]): string {
  const labels: Record<Proposal["state"], string> = {
    pending: t("pending"),
    accepted: t("accepted"),
    edited: t("edited"),
    rejected: t("rejected"),
    expired: t("expired"),
    failed: t("failed"),
  };
  return labels[state] ?? state;
}

const show = (v: unknown) =>
  v === null || v === undefined || v === false ? "—" : typeof v === "object" ? JSON.stringify(v) : String(v);

/** Review an AI proposal: diff (write), editable message (message), or summary (create/transition/action),
 * with Accept / Edit then accept / Reject. Nothing is applied until the user decides. */
export function ProposalCard({ proposal, onDecided }: { proposal: Proposal; onDecided?: (p: Proposal) => void }) {
  const qc = useQueryClient();
  const [current, setCurrent] = useState(proposal);
  const [draft, setDraft] = useState<Record<string, unknown>>(proposal.payload);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const edited = JSON.stringify(draft) !== JSON.stringify(proposal.payload);
  const decided = current.state !== "pending";

  const finish = (p: Proposal) => {
    setCurrent(p);
    onDecided?.(p);
    void qc.invalidateQueries({ queryKey: ["ai", "proposals"] });
    void qc.invalidateQueries({ queryKey: ["record", proposal.res_model] });
    void qc.invalidateQueries({ queryKey: ["chatter", proposal.res_model, proposal.res_id] });
  };
  const accept = async () => {
    setBusy(true);
    setError(null);
    try {
      finish(await ai.accept(proposal.id, edited ? draft : undefined));
    } catch (e) {
      setError(aiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  const reject = async () => {
    setBusy(true);
    try {
      finish(await ai.reject(proposal.id));
    } catch (e) {
      setError(aiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className={`ai-proposal state-${current.state}`} aria-label={current.title ?? t("AI proposal")}>
      <header>
        <strong>{current.title ?? t("Proposed change")}</strong> {current.ai_generated && !edited && <AiLabel />}
        {decided && <span className="badge">{proposalStateLabel(current.state)}</span>}
      </header>
      {proposal.kind === "message" ? (
        <label className="field">
          <span className="sr-only">{t("Message")}</span>
          <textarea
            rows={6}
            value={String(draft.body ?? "")}
            disabled={decided}
            onChange={(e) => setDraft({ ...draft, body: e.target.value })}
          />
        </label>
      ) : proposal.kind === "write" ? (
        <table className="ai-diff">
          <thead>
            <tr>
              <th scope="col">{t("Field")}</th>
              <th scope="col">{t("Now")}</th>
              <th scope="col">{t("Proposed")}</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(draft).map(([k, v]) => (
              <tr key={k}>
                <th scope="row">{k}</th>
                <td>{show(proposal.original?.[k])}</td>
                <td>
                  <input
                    aria-label={t("Proposed {field}", { field: k })}
                    value={typeof v === "object" ? JSON.stringify(v) : String(v ?? "")}
                    disabled={decided}
                    onChange={(e) => {
                      const raw = e.target.value;
                      const num = Number(raw);
                      setDraft({
                        ...draft,
                        [k]: typeof proposal.payload[k] === "number" && raw !== "" && !Number.isNaN(num) ? num : raw,
                      });
                    }}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <pre className="ai-payload" tabIndex={0}>
          {JSON.stringify(proposal.payload, null, 2)}
        </pre>
      )}
      {current.state === "failed" && current.reason && <div className="alert error">{current.reason}</div>}
      {error && <div className="alert error">{error}</div>}
      {!decided && (
        <div className="ai-actions">
          <button type="button" className="btn primary small" disabled={busy} onClick={accept}>
            {edited ? t("Accept edited") : proposal.kind === "message" ? t("Send") : t("Accept")}
          </button>
          <button type="button" className="btn small" disabled={busy} onClick={reject}>
            {t("Reject")}
          </button>
        </div>
      )}
    </section>
  );
}
