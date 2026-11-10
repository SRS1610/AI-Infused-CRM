// A plain-language editor for simple domains: rows of "field operator value", all or any of them.
// Used wherever a condition is written without code (Studio: required/read-only when…, search
// filters, workflow transitions). Domains it cannot show as rows are displayed as JSON and can be cleared.
import { useQuery } from "@tanstack/react-query";
import { useId, useState } from "react";
import { api } from "../api/client";
import type { Domain, FieldMeta, FieldsMeta } from "../api/types";
import { conditionOperatorsFor, DATE_TYPES, NUMERIC_TYPES, operatorTable, valueless } from "../conditions";
import { and, isEmpty, or } from "../domain";
import { t } from "../i18n";

export interface ConditionRow {
  field: string;
  op: string;
  value: unknown;
}
type Mode = "all" | "any";

const isLeaf = (d: unknown): d is unknown[] =>
  Array.isArray(d) && d.length >= 2 && typeof d[0] === "string" && typeof d[1] === "string";

const toRow = (leaf: unknown[]): ConditionRow => ({ field: String(leaf[0]), op: String(leaf[1]), value: leaf[2] });

/** Rows of a simple domain (a leaf, a list of leaves, or one all/any of leaves); null when it is not simple. */
export function parseCondition(domain: Domain | null | undefined): { rows: ConditionRow[]; mode: Mode } | null {
  if (isEmpty(domain as Domain)) return { rows: [], mode: "all" };
  if (isLeaf(domain)) return { rows: [toRow(domain)], mode: "all" };
  if (Array.isArray(domain) && domain.every(isLeaf)) return { rows: domain.map(toRow), mode: "all" };
  if (domain && typeof domain === "object" && !Array.isArray(domain)) {
    const node = domain as Record<string, unknown>;
    for (const mode of ["all", "any"] as const)
      if (Array.isArray(node[mode]) && Object.keys(node).length === 1 && (node[mode] as unknown[]).every(isLeaf))
        return { rows: (node[mode] as unknown[][]).map(toRow), mode };
  }
  return null;
}

/** The domain of complete rows (null when there is none). */
export function buildCondition(rows: ConditionRow[], mode: Mode): Domain | null {
  const leaves = rows
    .filter((r) => r.field && r.op && (valueless(r.op) || (r.value !== undefined && r.value !== "")))
    .map((r) => (valueless(r.op) ? [r.field, r.op] : [r.field, r.op, r.value]));
  if (!leaves.length) return null;
  return mode === "any" ? or(...leaves) : and(...leaves);
}

/** Records of a model to pick from (the first 80 by name). */
export function useRecordChoices(model: string | null | undefined) {
  return useQuery({
    queryKey: ["choices", model],
    queryFn: () => api.nameSearch(model!, "", undefined, 80),
    enabled: !!model,
    staleTime: 60_000,
  });
}

export function RecordSelect({
  model,
  value,
  onChange,
  label,
  id,
  emptyLabel,
}: {
  model: string;
  value: number | null | undefined;
  onChange: (id: number | null) => void;
  label: string;
  id?: string;
  emptyLabel?: string;
}) {
  const choices = useRecordChoices(model);
  return (
    <select
      id={id}
      className="input small"
      aria-label={label}
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)}
    >
      <option value="">{emptyLabel ?? "—"}</option>
      {(choices.data ?? []).map((r) => (
        <option key={r.id} value={r.id}>
          {r.display_name}
        </option>
      ))}
    </select>
  );
}

function ValueInput({ field, row, onChange }: { field: FieldMeta; row: ConditionRow; onChange: (v: unknown) => void }) {
  const label = t("Value");
  if (field.type === "selection")
    return (
      <select
        className="input small"
        aria-label={label}
        value={String(row.value ?? "")}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="" />
        {(field.selection ?? []).map(([k, l]) => (
          <option key={k} value={k}>
            {l}
          </option>
        ))}
      </select>
    );
  if (field.type === "boolean")
    return (
      <select
        className="input small"
        aria-label={label}
        value={row.value === false ? "false" : "true"}
        onChange={(e) => onChange(e.target.value === "true")}
      >
        <option value="true">{t("Yes")}</option>
        <option value="false">{t("No")}</option>
      </select>
    );
  if (field.type === "many2one" && field.comodel)
    return (
      <RecordSelect
        model={field.comodel}
        label={label}
        value={typeof row.value === "number" ? row.value : null}
        onChange={(v) => onChange(v ?? "")}
      />
    );
  const numeric = NUMERIC_TYPES.includes(field.type);
  return (
    <input
      className="input small"
      aria-label={label}
      type={DATE_TYPES.includes(field.type) ? "date" : numeric ? "number" : "text"}
      value={row.value === undefined || row.value === null ? "" : String(row.value)}
      onChange={(e) => onChange(numeric && e.target.value !== "" ? Number(e.target.value) : e.target.value)}
    />
  );
}

