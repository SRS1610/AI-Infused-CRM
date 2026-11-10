import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import { dataio, type ImportJob } from "../api/platform";
import { useRealtime } from "../realtime";
import { t, tn } from "../i18n";

/** /import/:model — upload, map columns, dry run, then import in the background with live progress. */
export function ImportPage() {
  const { model = "" } = useParams();
  const navigate = useNavigate();
  const [job, setJob] = useState<ImportJob | null>(null);
  const [fields, setFields] = useState<NonNullable<ImportJob["fields"]>>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<{ value: number; message: string } | null>(null);
  const [saveAs, setSaveAs] = useState("");
  const views = useQuery({ queryKey: ["views", model, "list"], queryFn: () => api.views(model, ["list"]) });
  const label = job?.model_label ?? views.data?.model.description ?? model;

  useRealtime(job?.job_id ? `job:${job.job_id}` : null, (event) => {
    if (event.event === "progress") {
      setProgress({ value: Number(event.data.progress ?? 0), message: String(event.data.message ?? "") });
      if (Number(event.data.progress) >= 100 && job) void dataio.get(job.id).then(setJob);
    }
  });

  const run = async (fn: () => Promise<ImportJob>) => {
    setBusy(true);
    setError(null);
    try {
      const next = await fn();
      setJob((prev) => ({ ...prev, ...next }));
      return next;
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
      return null;
    } finally {
      setBusy(false);
    }
  };

  const onFile = async (file: File) => {
    const uploaded = await run(() => dataio.upload(model, file));
    if (uploaded?.fields) setFields(uploaded.fields);
  };
  const setMapping = (header: string, path: string) => {
    if (!job) return;
    const mapping = { ...job.mapping, [header]: path || null };
    setJob({ ...job, mapping, state: "draft" });
  };

  const blocking = (job?.errors ?? []).filter((e) => e.row === null);
  const options = (header: string) => {
    const opts: { value: string; label: string }[] = [{ value: "id", label: t("External ID") }];
    for (const f of fields) {
      opts.push({ value: f.name, label: `${f.label}${f.required ? " *" : ""}` });
      if (f.paths) {
        opts.push({ value: `${f.name}/id`, label: t("{field} (external ID)", { field: f.label }) });
        opts.push({ value: `${f.name}/.id`, label: t("{field} (database ID)", { field: f.label }) });
      }
    }
    return opts.filter((o) => o.value === job?.mapping[header] || !Object.values(job?.mapping ?? {}).includes(o.value));
  };

  return (
    <div className="import-page">
      <div className="form-toolbar">
        <button type="button" className="btn ghost" onClick={() => navigate(-1)}>
          ← {t("Back")}
        </button>
        <h1 className="page-title">{t("Import {model}", { model: label })}</h1>
        <a className="btn ghost small" href={`/api/v1/import/${model}/template`}>
          {t("Download template")}
        </a>
      </div>
      {!job && (
        <label className="dropzone">
          <span>{t("Choose a CSV or Excel file")}</span>
          <input
            type="file"
            accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            onChange={(e) => e.target.files?.[0] && void onFile(e.target.files[0])}
          />
        </label>
      )}
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
      {job && (
        <>
          <p>
            <strong>{job.filename}</strong> — {tn("{count} row", "{count} rows", job.row_count)}
            {job.saved_mapping && (
              <span className="badge">{t("Mapping “{name}” applied", { name: job.saved_mapping })}</span>
            )}
          </p>
          <div className="table-scroll">
            <table className="import-mapping">
              <thead>
                <tr>
                  <th scope="col">{t("Column in file")}</th>
                  <th scope="col">{t("Imports into")}</th>
                  <th scope="col">{t("Sample")}</th>
                </tr>
              </thead>
              <tbody>
                {job.headers.map((h, i) => (
                  <tr key={h}>
                    <th scope="row">{h}</th>
                    <td>
                      <select
                        className="input small"
                        aria-label={t("Field for column {column}", { column: h })}
                        value={job.mapping[h] ?? ""}
                        onChange={(e) => setMapping(h, e.target.value)}
                      >
                        <option value="">{t("Do not import")}</option>
                        {options(h).map((o) => (
                          <option key={o.value} value={o.value}>
                            {o.label}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td className="muted">
                      {job.preview
                        .slice(0, 3)
                        .map((r) => r[i])
                        .filter(Boolean)
                        .join(", ")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="button-row">
            <input
              className="input small"
              placeholder={t("Save mapping as…")}
              aria-label={t("Save mapping as")}
              value={saveAs}
              onChange={(e) => setSaveAs(e.target.value)}
            />
            <button
              type="button"
              className="btn"
              disabled={busy}
              onClick={async () => {
                const updated = await run(() =>
                  dataio.update(job.id, { mapping: job.mapping, save_as: saveAs || undefined }),
                );
                if (updated) await run(() => dataio.dryRun(job.id));
              }}
            >
              {t("Test (dry run)")}
            </button>
            <button
              type="button"
              className="btn primary"
              disabled={busy || job.state !== "tested" || blocking.length > 0}
              onClick={() => void run(() => dataio.start(job.id))}
            >
              {t("Import")}
            </button>
          </div>
          {job.state === "tested" && (
            <div className={`alert ${job.error_count ? "error" : "info"}`} role="status">
              {blocking.length
                ? t("Fix the column mapping first: {problems}", { problems: blocking.map((e) => e.message).join("; ") })
                : job.error_count
                  ? tn(
                      "{count} row has errors and will be skipped; the other rows will be imported.",
                      "{count} rows have errors and will be skipped; the other rows will be imported.",
                      job.error_count,
                    )
                  : t("Everything looks fine. Nothing has been written yet.")}
            </div>
          )}
          {(job.state === "queued" || job.state === "running") && (
            <div className="import-progress" role="status">
              <progress max={100} value={progress?.value ?? 0} aria-label={t("Import progress")} />{" "}
              {progress?.message ?? t("Queued…")}
            </div>
          )}
          {job.state === "done" && (
            <div className="alert info" role="status">
              {t("Import finished: {created} created, {updated} updated, {skipped} skipped.", {
                created: job.created,
                updated: job.updated,
                skipped: job.error_count,
              })}
            </div>
          )}
          {job.errors.length > 0 && (
            <table className="import-errors">
              <caption>{t("Errors")}</caption>
              <thead>
                <tr>
                  <th scope="col">{t("Row")}</th>
                  <th scope="col">{t("Column")}</th>
                  <th scope="col">{t("Problem")}</th>
                </tr>
              </thead>
              <tbody>
                {job.errors.map((e, i) => (
                  <tr key={i}>
                    <td>{e.row ?? "—"}</td>
                    <td>{e.column ?? "—"}</td>
                    <td>{e.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </div>
  );
}
