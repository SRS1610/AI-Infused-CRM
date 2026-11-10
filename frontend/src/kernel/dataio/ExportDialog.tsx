import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { dataio } from "../api/platform";
import type { Domain } from "../api/types";

/** Pick fields (including one level through relations) and download CSV or XLSX. */
export function ExportDialog({
  model,
  domain,
  ids,
  initial,
  onClose,
}: {
  model: string;
  domain: Domain;
  ids?: number[];
  initial: string[];
  onClose: () => void;
}) {
  const [selected, setSelected] = useState<string[]>(initial);
  const [prefix, setPrefix] = useState("");
  const [format, setFormat] = useState<"csv" | "xlsx">("xlsx");
  const [error, setError] = useState<string | null>(null);
  const [dropped, setDropped] = useState<string | null>(null);
  const fields = useQuery({
    queryKey: ["export-fields", model, prefix],
    queryFn: () => dataio.exportFields(model, prefix),
  });

  const download = async () => {
    setError(null);
    try {
      const response = await dataio.export({ model, fields: selected, domain, ids, format });
      setDropped(response.headers.get("X-Tessera-Dropped-Fields") || null);
      const blob = await response.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `${model}.${format}`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="palette-backdrop" onMouseDown={onClose}>
      <div
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-label="Export"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <h2>Export {ids?.length ? `${ids.length} selected record(s)` : "all matching records"}</h2>
        <div className="export-columns">
          <div>
            <h3>
              Available{prefix && ` — ${prefix}`}{" "}
              {prefix && (
                <button
                  type="button"
                  className="btn small ghost"
                  onClick={() => setPrefix(prefix.split("/").slice(0, -1).join("/"))}
                >
                  ↑ Up
                </button>
              )}
            </h3>
            <ul className="field-list">
              {(fields.data ?? []).map((f) => (
                <li key={f.path}>
                  <button
                    type="button"
                    className="btn ghost small"
                    disabled={selected.includes(f.path)}
                    onClick={() => setSelected((s) => [...s, f.path])}
                  >
                    + {f.label}
                  </button>
                  {f.relational && prefix.split("/").length < 2 && (
                    <button
                      type="button"
                      className="btn ghost small"
                      aria-label={`Fields of ${f.label}`}
                      onClick={() => setPrefix(f.path)}
                    >
                      ›
                    </button>
                  )}
                </li>
              ))}
            </ul>
          </div>
          <div>
            <h3>To export</h3>
            <ol className="field-list">
              {selected.map((p) => (
                <li key={p}>
                  {p}{" "}
                  <button
                    type="button"
                    className="btn ghost small"
                    aria-label={`Remove ${p}`}
                    onClick={() => setSelected((s) => s.filter((x) => x !== p))}
                  >
                    ✕
                  </button>
                </li>
              ))}
            </ol>
          </div>
        </div>
        <div className="button-row">
          <label>
            Format{" "}
            <select
              className="input small"
              value={format}
              onChange={(e) => setFormat(e.target.value as "csv" | "xlsx")}
            >
              <option value="xlsx">Excel (XLSX)</option>
              <option value="csv">CSV</option>
            </select>
          </label>
          <button type="button" className="btn primary" disabled={!selected.length} onClick={() => void download()}>
            Download
          </button>
          <button type="button" className="btn ghost" onClick={onClose}>
            Close
          </button>
        </div>
        {dropped && (
          <div className="alert info" role="status">
            Left out because you cannot read them: {dropped}
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
