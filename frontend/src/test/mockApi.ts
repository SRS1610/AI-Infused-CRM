// A tiny fake backend for component tests: routes fetch() calls to handlers.
import { vi } from "vitest";

type Handler = (url: URL, init: RequestInit) => unknown | Promise<unknown>;
export interface MockCall {
  method: string;
  path: string;
  url: URL;
  body: any;
  headers: Record<string, string>;
}

export function mockFetch(routes: Record<string, Handler>) {
  const calls: MockCall[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input), "http://localhost");
    const method = (init.method ?? "GET").toUpperCase();
    const key = `${method} ${url.pathname}`;
    const body = typeof init.body === "string" ? JSON.parse(init.body) : init.body;
    calls.push({ method, path: url.pathname, url, body, headers: (init.headers ?? {}) as Record<string, string> });
    const handler =
      routes[key] ?? Object.entries(routes).find(([k]) => k.endsWith("*") && key.startsWith(k.slice(0, -1)))?.[1];
    if (!handler) return new Response(JSON.stringify({ error: "NotMocked", message: key }), { status: 404 });
    const result = await handler(url, init);
    if (result instanceof Response) return result;
    return new Response(result === undefined ? null : JSON.stringify(result), {
      status: result === undefined ? 204 : 200,
      headers: { "Content-Type": "application/json", ETag: '"etag-1"' },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}