/** Edit a condition as rows. `fields` are the model's fields (from the views endpoint). */
export function ConditionEditor({
  fields,
  value,
  onChange,
  label,
}: {
  fields: FieldsMeta;
  value: Domain | null | undefined;
  onChange: (domain: Domain | null) => void;
  label: string;
}) {
  const parsed = parseCondition(value);
  const [rows, setRows] = useState<ConditionRow[]>(parsed?.rows ?? []);
  const [mode, setMode] = useState<Mode>(parsed?.mode ?? "all");
  const [raw, setRaw] = useState(parsed === null);
  const groupId = useId();
  const choices = Object.values(fields)
    .filter((f) => f.store !== false && f.type !== "binary" && f.type !== "json" && f.name !== "id")
    .sort((a, b) => a.string.localeCompare(b.string));
  const update = (next: ConditionRow[], nextMode: Mode = mode) => {
    setRows(next);
    setMode(nextMode);
    onChange(buildCondition(next, nextMode));
  };
  const setRow = (i: number, patch: Partial<ConditionRow>) =>
    update(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  if (raw)
    return (
      <div className="condition-editor" role="group" aria-label={label}>
        <code className="condition-raw">{JSON.stringify(value)}</code>
        <button
          type="button"
          className="btn small ghost"
          onClick={() => {
            setRaw(false);
            update([], "all");
          }}
        >
          {t("Clear condition")}
        </button>
      </div>
    );
  return (
    <div className="condition-editor" role="group" aria-label={label} id={groupId}>
      {rows.length > 1 && (
        <select
          className="input small"
          aria-label={t("Combine conditions")}
          value={mode}
          onChange={(e) => update(rows, e.target.value as Mode)}
        >
          <option value="all">{t("All of these")}</option>
          <option value="any">{t("Any of these")}</option>
        </select>
      )}
      {rows.map((row, i) => {
        const field = fields[row.field];
        const ops = field ? conditionOperatorsFor(field) : operatorTable().text;
        return (
          <div key={i} className="condition-row">
            <select
              className="input small"
              aria-label={t("Field")}
              value={row.field}
              onChange={(e) =>
                setRow(i, {
                  field: e.target.value,
                  op: conditionOperatorsFor(fields[e.target.value])[0][0],
                  value: fields[e.target.value]?.type === "boolean" ? true : "",
                })
              }
            >
              <option value="" />
              {choices.map((f) => (
                <option key={f.name} value={f.name}>
                  {f.string}
                </option>
              ))}
              {row.field && !field && <option value={row.field}>{row.field}</option>}
            </select>
            <select
              className="input small"
              aria-label={t("Operator")}
              value={row.op}
              onChange={(e) => setRow(i, { op: e.target.value })}
            >
              {ops.map(([op, l]) => (
                <option key={op} value={op}>
                  {l}
                </option>
              ))}
            </select>
            {field && !valueless(row.op) && (
              <ValueInput field={field} row={row} onChange={(v) => setRow(i, { value: v })} />
            )}
            <button
              type="button"
              className="btn small ghost"
              aria-label={t("Remove condition")}
              onClick={() => update(rows.filter((_, j) => j !== i))}
            >
              ×
            </button>
          </div>
        );
      })}
      <button type="button" className="btn small" onClick={() => setRows([...rows, { field: "", op: "=", value: "" }])}>
        {t("Add a condition")}
      </button>
    </div>
  );
}

/** One line of plain language for a simple condition ("Stage is Won"), JSON otherwise. */
export function ConditionSummary({ fields, domain }: { fields: FieldsMeta; domain: Domain | null | undefined }) {
  const parsed = parseCondition(domain);
  const comodels = [
    ...new Set(
      (parsed?.rows ?? [])
        .map((r) => fields[r.field])
        .filter((f) => f?.type === "many2one")
        .map((f) => f!.comodel),
    ),
  ];
  const first = useRecordChoices(comodels[0]);
  const second = useRecordChoices(comodels[1]);
  if (!parsed) return <code>{JSON.stringify(domain)}</code>;
  if (!parsed.rows.length) return <span className="muted">{t("always")}</span>;
  const names = new Map<string, Map<number, string>>();
  [first, second].forEach((q, i) => {
    if (comodels[i]) names.set(comodels[i]!, new Map((q.data ?? []).map((r) => [r.id, r.display_name])));
  });
  const opLabel = (f: FieldMeta | undefined, op: string) =>
    (f ? conditionOperatorsFor(f) : operatorTable().text).find(([o]) => o === op)?.[1] ?? op;
  const valueText = (f: FieldMeta | undefined, v: unknown): string => {
    if (!f) return JSON.stringify(v);
    if (f.type === "selection") return (f.selection ?? []).find(([k]) => k === v)?.[1] ?? String(v);
    if (f.type === "boolean") return v ? t("Yes") : t("No");
    if (f.type === "many2one") return names.get(f.comodel ?? "")?.get(Number(v)) ?? `#${String(v)}`;
    return String(v);
  };
  const parts = parsed.rows.map((r) => {
    const f = fields[r.field];
    const head = `${f?.string ?? r.field} ${opLabel(f, r.op)}`;
    return valueless(r.op) ? head : `${head} ${valueText(f, r.value)}`;
  });
  return <span>{parts.join(parsed.mode === "any" ? ` ${t("or")} ` : ` ${t("and")} `)}</span>;
}
