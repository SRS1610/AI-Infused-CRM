// Client of the accounting AI API (/api/v1/account/ai): document reading, anomaly flags, cash-flow forecast.
import { request } from "../../kernel/api/client";
import type { Proposal } from "../../kernel/ai/api";

export interface CodedLine {
  description: string;
  quantity: number;
  unit_price: number;
  amount: number;
  tax_rate: number | null;
  account_id: number | null;
  account: string | null;
  account_source: "product" | "history" | "chart" | "default";
  tax_ids: number[];
  taxes: string[];
  tax_source: string;
  issue: string | null;
  confidence: number;
  reason: string;
}

export interface Difference {
  check: "lines_vs_untaxed" | "tax" | "total";
  document: number;
  computed: number;
  difference: number;
}

export interface AnomalyFinding {
  kind: string;
  severity: "low" | "medium" | "high";
  explanation: string;
  related_id: number | null;
}

export interface Review {
  method: "text" | "model" | "vision";
  move_type: string;
  extraction: Record<string, unknown> & {
    partner_name: string | null;
    partner_vat: string | null;
    iban: string | null;
    invoice_number: string | null;
    invoice_date: string | null;
    due_date: string | null;
    currency: string | null;
    amount_untaxed: number | null;
    amount_tax: number | null;
    amount_total: number | null;
  };
  partner: { id: number | null; display_name: string | null; method: string; confidence: number };
  lines: CodedLine[];
  currency: string;
  checks: { lines_total: number; computed_tax: number; differences: Difference[] };
  needs_review: boolean;
  issues: string[];
  warnings: string[];
  anomalies: AnomalyFinding[];
}

export interface ReadResult {
  run_id: number;
  data: Review | null;
  proposals: Proposal[];
  status: string;
  ai_generated: boolean;
}

export interface AnomalyFlag {
  id: number;
  kind: string;
  severity: "low" | "medium" | "high";
  score: number;
  explanation: string;
  state: "open" | "dismissed" | "confirmed";
  related: { id: number; name: string } | null;
}

export interface ForecastWeek {
  week: number;
  start: string;
  end: string;
  inflow: number;
  outflow: number;
  net: number;
  position: number;
  low: number;
  high: number;
}

export interface Forecast {
  today: string;
  currency: string;
  opening: number;
  weeks: ForecastWeek[];
  closing: number;
  lowest: { date: string; balance: number };
  open_receivables: number;
  open_payables: number;
  largest_items: { label: string; date: string; amount: number; kind: string }[];
  recurring: { label: string; amount: number; day: number }[];
  summary: string;
}

export interface Narrative {
  run_id: number;
  narrative: string;
  narrative_source: "model" | "facts";
  foreign_numbers: string[];
}

export const accountAi = {
  readDocument: (file: File, opts: { moveType?: string; moveId?: number } = {}) => {
    const form = new FormData();
    form.append("file", file);
    if (opts.moveType) form.append("move_type", opts.moveType);
    if (opts.moveId) form.append("move_id", String(opts.moveId));
    return request<ReadResult>("POST", "/account/ai/bills/read", { body: form });
  },
  accept: (proposalId: number, payload?: Record<string, unknown>) =>
    request<{ proposal: Proposal; move_id: number | null }>(
      "POST",
      `/account/ai/bills/proposals/${proposalId}/accept`,
      {
        body: payload ? { payload } : {},
      },
    ),
  anomalies: (moveId: number) => request<{ flags: AnomalyFlag[] }>("GET", `/account/ai/moves/${moveId}/anomalies`),
  check: (moveId: number) =>
    request<{ flags: AnomalyFlag[]; new: number }>("POST", `/account/ai/moves/${moveId}/anomalies/check`),
  decide: (flagId: number, decision: "dismiss" | "confirm", note?: string) =>
    request<{ id: number; state: string }>("POST", `/account/ai/anomalies/${flagId}/${decision}`, { body: { note } }),
  forecast: (weeks: number) => request<Forecast>("GET", "/account/ai/cashflow", { query: { weeks } }),
  narrative: (weeks: number) => request<Narrative>("POST", "/account/ai/cashflow/narrative", { body: { weeks } }),
};

export const money = (v: number | null | undefined, currency = ""): string =>
  v === null || v === undefined
    ? "—"
    : `${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}${currency ? ` ${currency}` : ""}`;
