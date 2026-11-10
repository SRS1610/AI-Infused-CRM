import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { mockFetch } from "../../test/mockApi";
import { ApiError } from "../api/client";
import { Chatter } from "../chatter/Chatter";
import { clientAction, registerAiDraft } from "../extensions";
import { aiErrorMessage, streamFeature, type Proposal, type RunResult } from "./api";
import { AiLabel, Citations, ProposalCard } from "./components";
import "../../modules/ai_core/AiDashboard";

const wrap = (ui: React.ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );

const sse = (events: object[]) =>
  new Response(events.map((e) => `event: x\ndata: ${JSON.stringify(e)}\n\n`).join(""), {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });

const proposal = (over: Partial<Proposal> = {}): Proposal => ({
  id: 41,
  kind: "message",
  title: "Reply",
  res_model: "demo.deal",
  res_id: 7,
  payload: { body: "Dear Paul, thanks." },
  original: null,
  state: "pending",
  reason: null,
  ai_generated: true,
  run_id: 9,
  feature: "demo.reply",
  ...over,
});

const result = (over: Partial<RunResult> = {}): RunResult => ({
  run_id: 9,
  feature: "demo.reply",
  text: "Dear Paul, thanks.",
  data: null,
  ai_generated: true,
  citations: [],
  flags: [],
  proposals: [proposal()],
  status: "ok",
  ...over,
});

const chatterRoutes = {
  "GET /api/v1/chatter/demo.deal/7/messages": () => ({ items: [], has_more: false }),
  "GET /api/v1/chatter/demo.deal/7/followers": () => ({ items: [], following: false, subtypes: [] }),
  "GET /api/v1/chatter/demo.deal/7/activities": () => [],
};

