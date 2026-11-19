// The predefined filters (name + condition) and group-by options of a model's search view: add,
// remove, reorder and edit them; saving stores one set of view-inheritance operations.
import { useMemo, useState } from "react";
import type { Domain, FieldsMeta, SearchFilterDef, ViewDef } from "../../kernel/api/types";
import { t } from "../../kernel/i18n";
import { ConditionEditor, ConditionSummary } from "../../kernel/views/ConditionEditor";
import { errorText, reorderOps, slugify, studio, type Op } from "./api";
import { OrderedList } from "./OrderedList";

type GroupBy = { field: string; label?: string };
const GROUPABLE = new Set(["many2one", "selection", "date", "datetime", "char", "boolean"]);

function FilterForm({
  fields,
  initial,
  submitLabel,
  onSubmit,
  onCancel,
}: {
  fields: FieldsMeta;
  initial?: SearchFilterDef;
  submitLabel: string;
  onSubmit: (label: string, domain: Domain) => void;
  onCancel?: () => void;
}) {
  const [label, setLabel] = useState(initial?.label ?? "");
  const [domain, setDomain] = useState<Domain | null>(initial?.domain ?? null);
  return (
    <div className="studio-subform" role="group" aria-label={initial ? t("Edit filter") : t("New filter")}>
      <label className="field">
        <span>{t("Filter name")}</span>
        <input className="input" value={label} onChange={(e) => setLabel(e.target.value)} />
      </label>
      <ConditionEditor fields={fields} value={domain} onChange={setDomain} label={t("Records matching")} />
      <div className="studio-actions">
        <button
          type="button"
          className="btn small"
          disabled={!label.trim() || !domain}
          onClick={() => {
            onSubmit(label.trim(), domain!);
            setLabel("");
            setDomain(null);
          }}
        >
          {submitLabel}
        </button>
        {onCancel && (
          <button type="button" className="btn small ghost" onClick={onCancel}>
            {t("Cancel")}
          </button>
        )}
      </div>
    </div>
  );
}

