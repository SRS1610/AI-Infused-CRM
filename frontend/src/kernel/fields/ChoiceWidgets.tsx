import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { FieldMeta } from "../api/types";

/** Pipeline-style picker for a many2one (e.g. CRM stages) or a selection. */
export function StatusbarWidget({
  field,
  value,
  onChange,
  readonly,
  id,
}: {
  field: FieldMeta;
  value: any;
  onChange: (v: any) => void;
  readonly: boolean;
  id: string;
}) {
  const options = useQuery({
    queryKey: ["statusbar-options", field.comodel],
    queryFn: () => api.list(field.comodel!, { fields: ["display_name"], limit: 30 }),
    enabled: field.type === "many2one" && !!field.comodel,
  });
  const items: { key: any; label: string }[] =
    field.type === "selection"
      ? (field.selection ?? []).map(([k, l]) => ({ key: k, label: l }))
      : (options.data?.items ?? []).map((r) => ({ key: r.id, label: String(r.display_name) }));
  const current = field.type === "many2one" ? (value?.id ?? null) : value;
  return (
    <div className="statusbar-widget" role="radiogroup" aria-label={field.string} id={id}>
      {items.map((item) => (
        <button
          key={String(item.key)}
          type="button"
          role="radio"
          aria-checked={item.key === current}
          className={item.key === current ? "step current" : "step"}
          disabled={readonly}
          onClick={() => onChange(field.type === "many2one" ? { id: item.key, display_name: item.label } : item.key)}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}

/** 0–3 stars for a selection whose keys are "0".."3". */
export function PriorityWidget({
  field,
  value,
  onChange,
  readonly,
  id,
}: {
  field: FieldMeta;
  value: any;
  onChange: (v: any) => void;
  readonly: boolean;
  id: string;
}) {
  const options = field.selection ?? [];
  const level = Number(value ?? 0);
  return (
    <span className="priority-widget" role="radiogroup" aria-label={field.string} id={id}>
      {options.slice(1).map(([key, label]) => {
        const n = Number(key);
        const on = level >= n;
        return (
          <button
            key={key}
            type="button"
            role="radio"
            aria-checked={level === n}
            aria-label={label}
            className={`star${on ? " on" : ""}`}
            disabled={readonly}
            onClick={() => onChange(level === n ? "0" : key)}
          >
            {on ? "★" : "☆"}
          </button>
        );
      })}
    </span>
  );
}
