// Studio: customise models without code. Every change is recorded and can be rolled back.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api, ApiError, request } from "../../kernel/api/client";
import type { ViewDef, ViewNode } from "../../kernel/api/types";
import { registerClientAction } from "../../kernel/extensions";
import { t } from "../../kernel/i18n";

interface StudioModel {
  model: string;
  description: string;
  module: string;
}
interface StudioFieldInfo {
  name: string;
  label: string;
  type: string;
  studio: boolean;
}
interface Change {
  id: number;
  kind: string;
  model: string | null;
  summary: string;
  state: "applied" | "rolled_back";
  user: string | null;
  date: string | null;
}
type Op = Record<string, unknown>;

const fieldTypes = (): [string, string][] => [
  ["char", t("Text")],
  ["text", t("Long text")],
  ["html", t("Rich text")],
  ["integer", t("Whole number")],
  ["float", t("Decimal number")],
  ["monetary", t("Amount")],
  ["boolean", t("Checkbox")],
  ["date", t("Date")],
  ["datetime", t("Date and time")],
  ["selection", t("Choice")],
  ["many2one", t("Link to one record")],
  ["many2many", t("Links to several records")],
];

const studio = {
  models: () => request<StudioModel[]>("GET", "/studio/models"),
  fields: (model: string) => request<StudioFieldInfo[]>("GET", `/studio/models/${model}/fields`),
  addField: (body: unknown) => request<{ field: string; change_id: number }>("POST", "/studio/fields", { body }),
  changeView: (model: string, viewType: string, operations: Op[], summary: string) =>
    request<Change>("POST", `/studio/views/${model}/${viewType}/operations`, { body: { operations, summary } }),
  addAutomation: (values: unknown) => request<Change>("POST", "/studio/automations", { body: values }),
  changes: (model?: string) => request<Change[]>("GET", "/studio/changes", { query: { model } }),
  rollback: (id: number) => request<Change>("POST", `/studio/changes/${id}/rollback`),
  snapshot: (name: string) => request<{ id: number }>("POST", "/studio/snapshots", { query: { name } }),
  snapshots: () => request<{ id: number; name: string; date: string }[]>("GET", "/studio/snapshots"),
  rollbackTo: (id: number) => request<{ rolled_back: number }>("POST", `/studio/snapshots/${id}/rollback`),
};

const errorText = (e: unknown) => (e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e));
const slug = (label: string) =>
  "x_" +
  label
    .toLowerCase()
    .normalize("NFD")
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_|_$/g, "")
    .slice(0, 40);

/** Field nodes of a form view in document order. */
function formFieldNodes(node: ViewNode | undefined, out: ViewNode[] = []): ViewNode[] {
  if (!node) return out;
  if (node.kind === "field") out.push(node);
  for (const child of (node.children as ViewNode[] | undefined) ?? []) formFieldNodes(child, out);
  return out;
}

function AddField({ model, fields, onDone }: { model: string; fields: StudioFieldInfo[]; onDone: () => void }) {
  const [label, setLabel] = useState("");
  const [type, setType] = useState("char");
  const [after, setAfter] = useState("");
  const [inList, setInList] = useState(true);
  const [options, setOptions] = useState("");
  const [comodel, setComodel] = useState("");
  const [required, setRequired] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const models = useQuery({ queryKey: ["studio", "models"], queryFn: studio.models });
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const opts: Record<string, unknown> = { required };
    if (type === "selection")
      opts.selection = options
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean)
        .map((s) => [slug(s).slice(2), s]);
    if (type === "many2one" || type === "many2many") opts.comodel = comodel;
    const placements = [{ view_type: "form", ...(after ? { after } : {}) }, ...(inList ? [{ view_type: "list" }] : [])];
    try {
      await studio.addField({ model, name: slug(label), type, label, placements, options: opts });
      setLabel("");
      onDone();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="studio-card" onSubmit={submit} aria-label={t("Add a field")}>
      <h3>{t("Add a field")}</h3>
      <label className="field">
        <span>{t("Label")}</span>
        <input className="input" required value={label} onChange={(e) => setLabel(e.target.value)} />
      </label>
      <p className="muted small">{t("Technical name: {name}", { name: label ? slug(label) : "—" })}</p>
      <label className="field">
        <span>{t("Type")}</span>
        <select className="input" value={type} onChange={(e) => setType(e.target.value)}>
          {fieldTypes().map(([v, l]) => (
            <option key={v} value={v}>
              {l}
            </option>
          ))}
        </select>
      </label>
      {type === "selection" && (
        <label className="field">
          <span>{t("Options (comma separated)")}</span>
          <input className="input" required value={options} onChange={(e) => setOptions(e.target.value)} />
        </label>
      )}
      {(type === "many2one" || type === "many2many") && (
        <label className="field">
          <span>{t("Linked model")}</span>
          <select className="input" required value={comodel} onChange={(e) => setComodel(e.target.value)}>
            <option value="">{t("Choose…")}</option>
            {(models.data ?? []).map((m) => (
              <option key={m.model} value={m.model}>
                {m.description}
              </option>
            ))}
          </select>
        </label>
      )}
      <label className="field">
        <span>{t("Place on the form after")}</span>
        <select className="input" value={after} onChange={(e) => setAfter(e.target.value)}>
          <option value="">{t("End of the form")}</option>
          {fields.map((f) => (
            <option key={f.name} value={f.name}>
              {f.label}
            </option>
          ))}
        </select>
      </label>
      <label className="checkbox">
        <input type="checkbox" checked={inList} onChange={(e) => setInList(e.target.checked)} /> {t("Show in the list")}
      </label>
      <label className="checkbox">
        <input type="checkbox" checked={required} onChange={(e) => setRequired(e.target.checked)} /> {t("Required")}
      </label>
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
      <button type="submit" className="btn primary" disabled={busy || !label}>
        {busy ? t("Adding…") : t("Add field")}
      </button>
    </form>
  );
}

