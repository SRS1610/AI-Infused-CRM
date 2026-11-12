// Financial reports API (backend: modules/account_reports/api.py).
import { request } from "../../kernel/api/client";
import type { Domain } from "../../kernel/api/types";

export type CellValue = number | string | null;

export interface ReportColumn {
  key: string;
  label: string;
  type: "monetary" | "date" | "text";
  group?: string;
  date_from?: string | null;
  date_to?: string | null;
}

export interface ReportLine {
  id: string;
  parent_id: string | null;
  code: string | null;
  name: string;
  level: number;
  style: "normal" | "header" | "total" | "account" | "item" | "initial" | "load_more";
  values: CellValue[];
  domains: (Domain | null)[];
  unfoldable: boolean;
  unfolded: boolean;
  move_id?: number;
  next_offset?: number;
  remaining?: number;
}

export interface ReportData {
  report: {
    code: string;
    requested: string;
    name: string;
    engine: string;
    date_mode: "range" | "as_of";
    allow_comparison: boolean;
  };
  periods: { date_from: string | null; date_to: string }[];
  currency: { name: string; symbol: string; decimal_places: number };
  companies: { id: number; name: string }[];
  consolidated: boolean;
  columns: ReportColumn[];
  lines: ReportLine[];
  warnings: string[];
}

/** The report-parameter object (``ReportParams`` on the server). */
export interface ReportFilters {
  date_from?: string;
  date_to?: string;
  comparison?: "none" | "previous_period" | "previous_year";
  periods?: number;
  include_draft?: boolean;
  account_type?: "receivable" | "payable" | "all";
  company_ids?: number[];
  partner_ids?: number[];
  journal_ids?: number[];
  currency?: string;
  unfold?: string[];
  expand?: string;
  offset?: number;
}

export function reportQuery(filters: ReportFilters): Record<string, string | number | boolean | undefined> {
  return {
    date_from: filters.date_from || undefined,
    date_to: filters.date_to || undefined,
    comparison: filters.comparison && filters.comparison !== "none" ? filters.comparison : undefined,
    periods: filters.comparison && filters.comparison !== "none" ? filters.periods : undefined,
    include_draft: filters.include_draft || undefined,
    account_type: filters.account_type && filters.account_type !== "all" ? filters.account_type : undefined,
    company_ids: filters.company_ids?.length ? filters.company_ids.join(",") : undefined,
    partner_ids: filters.partner_ids?.length ? filters.partner_ids.join(",") : undefined,
    journal_ids: filters.journal_ids?.length ? filters.journal_ids.join(",") : undefined,
    currency: filters.currency || undefined,
    unfold: filters.unfold?.length ? filters.unfold.join(",") : undefined,
    expand: filters.expand,
    offset: filters.offset,
  };
}

export const reportsApi = {
  run: (code: string, filters: ReportFilters) =>
    request<ReportData>("GET", `/account/reports/${encodeURIComponent(code)}`, { query: reportQuery(filters) }),
  exportUrl: (code: string, filters: ReportFilters, format: "csv" | "xlsx" | "pdf") => {
    const params = new URLSearchParams({ format });
    for (const [k, v] of Object.entries(reportQuery({ ...filters, unfold: undefined })))
      if (v !== undefined && v !== "") params.set(k, String(v));
    return `/api/v1/account/reports/${encodeURIComponent(code)}/export?${params.toString()}`;
  },
};
