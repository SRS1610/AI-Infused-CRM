// Thin typed client over fetch. Adds CSRF, company/lang headers and turns error bodies into ApiError.
import type {
  ActionDef,
  ApiErrorBody,
  Domain,
  GroupRow,
  ListResponse,
  MenuNode,
  RecordData,
  SessionInfo,
  ViewsResponse,
} from "./types";

export class ApiError extends Error {
  readonly status: number;
  readonly body: ApiErrorBody;
  /** Seconds the server asked us to wait before retrying (429, 503), when it said so. */
  readonly retryAfter: number | undefined;
  constructor(status: number, body: ApiErrorBody, retryAfter?: number) {
    super(body.message || `HTTP ${status}`);
    this.status = status;
    this.body = body;
    this.retryAfter = retryAfter;
  }
  get isConflict(): boolean {
    return this.status === 409;
  }
  get isAuth(): boolean {
    return this.status === 401;
  }
}

interface ClientState {
  csrf: string | null;
  companies: number[] | null;
  company: number | null;
  onUnauthorized?: () => void;
}

const state: ClientState = { csrf: null, companies: null, company: null };

/** Identifies this browser tab; realtime events carry it back so a tab can ignore its own echoes. */
export const CLIENT_ID =
  typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : Math.random().toString(36).slice(2);

export function configureClient(patch: Partial<ClientState>): void {
  Object.assign(state, patch);
}

export function clientState(): Readonly<ClientState> {
  return state;
}

type Query = Record<string, string | number | boolean | null | undefined>;

function qs(query?: Query): string {
  if (!query) return "";
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
  const s = params.toString();
  return s ? `?${s}` : "";
}

export async function request<T>(
  method: string,
  path: string,
  opts: { query?: Query; body?: unknown; headers?: Record<string, string>; raw?: boolean } = {},
): Promise<T> {
  const headers: Record<string, string> = {
    Accept: "application/json",
    "X-Tessera-Client": CLIENT_ID,
    ...(opts.headers ?? {}),
  };
  if (opts.body !== undefined && !(opts.body instanceof FormData)) headers["Content-Type"] = "application/json";
  if (method !== "GET" && state.csrf) headers["X-CSRF-Token"] = state.csrf;
  if (state.companies?.length) headers["X-Tessera-Companies"] = state.companies.join(",");
  if (state.company) headers["X-Tessera-Company"] = String(state.company);
  const response = await fetch(`/api/v1${path}${qs(opts.query)}`, {
    method,
    headers,
    credentials: "same-origin",
    body: opts.body === undefined ? undefined : opts.body instanceof FormData ? opts.body : JSON.stringify(opts.body),
  });
  if (response.status === 204) return undefined as T;
  if (opts.raw && response.ok) return response as unknown as T;
  const text = await response.text();
  const data = text ? JSON.parse(text) : undefined;
  if (!response.ok) {
    if (response.status === 401 && state.onUnauthorized && !path.startsWith("/auth/")) state.onUnauthorized();
    const retryAfter = Number(response.headers?.get?.("Retry-After")) || undefined;
    throw new ApiError(
      response.status,
      (data as ApiErrorBody) ?? { error: "HttpError", message: response.statusText },
      retryAfter,
    );
  }
  return data as T;
}

const json = (v: unknown) => (v === undefined || v === null ? undefined : JSON.stringify(v));

export const api = {
  // auth
  login: (login: string, password: string) =>
    request<SessionInfo | { totp_required: true; pre_auth_token: string }>("POST", "/auth/login", {
      body: { login, password },
    }),
  loginTotp: (pre_auth_token: string, code: string) =>
    request<SessionInfo>("POST", "/auth/totp", { body: { pre_auth_token, code } }),
  logout: () => request<{ ok: boolean }>("POST", "/auth/logout"),
  session: () => request<SessionInfo>("GET", "/auth/session"),
  providers: () => request<{ code: string; name: string }[]>("GET", "/auth/providers"),
  // SAML identity providers (auth_saml module; the endpoint is missing when the module is not installed)
  samlProviders: () => request<{ id: number; name: string }[]>("GET", "/auth/saml/providers"),

  // web metadata
  menus: () => request<MenuNode[]>("GET", "/web/menus"),
  action: (ref: string) => request<ActionDef>("GET", `/web/actions/${encodeURIComponent(ref)}`),
  views: (model: string, types: string[], refs?: Record<string, string>) =>
    request<ViewsResponse>("GET", "/web/views", {
      query: {
        model,
        types: types.join(","),
        refs: refs
          ? Object.entries(refs)
              .map(([t, x]) => `${t}:${x}`)
              .join(",")
          : undefined,
      },
    }),

  // records
  list: (
    model: string,
    p: {
      domain?: Domain;
      fields?: string[];
      order?: string;
      limit?: number;
      offset?: number;
      count?: boolean;
      context?: Record<string, any>;
    } = {},
  ) =>
    request<ListResponse>("GET", `/models/${model}`, {
      query: {
        domain: json(p.domain),
        fields: p.fields?.join(","),
        order: p.order,
        limit: p.limit,
        offset: p.offset,
        count: p.count,
        context: json(p.context),
      },
    }),
  get: (model: string, id: number, fields?: string[]) =>
    request<RecordData>("GET", `/models/${model}/${id}`, { query: { fields: fields?.join(",") } }),
  getWithEtag: async (
    model: string,
    id: number,
    fields?: string[],
  ): Promise<{ record: RecordData; etag: string | null }> => {
    const response = await request<Response>("GET", `/models/${model}/${id}`, {
      query: { fields: fields?.join(",") },
      raw: true,
    });
    return { record: (await response.json()) as RecordData, etag: response.headers.get("ETag") };
  },
  create: (model: string, values: RecordData, fields?: string[]) =>
    request<RecordData>("POST", `/models/${model}`, { body: { values, fields } }),
  update: (model: string, id: number, values: RecordData, opts: { fields?: string[]; etag?: string | null } = {}) =>
    request<RecordData>("PATCH", `/models/${model}/${id}`, {
      body: { values, fields: opts.fields },
      headers: opts.etag ? { "If-Match": opts.etag } : undefined,
    }),
  remove: (model: string, id: number) => request<void>("DELETE", `/models/${model}/${id}`),
  readGroup: (
    model: string,
    p: { domain?: Domain; aggregates?: string[]; groupby: string[]; orderby?: string; limit?: number },
  ) =>
    request<{ groups: GroupRow[] }>("POST", `/models/${model}/read_group`, {
      body: {
        domain: p.domain,
        aggregates: p.aggregates ?? [],
        groupby: p.groupby,
        orderby: p.orderby,
        limit: p.limit,
      },
    }),
  nameSearch: (model: string, q: string, domain?: Domain, limit = 8) =>
    request<{ id: number; display_name: string }[]>("GET", `/models/${model}/name_search`, {
      query: { q, domain: json(domain), limit },
    }),
  call: (
    model: string,
    id: number | null,
    method: string,
    args: unknown[] = [],
    kwargs: Record<string, unknown> = {},
  ) =>
    request<{ result: any }>("POST", id ? `/models/${model}/${id}/call/${method}` : `/models/${model}/call/${method}`, {
      body: { args, kwargs },
    }),
};
