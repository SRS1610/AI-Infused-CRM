import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Many2oneValue } from "../api/types";
import { Many2oneInput } from "./Many2oneInput";
import { t } from "../i18n";

interface Props {
  comodel: string;
  ids: number[];
  onChange: (ids: number[]) => void;
  disabled?: boolean;
  label: string;
  id?: string;
}

/** Tags for many2many values: names are resolved with one request, new tags picked via name_search. */
export function Many2manyTags({ comodel, ids, onChange, disabled, label, id }: Props) {
  const [names, setNames] = useState<Record<number, string>>({});
  const key = ids.join(",");

  useEffect(() => {
    const missing = ids.filter((i) => !(i in names));
    if (!missing.length) return;
    api
      .list(comodel, { domain: [["id", "in", missing]], fields: ["display_name"], limit: missing.length, count: false })
      .then((res) =>
        setNames((prev) => ({ ...prev, ...Object.fromEntries(res.items.map((r) => [r.id, String(r.display_name)])) })),
      )
      .catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [comodel, key]);

  const add = (value: Many2oneValue | null) => {
    if (!value || ids.includes(value.id)) return;
    setNames((prev) => ({ ...prev, [value.id]: value.display_name ?? `#${value.id}` }));
    onChange([...ids, value.id]);
  };

  return (
    <div className="tags" aria-label={label}>
      <ul className="tag-list">
        {ids.map((tid) => (
          <li key={tid} className="tag">
            {names[tid] ?? `#${tid}`}
            {!disabled && (
              <button
                type="button"
                className="tag-remove"
                aria-label={t("Remove {name}", { name: names[tid] ?? tid })}
                onClick={() => onChange(ids.filter((x) => x !== tid))}
              >
                ×
              </button>
            )}
          </li>
        ))}
      </ul>
      {!disabled && (
        <Many2oneInput
          id={id}
          comodel={comodel}
          value={null}
          onChange={add}
          label={t("Add {field}", { field: label })}
          domain={ids.length ? { not: ["id", "in", ids] } : undefined}
        />
      )}
    </div>
  );
}
