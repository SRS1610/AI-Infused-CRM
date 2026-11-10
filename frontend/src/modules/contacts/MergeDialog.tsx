import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { request } from "../../kernel/api/client";
import { registerSelectionAction } from "../../kernel/extensions";

interface Preview {
  records: Record<string, any>[];
  fields: string[];
  conflicts: string[];
}

const label = (value: any): string =>
  value === null || value === undefined || value === false
    ? "—"
    : typeof value === "object" && "display_name" in value
      ? String(value.display_name ?? "—")
      : String(value);

/** Pick the contact to keep and, for each conflicting field, which value survives. */
export function MergeDialog({ ids, onDone }: { ids: number[]; onDone: () => void }) {
  const preview = useQuery({
    queryKey: ["merge-preview", ids.join(",")],
    queryFn: () => request<Preview>("POST", "/contacts/merge/preview", { body: ids }),
  });
  const [survivor, setSurvivor] = useState<number | null>(null);
  const [choices, setChoices] = useState<Record<string, number>>({});
  const [result, setResult] = useState<{ merge_id: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const keep = survivor ?? preview.data?.records[0]?.id ?? null;

  const merge = async () => {
    if (!keep) return;
    setError(null);
    try {
      const field_choices = Object.fromEntries(Object.entries(choices).filter(([, id]) => id !== keep));
      setResult(await request("POST", "/contacts/merge", { body: { ids, survivor_id: keep, field_choices } }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };
  const undo = async () => {
    if (!result) return;
    await request("POST", `/contacts/merge/${result.merge_id}/undo`);
    onDone();
  };

  return (
    <div className="palette-backdrop" onMouseDown={onDone}>
      <div
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-label="Merge contacts"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <h2>Merge {ids.length} contacts</h2>
        {preview.isLoading && <p className="muted">Loading…</p>}
        {preview.isError && (
          <div className="alert error" role="alert">
            {(preview.error as Error).message}
          </div>
        )}
        {preview.data && !result && (
          <>
            <p className="muted">
              The kept contact receives every reference (quotes, leads, messages, activities, attachments) of the
              others, which are archived. You can undo the merge for 7 days.
            </p>
            <div className="table-scroll">
              <table className="import-mapping">
                <thead>
                  <tr>
                    <th scope="col">Field</th>
                    {preview.data.records.map((r) => (
                      <th key={r.id} scope="col">
                        <label>
                          <input
                            type="radio"
                            name="survivor"
                            checked={keep === r.id}
                            onChange={() => setSurvivor(r.id)}
                            aria-label={`Keep ${r.display_name}`}
                          />{" "}
                          Keep #{r.id}
                        </label>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {preview.data.fields.map((f) => {
                    const conflict = preview.data!.conflicts.includes(f);
                    return (
                      <tr key={f} className={conflict ? "conflict" : undefined}>
                        <th scope="row">{f.replace(/_id$/, "").replace(/_/g, " ")}</th>
                        {preview.data!.records.map((r) => (
                          <td key={r.id}>
                            {conflict ? (
                              <label>
                                <input
                                  type="radio"
                                  name={`choice-${f}`}
                                  checked={(choices[f] ?? keep) === r.id}
                                  onChange={() => setChoices((c) => ({ ...c, [f]: r.id }))}
                                  aria-label={`Use ${f} from #${r.id}`}
                                />{" "}
                                {label(r[f])}
                              </label>
                            ) : (
                              label(r[f])
                            )}
                          </td>
                        ))}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <div className="button-row">
              <button type="button" className="btn primary" onClick={() => void merge()} disabled={!keep}>
                Merge
              </button>
              <button type="button" className="btn ghost" onClick={onDone}>
                Cancel
              </button>
            </div>
          </>
        )}
        {result && (
          <div className="alert info" role="status">
            Contacts merged.{" "}
            <button type="button" className="btn small" onClick={() => void undo()}>
              Undo
            </button>{" "}
            <button type="button" className="btn small primary" onClick={onDone}>
              Close
            </button>
          </div>
        )}
        {error && (
          <div className="alert error" role="alert">
            {error}
          </div>
        )}
      </div>
    </div>
  );
}

registerSelectionAction("contacts.partner", {
  label: "Merge",
  min: 2,
  render: ({ ids, onDone }) => <MergeDialog ids={ids} onDone={onDone} />,
});
