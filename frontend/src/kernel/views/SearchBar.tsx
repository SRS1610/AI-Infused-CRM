import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import type { Domain, FieldMeta, ViewDef, ViewsResponse } from "../api/types";
import { searchAddonsFor } from "../extensions";
import { buildDomain } from "./searchDomain";

export interface SearchState {
  text: string;
  filters: string[];
  groupBy: string[];
  custom: Domain[];
}

export const emptySearch: SearchState = { text: "", filters: [], groupBy: [], custom: [] };

interface Props {
  model: string;
  meta: ViewsResponse;
  view: ViewDef | undefined;
  value: SearchState;
  onChange: (v: SearchState) => void;
  inputRef?: React.Ref<HTMLInputElement>;
}

const OPERATORS: Record<string, [string, string][]> = {
  text: [
    ["ilike", "contains"],
    ["not_ilike", "does not contain"],
    ["=", "is"],
    ["!=", "is not"],
    ["is_set", "is set"],
    ["is_not_set", "is not set"],
  ],
  number: [
    ["=", "="],
    ["!=", "≠"],
    [">", ">"],
    [">=", "≥"],
    ["<", "<"],
    ["<=", "≤"],
    ["is_set", "is set"],
  ],
  date: [
    ["=", "on"],
    [">=", "on or after"],
    ["<=", "on or before"],
    ["is_set", "is set"],
    ["is_not_set", "is not set"],
  ],
  boolean: [["=", "is"]],
  selection: [
    ["=", "is"],
    ["!=", "is not"],
    ["is_set", "is set"],
  ],
  relation: [
    ["ilike", "name contains"],
    ["is_set", "is set"],
    ["is_not_set", "is not set"],
  ],
};

function operatorsFor(field?: FieldMeta): [string, string][] {
  if (!field) return OPERATORS.text;
  if (["integer", "float", "monetary"].includes(field.type)) return OPERATORS.number;
  if (["date", "datetime"].includes(field.type)) return OPERATORS.date;
  if (field.type === "boolean") return OPERATORS.boolean;
  if (field.type === "selection") return OPERATORS.selection;
  if (["many2one", "many2many", "one2many"].includes(field.type)) return OPERATORS.relation;
  return OPERATORS.text;
}

