// Contacts AI (Phase 1.6) beside the contact form: possible duplicates (merge as a proposal),
// suggested tags and parent company, and enrichment suggestions that keep their sources. Every
// change is a proposal the user accepts, edits or rejects.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { request } from "../../kernel/api/client";
import { ai, aiErrorMessage, type Proposal } from "../../kernel/ai/api";
import { AiLabel, AiUnavailable, Feedback, ProposalCard, useAiFeature } from "../../kernel/ai/components";
import { registerFormPanel } from "../../kernel/extensions";

const PARTNER = "contacts.partner";

/** Lets the form panel disappear when the feature is switched off (see the contacts block in styles.css). */
const Hidden = () => <span className="ai-panel-hidden" hidden />;

const ErrorNote = ({ message }: { message: string | null }) =>
  message ? (
    <div className="alert error" role="alert">
      {message}
    </div>
  ) : null;

// ------------------------------------------------------------------------------------------ duplicates

interface Candidate {
  id: number;
  display_name: string;
  email: string | null;
  phone: string | null;
  city: string | null;
  score: number;
  duplicate: boolean;
  reasons: string[];
}

export function PossibleDuplicates({ resId, reload }: { resId: number; reload: () => void }) {
  const feature = useAiFeature("contacts.duplicates");
  const [picked, setPicked] = useState<number[]>([]);
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [error, setError] = useState<string | null>(null);
  const found = useQuery({
    queryKey: ["contacts-duplicates", resId],
    queryFn: () => request<{ candidates: Candidate[]; embedding: string }>("GET", `/contacts/${resId}/ai/duplicates`),
    enabled: feature.enabled,
    retry: false,
  });
  if (!feature.enabled) return <Hidden />;
  if (found.isLoading) return <p className="muted small">Looking for duplicates…</p>;
  if (found.isError) return <ErrorNote message={aiErrorMessage(found.error)} />;
  const candidates = found.data?.candidates ?? [];
  if (!candidates.length) return <p className="muted small">No likely duplicates.</p>;

  const toggle = (id: number) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));
  const propose = async () => {
    setError(null);
    try {
      setProposal(
        await request<Proposal>("POST", `/contacts/${resId}/ai/duplicates/propose-merge`, { body: { ids: picked } }),
      );
    } catch (e) {
      setError(aiErrorMessage(e));
    }
  };

  return (
    <div className="contacts-ai">
      <ul className="contacts-ai-list" aria-label="Possible duplicates">
        {candidates.map((c) => (
          <li key={c.id}>
            <label>
              <input
                type="checkbox"
                checked={picked.includes(c.id)}
                onChange={() => toggle(c.id)}
                aria-label={`Select ${c.display_name}`}
                disabled={!!proposal}
              />{" "}
              <Link to={`/m/${PARTNER}/${c.id}`}>{c.display_name}</Link>{" "}
              <span className={c.duplicate ? "badge" : "badge muted"}>
                {c.duplicate ? "likely" : "possible"} · {Math.round(c.score * 100)}%
              </span>
            </label>
            <div className="muted small">{c.reasons.join(" · ") || "Weak match"}</div>
          </li>
        ))}
      </ul>
      {found.data?.embedding === "unavailable" && <AiUnavailable />}
      {!proposal && (
        <button type="button" className="btn small" disabled={!picked.length} onClick={() => void propose()}>
          Propose merge
        </button>
      )}
      {proposal && (
        <ProposalCard
          proposal={proposal}
          onDecided={() => {
            void found.refetch();
            reload();
          }}
        />
      )}
      <ErrorNote message={error} />
    </div>
  );
}

// ------------------------------------------------------------------------------------------ tags & parent

interface TagData {
  suggested: { tag_id: number; name: string; confidence: number; reason: string }[];
  parent: { id: number; display_name: string; reason: string } | null;
}

