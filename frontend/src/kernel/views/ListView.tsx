import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api/client";
import type { Domain, FieldMeta, GroupRow, ListColumn, RecordData, ViewDef, ViewsResponse } from "../api/types";
import { and } from "../domain";
import { FieldInput } from "../fields/FieldInput";
import { displayValue } from "../format";

interface Props {
  model: string;
  meta: ViewsResponse;
  view: ViewDef;
  domain: Domain;
  groupBy: string[];
  context?: Record<string, any>;
  pageSize?: number;
  onOpen: (id: number) => void;
}

const NUMERIC = new Set(["integer", "float", "monetary"]);
const COLUMN_KEY = (model: string) => `tessera.columns.${model}`;

function loadHidden(model: string, columns: ListColumn[]): Set<string> {
  try {
    const raw = window.localStorage.getItem(COLUMN_KEY(model));
    if (raw) return new Set(JSON.parse(raw) as string[]);
  } catch {
    /* ignore */
  }
  return new Set(columns.filter((c) => c.optional === "hide").map((c) => c.field));
}

function toCsv(columns: ListColumn[], fields: Record<string, FieldMeta>, rows: RecordData[]): string {
  const esc = (s: string) => (/[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s);
  const head = columns.map((c) => esc(c.label ?? fields[c.field]?.string ?? c.field)).join(",");
  const body = rows.map((r) => columns.map((c) => esc(displayValue(fields[c.field], r[c.field], r))).join(","));
  return [head, ...body].join("\n");
}

export function ListView({ model, meta, view, domain, groupBy, context, pageSize = 80, onOpen }: Props) {
  const queryClient = useQueryClient();
  const allColumns = useMemo(() => (view.columns ?? []).filter((c) => c.field in meta.fields), [view, meta.fields]);
  const [hidden, setHidden] = useState<Set<string>>(() => loadHidden(model, allColumns));
  const columns = allColumns.filter((c) => !hidden.has(c.field));
  const [order, setOrder] = useState<string | undefined>(undefined);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [editing, setEditing] = useState<{
    id: number;
    values: RecordData;
    etag: string | null;
    record: RecordData;
  } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [conflict, setConflict] = useState<string | null>(null);
  const [showColumns, setShowColumns] = useState(false);

  useEffect(() => {
    setOffset(0);
    setSelected(new Set());
  }, [domain, groupBy]);

  const fieldNames = useMemo(() => {
    const names = new Set(["display_name", ...columns.map((c) => c.field)]);
    for (const c of columns) {
      const f = meta.fields[c.field];
      if (f?.type === "monetary") names.add(f.currency_field ?? "currency_id");
    }
    return [...names].filter((n) => n in meta.fields);
  }, [columns, meta.fields]);

  const sumColumns = columns.filter(
    (c) =>
      NUMERIC.has(meta.fields[c.field]?.type) &&
      meta.fields[c.field]?.store &&
      c.field !== "id" &&
      c.field !== "sequence",
  );
  const aggregates = sumColumns.map((c) => `${c.field}:sum`);

  const rows = useQuery({
    queryKey: ["list", model, domain, order, offset, pageSize, fieldNames.join(","), context],
    queryFn: () => api.list(model, { domain, fields: fieldNames, order, limit: pageSize, offset, context }),
    enabled: groupBy.length === 0,
    placeholderData: (prev) => prev,
  });
  const groups = useQuery({
    queryKey: ["list", model, "groups", domain, groupBy, aggregates.join(",")],
    queryFn: () => api.readGroup(model, { domain, groupby: groupBy.slice(0, 1), aggregates }),
    enabled: groupBy.length > 0,
  });
  const totals = useQuery({
    queryKey: ["list", model, "totals", domain, aggregates.join(",")],
    queryFn: () => api.readGroup(model, { domain, groupby: [], aggregates }),
    enabled: aggregates.length > 0,
  });

  const items = rows.data?.items ?? [];
  const total = rows.data?.total ?? 0;
  const allSelected = items.length > 0 && items.every((r) => selected.has(Number(r.id)));

  const toggleSort = (field: string) => {
    if (!meta.fields[field]?.store) return;
    setOrder((current) => (current === `${field} asc` ? `${field} desc` : `${field} asc`));
  };

  const persistHidden = (next: Set<string>) => {
    setHidden(next);
    try {
      window.localStorage.setItem(COLUMN_KEY(model), JSON.stringify([...next]));
    } catch {
      /* ignore */
    }
  };

  const startEdit = async (row: RecordData) => {
    if (!meta.model.access.write) return;
    const { record, etag } = await api.getWithEtag(model, Number(row.id), fieldNames);
    setEditing({ id: Number(row.id), values: {}, etag, record });
    setConflict(null);
  };

  const saveEdit = async () => {
    if (!editing) return;
    const values = Object.fromEntries(
      Object.entries(editing.values).map(([k, v]) => [k, meta.fields[k]?.type === "many2one" ? (v ? v.id : null) : v]),
    );
    if (!Object.keys(values).length) {
      setEditing(null);
      return;
    }
    try {
      await api.update(model, editing.id, values, { fields: ["id"], etag: editing.etag });
      setEditing(null);
      await queryClient.invalidateQueries({ queryKey: ["list", model] });
    } catch (e) {
      if (e instanceof ApiError && e.isConflict)
        setConflict("This record was changed by someone else since you started editing.");
      else setNotice(e instanceof Error ? e.message : String(e));
    }
  };

  const bulk = async (action: "delete" | "archive" | "unarchive" | "export") => {
    const ids = [...selected];
    if (!ids.length) return;
    if (action === "export") {
      const data = await api.list(model, {
        domain: [["id", "in", ids]],
        fields: fieldNames,
        limit: ids.length,
        count: false,
      });
      const blob = new Blob([toCsv(columns, meta.fields, data.items)], { type: "text/csv" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${model}.csv`;
      a.click();
      URL.revokeObjectURL(url);
      return;
    }
    if (action === "delete" && !window.confirm(`Delete ${ids.length} record(s)?`)) return;
    try {
      for (const id of ids) {
        if (action === "delete") await api.remove(model, id);
        else await api.call(model, id, action === "archive" ? "action_archive" : "action_unarchive");
      }
      setSelected(new Set());
      await queryClient.invalidateQueries({ queryKey: ["list", model] });
    } catch (e) {
      setNotice(e instanceof Error ? e.message : String(e));
    }
  };

  const renderCell = (row: RecordData, col: ListColumn) => {
    const field = meta.fields[col.field];
    if (
      editing &&
      editing.id === row.id &&
      field &&
      !field.readonly &&
      field.type !== "one2many" &&
      field.type !== "many2many"
    ) {
      const value = col.field in editing.values ? editing.values[col.field] : editing.record[col.field];
      return (
        <FieldInput
          id={`cell-${row.id}-${col.field}`}
          field={field}
          value={value}
          record={{ ...editing.record, ...editing.values }}
          readonly={false}
          node={{ widget: col.widget }}
          onChange={(v) => setEditing((e) => (e ? { ...e, values: { ...e.values, [col.field]: v } } : e))}
        />
      );
    }
    const text = displayValue(field, row[col.field], row);
    if (col.widget === "badge" && text) return <span className={`badge badge-${row[col.field]}`}>{text}</span>;
    return text;
  };

  const hasActive = "active" in meta.fields;

  return (
    <div className="list-view">
      <div className="list-toolbar">
        {selected.size > 0 ? (
          <div className="bulk" role="toolbar" aria-label="Bulk actions">
            <span>{selected.size} selected</span>
            <button type="button" className="btn small" onClick={() => void bulk("export")}>
              Export CSV
            </button>
            {hasActive && meta.model.access.write && (
              <button type="button" className="btn small" onClick={() => void bulk("archive")}>
                Archive
              </button>
            )}
            {hasActive && meta.model.access.write && (
              <button type="button" className="btn small" onClick={() => void bulk("unarchive")}>
                Unarchive
              </button>
            )}
            {meta.model.access.unlink && (
              <button type="button" className="btn small danger" onClick={() => void bulk("delete")}>
                Delete
              </button>
            )}
          </div>
        ) : (
          <span className="muted">
            {groupBy.length ? `${groups.data?.groups.length ?? 0} groups` : `${total} record${total === 1 ? "" : "s"}`}
          </span>
        )}
        <div className="spacer" />
        <div className="dropdown">
          <button
            type="button"
            className="btn small ghost"
            aria-expanded={showColumns}
            onClick={() => setShowColumns((s) => !s)}
          >
            Columns
          </button>
          {showColumns && (
            <div className="dropdown-menu" role="menu">
              {allColumns.map((c) => (
                <label key={c.field} className="menu-check">
                  <input
                    type="checkbox"
                    checked={!hidden.has(c.field)}
                    onChange={(e) => {
                      const next = new Set(hidden);
                      if (e.target.checked) next.delete(c.field);
                      else next.add(c.field);
                      persistHidden(next);
                    }}
                  />
                  {c.label ?? meta.fields[c.field]?.string ?? c.field}
                </label>
              ))}
            </div>
          )}
        </div>
        {groupBy.length === 0 && total > pageSize && (
          <nav className="pager" aria-label="Pagination">
            <button
              type="button"
              className="btn small ghost"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - pageSize))}
              aria-label="Previous page"
            >
              ‹
            </button>
            <span>
              {offset + 1}–{Math.min(offset + pageSize, total)} / {total}
            </span>
            <button
              type="button"
              className="btn small ghost"
              disabled={offset + pageSize >= total}
              onClick={() => setOffset(offset + pageSize)}
              aria-label="Next page"
            >
              ›
            </button>
          </nav>
        )}
      </div>
      {notice && (
        <div className="alert error" role="alert">
          {notice}{" "}
          <button type="button" className="btn small" onClick={() => setNotice(null)}>
            Dismiss
          </button>
        </div>
      )}
      {conflict && (
        <div className="alert conflict" role="alertdialog" aria-label="Edit conflict">
          {conflict}
          <button type="button" className="btn small" onClick={() => editing && void startEdit({ id: editing.id })}>
            Reload record
          </button>
          <button
            type="button"
            className="btn small ghost"
            onClick={() => {
              setEditing(null);
              setConflict(null);
            }}
          >
            Cancel
          </button>
        </div>
      )}
      <div className="table-wrap">
        <table className="table" aria-label={meta.model.description} aria-busy={rows.isFetching || groups.isFetching}>
          <thead>
            <tr>
              {groupBy.length === 0 && (
                <th className="select-col">
                  <input
                    type="checkbox"
                    aria-label="Select all"
                    checked={allSelected}
                    onChange={(e) =>
                      setSelected(e.target.checked ? new Set(items.map((r) => Number(r.id))) : new Set())
                    }
                  />
                </th>
              )}
              {columns.map((c) => {
                const sorted = order?.startsWith(`${c.field} `)
                  ? order.endsWith("desc")
                    ? "descending"
                    : "ascending"
                  : "none";
                const numeric = NUMERIC.has(meta.fields[c.field]?.type);
                return (
                  <th key={c.field} scope="col" aria-sort={sorted} className={numeric ? "num" : undefined}>
                    <button
                      type="button"
                      className="th-button"
                      onClick={() => toggleSort(c.field)}
                      disabled={!meta.fields[c.field]?.store}
                    >
                      {c.label ?? meta.fields[c.field]?.string ?? c.field}
                      {sorted === "ascending" ? " ▲" : sorted === "descending" ? " ▼" : ""}
                    </button>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {groupBy.length > 0
              ? (groups.data?.groups ?? []).map((g, i) => (
                  <GroupRows
                    key={i}
                    group={g}
                    groupBy={groupBy}
                    model={model}
                    meta={meta}
                    columns={columns}
                    fieldNames={fieldNames}
                    onOpen={onOpen}
                    renderCell={renderCell}
                  />
                ))
              : items.map((row) => (
                  <tr
                    key={row.id}
                    className={`clickable${editing?.id === row.id ? " editing" : ""}`}
                    onClick={() => editing?.id !== row.id && onOpen(Number(row.id))}
                    onDoubleClick={(e) => {
                      e.stopPropagation();
                      void startEdit(row);
                    }}
                    onKeyDown={(e) => {
                      if (editing?.id === row.id) {
                        if (e.key === "Enter") {
                          e.preventDefault();
                          void saveEdit();
                        } else if (e.key === "Escape") setEditing(null);
                      } else if (e.key === "Enter") onOpen(Number(row.id));
                    }}
                    tabIndex={0}
                  >
                    <td className="select-col" onClick={(e) => e.stopPropagation()}>
                      <input
                        type="checkbox"
                        aria-label={`Select ${row.display_name ?? row.id}`}
                        checked={selected.has(Number(row.id))}
                        onChange={(e) => {
                          const next = new Set(selected);
                          if (e.target.checked) next.add(Number(row.id));
                          else next.delete(Number(row.id));
                          setSelected(next);
                        }}
                      />
                    </td>
                    {columns.map((c) => (
                      <td
                        key={c.field}
                        className={NUMERIC.has(meta.fields[c.field]?.type) ? "num" : undefined}
                        onClick={(e) => editing?.id === row.id && e.stopPropagation()}
                      >
                        {renderCell(row, c)}
                      </td>
                    ))}
                  </tr>
                ))}
            {groupBy.length === 0 && !rows.isLoading && items.length === 0 && (
              <tr>
                <td colSpan={columns.length + 1} className="empty">
                  No records match.
                </td>
              </tr>
            )}
          </tbody>
          {sumColumns.length > 0 && totals.data?.groups[0] && (
            <tfoot>
              <tr>
                {groupBy.length === 0 && <td />}
                {columns.map((c) => (
                  <td key={c.field} className="num total">
                    {sumColumns.includes(c)
                      ? displayValue(
                          {
                            ...meta.fields[c.field],
                            type: meta.fields[c.field].type === "monetary" ? "float" : meta.fields[c.field].type,
                          },
                          totals.data.groups[0][`${c.field}:sum`],
                        )
                      : ""}
                  </td>
                ))}
              </tr>
            </tfoot>
          )}
        </table>
      </div>
      {editing && (
        <div className="edit-bar" role="status">
          Editing row — <kbd>Enter</kbd> to save, <kbd>Esc</kbd> to cancel
          <button type="button" className="btn small primary" onClick={() => void saveEdit()}>
            Save
          </button>
          <button type="button" className="btn small ghost" onClick={() => setEditing(null)}>
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}

function groupLabel(meta: ViewsResponse, field: string, value: any): string {
  if (value === null || value === undefined || value === false) return "None";
  const f = meta.fields[field.split(":")[0]];
  if (f?.type === "many2one") return value.display_name ?? `#${value.id}`;
  return displayValue(f, value) || String(value);
}

function GroupRows({
  group,
  groupBy,
  model,
  meta,
  columns,
  fieldNames,
  onOpen,
  renderCell,
}: {
  group: GroupRow;
  groupBy: string[];
  model: string;
  meta: ViewsResponse;
  columns: ListColumn[];
  fieldNames: string[];
  onOpen: (id: number) => void;
  renderCell: (row: RecordData, col: ListColumn) => React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const records = useQuery({
    queryKey: ["list", model, "group-rows", group.__domain, fieldNames.join(",")],
    queryFn: () => api.list(model, { domain: and(group.__domain), fields: fieldNames, limit: 200, count: false }),
    enabled: open,
  });
  const key = groupBy[0];
  return (
    <>
      <tr className="group-row">
        <td colSpan={columns.length}>
          <button type="button" className="th-button" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
            {open ? "▾" : "▸"} {groupLabel(meta, key, group[key])} <span className="muted">({group.__count})</span>
          </button>
        </td>
      </tr>
      {open &&
        (records.data?.items ?? []).map((row) => (
          <tr key={row.id} className="clickable grouped" onClick={() => onOpen(Number(row.id))}>
            {columns.map((c) => (
              <td key={c.field}>{renderCell(row, c)}</td>
            ))}
          </tr>
        ))}
    </>
  );
}