/** Reorder (drag and drop or the arrow buttons), hide or lock fields of the form view. Saving stores
 * the changes as view-inheritance operations. */
function LayoutEditor({ model, view, onDone }: { model: string; view: ViewDef | undefined; onDone: () => void }) {
  const original = useMemo(() => formFieldNodes(view?.root), [view]);
  const [order, setOrder] = useState<ViewNode[] | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [locked, setLocked] = useState<Record<string, boolean>>({});
  const [dragging, setDragging] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const items = order ?? original;
  const name = (n: ViewNode) => String(n.name);
  const move = (from: string, to: string) => {
    if (from === to) return;
    const list = [...items];
    const node = list.splice(
      list.findIndex((n) => name(n) === from),
      1,
    )[0];
    const at = list.findIndex((n) => name(n) === to);
    list.splice(at + 1, 0, node);
    setOrder(list);
  };
  const step = (n: string, delta: -1 | 1) => {
    const i = items.findIndex((x) => name(x) === n);
    const j = i + delta;
    if (j < 0 || j >= items.length) return;
    const list = [...items];
    [list[i], list[j]] = [list[j], list[i]];
    setOrder(list);
  };
  const save = async () => {
    const ops: Op[] = [];
    for (const n of hidden) ops.push({ op: "remove", anchor: { field: n } });
    const visible = items.filter((n) => !hidden.has(name(n)));
    const origVisible = original.filter((n) => !hidden.has(name(n))).map(name);
    visible.forEach((n, i) => {
      if (i === 0 || origVisible.indexOf(name(n)) === i) return;
      ops.push({ op: "remove", anchor: { field: name(n) } });
      ops.push({ op: "insert_after", anchor: { field: name(visible[i - 1]) }, node: n });
    });
    for (const [field, value] of Object.entries(locked))
      if (!hidden.has(field)) ops.push({ op: "set_attr", anchor: { field }, attrs: { readonly: value } });
    if (!ops.length) return;
    try {
      await studio.changeView(model, "form", ops, t("Form layout changed in Studio"));
      setOrder(null);
      setHidden(new Set());
      setLocked({});
      onDone();
    } catch (e) {
      setError(errorText(e));
    }
  };
  return (
    <section className="studio-card" aria-label={t("Form layout")}>
      <h3>{t("Form layout")}</h3>
      <p className="muted small">{t("Drag a field onto another to move it after it, or use the arrows.")}</p>
      <ol className="studio-layout">
        {items.map((n) => (
          <li
            key={name(n)}
            draggable
            onDragStart={() => setDragging(name(n))}
            onDragOver={(e) => e.preventDefault()}
            onDrop={() => {
              if (dragging) move(dragging, name(n));
              setDragging(null);
            }}
            className={hidden.has(name(n)) ? "hidden-field" : undefined}
          >
            <span className="studio-handle" aria-hidden="true">
              ⠿
            </span>
            <span className="studio-name">{name(n)}</span>
            <button
              type="button"
              className="btn ghost small"
              aria-label={t("Move {field} up", { field: name(n) })}
              onClick={() => step(name(n), -1)}
            >
              ↑
            </button>
            <button
              type="button"
              className="btn ghost small"
              aria-label={t("Move {field} down", { field: name(n) })}
              onClick={() => step(name(n), 1)}
            >
              ↓
            </button>
            <label className="checkbox small">
              <input
                type="checkbox"
                checked={!!(locked[name(n)] ?? n.readonly)}
                onChange={(e) => setLocked({ ...locked, [name(n)]: e.target.checked })}
              />{" "}
              {t("Read-only")}
            </label>
            <label className="checkbox small">
              <input
                type="checkbox"
                checked={hidden.has(name(n))}
                onChange={(e) => {
                  const next = new Set(hidden);
                  if (e.target.checked) next.add(name(n));
                  else next.delete(name(n));
                  setHidden(next);
                }}
              />{" "}
              {t("Hide")}
            </label>
          </li>
        ))}
      </ol>
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
      <button type="button" className="btn primary" onClick={() => void save()}>
        {t("Save layout")}
      </button>
    </section>
  );
}

