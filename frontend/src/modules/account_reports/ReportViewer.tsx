import { useQuery } from "@tanstack/react-query";
import { Fragment, useEffect, useId, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../../kernel/api/client";
import type { Domain } from "../../kernel/api/types";
import { registerClientAction } from "../../kernel/extensions";
import { formatDate, formatNumber } from "../../kernel/format";
import { t } from "../../kernel/i18n";
import {
  reportsApi,
  type CellValue,
  type ReportColumn,
  type ReportData,
  type ReportFilters,
  type ReportLine,
} from "./api";
import { ReportAsk, type ScopeLabels } from "./ReportAsk";

const ITEM_FIELDS = ["date", "move_id", "account_id", "partner_id", "name", "debit", "credit", "balance"];

function cellText(value: CellValue, column: ReportColumn, digits: number): string {
  if (value === null || value === undefined) return "";
  if (column.type === "monetary") return formatNumber(Number(value), digits);
  if (column.type === "date") return formatDate(String(value));
  return String(value);
}

/** Journal items behind a figure (drill-down), with links to their entries. */
function DrillDown({ title, domain, onClose }: { title: string; domain: Domain; onClose: () => void }) {
  const headingId = useId();
  const [offset, setOffset] = useState(0);
  const items = useQuery({
    queryKey: ["report-items", JSON.stringify(domain), offset],
    queryFn: () =>
      api.list("account.move_line", { domain, fields: ITEM_FIELDS, order: "date, id", limit: 80, offset, count: true }),
  });
  const total = items.data?.total ?? 0;
  return (
    <section className="report-drilldown" aria-labelledby={headingId}>
      <div className="control-panel">
        <h2 id={headingId}>{title}</h2>
        <button type="button" className="btn small" onClick={onClose}>
          {t("Close")}
        </button>
      </div>
      {items.isLoading && (
        <p role="status" className="loading">
          {t("Loading…")}
        </p>
      )}
      {items.data && (
        <table className="pivot-table">
          <caption className="sr-only">{t("Journal items")}</caption>
          <thead>
            <tr>
              <th scope="col">{t("Date")}</th>
              <th scope="col">{t("Entry")}</th>
              <th scope="col">{t("Account")}</th>
              <th scope="col">{t("Partner")}</th>
              <th scope="col">{t("Label")}</th>
              <th scope="col" className="num">
                {t("Debit")}
              </th>
              <th scope="col" className="num">
                {t("Credit")}
              </th>
            </tr>
          </thead>
          <tbody>
            {items.data.items.map((item) => {
              const move = item.move_id as { id: number; display_name: string } | null;
              return (
                <tr key={String(item.id)}>
                  <td>{formatDate(item.date as string)}</td>
                  <td>{move ? <Link to={`/m/account.move/${move.id}`}>{move.display_name}</Link> : ""}</td>
                  <td>{(item.account_id as { display_name?: string } | null)?.display_name ?? ""}</td>
                  <td>{(item.partner_id as { display_name?: string } | null)?.display_name ?? ""}</td>
                  <td>{String(item.name ?? "")}</td>
                  <td className="num">{formatNumber(Number(item.debit), 2)}</td>
                  <td className="num">{formatNumber(Number(item.credit), 2)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {total > offset + 80 && (
        <button type="button" className="btn small" onClick={() => setOffset(offset + 80)}>
          {t("Next page")}
        </button>
      )}
      {offset > 0 && (
        <button type="button" className="btn small" onClick={() => setOffset(Math.max(0, offset - 80))}>
          {t("Previous page")}
        </button>
      )}
    </section>
  );
}

function defaultFilters(): ReportFilters {
  return { comparison: "none", periods: 1, include_draft: false, account_type: "all" };
}

/** Any financial report: filters, comparison columns, expandable lines, drill-down, exports. */
export function ReportViewer({
  code,
  initialFilters,
  initialScope,
  onSwitch,
}: {
  code: string;
  initialFilters?: ReportFilters;
  initialScope?: ScopeLabels;
  /** Open another report with filters (an "Ask" answer about a different report). */
  onSwitch?: (code: string, filters: ReportFilters, scope: ScopeLabels) => void;
}) {
  const [filters, setFilters] = useState<ReportFilters>(() => ({ ...defaultFilters(), ...initialFilters }));
  const [scope, setScope] = useState<ScopeLabels>(initialScope ?? { partners: [], journals: [] });
  const [unfolded, setUnfolded] = useState<string[]>([]);
  const [extra, setExtra] = useState<Record<string, ReportLine[]>>({});
  const [drill, setDrill] = useState<{ title: string; domain: Domain } | null>(null);
  const [loadingMore, setLoadingMore] = useState<string | null>(null);
  const query = useMemo(() => ({ ...filters, unfold: unfolded }), [filters, unfolded]);
  const report = useQuery({ queryKey: ["account-report", code, query], queryFn: () => reportsApi.run(code, query) });
  const session = useQuery({ queryKey: ["session"], queryFn: () => api.session() });
  useEffect(() => setExtra({}), [code, query]);
  const data: ReportData | undefined = report.data;
  const digits = data?.currency.decimal_places ?? 2;
  const companies = session.data?.allowed_companies ?? [];
  const asOf = data?.report.date_mode === "as_of";

  const set = (patch: Partial<ReportFilters>) => setFilters((f) => ({ ...f, ...patch }));
  const toggle = (line: ReportLine) =>
    setUnfolded((list) => (list.includes(line.id) ? list.filter((x) => x !== line.id) : [...list, line.id]));
  const loadMore = async (line: ReportLine) => {
    if (!line.parent_id || line.next_offset === undefined) return;
    setLoadingMore(line.id);
    try {
      const page = await reportsApi.run(code, { ...query, expand: line.parent_id, offset: line.next_offset });
      setExtra((e) => ({ ...e, [line.id]: page.lines }));
    } finally {
      setLoadingMore(null);
    }
  };

  // lines (and the pages loaded with "Load more", which replace their button)
  const rows: ReportLine[] = [];
  const expandRows = (lines: ReportLine[]) => {
    for (const line of lines) {
      if (line.style === "load_more" && extra[line.id]) expandRows(extra[line.id]);
      else rows.push(line);
    }
  };
  expandRows(data?.lines ?? []);
  const groups: { label: string; span: number }[] = [];
  for (const column of data?.columns ?? []) {
    const label = column.group ?? "";
    if (groups.length && groups[groups.length - 1].label === label) groups[groups.length - 1].span += 1;
    else groups.push({ label, span: 1 });
  }
  // a download link carries no company header: name the companies the report was computed for
  const exportFilters = {
    ...filters,
    company_ids: filters.company_ids?.length ? filters.company_ids : data?.companies.map((c) => c.id),
  };

  return (
    <div className="account-report">
      <h1 className="page-title">{data?.report.name ?? t("Financial report")}</h1>
      <ReportAsk
        onApply={(target, next, labels) => {
          if (target === code) {
            setFilters({ ...defaultFilters(), ...next });
            setScope(labels);
          } else onSwitch?.(target, next, labels);
        }}
      />
      {((filters.partner_ids?.length ?? 0) > 0 || (filters.journal_ids?.length ?? 0) > 0) && (
        <p className="report-scope">
          {t("Only: {names}", { names: [...scope.partners, ...scope.journals].join(", ") })}{" "}
          <button
            type="button"
            className="btn ghost small"
            onClick={() => {
              set({ partner_ids: [], journal_ids: [] });
              setScope({ partners: [], journals: [] });
            }}
          >
            {t("Remove")}
          </button>
        </p>
      )}
      <form
        className="report-filters control-panel"
        aria-label={t("Report filters")}
        onSubmit={(e) => e.preventDefault()}
      >
        {!asOf && (
          <label>
            {t("From")}{" "}
            <input
              className="input small"
              type="date"
              value={filters.date_from ?? ""}
              onChange={(e) => set({ date_from: e.target.value || undefined })}
            />
          </label>
        )}
        <label>
          {asOf ? t("As of") : t("To")}{" "}
          <input
            className="input small"
            type="date"
            value={filters.date_to ?? ""}
            onChange={(e) => set({ date_to: e.target.value || undefined })}
          />
        </label>
        {data?.report.allow_comparison && (
          <label>
            {t("Compare with")}{" "}
            <select
              className="input small"
              value={filters.comparison}
              onChange={(e) => set({ comparison: e.target.value as ReportFilters["comparison"] })}
            >
              <option value="none">{t("No comparison")}</option>
              <option value="previous_period">{t("Previous period")}</option>
              <option value="previous_year">{t("Previous year")}</option>
            </select>
          </label>
        )}
        {data?.report.allow_comparison && filters.comparison !== "none" && (
          <label>
            {t("Periods")}{" "}
            <input
              className="input small num"
              type="number"
              min={1}
              max={12}
              value={filters.periods ?? 1}
              onChange={(e) => set({ periods: Math.min(12, Math.max(1, Number(e.target.value) || 1)) })}
            />
          </label>
        )}
        {data?.report.engine === "partner_ledger" && (
          <label>
            {t("Accounts")}{" "}
            <select
              className="input small"
              value={filters.account_type}
              onChange={(e) => set({ account_type: e.target.value as ReportFilters["account_type"] })}
            >
              <option value="all">{t("Receivable and payable")}</option>
              <option value="receivable">{t("Receivable")}</option>
              <option value="payable">{t("Payable")}</option>
            </select>
          </label>
        )}
        <label>
          <input
            type="checkbox"
            checked={!!filters.include_draft}
            onChange={(e) => set({ include_draft: e.target.checked })}
          />{" "}
          {t("Include draft entries")}
        </label>
        {companies.length > 1 && (
          <fieldset className="report-companies">
            <legend>{t("Companies")}</legend>
            {companies.map((c) => (
              <label key={c.id}>
                <input
                  type="checkbox"
                  checked={(filters.company_ids ?? []).includes(c.id)}
                  onChange={(e) =>
                    set({
                      company_ids: e.target.checked
                        ? [...(filters.company_ids ?? []), c.id]
                        : (filters.company_ids ?? []).filter((id) => id !== c.id),
                    })
                  }
                />{" "}
                {c.name}
              </label>
            ))}
          </fieldset>
        )}
        {(filters.company_ids?.length ?? 0) > 1 && (
          <label>
            {t("Currency")}{" "}
            <input
              className="input small"
              maxLength={3}
              size={4}
              value={filters.currency ?? ""}
              placeholder={data?.currency.name}
              onChange={(e) => set({ currency: e.target.value.toUpperCase() || undefined })}
            />
          </label>
        )}
        <div className="report-exports" role="group" aria-label={t("Export")}>
          {(["xlsx", "csv", "pdf"] as const).map((fmt) => (
            <a key={fmt} className="btn small" href={reportsApi.exportUrl(code, exportFilters, fmt)} download>
              {fmt.toUpperCase()}
            </a>
          ))}
        </div>
      </form>
      {report.isLoading && (
        <p role="status" className="loading">
          {t("Loading…")}
        </p>
      )}
      {report.isError && (
        <div className="alert error" role="alert">
          {(report.error as Error).message}
        </div>
      )}
      {data?.warnings.map((w) => (
        <div key={w} className="alert info" role="status">
          {w}
        </div>
      ))}
      {data && (
        <>
          <p className="report-meta">
            {data.companies.map((c) => c.name).join(", ")} · {data.currency.name}
            {data.consolidated ? ` · ${t("Consolidated")}` : ""}
          </p>
          <table className="pivot-table report-table">
            <caption className="sr-only">{data.report.name}</caption>
            <thead>
              {groups.some((g) => g.label) && (
                <tr>
                  <td />
                  {groups.map((g, i) => (
                    <th key={i} scope="colgroup" colSpan={g.span} className="num">
                      {g.label}
                    </th>
                  ))}
                </tr>
              )}
              <tr>
                <th scope="col">{t("Line")}</th>
                {data.columns.map((c) => (
                  <th key={c.key} scope="col" className={c.type === "monetary" ? "num" : undefined}>
                    {c.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((line) => {
                const strong = line.style === "total" || line.style === "header";
                if (line.style === "load_more")
                  return (
                    <tr key={line.id}>
                      <td colSpan={data.columns.length + 1} style={{ paddingLeft: `${0.5 + line.level * 1.25}rem` }}>
                        <button
                          type="button"
                          className="btn small"
                          disabled={loadingMore === line.id}
                          onClick={() => void loadMore(line)}
                        >
                          {t("Load more ({count} remaining)", { count: line.remaining ?? 0 })}
                        </button>
                      </td>
                    </tr>
                  );
                return (
                  <tr key={line.id} className={`report-line ${line.style}`}>
                    <th
                      scope="row"
                      style={{ paddingLeft: `${0.5 + line.level * 1.25}rem`, fontWeight: strong ? 700 : 400 }}
                    >
                      {line.unfoldable ? (
                        <button
                          type="button"
                          className="btn ghost small"
                          aria-expanded={line.unfolded}
                          onClick={() => toggle(line)}
                        >
                          <span aria-hidden="true">{line.unfolded ? "▾" : "▸"}</span> {line.name}
                        </button>
                      ) : line.move_id ? (
                        <Link to={`/m/account.move/${line.move_id}`}>{line.name}</Link>
                      ) : (
                        line.name
                      )}
                    </th>
                    {data.columns.map((column, i) => {
                      const text = cellText(line.values[i], column, digits);
                      const domain = line.domains?.[i];
                      return (
                        <td key={column.key} className={column.type === "monetary" ? "num" : undefined}>
                          {domain && text && column.type === "monetary" && line.style !== "item" ? (
                            <button
                              type="button"
                              className="btn ghost small"
                              title={t("Show the journal items")}
                              aria-label={t("Journal items of {line}, {column}: {amount}", {
                                line: line.name,
                                column: [column.group, column.label].filter(Boolean).join(" "),
                                amount: text,
                              })}
                              onClick={() => setDrill({ title: `${line.name} · ${column.label}`, domain })}
                            >
                              {text}
                            </button>
                          ) : (
                            <span style={{ fontWeight: strong ? 700 : 400 }}>{text}</span>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
          {rows.length === 0 && <p className="empty">{t("Nothing to report for these filters.")}</p>}
        </>
      )}
      {drill && <DrillDown title={drill.title} domain={drill.domain} onClose={() => setDrill(null)} />}
    </div>
  );
}

/** The client action: the report code comes from the action's context (``{"report": "balance_sheet"}``). */
export function ReportAction() {
  const { actionRef = "" } = useParams();
  const action = useQuery({ queryKey: ["action", actionRef], queryFn: () => api.action(actionRef) });
  const code = (action.data?.context as Record<string, unknown> | undefined)?.report;
  if (action.isLoading)
    return (
      <p role="status" className="loading">
        {t("Loading…")}
      </p>
    );
  if (typeof code !== "string")
    return (
      <div className="alert error" role="alert">
        {t("This action does not name a report.")}
      </div>
    );
  return <SwitchableReport code={code} />;
}

/** The action's report, or another one the user opened from an "Ask" answer (with its filters). */
function SwitchableReport({ code }: { code: string }) {
  const [target, setTarget] = useState<{ code: string; filters: ReportFilters; scope: ScopeLabels } | null>(null);
  const current = target?.code ?? code;
  return (
    <Fragment key={`${current}-${JSON.stringify(target?.filters ?? {})}`}>
      <ReportViewer
        code={current}
        initialFilters={target?.filters}
        initialScope={target?.scope}
        onSwitch={(next, filters, scope) => setTarget({ code: next, filters, scope })}
      />
    </Fragment>
  );
}

registerClientAction("account_reports.viewer", () => <ReportAction />);
