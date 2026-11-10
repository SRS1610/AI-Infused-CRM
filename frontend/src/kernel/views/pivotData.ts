// Pure helpers turning read_group rows into pivot tables and chart series (unit-tested separately).
import type { GroupRow } from "../api/types";
import { t } from "../i18n";

export function groupLabel(value: unknown): string {
  if (value === null || value === undefined || value === false) return t("None");
  if (typeof value === "object" && value && "display_name" in value)
    return String((value as { display_name: unknown }).display_name ?? t("None"));
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

export function buildPivot(groups: GroupRow[], rowBy: string[], colBy: string | null, measure: string): PivotTable {
  const rows = new Map<string, string[]>();
  const cols = new Set<string>();
  const cells = new Map<string, number>();
  const rowTotals = new Map<string, number>();
  const colTotals = new Map<string, number>();
  let total = 0;
  for (const g of groups) {
    const path = rowBy.map((r) => groupLabel(g[r]));
    const rowKey = path.join(" / ");
    const col = colBy ? groupLabel(g[colBy]) : t("Total");
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
    a === none ? 1 : b === none ? -1 : a.localeCompare(b, undefined, { numeric: true }),
  );
  const sortedRows = [...rows.values()].sort((a, b) =>
    a.join("/").localeCompare(b.join("/"), undefined, { numeric: true }),
  );
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

export function series(groups: GroupRow[], by: string, measure: string): { label: string; value: number }[] {
  const out = new Map<string, number>();
  for (const g of groups) {
    const label = groupLabel(g[by]);
    const value = Number(measure === "__count" ? g.__count : (g[measure] ?? 0)) || 0;
    out.set(label, (out.get(label) ?? 0) + value);
  }
  return [...out.entries()].map(([label, value]) => ({ label, value }));
}
