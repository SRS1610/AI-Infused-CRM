import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api } from "../api/client";
import type { Domain, FieldMeta, ViewDef, ViewsResponse } from "../api/types";
import { formatNumber } from "../format";
import { buildPivot, toCsv } from "./pivotData";
import { t } from "../i18n";

interface Props {
  model: string;
  meta: ViewsResponse;
  view: ViewDef;
  domain: Domain;
}

export function groupableFields(meta: ViewsResponse): FieldMeta[] {
  return Object.values(meta.fields).filter(
    (f) =>
      f.store &&
      ["many2one", "selection", "date", "datetime", "boolean", "char"].includes(f.type) &&
      f.name !== "display_name",
  );
}

export function measureFields(meta: ViewsResponse): FieldMeta[] {
  return Object.values(meta.fields).filter(
    (f) => f.store && ["integer", "float", "monetary"].includes(f.type) && f.name !== "id",
  );
}

function groupSpec(meta: ViewsResponse, name: string): string {
  const type = meta.fields[name]?.type;
  return type === "date" || type === "datetime" ? `${name}:month` : name;
}

/** Pivot table over read_group: up to two row groupings, one column grouping, one measure. */
export function PivotView({ model, meta, view, domain }: Props) {
  const groupable = groupableFields(meta);
  const measures = measureFields(meta);
  const defaults =
    (view.rows as string[] | undefined) ?? (view.row ? [view.row as string] : groupable[0] ? [groupable[0].name] : []);
  const [rows, setRows] = useState<string[]>(defaults.slice(0, 2));
  const [col, setCol] = useState<string>((view.col as string | undefined) ?? "");
  const [measure, setMeasure] = useState<string>(view.measures?.[0] ?? "__count");

  const groupby = useMemo(() => [...rows, ...(col ? [col] : [])].map((g) => groupSpec(meta, g)), [rows, col, meta]);
  const aggregates = measure === "__count" ? [] : [`${measure}:sum`];
  const result = useQuery({
    queryKey: ["list", model, "pivot", groupby.join(","), measure, JSON.stringify(domain)],
    queryFn: () => api.readGroup(model, { domain, aggregates, groupby }),
    enabled: groupby.length > 0,
  });
  const rowKeys = rows.map((r) => groupSpec(meta, r));
  const colKey = col ? groupSpec(meta, col) : null;
  const valueKey = measure === "__count" ? "__count" : `${measure}:sum`;
  const table = useMemo(
    () => buildPivot(result.data?.groups ?? [], rowKeys, colKey, valueKey),
    [result.data, rowKeys.join(","), colKey, valueKey], // eslint-disable-line react-hooks/exhaustive-deps
  );
  const label = (n: string) => meta.fields[n]?.string ?? n;
  const measureLabel = measure === "__count" ? t("Count") : label(measure);

  const download = () => {
    const blob = new Blob([toCsv(table, rows.map(label))], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${model}-pivot.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  return (
    <div className="pivot-view">
      <div className="pivot-controls">
        {[0, 1].map((i) => (
          <label key={i}>
            {i === 0 ? t("Rows") : t("Then by")}
            <select
              className="input small"
              value={rows[i] ?? ""}
              onChange={(e) => {
                const next = [...rows];
                if (e.target.value) next[i] = e.target.value;
                else next.splice(i, 1);
                setRows(next.filter(Boolean));
              }}
            >
              <option value="">{i === 0 ? "—" : t("(none)")}</option>
              {groupable.map((f) => (
                <option key={f.name} value={f.name}>
                  {f.string}
                </option>
              ))}
            </select>
          </label>
        ))}
        <label>
          {t("Columns")}
          <select className="input small" value={col} onChange={(e) => setCol(e.target.value)}>
            <option value="">{t("(none)")}</option>
            {groupable.map((f) => (
              <option key={f.name} value={f.name}>
                {f.string}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("Measure")}
          <select className="input small" value={measure} onChange={(e) => setMeasure(e.target.value)}>
            <option value="__count">{t("Count")}</option>
            {measures.map((f) => (
              <option key={f.name} value={f.name}>
                {f.string}
              </option>
            ))}
          </select>
        </label>
        <button type="button" className="btn small ghost" onClick={download} disabled={!table.rows.length}>
          {t("Download CSV")}
        </button>
      </div>
      {result.isLoading && <div className="loading">{t("Loading…")}</div>}
      {result.isError && (
        <div className="alert error" role="alert">
          {(result.error as Error).message}
        </div>
      )}
      {!result.isLoading && (
        <div className="table-scroll">
          <table className="pivot-table">
            <caption className="sr-only">
              {col
                ? t("{measure} by {rows} and {column}", {
                    measure: measureLabel,
                    rows: rows.map(label).join(", "),
                    column: label(col),
                  })
                : t("{measure} by {rows}", { measure: measureLabel, rows: rows.map(label).join(", ") })}
            </caption>
            <thead>
              <tr>
                {rows.map((r) => (
                  <th key={r} scope="col">
                    {label(r)}
                  </th>
                ))}
                {table.cols.map((c) => (
                  <th key={c} scope="col" className="num">
                    {col ? c : measureLabel}
                  </th>
                ))}
                {col && (
                  <th scope="col" className="num">
                    {t("Total")}
                  </th>
                )}
              </tr>
            </thead>
            <tbody>
              {table.rows.map((path) => {
                const key = path.join(" / ");
                return (
                  <tr key={key}>
                    {path.map((p, i) => (
                      <th key={i} scope="row">
                        {p}
                      </th>
                    ))}
                    {table.cols.map((c) => (
                      <td key={c} className="num">
                        {formatNumber(table.cells.get(`${key}||${c}`) ?? 0)}
                      </td>
                    ))}
                    {col && <td className="num total">{formatNumber(table.rowTotals.get(key) ?? 0)}</td>}
                  </tr>
                );
              })}
            </tbody>
            <tfoot>
              <tr>
                <th scope="row" colSpan={Math.max(rows.length, 1)}>
                  {t("Total")}
                </th>
                {table.cols.map((c) => (
                  <td key={c} className="num total">
                    {formatNumber(table.colTotals.get(c) ?? 0)}
                  </td>
                ))}
                {col && <td className="num total">{formatNumber(table.total)}</td>}
              </tr>
            </tfoot>
          </table>
        </div>
      )}
    </div>
  );
}
