// Studio: customise models without code. Every change is recorded and can be rolled back.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState } from "react";
import { api } from "../../kernel/api/client";
import type { ViewDef } from "../../kernel/api/types";
import { registerClientAction } from "../../kernel/extensions";
import { t } from "../../kernel/i18n";
import { errorText, studio, type StudioFieldInfo } from "./api";
import { AddField, EditField } from "./FieldEditors";
import { LayoutEditor, NewView, type LayoutType } from "./LayoutEditors";
import { SearchEditor } from "./SearchEditor";
import { WorkflowBuilder } from "./WorkflowBuilder";

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
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
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

type Tab = "fields" | "views" | "search" | "automations" | "workflow";
const TABS = (): [Tab, string][] => [
  ["fields", t("Fields")],
  ["views", t("Views")],
  ["search", t("Search")],
  ["automations", t("Automations")],
  ["workflow", t("Workflow")],
];
const LAYOUTS = (): [LayoutType, string][] => [
  ["form", t("Form")],
  ["list", t("List")],
  ["kanban", t("Kanban")],
];

export function StudioApp() {
  const qc = useQueryClient();
  const models = useQuery({ queryKey: ["studio", "models"], queryFn: studio.models });
  const [model, setModel] = useState("crm.lead");
  const [tab, setTab] = useState<Tab>("fields");
  const [layout, setLayout] = useState<LayoutType>("form");
  const baseId = useId();
  const fields = useQuery({
    queryKey: ["studio", "fields", model],
    queryFn: () => studio.fields(model),
    enabled: !!model,
  });
  const views = useQuery({
    queryKey: ["studio", "views", model],
    queryFn: () => api.views(model, ["form", "list", "kanban", "search"]),
    enabled: !!model,
  });
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["studio"] });
    void qc.invalidateQueries({ queryKey: ["views", model] });
  };
  const meta = views.data?.fields ?? {};
  const viewOf = (type: string) => views.data?.views?.[type] as ViewDef | undefined;
  const tabs = TABS();
  const panel = () => {
    switch (tab) {
      case "fields":
        return (
          <>
            <AddField model={model} fields={fields.data ?? []} meta={meta} onDone={refresh} />
            <EditField model={model} fields={fields.data ?? []} meta={meta} onDone={refresh} />
          </>
        );
      case "views": {
        const view = viewOf(layout);
        return (
          <>
            <div className="view-switcher" role="radiogroup" aria-label={t("View to edit")}>
              {LAYOUTS().map(([type, label]) => (
                <button
                  key={type}
                  type="button"
                  role="radio"
                  aria-checked={layout === type}
                  className={layout === type ? "btn small primary" : "btn small"}
                  onClick={() => setLayout(type)}
                >
                  {label}
                </button>
              ))}
            </div>
            <LayoutEditor
              key={`${model}-${layout}-${views.dataUpdatedAt}`}
              model={model}
              viewType={layout}
              view={view}
              fields={meta}
              onDone={refresh}
            />
            <NewView model={model} onDone={refresh} />
          </>
        );
      }
      case "search": {
        const view = viewOf("search");
        return (
          <SearchEditor
            key={`${model}-${views.dataUpdatedAt}`}
            model={model}
            view={view}
            fields={meta}
            onDone={refresh}
          />
        );
      }
      case "automations":
        return <AutomationForm model={model} fields={fields.data ?? []} onDone={refresh} />;
      case "workflow":
        return <WorkflowBuilder key={model} model={model} fields={meta} onDone={refresh} />;
    }
  };
  return (
    <div className="page studio-app">
      <header className="page-header">
        <h1>{t("Studio")}</h1>
        <label className="field inline">
          <span>{t("Model")}</span>
          <select className="input" aria-label={t("Model")} value={model} onChange={(e) => setModel(e.target.value)}>
            {(models.data ?? []).map((m) => (
              <option key={m.model} value={m.model}>
                {m.description} ({m.model})
              </option>
            ))}
          </select>
        </label>
      </header>
      {models.error && <div className="alert error">{errorText(models.error)}</div>}
      <div className="studio-main">
        <div className="studio-work">
          <div role="tablist" className="tabs" aria-label={t("Studio sections")}>
            {tabs.map(([key, label], i) => (
              <button
                key={key}
                type="button"
                role="tab"
                id={`${baseId}-tab-${key}`}
                aria-selected={tab === key}
                aria-controls={`${baseId}-panel`}
                tabIndex={tab === key ? 0 : -1}
                className={tab === key ? "tab active" : "tab"}
                onClick={() => setTab(key)}
                onKeyDown={(e) => {
                  const delta = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
                  if (!delta) return;
                  const next = tabs[(i + delta + tabs.length) % tabs.length][0];
                  setTab(next);
                  document.getElementById(`${baseId}-tab-${next}`)?.focus();
                }}
              >
                {label}
              </button>
            ))}
          </div>
          <div
            role="tabpanel"
            id={`${baseId}-panel`}
            aria-labelledby={`${baseId}-tab-${tab}`}
            className="studio-grid tab-panel"
          >
            {panel()}
          </div>
        </div>
        <History model={model} onDone={refresh} />
      </div>
    </div>
  );
}

registerClientAction("studio", () => <StudioApp />);
