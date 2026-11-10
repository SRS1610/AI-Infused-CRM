// Layout editors for form, list and kanban views, and creating a new view. Every change is saved as
// view-inheritance operations (one Studio change per save).
import { useMemo, useState } from "react";
import type { FieldsMeta, ListColumn, ViewDef, ViewNode } from "../../kernel/api/types";
import { t } from "../../kernel/i18n";
import { errorText, reorderOps, studio, type Op } from "./api";
import { OrderedList } from "./OrderedList";

export type LayoutType = "form" | "list" | "kanban";
type Optional = "" | "show" | "hide";

/** Field nodes of a form or kanban tree in document order. */
export function fieldNodes(node: ViewNode | undefined, out: ViewNode[] = []): ViewNode[] {
  if (!node) return out;
  if (node.kind === "field") out.push(node);
  for (const key of ["children", "pages"] as const)
    for (const child of (node[key] as ViewNode[] | undefined) ?? []) fieldNodes(child, out);
  return out;
}

interface Item {
  key: string;
  node: Record<string, unknown>;
}

function itemsOf(viewType: LayoutType, view: ViewDef | undefined): Item[] {
  if (!view) return [];
  if (viewType === "list")
    return (view.columns ?? []).map((c: ListColumn) => ({ key: c.field, node: { ...c } as Record<string, unknown> }));
  return fieldNodes(view.root)
    .filter((n) => n.name)
    .map((n) => ({ key: String(n.name), node: { ...n } }));
}

const TITLES = (): Record<LayoutType, [string, string, string]> => ({
  form: [
    t("Form layout"),
    t("Drag a field onto another to move it after it, or use the arrows."),
    t("Form layout changed in Studio"),
  ],
  list: [
    t("List columns"),
    t("Order the columns, hide them, or make them optional (users can show or hide optional columns)."),
    t("List columns changed in Studio"),
  ],
  kanban: [
    t("Kanban card"),
    t("Choose the fields shown on each card and their order."),
    t("Kanban card changed in Studio"),
  ],
});

/** Reorder (drag and drop or arrows) and hide fields; lock them (form), make columns optional (list),
 * add fields (list, kanban). */
