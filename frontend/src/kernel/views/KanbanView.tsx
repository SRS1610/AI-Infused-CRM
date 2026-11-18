import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api } from "../api/client";
import type { Domain, GroupRow, RecordData, ViewDef, ViewNode, ViewsResponse } from "../api/types";
import { and } from "../domain";
import { displayValue, formatNumber } from "../format";
import { collectFields } from "./FormView";
import { initials } from "../icons";
import { RelatedNames } from "../fields/RelatedNames";
import { t } from "../i18n";

interface Props {
  model: string;
  meta: ViewsResponse;
  view: ViewDef;
  domain: Domain;
  groupBy: string[];
  onOpen: (id: number) => void;
}

const GROUPABLE = new Set(["many2one", "selection", "boolean"]);

export function KanbanView({ model, meta, view, domain, groupBy, onOpen }: Props) {
  const groupField =
    groupBy[0] ??
    view.default_group_by ??
    Object.values(meta.fields).find((f) => f.store && GROUPABLE.has(f.type) && f.name !== "company_id")?.name ??
    null;
  const fieldNames = useMemo(() => {
    const names = collectFields(view.root);
    names.add("display_name");
    if (groupField) names.add(groupField);
    return [...names].filter((n) => n in meta.fields);
  }, [view, meta.fields, groupField]);

  const sumField = typeof view.sum_field === "string" && view.sum_field in meta.fields ? view.sum_field : null;
  const groups = useQuery({
    queryKey: ["list", model, "kanban-groups", domain, groupField, sumField],
    queryFn: () =>
      api.readGroup(model, {
        domain,
        groupby: groupField ? [groupField] : [],
        aggregates: sumField ? [`${sumField}:sum`] : [],
      }),
  });
  const groupMeta = groupField ? meta.fields[groupField] : undefined;
  // pipelines show every stage, even empty ones (view option "all_groups")
  const allGroups = useQuery({
    queryKey: ["list", groupMeta?.comodel, "kanban-all-groups"],
    queryFn: () => api.list(groupMeta!.comodel!, { fields: ["display_name"], limit: 50 }),
    enabled: !!view.all_groups && groupMeta?.type === "many2one" && !!groupMeta.comodel,
  });

  if (groups.isLoading)
    return (
      <div className="loading" role="status">
        {t("Loading…")}
      </div>
    );
  let columns: GroupRow[] = groupField ? (groups.data?.groups ?? []) : [{ __count: 0, __domain: domain }];
  if (groupField && allGroups.data) {
    const present = new Map(columns.map((g) => [g[groupField]?.id ?? g[groupField], g]));
    columns = [
      ...allGroups.data.items.map(
        (s) =>
          present.get(s.id) ?? {
            __count: 0,
            __domain: and(domain, [[groupField, "=", s.id]]),
            [groupField]: { id: s.id, display_name: s.display_name },
          },
      ),
      ...columns.filter((g) => !allGroups.data!.items.some((s) => s.id === (g[groupField]?.id ?? g[groupField]))),
    ];
  }
  return (
    <div className="kanban" role="list" aria-label={t("{model} board", { model: meta.model.description })}>
      {columns.map((g, i) => (
        <KanbanColumn
          key={i}
          model={model}
          meta={meta}
          view={view}
          group={g}
          groupField={groupField}
          fieldNames={fieldNames}
          sumField={sumField}
          onOpen={onOpen}
        />
      ))}
    </div>
  );
}