export function SuggestedTags({ resId, reload }: { resId: number; reload: () => void }) {
  const feature = useAiFeature("contacts.tag_suggest");
  const [result, setResult] = useState<{ runId: number; data: TagData; proposal: Proposal | null } | null>(null);
  const [decided, setDecided] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!feature.enabled) return <Hidden />;
  if (!feature.available) return <AiUnavailable />;

  const suggest = async () => {
    setBusy(true);
    setError(null);
    setDecided(false);
    try {
      const r = await ai.run("contacts.tag_suggest", { res_model: PARTNER, res_id: resId });
      setResult({ runId: r.run_id, data: r.data as TagData, proposal: r.proposals[0] ?? null });
    } catch (e) {
      setError(aiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="contacts-ai">
      <button type="button" className="btn small" disabled={busy} onClick={() => void suggest()}>
        {busy ? "Thinking…" : result ? "Suggest again" : "Suggest tags"}
      </button>
      {result && (
        <>
          {!decided && <AiLabel text="AI suggestion" />}
          {result.data.suggested.length === 0 && !result.data.parent && (
            <p className="muted small">No tag fits better than the ones already set.</p>
          )}
          <ul className="contacts-ai-list" aria-label="Suggested tags">
            {result.data.suggested.map((t) => (
              <li key={t.tag_id}>
                <strong>{t.name}</strong> <span className="muted small">{Math.round(t.confidence * 100)}%</span>
                <div className="muted small">{t.reason}</div>
              </li>
            ))}
          </ul>
          {result.data.parent && (
            <p className="small">
              Works for <strong>{result.data.parent.display_name}</strong>{" "}
              <span className="muted">({result.data.parent.reason})</span>
            </p>
          )}
          {result.proposal && (
            <ProposalCard
              proposal={result.proposal}
              onDecided={() => {
                setDecided(true);
                reload();
              }}
            />
          )}
          <Feedback runId={result.runId} />
        </>
      )}
      <ErrorNote message={error} />
    </div>
  );
}

// ------------------------------------------------------------------------------------------ enrichment

interface Suggestion {
  field: string;
  value: string;
  current: string | null;
  source: string;
  source_title: string;
  source_url: string;
  entity: string;
}

function EnrichmentProposal({ proposal, reload }: { proposal: Proposal; reload: () => void }) {
  const run = useQuery({
    queryKey: ["ai-run", proposal.run_id],
    queryFn: () =>
      request<{ output_data: { suggestions?: Suggestion[] } | null }>("GET", `/ai/runs/${proposal.run_id}`),
    enabled: proposal.run_id !== null,
  });
  const suggestions = run.data?.output_data?.suggestions ?? [];
  return (
    <div className="contacts-ai-enrich">
      {suggestions.length > 0 && (
        <table className="ai-diff" aria-label="Suggested values and their sources">
          <thead>
            <tr>
              <th scope="col">Field</th>
              <th scope="col">Suggested</th>
              <th scope="col">Source</th>
            </tr>
          </thead>
          <tbody>
            {suggestions.map((s) => (
              <tr key={s.field}>
                <th scope="row">{s.field}</th>
                <td>{s.value}</td>
                <td>
                  <a href={s.source_url} target="_blank" rel="noreferrer noopener">
                    {s.source_title}
                  </a>
                  <div className="muted small">{s.entity}</div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <ProposalCard proposal={proposal} onDecided={reload} />
      {proposal.run_id !== null && <Feedback runId={proposal.run_id} />}
    </div>
  );
}

export function EnrichmentSuggestions({ resId, reload }: { resId: number; reload: () => void }) {
  const feature = useAiFeature("contacts.enrich");
  const qc = useQueryClient();
  const [queued, setQueued] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending = useQuery({
    queryKey: ["ai", "proposals", PARTNER, resId, "enrich"],
    queryFn: () => ai.proposals({ res_model: PARTNER, res_id: resId }),
    enabled: feature.enabled && feature.available,
    select: (rows) => rows.filter((p) => p.feature === "contacts.enrich"),
  });
  if (!feature.enabled) return <Hidden />;
  if (!feature.available) return <AiUnavailable />;

  const start = async () => {
    setError(null);
    try {
      await request("POST", `/contacts/${resId}/ai/enrich`);
      setQueued(true);
    } catch (e) {
      setError(aiErrorMessage(e));
    }
  };
  const proposals = pending.data ?? [];

  return (
    <div className="contacts-ai">
      {proposals.map((p) => (
        <EnrichmentProposal
          key={p.id}
          proposal={p}
          reload={() => {
            void qc.invalidateQueries({ queryKey: ["ai", "proposals"] });
            reload();
          }}
        />
      ))}
      {queued ? (
        <p className="muted small" role="status">
          Looking this contact up in the approved sources. You will get a notification when suggestions are ready.{" "}
          <button type="button" className="btn ghost small" onClick={() => void pending.refetch()}>
            Check now
          </button>
        </p>
      ) : (
        <button type="button" className="btn small" onClick={() => void start()}>
          Find missing details
        </button>
      )}
      <ErrorNote message={error} />
    </div>
  );
}

registerFormPanel(PARTNER, {
  title: "Possible duplicates",
  render: ({ resId, reload }) => <PossibleDuplicates resId={resId} reload={reload} />,
});
registerFormPanel(PARTNER, {
  title: "Suggested tags",
  render: ({ resId, reload }) => <SuggestedTags resId={resId} reload={reload} />,
});
registerFormPanel(PARTNER, {
  title: "Enrichment suggestions",
  render: ({ resId, reload }) => <EnrichmentSuggestions resId={resId} reload={reload} />,
});