export function SearchEditor({
  model,
  view,
  fields,
  onDone,
}: {
  model: string;
  view: ViewDef | undefined;
  fields: FieldsMeta;
  onDone: () => void;
}) {
  const originalFilters = useMemo(() => (view?.filters ?? []).filter((f) => f.name), [view]);
  const originalGroups = useMemo(() => (view?.group_bys ?? []) as GroupBy[], [view]);
  const [filters, setFilters] = useState<SearchFilterDef[]>(originalFilters);
  const [groups, setGroups] = useState<GroupBy[]>(originalGroups);
  const [editing, setEditing] = useState<string | null>(null);
  const [candidate, setCandidate] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const groupLabel = (g: GroupBy) => g.label ?? fields[g.field.split(":")[0]]?.string ?? g.field;
  const groupable = Object.values(fields)
    .filter((f) => f.store && GROUPABLE.has(f.type) && !groups.some((g) => g.field === f.name))
    .sort((a, b) => a.string.localeCompare(b.string));

  const operations = (): Op[] => {
    const ops: Op[] = [];
    const keptFilters = filters.filter((f) => originalFilters.some((o) => o.name === f.name));
    for (const f of originalFilters)
      if (!filters.some((x) => x.name === f.name)) ops.push({ op: "remove", anchor: { name: f.name } });
    ops.push(
      ...reorderOps(
        originalFilters.map((f) => f.name).filter((n) => keptFilters.some((k) => k.name === n)),
        keptFilters.map((f) => f.name),
        (name) => ({ name }),
        (name) => originalFilters.find((f) => f.name === name)! as unknown as Op,
      ),
    );
    for (const f of keptFilters) {
      const o = originalFilters.find((x) => x.name === f.name)!;
      if (o.label !== f.label || JSON.stringify(o.domain) !== JSON.stringify(f.domain))
        ops.push({ op: "set_attr", anchor: { name: f.name }, attrs: { label: f.label, domain: f.domain } });
    }
    for (const f of filters.filter((x) => !originalFilters.some((o) => o.name === x.name)))
      ops.push({ op: "append_to", list: "filters", node: f });
    const keptGroups = groups.filter((g) => originalGroups.some((o) => o.field === g.field));
    for (const g of originalGroups)
      if (!groups.some((x) => x.field === g.field)) ops.push({ op: "remove", anchor: { field: g.field } });
    ops.push(
      ...reorderOps(
        originalGroups.map((g) => g.field).filter((n) => keptGroups.some((k) => k.field === n)),
        keptGroups.map((g) => g.field),
        (field) => ({ field }),
        (field) => originalGroups.find((g) => g.field === field)! as unknown as Op,
      ),
    );
    for (const g of groups.filter((x) => !originalGroups.some((o) => o.field === x.field)))
      ops.push({ op: "append_to", list: "group_bys", node: g });
    return ops;
  };

  const save = async () => {
    const ops = operations();
    setError(null);
    if (!ops.length) {
      setError(t("Nothing to save: change the filters first."));
      return;
    }
    setBusy(true);
    try {
      await studio.changeView(model, "search", ops, t("Search filters changed in Studio"));
      onDone();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const newName = (label: string) => {
    const base = `x_${slugify(label) || "filter"}`;
    let name = base;
    for (let i = 2; filters.some((f) => f.name === name) || originalFilters.some((f) => f.name === name); i++)
      name = `${base}_${i}`;
    return name;
  };

  return (
    <section className="studio-card" aria-label={t("Search filters")}>
      <h3>{t("Search filters")}</h3>
      <p className="muted small">{t("Predefined filters and grouping options offered in the search panel.")}</p>
      <h4>{t("Filters")}</h4>
      {!filters.length && <p className="muted">{t("No predefined filters.")}</p>}
      <OrderedList
        label={t("Filters")}
        items={filters}
        keyOf={(f) => f.name}
        labelOf={(f) => f.label}
        detailOf={(f) => <ConditionSummary fields={fields} domain={f.domain} />}
        onReorder={setFilters}
        controls={(f) => (
          <>
            <button
              type="button"
              className="btn ghost small"
              aria-label={t("Edit filter {name}", { name: f.label })}
              onClick={() => setEditing(editing === f.name ? null : f.name)}
            >
              {t("Edit")}
            </button>
            <button
              type="button"
              className="btn ghost small"
              aria-label={t("Remove filter {name}", { name: f.label })}
              onClick={() => setFilters(filters.filter((x) => x.name !== f.name))}
            >
              ×
            </button>
          </>
        )}
      />
      {editing && filters.some((f) => f.name === editing) && (
        <FilterForm
          key={editing}
          fields={fields}
          initial={filters.find((f) => f.name === editing)}
          submitLabel={t("Apply")}
          onSubmit={(label, domain) => {
            setFilters(filters.map((f) => (f.name === editing ? { ...f, label, domain } : f)));
            setEditing(null);
          }}
          onCancel={() => setEditing(null)}
        />
      )}
      <FilterForm
        fields={fields}
        submitLabel={t("Add filter")}
        onSubmit={(label, domain) => setFilters([...filters, { name: newName(label), label, domain }])}
      />
      <h4>{t("Group by")}</h4>
      {!groups.length && <p className="muted">{t("No grouping options.")}</p>}
      <OrderedList
        label={t("Group by")}
        items={groups}
        keyOf={(g) => g.field}
        labelOf={groupLabel}
        onReorder={setGroups}
        controls={(g) => (
          <button
            type="button"
            className="btn ghost small"
            aria-label={t("Remove grouping {name}", { name: groupLabel(g) })}
            onClick={() => setGroups(groups.filter((x) => x.field !== g.field))}
          >
            ×
          </button>
        )}
      />
      <div className="studio-actions">
        <select
          className="input small"
          aria-label={t("Grouping to add")}
          value={candidate}
          onChange={(e) => setCandidate(e.target.value)}
        >
          <option value="">{t("Choose…")}</option>
          {groupable.map((f) => (
            <option key={f.name} value={f.name}>
              {f.string}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="btn small"
          disabled={!candidate}
          onClick={() => {
            setGroups([...groups, { field: candidate }]);
            setCandidate("");
          }}
        >
          {t("Add grouping")}
        </button>
      </div>
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
      <button type="button" className="btn primary" disabled={busy} onClick={() => void save()}>
        {t("Save search filters")}
      </button>
    </section>
  );
}