function KanbanColumn({
  model,
  meta,
  view,
  group,
  groupField,
  fieldNames,
  sumField,
  onOpen,
}: {
  model: string;
  meta: ViewsResponse;
  view: ViewDef;
  group: GroupRow;
  groupField: string | null;
  fieldNames: string[];
  sumField?: string | null;
  onOpen: (id: number) => void;
}) {
  const queryClient = useQueryClient();
  const [over, setOver] = useState(false);
  const [quick, setQuick] = useState("");
  const [error, setError] = useState<string | null>(null);
  const records = useQuery({
    queryKey: ["list", model, "kanban", group.__domain, fieldNames.join(",")],
    queryFn: () => api.list(model, { domain: and(group.__domain), fields: fieldNames, limit: 40 }),
  });
  const value = groupField ? group[groupField] : null;
  const groupValue = value && typeof value === "object" && "id" in value ? value.id : (value ?? null);
  const title = groupField
    ? value === null || value === undefined || value === false
      ? t("None")
      : typeof value === "object"
        ? value.display_name
        : displayValue(meta.fields[groupField], value)
    : meta.model.description;
  const recName = meta.model.rec_name in meta.fields ? meta.model.rec_name : "name";

  const move = async (id: number) => {
    if (!groupField) return;
    try {
      await api.update(model, id, { [groupField]: groupValue }, { fields: ["id"] });
      await queryClient.invalidateQueries({ queryKey: ["list", model] });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const quickCreate = async () => {
    if (!quick.trim()) return;
    try {
      await api.create(model, { [recName]: quick.trim(), ...(groupField ? { [groupField]: groupValue } : {}) }, ["id"]);
      setQuick("");
      await queryClient.invalidateQueries({ queryKey: ["list", model] });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <section
      className={`kanban-column${over ? " drop-target" : ""}`}
      aria-label={String(title)}
      role="listitem"
      onDragOver={(e) => {
        if (!groupField) return;
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        setOver(false);
        const id = Number(e.dataTransfer.getData("text/x-tessera-id"));
        if (id) void move(id);
      }}
    >
      <header className="kanban-header">
        <h3>{String(title)}</h3>
        <span className="count">{records.data?.total ?? group.__count}</span>
        {sumField && (
          <span className="kanban-sum" title={t("Total {field}", { field: meta.fields[sumField]?.string ?? sumField })}>
            {formatNumber(Number(group[`${sumField}:sum`] ?? 0), 0)}
          </span>
        )}
      </header>
      {meta.model.access.create && (
        <form
          className="quick-create"
          onSubmit={(e) => {
            e.preventDefault();
            void quickCreate();
          }}
        >
          <input
            className="input small"
            placeholder={t("Quick create…")}
            aria-label={t("Quick create in {column}", { column: String(title) })}
            value={quick}
            onChange={(e) => setQuick(e.target.value)}
          />
        </form>
      )}
      {error && (
        <div className="alert error small" role="alert">
          {error}
        </div>
      )}
      <ul className="kanban-cards">
        {(records.data?.items ?? []).map((record) => (
          <li
            key={record.id}
            className="kanban-card"
            draggable={!!groupField && meta.model.access.write}
            tabIndex={0}
            onDragStart={(e) => e.dataTransfer.setData("text/x-tessera-id", String(record.id))}
            onClick={() => onOpen(Number(record.id))}
            onKeyDown={(e) => e.key === "Enter" && onOpen(Number(record.id))}
          >
            <Card node={view.root} record={record} meta={meta} />
          </li>
        ))}
      </ul>
    </section>
  );
}

function Card({ node, record, meta }: { node?: ViewNode; record: RecordData; meta: ViewsResponse }) {
  if (!node) return <strong className="card-title">{String(record.display_name ?? "")}</strong>;
  const head: React.ReactNode[] = [];
  const lines: React.ReactNode[] = [];
  const foot: React.ReactNode[] = [];
  const walk = (n: ViewNode, key: string) => {
    if (n.kind !== "field" || !n.name) {
      (n.children ?? []).forEach((c, i) => walk(c, `${key}.${i}`));
      return;
    }
    const field = meta.fields[n.name];
    const value = record[n.name];
    if (n.name === "display_name" || n.widget === "title") {
      const text = n.name === "display_name" ? String(record.display_name ?? "") : displayValue(field, value, record);
      if (text)
        head.push(
          <strong key={key} className="card-title">
            {text}
          </strong>,
        );
      return;
    }
    if (!field || value === null || value === undefined || value === false) return;
    if (n.widget === "priority" && field.type === "selection") {
      const level = Number(value);
      const max = Math.max(0, (field.selection?.length ?? 1) - 1);
      if (max)
        foot.push(
          <span
            key={key}
            className="card-stars"
            title={displayValue(field, value)}
            aria-label={displayValue(field, value)}
          >
            {"★".repeat(level)}
            <span className="muted">{"☆".repeat(Math.max(0, max - level))}</span>
          </span>,
        );
      return;
    }
    if (field.type === "many2one" && field.comodel === "core.user") {
      if (value.display_name)
        foot.push(
          <span key={key} className="card-owner" title={field.string}>
            <span className="avatar small" aria-hidden="true">
              {initials(value.display_name)}
            </span>
            {value.display_name}
          </span>,
        );
      return;
    }
    if (field.type === "many2one" && !value.display_name) return; // a record the user cannot read
    if (field.type === "many2many" || field.type === "one2many") {
      const ids = Array.isArray(value) ? value.filter((v: any) => typeof v === "number") : [];
      if (ids.length && field.type === "many2many")
        lines.push(<RelatedNames key={key} comodel={field.comodel} ids={ids} chips />);
      return;
    }
    const text = displayValue(field, value, record);
    if (!text) return;
    if (n.widget === "badge" || field.type === "selection") {
      foot.push(
        <span key={key} className={`badge badge-${value}`}>
          {text}
        </span>,
      );
      return;
    }
    lines.push(
      <div key={key} className={field.type === "monetary" ? "card-line card-amount" : "card-line"}>
        {text}
      </div>,
    );
  };
  walk(node, "c");
  return (
    <>
      {head}
      {lines}
      {foot.length > 0 && <div className="card-foot">{foot}</div>}
    </>
  );
}
