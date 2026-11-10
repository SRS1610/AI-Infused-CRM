import { mockFetch } from "../../test/mockApi";
import { api, ApiError, configureClient } from "./client";

describe("api client", () => {
  afterEach(() => configureClient({ csrf: null, companies: null, company: null }));

  it("sends CSRF and company headers on unsafe requests and JSON-encodes domains", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/models/crm.lead": () => ({ items: [], total: 0, limit: 80, offset: 0 }),
      "PATCH /api/v1/models/crm.lead/7": () => ({ id: 7 }),
    });
    configureClient({ csrf: "tok", companies: [1, 2], company: 2 });
    await api.list("crm.lead", { domain: [["name", "ilike", "x"]], fields: ["name"], limit: 5 });
    await api.update("crm.lead", 7, { name: "y" }, { etag: '"e"' });
    expect(calls[0].url.searchParams.get("domain")).toBe('[["name","ilike","x"]]');
    expect(calls[0].headers["X-CSRF-Token"]).toBeUndefined();
    expect(calls[0].headers["X-Tessera-Companies"]).toBe("1,2");
    expect(calls[1].headers["X-CSRF-Token"]).toBe("tok");
    expect(calls[1].headers["If-Match"]).toBe('"e"');
    expect(calls[1].body).toEqual({ values: { name: "y" }, fields: undefined });
  });

  it("turns error bodies into ApiError and reports 401", async () => {
    const onUnauthorized = vi.fn();
    configureClient({ onUnauthorized });
    mockFetch({
      "PATCH /api/v1/models/x/1": () =>
        new Response(JSON.stringify({ error: "ConcurrencyError", message: "stale" }), { status: 409 }),
      "GET /api/v1/web/menus": () =>
        new Response(JSON.stringify({ error: "AccessDenied", message: "login" }), { status: 401 }),
    });
    const conflict = await api.update("x", 1, {}).catch((e) => e);
    expect(conflict).toBeInstanceOf(ApiError);
    expect(conflict.isConflict).toBe(true);
    expect(conflict.message).toBe("stale");
    await expect(api.menus()).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("keeps Retry-After on 429 so queries wait as the server asks before retrying", async () => {
    mockFetch({
      "GET /api/v1/web/menus": () =>
        new Response(JSON.stringify({ error: "RateLimited", message: "Rate limit exceeded" }), {
          status: 429,
          headers: { "Retry-After": "7" },
        }),
    });
    const limited = await api.menus().catch((e) => e);
    expect(limited).toMatchObject({ status: 429, retryAfter: 7 });
    const { createQueryClient } = await import("../../App");
    const { retry, retryDelay } = createQueryClient().getDefaultOptions().queries as {
      retry: (n: number, e: unknown) => boolean;
      retryDelay: (n: number, e: unknown) => number;
    };
    expect(retry(0, limited)).toBe(true);
    expect(retryDelay(0, limited)).toBe(7000);
    expect(retry(0, new ApiError(403, { error: "AccessDenied", message: "no" }))).toBe(false);
  });

  it("reads ETags", async () => {
    mockFetch({ "GET /api/v1/models/x/3": () => ({ id: 3, name: "n" }) });
    const { record, etag } = await api.getWithEtag("x", 3, ["name"]);
    expect(record.name).toBe("n");
    expect(etag).toBe('"etag-1"');
  });
});
