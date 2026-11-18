// Pure helpers turning read_group rows into pivot tables and chart series (unit-tested separately).
import type { FieldsMeta, GroupRow } from "../api/types";
import { currentLocale, formatDate } from "../format";
import { t } from "../i18n";

const ISO = /^\d{4}-\d{2}-\d{2}/;

/** "Nov 2026", "Q4 2026", "W46 2026", "2026" or a date, for a group of a date field by `granularity`. */
export function dateGroupLabel(value: string, granularity = "month"): string {
  const [y, m, d] = value.slice(0, 10).split("-").map(Number);
  const date = new Date(Date.UTC(y, m - 1, d));
  switch (granularity) {
    case "year":
      return String(y);
    case "quarter":
      return `Q${Math.floor((m - 1) / 3) + 1} ${y}`;
    case "week": {
      const thursday = new Date(date);
      thursday.setUTCDate(date.getUTCDate() + 3 - ((date.getUTCDay() + 6) % 7));
      const firstThursday = new Date(Date.UTC(thursday.getUTCFullYear(), 0, 4));
      const week = 1 + Math.round((thursday.getTime() - firstThursday.getTime()) / 604_800_000);
      return t("W{week} {year}", { week, year: thursday.getUTCFullYear() });
    }
    case "day":
      return formatDate(value);
    default:
      return new Intl.DateTimeFormat(currentLocale(), { month: "short", year: "numeric", timeZone: "UTC" }).format(
        date,
      );
  }
}

/** Label of a group value; `by` is the group spec (`field` or `field:granularity`). */
export function groupLabel(value: unknown, by?: string, fields?: FieldsMeta): string {
  if (value === null || value === undefined || value === false) return t("None");
  if (typeof value === "object" && value && "display_name" in value)
    return String((value as { display_name: unknown }).display_name ?? t("None"));
  const [name, granularity] = (by ?? "").split(":");
  const field = fields?.[name];
  if (
    typeof value === "string" &&
    ISO.test(value) &&
    (granularity || field?.type === "date" || field?.type === "datetime")
  )
    return dateGroupLabel(value, granularity || "month");
  if (field?.type === "selection") return field.selection?.find(([k]) => k === value)?.[1] ?? String(value);
  if (field?.type === "boolean") return value ? t("Yes") : t("No");
  return String(value);
}

export interface PivotTable {
  rows: string[][]; // one label path per row
  cols: string[];
  cells: Map<string, number>; // `${rowKey}||${col}` → value
  rowTotals: Map<string, number>;
  colTotals: Map<string, number>;
  total: number;
}

export function buildPivot(
  groups: GroupRow[],
  rowBy: string[],
  colBy: string | null,
  measure: string,
  fields?: FieldsMeta,
): PivotTable {
  const rows = new Map<string, string[]>();
  const rowSort = new Map<string, string>();
  const cols = new Set<string>();
  const colSort = new Map<string, string>();
  const sortKey = (v: unknown, label: string) => (typeof v === "string" && ISO.test(v) ? v : label);
  const cells = new Map<string, number>();
  const rowTotals = new Map<string, number>();
  const colTotals = new Map<string, number>();
  let total = 0;
  for (const g of groups) {
    const path = rowBy.map((r) => groupLabel(g[r], r, fields));
    const rowKey = path.join(" / ");
    rowSort.set(rowKey, rowBy.map((r, i) => sortKey(g[r], path[i])).join("/"));
    const col = colBy ? groupLabel(g[colBy], colBy, fields) : t("Total");
    colSort.set(col, colBy ? sortKey(g[colBy], col) : col);
    const value = Number(measure === "__count" ? g.__count : (g[measure] ?? 0)) || 0;
    rows.set(rowKey, path);
    cols.add(col);
    cells.set(`${rowKey}||${col}`, (cells.get(`${rowKey}||${col}`) ?? 0) + value);
    rowTotals.set(rowKey, (rowTotals.get(rowKey) ?? 0) + value);
    colTotals.set(col, (colTotals.get(col) ?? 0) + value);
    total += value;
  }
  const none = t("None");
  const sortedCols = [...cols].sort((a, b) =>
    a === none
      ? 1
      : b === none
        ? -1
        : (colSort.get(a) ?? a).localeCompare(colSort.get(b) ?? b, undefined, { numeric: true }),
  );
  const sortedRows = [...rows.entries()]
    .sort(([a], [b]) => (rowSort.get(a) ?? a).localeCompare(rowSort.get(b) ?? b, undefined, { numeric: true }))
    .map(([, path]) => path);
  return { rows: sortedRows, cols: sortedCols, cells, rowTotals, colTotals, total };
}

export function toCsv(table: PivotTable, rowHeaders: string[]): string {
  const esc = (v: string | number) => {
    const s = String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const lines = [[...rowHeaders, ...table.cols, t("Total")].map(esc).join(",")];
  for (const path of table.rows) {
    const key = path.join(" / ");
    lines.push(
      [...path, ...table.cols.map((c) => table.cells.get(`${key}||${c}`) ?? 0), table.rowTotals.get(key) ?? 0]
        .map(esc)
        .join(","),
    );
  }
  lines.push(
    [
      ...rowHeaders.map((_, i) => (i === 0 ? t("Total") : "")),
      ...table.cols.map((c) => table.colTotals.get(c) ?? 0),
      table.total,
    ]
      .map(esc)
      .join(","),
  );
  return lines.join("\n");
}

export function series(
  groups: GroupRow[],
  by: string,
  measure: string,
  fields?: FieldsMeta,
): { label: string; value: number }[] {
  const out = new Map<string, number>();
  for (const g of groups) {
    const label = groupLabel(g[by], by, fields);
    const value = Number(measure === "__count" ? g.__count : (g[measure] ?? 0)) || 0;
    out.set(label, (out.get(label) ?? 0) + value);
  }
  return [...out.entries()].map(([label, value]) => ({ label, value }));
}
