import { useQuery } from "@tanstack/react-query";
import { useCallback, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type { ActionDef, RecordData } from "../api/types";
import { and } from "../domain";
import { useHotkeys } from "../hotkeys";
import { ActivityDashboard } from "../chatter/ActivityDashboard";
import { ExportDialog } from "../dataio/ExportDialog";
import { clientAction, registerClientAction } from "../extensions";
import { CalendarView } from "./CalendarView";
import { FormView } from "./FormView";
import { GraphView } from "./GraphView";
import { KanbanView } from "./KanbanView";
import { ListView } from "./ListView";
import { PivotView } from "./PivotView";
import { emptySearch, SearchBar, type SearchState } from "./SearchBar";
import { buildDomain } from "./searchDomain";
import { t } from "../i18n";

const LATER = new Set(["activity", "gantt", "map"]);

/** Display names of the view types (the API values stay in English). */
function viewTypeLabel(type: string): string {
  const labels: Record<string, string> = {
    list: t("List"),
    kanban: t("Kanban"),
    calendar: t("Calendar"),
    pivot: t("Pivot"),
    graph: t("Graph"),
    activity: t("Activity"),
    gantt: t("Gantt"),
    map: t("Map"),
  };
  return labels[type] ?? type[0].toUpperCase() + type.slice(1);
}
registerClientAction("activities", () => <ActivityDashboard />);

/** Defaults passed through the action context (`default_<field>`). */
function contextDefaults(action: ActionDef): RecordData {
  return Object.fromEntries(
    Object.entries(action.context ?? {})
      .filter(([k]) => k.startsWith("default_"))
      .map(([k, v]) => [k.slice(8), v]),
  );
}

export function ActionView() {
  const { actionRef = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const searchInput = useRef<HTMLInputElement>(null);
  const [search, setSearch] = useState<SearchState>(emptySearch);
  const [exporting, setExporting] = useState(false);

  const action = useQuery({ queryKey: ["action", actionRef], queryFn: () => api.action(actionRef) });
  const types = useMemo(() => {
    const declared = action.data?.view_types ?? ["list", "form"];
    return [...new Set([...declared, "form", "search"])];
  }, [action.data]);
  const refs = useMemo(
    () => ({
      ...(action.data?.view_refs ?? {}),
      ...(action.data?.search_view_ref ? { search: action.data.search_view_ref } : {}),
    }),
    [action.data],
  );
  const views = useQuery({
    queryKey: ["views", action.data?.model, types.join(","), JSON.stringify(refs)],
    queryFn: () => api.views(action.data!.model!, types, refs),
    enabled: !!action.data?.model,
  });

  const recordParam = params.get("id");
  const recordId = recordParam === "new" ? null : recordParam ? Number(recordParam) : undefined;
  const firstMulti = (action.data?.view_types ?? ["list"]).find((vt) => vt !== "form") ?? "list";
  const viewType = recordParam ? "form" : (params.get("view") ?? firstMulti);

  const go = useCallback(
    (next: Record<string, string | null>) => {
      const p = new URLSearchParams(params);
      for (const [k, v] of Object.entries(next))
        if (v === null) p.delete(k);
        else p.set(k, v);
      setParams(p);
    },
    [params, setParams],
  );

  useHotkeys({
    "alt+n": () => action.data && views.data?.model.access.create && go({ id: "new" }),
    "/": () => searchInput.current?.focus(),
  });

  if (action.isLoading || (action.data?.model && views.isLoading))
    return (
      <div className="loading" role="status">
        {t("Loading…")}
      </div>
    );
  if (action.isError)
    return (
      <div className="alert error" role="alert">
        {(action.error as Error).message}
      </div>
    );
  const def = action.data!;
  if (def.kind === "url" && def.url) {
    window.location.assign(def.url);
    return null;
  }
  const client = def.kind === "client" ? clientAction(def.client_tag) : undefined;
  if (client) return client();
  if (def.kind !== "window" || !def.model)
    return (
      <div className="alert info">
        {t("This action type ({kind}) is not available in the browser yet.", { kind: def.kind })}
      </div>
    );
  if (views.isError || !views.data)
    return (
      <div className="alert error" role="alert">
        {(views.error as Error | undefined)?.message ?? t("Cannot load views")}
      </div>
    );
  const meta = views.data;
  const domain = and(def.domain as any, buildDomain(meta, meta.views.search, search));

  return (
    <div className="action-view">
      {viewType !== "form" && (
        <div className="control-panel">
          <h1 className="page-title">{def.name}</h1>
          {meta.model.access.create && (
            <button type="button" className="btn primary" onClick={() => go({ id: "new" })} title={t("New (Alt+N)")}>
              {t("New")}
            </button>
          )}
          {meta.model.access.create && (
            <button type="button" className="btn ghost" onClick={() => navigate(`/import/${def.model}`)}>
              {t("Import")}
            </button>
          )}
          <button type="button" className="btn ghost" onClick={() => setExporting(true)}>
            {t("Export")}
          </button>
          <SearchBar
            model={def.model}
            meta={meta}
            view={meta.views.search}
            value={search}
            onChange={setSearch}
            inputRef={searchInput}
          />
          <div className="view-switcher" role="tablist" aria-label={t("View type")}>
            {def.view_types
              .filter((vt) => vt !== "form")
              .map((vt) => (
                <button
                  key={vt}
                  type="button"
                  role="tab"
                  aria-selected={viewType === vt}
                  className={viewType === vt ? "btn small active" : "btn small ghost"}
                  onClick={() => go({ view: vt, id: null })}
                >
                  {viewTypeLabel(vt)}
                </button>
              ))}
          </div>
        </div>
      )}
      {viewType === "form" && meta.views.form && (
        <FormView
          model={def.model}
          recordId={recordId ?? null}
          meta={meta}
          view={meta.views.form}
          defaults={contextDefaults(def)}
          onSaved={(id) => go({ id: String(id) })}
          onBack={() => go({ id: null })}
        />
      )}
      {viewType === "list" && meta.views.list && (
        <ListView
          model={def.model}
          meta={meta}
          view={meta.views.list}
          domain={domain}
          groupBy={search.groupBy}
          context={def.context}
          pageSize={def.limit || 80}
          onOpen={(id) => go({ id: String(id) })}
        />
      )}
      {viewType === "kanban" && meta.views.kanban && (
        <KanbanView
          model={def.model}
          meta={meta}
          view={meta.views.kanban}
          domain={domain}
          groupBy={search.groupBy}
          onOpen={(id) => go({ id: String(id) })}
        />
      )}
      {viewType === "calendar" && meta.views.calendar && (
        <CalendarView
          model={def.model}
          meta={meta}
          view={meta.views.calendar}
          domain={domain}
          onOpen={(id) => go({ id: String(id) })}
        />
      )}
      {viewType === "pivot" && meta.views.pivot && (
        <PivotView model={def.model} meta={meta} view={meta.views.pivot} domain={domain} />
      )}
      {viewType === "graph" && meta.views.graph && (
        <GraphView model={def.model} meta={meta} view={meta.views.graph} domain={domain} />
      )}
      {LATER.has(viewType) && (
        <div className="alert info">
          {t("The {view} view arrives with a later phase; pick another view above.", { view: viewTypeLabel(viewType) })}
        </div>
      )}
      {exporting && (
        <ExportDialog
          model={def.model}
          domain={domain}
          initial={(meta.views.list?.columns ?? []).map((c) => c.field).filter((f) => f in meta.fields)}
          onClose={() => setExporting(false)}
        />
      )}
      {!meta.views[viewType] && !LATER.has(viewType) && viewType !== "form" && (
        <button type="button" className="btn" onClick={() => navigate(-1)}>
          {t("Back")}
        </button>
      )}
    </div>
  );
}
