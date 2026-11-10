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

const LATER = new Set(["activity", "gantt", "map"]);
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
    const t = action.data?.view_types ?? ["list", "form"];
    return [...new Set([...t, "form", "search"])];
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
  const firstMulti = (action.data?.view_types ?? ["list"]).find((t) => t !== "form") ?? "list";
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
        Loading…
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
    return <div className="alert info">This action type ({def.kind}) is not available in the browser yet.</div>;
  if (views.isError || !views.data)
    return (
      <div className="alert error" role="alert">
        {(views.error as Error | undefined)?.message ?? "Cannot load views"}
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
            <button type="button" className="btn primary" onClick={() => go({ id: "new" })} title="New (Alt+N)">
              New
            </button>
          )}
          {meta.model.access.create && (
            <button type="button" className="btn ghost" onClick={() => navigate(`/import/${def.model}`)}>
              Import
            </button>
          )}
          <button type="button" className="btn ghost" onClick={() => setExporting(true)}>
            Export
          </button>
          <SearchBar
            model={def.model}
            meta={meta}
            view={meta.views.search}
            value={search}
            onChange={setSearch}
            inputRef={searchInput}
          />
          <div className="view-switcher" role="tablist" aria-label="View type">
            {def.view_types
              .filter((t) => t !== "form")
              .map((t) => (
                <button
                  key={t}
                  type="button"
                  role="tab"
                  aria-selected={viewType === t}
                  className={viewType === t ? "btn small active" : "btn small ghost"}
                  onClick={() => go({ view: t, id: null })}
                >
                  {t[0].toUpperCase() + t.slice(1)}
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
        <div className="alert info">The {viewType} view arrives with a later phase; pick another view above.</div>
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
          Back
        </button>
      )}
    </div>
  );
}
