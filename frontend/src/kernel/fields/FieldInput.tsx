import type { FieldMeta, RecordData, ViewNode } from "../api/types";
import { bindRecord } from "../domain";
import { displayValue } from "../format";
import { Many2manyTags } from "./Many2manyTags";
import { Many2oneInput } from "./Many2oneInput";
import { One2manyTable } from "./One2manyTable";

export interface FieldInputProps {
  field: FieldMeta;
  value: any;
  record: RecordData;
  onChange: (value: any) => void;
  readonly: boolean;
  node?: ViewNode;
  id: string;
  invalid?: boolean;
}

const toLocalInput = (iso: string | null) => {
  if (!iso) return "";
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
};

function readFile(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] ?? "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

/** Edit widget for one field. Readonly fields render their formatted value. */
export function FieldInput({ field, value, record, onChange, readonly, node, id, invalid }: FieldInputProps) {
  const widget = node?.widget;
  const common = {
    id,
    "aria-invalid": invalid || undefined,
    "aria-required": field.required || undefined,
    disabled: readonly,
  };

  if (field.type === "one2many") {
    return (
      <One2manyTable
        comodel={field.comodel ?? ""}
        ids={Array.isArray(value) ? value : []}
        columns={node?.columns as any}
        label={field.string}
      />
    );
  }
  if (field.type === "many2many") {
    if (widget === "many2many_checkboxes" || widget === "tags" || !field.comodel) {
      return (
        <Many2manyTags
          comodel={field.comodel ?? ""}
          ids={Array.isArray(value) ? value : []}
          onChange={onChange}
          disabled={readonly}
          label={field.string}
          id={id}
        />
      );
    }
    return (
      <Many2manyTags
        comodel={field.comodel}
        ids={Array.isArray(value) ? value : []}
        onChange={onChange}
        disabled={readonly}
        label={field.string}
        id={id}
      />
    );
  }
  if (readonly && field.type !== "boolean") {
    const text = displayValue(field, value, record);
    if (field.type === "html" && value)
      return <div className="readonly html" id={id} dangerouslySetInnerHTML={{ __html: String(value) }} />;
    if (widget === "badge" && value)
      return (
        <span className={`badge badge-${value}`} id={id}>
          {text}
        </span>
      );
    return (
      <span className="readonly" id={id}>
        {text || "—"}
      </span>
    );
  }
  switch (field.type) {
    case "boolean":
      return (
        <input
          type="checkbox"
          {...common}
          className="checkbox"
          checked={!!value}
          onChange={(e) => onChange(e.target.checked)}
        />
      );
    case "integer":
      return (
        <input
          type="number"
          step={1}
          {...common}
          className="input num"
          value={value ?? ""}
          onChange={(e) => onChange(e.target.value === "" ? null : Number.parseInt(e.target.value, 10))}
        />
      );
    case "float":
    case "monetary":
      return (
        <input
          type="number"
          step="any"
          inputMode="decimal"
          {...common}
          className="input num"
          value={value ?? ""}
          onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
        />
      );
    case "date":
      return (
        <input
          type="date"
          {...common}
          className="input"
          value={value ?? ""}
          onChange={(e) => onChange(e.target.value || null)}
        />
      );
    case "datetime":
      return (
        <input
          type="datetime-local"
          {...common}
          className="input"
          value={toLocalInput(value)}
          onChange={(e) => onChange(e.target.value ? new Date(e.target.value).toISOString() : null)}
        />
      );
    case "selection":
      return (
        <select {...common} className="input" value={value ?? ""} onChange={(e) => onChange(e.target.value || null)}>
          {!field.required && <option value="">—</option>}
          {(field.selection ?? []).map(([k, label]) => (
            <option key={k} value={k}>
              {label}
            </option>
          ))}
        </select>
      );
    case "text":
    case "html":
      return (
        <textarea
          {...common}
          className="input textarea"
          rows={field.type === "html" ? 6 : 3}
          value={value ?? ""}
          onChange={(e) => onChange(e.target.value)}
        />
      );
    case "json":
      return (
        <textarea
          {...common}
          className="input textarea mono"
          rows={3}
          defaultValue={value === null || value === undefined ? "" : JSON.stringify(value)}
          onBlur={(e) => {
            try {
              onChange(e.target.value ? JSON.parse(e.target.value) : null);
            } catch {
              /* keep the previous value until the JSON is valid */
            }
          }}
        />
      );
    case "many2one":
      return (
        <Many2oneInput
          id={id}
          comodel={field.comodel ?? ""}
          value={value ?? null}
          onChange={onChange}
          disabled={readonly}
          required={field.required}
          label={field.string}
          domain={node?.domain ? bindRecord(node.domain, record) : field.domain}
          allowCreate={node?.options?.create !== false}
        />
      );
    case "binary":
    case "image":
      return (
        <div className="binary">
          {field.type === "image" && value && (
            <img className="thumb" alt={field.string} src={`data:image/*;base64,${value}`} />
          )}
          <input
            type="file"
            {...common}
            accept={field.type === "image" ? "image/*" : undefined}
            onChange={async (e) => e.target.files?.[0] && onChange(await readFile(e.target.files[0]))}
          />
        </div>
      );
    default:
      return (
        <input
          type={widget === "password" ? "password" : widget === "email" ? "email" : widget === "url" ? "url" : "text"}
          {...common}
          className={`input${widget === "title" ? " title" : ""}`}
          value={value ?? ""}
          autoComplete={widget === "password" ? "new-password" : "off"}
          onChange={(e) => onChange(e.target.value)}
        />
      );
  }
}
