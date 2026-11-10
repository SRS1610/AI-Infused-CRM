// Client of the AI API (/api/v1/ai): feature status, runs, streamed drafts, proposals, feedback, admin.
import { ApiError, CLIENT_ID, clientState, request } from "../api/client";

export interface AiFeatureInfo {
  title: string;
  task_class: string;
  interactive: boolean;
  writes: boolean;
  outbound: boolean;
  disclosure: string;
  risk_class: string;
}

export interface AiStatus {
  available: boolean;
  features: Record<string, AiFeatureInfo>;
}

export interface Proposal {
  id: number;
  kind: "create" | "write" | "transition" | "message" | "action";
  title: string | null;
  res_model: string;
  res_id: number | null;
  payload: Record<string, unknown>;
  original: Record<string, unknown> | null;
  state: "pending" | "accepted" | "edited" | "rejected" | "expired" | "failed";
  reason: string | null;
  ai_generated: boolean;
  run_id: number | null;
  feature: string | null;
}

export interface Citation {
  id: string;
  res_model: string;
  res_id: number;
  title: string;
}

export interface RunResult {
  run_id: number;
  feature: string;
  text: string;
  data: unknown;
  ai_generated: boolean;
  citations: Citation[];
  flags: string[];
  proposals: Proposal[];
  status: string;
}

export interface UsageRow {
  company_id: number | null;
  company: string;
  feature: string;
  feature_title: string;
  requests: number;
  blocked: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cost: string;
  latency_p50: number;
  latency_p95: number;
  fallback_rate: number;
  cache_hit_rate: number;
  acceptance_rate: number | null;
  decisions: Record<string, number>;
}

export interface AdminFeature {
  name: string;
  title: string;
  module: string;
  task_class: string;
  risk_class: string;
  writes: boolean;
  outbound: boolean;
  enabled_global: boolean;
  companies: { id: number; name: string; enabled: boolean }[];
}

export type StreamEvent =
  | { event: "start"; run_id: number; feature: string; ai_generated: boolean }
  | { event: "delta"; text: string }
  | { event: "done"; result: RunResult }
  | { event: "error"; error: { code: string; message: string } };

export interface RunInput {
  inputs?: Record<string, unknown>;
  res_model?: string;
  res_id?: number;
}

export const ai = {
  status: () => request<AiStatus>("GET", "/ai/status"),
  run: (feature: string, body: RunInput) =>
    request<RunResult>("POST", `/ai/features/${encodeURIComponent(feature)}/run`, { body }),
  proposals: (q: { res_model?: string; res_id?: number; state?: string } = {}) =>
    request<Proposal[]>("GET", "/ai/proposals", { query: q }),
  accept: (id: number, payload?: Record<string, unknown>) =>
    request<Proposal>("POST", `/ai/proposals/${id}/accept`, { body: payload ? { payload } : {} }),
  reject: (id: number, reason = "") => request<Proposal>("POST", `/ai/proposals/${id}/reject`, { body: { reason } }),
  feedback: (runId: number, body: { rating?: "up" | "down"; comment?: string; correction?: string }) =>
    request<{ id: number }>("POST", `/ai/runs/${runId}/feedback`, { body }),
  usage: (q: { since?: string; until?: string; company_id?: number } = {}) =>
    request<{ available: boolean; message?: string; rows: UsageRow[]; since: string }>("GET", "/ai/admin/usage", {
      query: q,
    }),
  adminFeatures: () => request<AdminFeature[]>("GET", "/ai/admin/features"),
  setFlag: (feature: string, enabled: boolean, company_id: number | null) =>
    request<{ enabled: boolean }>("PUT", `/ai/admin/features/${encodeURIComponent(feature)}/flag`, {
      body: { enabled, company_id },
    }),
};

/** POST a streamed run and call ``onEvent`` for every Server-Sent Event. Resolves with the final
 * result (or rejects with an ApiError for HTTP errors such as 404 ``feature_disabled``). */
export async function streamFeature(
  feature: string,
  body: RunInput,
  onEvent: (ev: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<RunResult | null> {
  const state = clientState();
  const headers: Record<string, string> = {
    Accept: "text/event-stream",
    "Content-Type": "application/json",
    "X-Tessera-Client": CLIENT_ID,
  };
  if (state.csrf) headers["X-CSRF-Token"] = state.csrf;
  if (state.companies?.length) headers["X-Tessera-Companies"] = state.companies.join(",");
  if (state.company) headers["X-Tessera-Company"] = String(state.company);
  const response = await fetch(`/api/v1/ai/features/${encodeURIComponent(feature)}/stream`, {
    method: "POST",
    headers,
    credentials: "same-origin",
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok || !response.body) {
    const text = await response.text();
    throw new ApiError(response.status, text ? JSON.parse(text) : { error: "HttpError", message: response.statusText });
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result: RunResult | null = null;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut: number;
    while ((cut = buffer.indexOf("\n\n")) !== -1) {
      const chunk = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      const data = chunk
        .split("\n")
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).trim())
        .join("");
      if (!data) continue;
      const ev = JSON.parse(data) as StreamEvent;
      if (ev.event === "done") result = ev.result;
      onEvent(ev);
    }
  }
  return result;
}

export function aiCodeMessage(code: string | undefined, fallback: string): string {
  if (code === "ai_unavailable" || code === "provider_unavailable")
    return "AI is unavailable right now. You can carry on without it.";
  if (code === "feature_disabled") return "This AI feature is turned off.";
  if (code === "budget_exceeded" || code === "quota_exceeded") return "The AI budget for this period is used up.";
  if (code === "model_refused") return "The AI declined this request.";
  if (code === "invalid_output") return "The AI answer could not be used. Please try again or continue manually.";
  return fallback;
}

export function aiErrorMessage(e: unknown): string {
  if (e instanceof ApiError) return aiCodeMessage((e.body as { code?: string }).code, e.message);
  return e instanceof Error ? e.message : String(e);
}