export function LayoutEditor({
  model,
  viewType,
  view,
  fields,
  onDone,
}: {
  model: string;
  viewType: LayoutType;
  view: ViewDef | undefined;
  fields: FieldsMeta;
  onDone: () => void;
}) {
  const original = useMemo(() => itemsOf(viewType, view), [viewType, view]);
  const [order, setOrder] = useState<Item[] | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [locked, setLocked] = useState<Record<string, boolean>>({});
  const [optional, setOptional] = useState<Record<string, Optional>>({});
  const [added, setAdded] = useState<string[]>([]);
  const [candidate, setCandidate] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const items = order ?? original;
  const label = (key: string) => fields[key]?.string ?? key;
  const [title, help, summary] = TITLES()[viewType];
  const shown = new Set([...items.map((i) => i.key), ...added]);
  const addable = Object.values(fields)
    .filter((f) => !shown.has(f.name) && f.type !== "one2many" && f.type !== "binary" && f.name !== "id")
    .sort((a, b) => a.string.localeCompare(b.string));

  const operations = (): Op[] => {
    const ops: Op[] = [...hidden].map((key) => ({ op: "remove", anchor: { field: key } }));
    const nodeOf = new Map(original.map((i) => [i.key, i.node]));
    ops.push(
      ...reorderOps(
        original.map((i) => i.key).filter((k) => !hidden.has(k)),
        items.map((i) => i.key).filter((k) => !hidden.has(k)),
        (key) => ({ field: key }),
        (key) => nodeOf.get(key) ?? {},
      ),
    );
    for (const [key, value] of Object.entries(locked))
      if (!hidden.has(key) && value !== !!nodeOf.get(key)?.readonly)
        ops.push({ op: "set_attr", anchor: { field: key }, attrs: { readonly: value } });
    for (const [key, value] of Object.entries(optional))
      if (!hidden.has(key) && value !== ((nodeOf.get(key)?.optional as string | undefined) ?? ""))
        ops.push({ op: "set_attr", anchor: { field: key }, attrs: { optional: value || null } });
    for (const key of added)
      ops.push(
        viewType === "list"
          ? { op: "append_to", list: "columns", node: { field: key } }
          : { op: "append", anchor: { path: "/root" }, node: { kind: "field", name: key } },
      );
    return ops;
  };

  const save = async () => {
    const ops = operations();
    setError(null);
    if (!ops.length) {
      setError(t("Nothing to save: change the layout first."));
      return;
    }
    setBusy(true);
    try {
      await studio.changeView(model, viewType, ops, summary);
      setOrder(null);
      setHidden(new Set());
      setLocked({});
      setOptional({});
      setAdded([]);
      onDone();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="studio-card" aria-label={title}>
      <h3>{title}</h3>
      <p className="muted small">{help}</p>
      {!items.length && <p className="muted">{t("This view has no fields yet.")}</p>}
      <OrderedList
        items={items}
        keyOf={(i) => i.key}
        labelOf={(i) => label(i.key)}
        detailOf={(i) => i.key}
        onReorder={setOrder}
        className={(i) => (hidden.has(i.key) ? "hidden-field" : undefined)}
        controls={(i) => (
          <>
            {viewType === "form" && (
              <label className="checkbox small">
                <input
                  type="checkbox"
                  checked={!!(locked[i.key] ?? i.node.readonly)}
                  onChange={(e) => setLocked({ ...locked, [i.key]: e.target.checked })}
                />{" "}
                {t("Read-only")}
              </label>
            )}
            {viewType === "list" && (
              <select
                className="input small"
                aria-label={t("Visibility of {field}", { field: label(i.key) })}
                value={optional[i.key] ?? ((i.node.optional as Optional | undefined) || "")}
                onChange={(e) => setOptional({ ...optional, [i.key]: e.target.value as Optional })}
              >
                <option value="">{t("Always shown")}</option>
                <option value="show">{t("Optional, shown")}</option>
                <option value="hide">{t("Optional, hidden")}</option>
              </select>
            )}
            <label className="checkbox small">
              <input
                type="checkbox"
                checked={hidden.has(i.key)}
                onChange={(e) => {
                  const next = new Set(hidden);
                  if (e.target.checked) next.add(i.key);
                  else next.delete(i.key);
                  setHidden(next);
                }}
              />{" "}
              {t("Hide")}
            </label>
          </>
        )}
      />
      {viewType !== "form" && (
        <div className="studio-actions">
          {added.map((key) => (
            <span key={key} className="badge">
              + {label(key)}{" "}
              <button
                type="button"
                className="btn ghost small"
                aria-label={t("Do not add {field}", { field: label(key) })}
                onClick={() => setAdded(added.filter((k) => k !== key))}
              >
                ×
              </button>
            </span>
          ))}
          <select
            className="input small"
            aria-label={viewType === "list" ? t("Column to add") : t("Field to add to the card")}
            value={candidate}
            onChange={(e) => setCandidate(e.target.value)}
          >
            <option value="">{t("Choose…")}</option>
            {addable.map((f) => (
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
              setAdded([...added, candidate]);
              setCandidate("");
            }}
          >
            {viewType === "list" ? t("Add column") : t("Add to the card")}
          </button>
        </div>
      )}
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
      <button type="button" className="btn primary" disabled={busy} onClick={() => void save()}>
        {t("Save layout")}
      </button>
    </section>
  );
}

/** A new form, list or kanban view starting from the generated default layout. */
export function NewView({ model, onDone }: { model: string; onDone: () => void }) {
  const [viewType, setViewType] = useState<LayoutType>("list");
  const [name, setName] = useState("");
  const [makeDefault, setMakeDefault] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await studio.createView({ model, view_type: viewType, name, make_default: makeDefault });
      setName("");
      onDone();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="studio-card" onSubmit={submit} aria-label={t("Create a view")}>
      <h3>{t("Create a view")}</h3>
      <p className="muted small">
        {t("The new view starts from the layout generated from the model's fields; edit it with the layout editor.")}
      </p>
      <label className="field">
        <span>{t("View type")}</span>
        <select className="input" value={viewType} onChange={(e) => setViewType(e.target.value as LayoutType)}>
          <option value="form">{t("Form")}</option>
          <option value="list">{t("List")}</option>
          <option value="kanban">{t("Kanban")}</option>
        </select>
      </label>
      <label className="field">
        <span>{t("View name")}</span>
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="checkbox">
        <input type="checkbox" checked={makeDefault} onChange={(e) => setMakeDefault(e.target.checked)} />{" "}
        {t("Use it as the default view")}
      </label>
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
      <button type="submit" className="btn primary" disabled={busy}>
        {t("Create view")}
      </button>
    </form>
  );
}