describe("AI client", () => {
  it("streams a draft into the composer, labels it until edited and sends it through the proposal", async () => {
    registerAiDraft("demo.deal", "demo.reply");
    const { calls } = mockFetch({
      ...chatterRoutes,
      "GET /api/v1/ai/status": () => ({ available: true, features: { "demo.reply": { title: "Reply draft" } } }),
      "POST /api/v1/ai/features/demo.reply/stream": () =>
        sse([
          { event: "start", run_id: 9 },
          { event: "delta", text: "Dear Paul, " },
          { event: "delta", text: "thanks." },
          { event: "done", result: result() },
        ]),
      "POST /api/v1/ai/proposals/41/accept": () => proposal({ state: "edited", ai_generated: false }),
    });
    wrap(<Chatter model="demo.deal" resId={7} />);
    await userEvent.click(await screen.findByRole("button", { name: "Send message" }));
    await userEvent.click(await screen.findByRole("button", { name: /Draft with AI/ }));
    const box = screen.getByLabelText("Message to followers") as HTMLTextAreaElement;
    await waitFor(() => expect(box.value).toBe("Dear Paul, thanks."));
    expect(screen.getByRole("note", { name: /AI-drafted/ })).toBeInTheDocument();
    await userEvent.type(box, " Best, Dana");
    expect(screen.queryByRole("note", { name: /AI-drafted/ })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v1/ai/proposals/41/accept")).toBe(true));
    const accept = calls.find((c) => c.path === "/api/v1/ai/proposals/41/accept")!;
    expect(accept.body.payload.body).toBe("<p>Dear Paul, thanks. Best, Dana</p>");
    expect(calls.some((c) => c.method === "POST" && c.path === "/api/v1/chatter/demo.deal/7/messages")).toBe(false);
  });

  it("shows 'AI unavailable' instead of the draft button when the gateway is down, and nothing when disabled", async () => {
    registerAiDraft("demo.deal", "demo.reply");
    mockFetch({
      ...chatterRoutes,
      "GET /api/v1/ai/status": () => ({ available: false, features: { "demo.reply": {} } }),
    });
    const { unmount } = wrap(<Chatter model="demo.deal" resId={7} />);
    await userEvent.click(await screen.findByRole("button", { name: "Send message" }));
    expect(await screen.findByText("AI unavailable")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Draft with AI/ })).not.toBeInTheDocument();
    unmount();
    mockFetch({ ...chatterRoutes, "GET /api/v1/ai/status": () => ({ available: true, features: {} }) });
    wrap(<Chatter model="demo.deal" resId={7} />);
    await userEvent.click(await screen.findByRole("button", { name: "Send message" }));
    await screen.findByRole("button", { name: "Send" });
    await waitFor(() => expect(screen.queryByRole("button", { name: /Draft with AI/ })).not.toBeInTheDocument());
  });

  it("reports a stream error with a manual-path message and keeps the composer usable", async () => {
    registerAiDraft("demo.deal", "demo.reply");
    mockFetch({
      ...chatterRoutes,
      "GET /api/v1/ai/status": () => ({ available: true, features: { "demo.reply": {} } }),
      "POST /api/v1/ai/features/demo.reply/stream": () =>
        sse([
          { event: "start", run_id: 3 },
          { event: "error", error: { code: "ai_unavailable", message: "down" } },
        ]),
    });
    wrap(<Chatter model="demo.deal" resId={7} />);
    await userEvent.click(await screen.findByRole("button", { name: "Send message" }));
    await userEvent.click(await screen.findByRole("button", { name: /Draft with AI/ }));
    expect(await screen.findByText(/AI is unavailable right now/)).toBeInTheDocument();
    expect(screen.getByLabelText("Message to followers")).not.toBeDisabled();
  });

  it("reviews a write proposal: diff, edit, accept", async () => {
    const { calls } = mockFetch({
      "POST /api/v1/ai/proposals/5/accept": () =>
        proposal({ id: 5, kind: "write", state: "edited", payload: { amount: 250 }, ai_generated: false }),
    });
    const onDecided = vi.fn();
    wrap(
      <ProposalCard
        proposal={proposal({
          id: 5,
          kind: "write",
          title: "Update deal",
          payload: { amount: 200 },
          original: { amount: 100 },
        })}
        onDecided={onDecided}
      />,
    );
    expect(screen.getByRole("cell", { name: "100" })).toBeInTheDocument();
    expect(screen.getByRole("note", { name: /AI-drafted/ })).toBeInTheDocument();
    const input = screen.getByLabelText("Proposed amount");
    await userEvent.clear(input);
    await userEvent.type(input, "250");
    await userEvent.click(screen.getByRole("button", { name: "Accept edited" }));
    await waitFor(() => expect(onDecided).toHaveBeenCalled());
    expect(calls[0].body).toEqual({ payload: { amount: 250 } });
    expect(screen.getByText("edited")).toBeInTheDocument();
  });

  it("rejects a proposal and shows failures", async () => {
    mockFetch({
      "POST /api/v1/ai/proposals/6/reject": () => proposal({ id: 6, state: "rejected" }),
      "POST /api/v1/ai/proposals/7/accept": () =>
        new Response(JSON.stringify({ error: "ProposalFailed", message: "Not allowed", code: "x" }), { status: 409 }),
    });
    wrap(<ProposalCard proposal={proposal({ id: 6, kind: "create", payload: { name: "X" } })} />);
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(await screen.findByText("rejected")).toBeInTheDocument();
    wrap(<ProposalCard proposal={proposal({ id: 7 })} />);
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("Not allowed")).toBeInTheDocument();
  });

  it("renders the cost dashboard and flips a kill switch", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/ai/admin/usage": () => ({
        available: true,
        since: "2026-11-01",
        rows: [
          {
            company_id: 1,
            company: "My Company",
            feature: "crm.followup_draft",
            feature_title: "Follow-up draft",
            requests: 12,
            blocked: 0,
            input_tokens: 1000,
            output_tokens: 300,
            cache_read_tokens: 50,
            cost: "0.012345",
            latency_p50: 800,
            latency_p95: 1900,
            fallback_rate: 0,
            cache_hit_rate: 0.25,
            acceptance_rate: 0.75,
            decisions: { accepted: 3, rejected: 1 },
          },
        ],
      }),
      "GET /api/v1/ai/admin/features": () => [
        {
          name: "crm.followup_draft",
          title: "Follow-up draft",
          module: "crm",
          task_class: "draft",
          risk_class: "limited",
          writes: false,
          outbound: true,
          enabled_global: true,
          companies: [
            { id: 1, name: "My Company", enabled: true },
            { id: 2, name: "Europe", enabled: true },
          ],
        },
      ],
      "PUT /api/v1/ai/admin/features/crm.followup_draft/flag": () => ({ enabled: false }),
    });
    wrap(clientAction("ai.dashboard")!());
    expect(await screen.findByRole("cell", { name: "Follow-up draft" })).toBeInTheDocument();
    expect(screen.getByText("75%")).toBeInTheDocument();
    expect(screen.getByText("1,900 ms".replace(",", "")) ?? screen.getByText(/1900 ms/)).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("Follow-up draft enabled for Europe"));
    await waitFor(() => expect(calls.some((c) => c.method === "PUT")).toBe(true));
    expect(calls.find((c) => c.method === "PUT")!.body).toEqual({ enabled: false, company_id: 2 });
  });

  it("dashboard shows the gateway outage", async () => {
    mockFetch({
      "GET /api/v1/ai/admin/usage": () => ({ available: false, message: "unreachable", rows: [], since: "2026-11-01" }),
      "GET /api/v1/ai/admin/features": () => [],
    });
    wrap(clientAction("ai.dashboard")!());
    expect(await screen.findByText(/gateway is unavailable: unreachable/)).toBeInTheDocument();
    expect(await screen.findByText("No AI features are installed yet.")).toBeInTheDocument();
  });

  it("parses SSE across chunk boundaries and maps errors", async () => {
    const body =
      'event: delta\ndata: {"event":"delta","text":"He"}\n\nevent: delta\ndata: {"event":"delta","text":"llo"}\n\nevent: done\ndata: {"event":"done","result":{"run_id":1,"text":"Hello","proposals":[]}}\n\n';
    const stream = new ReadableStream({
      start(controller) {
        const enc = new TextEncoder();
        for (let i = 0; i < body.length; i += 7) controller.enqueue(enc.encode(body.slice(i, i + 7)));
        controller.close();
      },
    });
    mockFetch({ "POST /api/v1/ai/features/x/stream": () => new Response(stream, { status: 200 }) });
    const seen: string[] = [];
    const res = await streamFeature("x", {}, (ev) => seen.push(ev.event === "delta" ? ev.text : ev.event));
    expect(seen).toEqual(["He", "llo", "done"]);
    expect(res?.text).toBe("Hello");
    mockFetch({
      "POST /api/v1/ai/features/x/stream": () =>
        new Response(JSON.stringify({ error: "FeatureDisabled", message: "off", code: "feature_disabled" }), {
          status: 404,
        }),
    });
    await expect(streamFeature("x", {}, () => undefined)).rejects.toBeInstanceOf(ApiError);
    expect(aiErrorMessage(new ApiError(429, { error: "B", message: "m", code: "budget_exceeded" } as never))).toMatch(
      /budget/,
    );
    expect(aiErrorMessage(new Error("plain"))).toBe("plain");
  });

  it("renders the label and citations", () => {
    wrap(
      <>
        <AiLabel />
        <Citations items={[{ id: "crm.lead:4", res_model: "crm.lead", res_id: 4, title: "Acme deal" }]} />
      </>,
    );
    expect(screen.getByRole("link", { name: "Acme deal" })).toHaveAttribute("href", "/m/crm.lead/4");
  });
});