export function SearchBar({ model, meta, view, value, onChange, inputRef }: Props) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<{ field: string; op: string; value: string }>({
    field: "",
    op: "ilike",
    value: "",
  });
  const favorites = useQuery({
    queryKey: ["filters", model],
    queryFn: () =>
      api.list("web.filter", {
        domain: [["model_name", "=", model]],
        fields: ["name", "domain", "group_bys", "user_id"],
        count: false,
      }),
  });
  const filters = view?.filters ?? [];
  const groupBys = view?.group_bys ?? [];
  const searchable = Object.values(meta.fields)
    .filter((f) => f.store && f.type !== "binary" && f.type !== "json")
    .sort((a, b) => a.string.localeCompare(b.string));

  const toggle = (list: string[], item: string) =>
    list.includes(item) ? list.filter((x) => x !== item) : [...list, item];

  const addCustom = () => {
    const field = meta.fields[draft.field];
    if (!field) return;
    let v: unknown = draft.value;
    if (["integer", "float", "monetary"].includes(field.type)) v = Number(draft.value);
    if (field.type === "boolean") v = draft.value === "true";
    const path =
      ["many2one", "many2many", "one2many"].includes(field.type) && draft.op === "ilike"
        ? `${field.name}.display_name`
        : field.name;
    const leaf = draft.op === "is_set" || draft.op === "is_not_set" ? [field.name, draft.op] : [path, draft.op, v];
    onChange({ ...value, custom: [...value.custom, leaf] });
    setDraft({ field: "", op: "ilike", value: "" });
  };

  const saveFavorite = async () => {
    const name = window.prompt("Name of this filter");
    if (!name) return;
    await api.create("web.filter", {
      name,
      model_name: model,
      domain: buildDomain(meta, view, value),
      group_bys: value.groupBy,
    });
    await queryClient.invalidateQueries({ queryKey: ["filters", model] });
  };

  const active = value.filters.length + value.groupBy.length + value.custom.length;

  return (
    <div className="search-bar" role="search">
      <input
        ref={inputRef}
        className="input search-input"
        type="search"
        placeholder={`Search ${meta.model.description}…`}
        aria-label={`Search ${meta.model.description}`}
        value={value.text}
        onChange={(e) => onChange({ ...value, text: e.target.value })}
      />
      <button
        type="button"
        className="btn ghost"
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen((o) => !o)}
      >
        Filters{active ? ` (${active})` : ""}
      </button>
      {searchAddonsFor(model).map((a) => (
        <span key={a.key} className="search-addon">
          {a.render({ model, apply: (domain) => onChange({ ...value, custom: [...value.custom, domain as Domain] }) })}
        </span>
      ))}
      {active > 0 && (
        <button
          type="button"
          className="btn ghost small"
          onClick={() => onChange({ ...value, filters: [], groupBy: [], custom: [] })}
        >
          Clear
        </button>
      )}
      {open && (
        <div className="search-panel" role="dialog" aria-label="Filters and grouping">
          <fieldset>
            <legend>Filters</legend>
            {filters.length === 0 && <p className="muted">No predefined filters.</p>}
            {filters.map((f) => (
              <label key={f.name} className="menu-check">
                <input
                  type="checkbox"
                  checked={value.filters.includes(f.name)}
                  onChange={() => onChange({ ...value, filters: toggle(value.filters, f.name) })}
                />
                {f.label}
              </label>
            ))}
            {value.custom.map((leaf, i) => (
              <div key={i} className="custom-leaf">
                <code>{JSON.stringify(leaf)}</code>
                <button
                  type="button"
                  className="btn small ghost"
                  aria-label="Remove condition"
                  onClick={() => onChange({ ...value, custom: value.custom.filter((_, j) => j !== i) })}
                >
                  ×
                </button>
              </div>
            ))}
            <div className="domain-editor">
              <select
                className="input small"
                aria-label="Field"
                value={draft.field}
                onChange={(e) =>
                  setDraft({ field: e.target.value, op: operatorsFor(meta.fields[e.target.value])[0][0], value: "" })
                }
              >
                <option value="">Add a condition…</option>
                {searchable.map((f) => (
                  <option key={f.name} value={f.name}>
                    {f.string}
                  </option>
                ))}
              </select>
              {draft.field && (
                <>
                  <select
                    className="input small"
                    aria-label="Operator"
                    value={draft.op}
                    onChange={(e) => setDraft({ ...draft, op: e.target.value })}
                  >
                    {operatorsFor(meta.fields[draft.field]).map(([op, label]) => (
                      <option key={op} value={op}>
                        {label}
                      </option>
                    ))}
                  </select>
                  {!["is_set", "is_not_set"].includes(draft.op) &&
                    (meta.fields[draft.field].type === "selection" ? (
                      <select
                        className="input small"
                        aria-label="Value"
                        value={draft.value}
                        onChange={(e) => setDraft({ ...draft, value: e.target.value })}
                      >
                        <option value="" />
                        {(meta.fields[draft.field].selection ?? []).map(([k, l]) => (
                          <option key={k} value={k}>
                            {l}
                          </option>
                        ))}
                      </select>
                    ) : meta.fields[draft.field].type === "boolean" ? (
                      <select
                        className="input small"
                        aria-label="Value"
                        value={draft.value}
                        onChange={(e) => setDraft({ ...draft, value: e.target.value })}
                      >
                        <option value="true">Yes</option>
                        <option value="false">No</option>
                      </select>
                    ) : (
                      <input
                        className="input small"
                        aria-label="Value"
                        type={
                          ["date", "datetime"].includes(meta.fields[draft.field].type)
                            ? "date"
                            : ["integer", "float", "monetary"].includes(meta.fields[draft.field].type)
                              ? "number"
                              : "text"
                        }
                        value={draft.value}
                        onChange={(e) => setDraft({ ...draft, value: e.target.value })}
                      />
                    ))}
                  <button type="button" className="btn small" onClick={addCustom}>
                    Add
                  </button>
                </>
              )}
            </div>
          </fieldset>
          <fieldset>
            <legend>Group by</legend>
            {groupBys.map((g) => (
              <label key={g.field} className="menu-check">
                <input
                  type="checkbox"
                  checked={value.groupBy.includes(g.field)}
                  onChange={() => onChange({ ...value, groupBy: toggle(value.groupBy, g.field) })}
                />
                {g.label ?? meta.fields[g.field.split(":")[0]]?.string ?? g.field}
              </label>
            ))}
            {groupBys.length === 0 && <p className="muted">No grouping options.</p>}
          </fieldset>
          <fieldset>
            <legend>Favourites</legend>
            {(favorites.data?.items ?? []).map((f) => (
              <button
                key={f.id}
                type="button"
                className="btn small ghost block"
                onClick={() =>
                  onChange({ text: "", filters: [], groupBy: (f.group_bys as string[]) ?? [], custom: [f.domain] })
                }
              >
                ★ {String(f.name)}
              </button>
            ))}
            <button type="button" className="btn small" onClick={() => void saveFavorite()}>
              Save current search
            </button>
          </fieldset>
        </div>
      )}
    </div>
  );
}