function AutomationForm({ model, fields, onDone }: { model: string; fields: StudioFieldInfo[]; onDone: () => void }) {
  const [name, setName] = useState("");
  const [watched, setWatched] = useState("stage_id");
  const [summary, setSummary] = useState(() => t("Follow up"));
  const [days, setDays] = useState(1);
  const [error, setError] = useState<string | null>(null);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      await studio.addAutomation({
        name,
        model_name: model,
        trigger: "on_write",
        watched_fields: [watched],
        actions: [{ type: "schedule_activity", summary: JSON.stringify(summary), days }],
      });
      setName("");
      onDone();
    } catch (err) {
      setError(errorText(err));
    }
  };
  return (
    <form className="studio-card" onSubmit={submit} aria-label={t("Add an automated action")}>
      <h3>{t("Automated action")}</h3>
      <label className="field">
        <span>{t("Name")}</span>
        <input className="input" required value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="field">
        <span>{t("When this field changes")}</span>
        <select className="input" value={watched} onChange={(e) => setWatched(e.target.value)}>
          {fields.map((f) => (
            <option key={f.name} value={f.name}>
              {f.label}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span>{t("Schedule a to-do")}</span>
        <input className="input" required value={summary} onChange={(e) => setSummary(e.target.value)} />
      </label>
      <label className="field">
        <span>{t("Due in (days)")}</span>
        <input className="input" type="number" min={0} value={days} onChange={(e) => setDays(Number(e.target.value))} />
      </label>
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
      <button type="submit" className="btn primary" disabled={!name}>
        {t("Add automation")}
      </button>
    </form>
  );
}

function History({ model, onDone }: { model: string; onDone: () => void }) {
  const qc = useQueryClient();
  const changes = useQuery({ queryKey: ["studio", "changes", model], queryFn: () => studio.changes(model) });
  const snapshots = useQuery({ queryKey: ["studio", "snapshots"], queryFn: studio.snapshots });
  const [error, setError] = useState<string | null>(null);
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["studio"] });
    onDone();
  };
  const act = async (fn: () => Promise<unknown>) => {
    setError(null);
    try {
      await fn();
      refresh();
    } catch (e) {
      setError(errorText(e));
    }
  };
  return (
    <section className="studio-card" aria-label={t("Change history")}>
      <h3>{t("Change history")}</h3>
      <div className="studio-actions">
        <button
          type="button"
          className="btn small"
          onClick={() => void act(() => studio.snapshot(t("Snapshot {date}", { date: new Date().toLocaleString() })))}
        >
          {t("Take a snapshot")}
        </button>
        <a className="btn small" href="/api/v1/studio/export" download>
          {t("Export as module")}
        </a>
      </div>
      {error && <div className="alert error">{error}</div>}
      <ul className="studio-changes">
        {(changes.data ?? []).map((c) => (
          <li key={c.id} className={c.state}>
            <span>{c.summary}</span>
            <span className="muted small">
              {c.user} · {c.date ? new Date(c.date).toLocaleString() : ""}
            </span>
            {c.state === "applied" ? (
              <button type="button" className="btn ghost small" onClick={() => void act(() => studio.rollback(c.id))}>
                {t("Roll back")}
              </button>
            ) : (
              <span className="badge">{t("rolled back")}</span>
            )}
          </li>
        ))}
      </ul>
      {(snapshots.data?.length ?? 0) > 0 && (
        <>
          <h4>{t("Snapshots")}</h4>
          <ul className="studio-changes">
            {snapshots.data!.map((s) => (
              <li key={s.id}>
                <span>{s.name}</span>
                <button
                  type="button"
                  className="btn ghost small"
                  onClick={() => void act(() => studio.rollbackTo(s.id))}
                >
                  {t("Roll back to here")}
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

export function StudioApp() {
  const qc = useQueryClient();
  const models = useQuery({ queryKey: ["studio", "models"], queryFn: studio.models });
  const [model, setModel] = useState("crm.lead");
  const fields = useQuery({
    queryKey: ["studio", "fields", model],
    queryFn: () => studio.fields(model),
    enabled: !!model,
  });
  const views = useQuery({
    queryKey: ["studio", "views", model],
    queryFn: () => api.views(model, ["form", "list"]),
    enabled: !!model,
  });
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["studio"] });
    void qc.invalidateQueries({ queryKey: ["views", model] });
  };
  const formView = views.data?.views?.form as ViewDef | undefined;
  return (
    <div className="page studio-app">
      <header className="page-header">
        <h1>{t("Studio")}</h1>
        <label className="field inline">
          <span>{t("Model")}</span>
          <select className="input" value={model} onChange={(e) => setModel(e.target.value)}>
            {(models.data ?? []).map((m) => (
              <option key={m.model} value={m.model}>
                {m.description} ({m.model})
              </option>
            ))}
          </select>
        </label>
      </header>
      {models.error && <div className="alert error">{errorText(models.error)}</div>}
      <div className="studio-grid">
        <AddField model={model} fields={fields.data ?? []} onDone={refresh} />
        <LayoutEditor key={`${model}-${formView?.view_id}`} model={model} view={formView} onDone={refresh} />
        <AutomationForm model={model} fields={fields.data ?? []} onDone={refresh} />
        <History model={model} onDone={refresh} />
      </div>
    </div>
  );
}

registerClientAction("studio", () => <StudioApp />);
