import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { mockFetch } from "../../test/mockApi";
import type { Proposal } from "../../kernel/ai/api";
import { formPanelsFor } from "../../kernel/extensions";
import { EnrichmentSuggestions, PossibleDuplicates, SuggestedTags } from "./ContactsAi";

const wrap = (ui: React.ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );

const status =
  (features: string[], available = true) =>
  () => ({
    available,
    features: Object.fromEntries(features.map((f) => [f, { title: f }])),
  });

const proposal = (over: Partial<Proposal> = {}): Proposal => ({
  id: 50,
  kind: "write",
  title: "Update",
  res_model: "contacts.partner",
  res_id: 7,
  payload: {},
  original: {},
  state: "pending",
  reason: null,
  ai_generated: true,
  run_id: 21,
  feature: "contacts.tag_suggest",
  ...over,
});

describe("contacts AI client", () => {
  it("adds three panels to the contact form", () => {
    expect(formPanelsFor("contacts.partner").map((p) => p.title)).toEqual([
      "Possible duplicates",
      "Suggested tags",
      "Enrichment suggestions",
    ]);
  });

  it("lists duplicates with reasons and proposes a merge that waits for acceptance", async () => {
    const reload = vi.fn();
    const merge = proposal({
      id: 60,
      kind: "action",
      title: "Merge Lucia Ferarro into Lucia Ferraro",
      payload: { action: "contacts.merge", args: { merge_ids: [8] } },
      feature: "contacts.duplicates",
      run_id: null,
    });
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": status(["contacts.duplicates"]),
      "GET /api/v1/contacts/7/ai/duplicates": () => ({
        embedding: "ok",
        candidates: [
          { id: 8, display_name: "Lucia Ferarro", score: 0.97, duplicate: true, reasons: ["Same e-mail address"] },
          { id: 9, display_name: "Marco Bianchi", score: 0.22, duplicate: false, reasons: [] },
        ],
      }),
      "POST /api/v1/contacts/7/ai/duplicates/propose-merge": () => merge,
      "POST /api/v1/ai/proposals/60/accept": () => ({ ...merge, state: "accepted", ai_generated: false }),
    });
    wrap(<PossibleDuplicates resId={7} reload={reload} />);
    const list = await screen.findByRole("list", { name: "Possible duplicates" });
    expect(within(list).getByText("likely · 97%")).toBeInTheDocument();
    expect(within(list).getByText("Same e-mail address")).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("Select Lucia Ferarro"));
    await userEvent.click(screen.getByRole("button", { name: "Propose merge" }));
    expect(await screen.findByText("Merge Lucia Ferarro into Lucia Ferraro")).toBeInTheDocument();
    expect(calls.find((c) => c.path.endsWith("/propose-merge"))?.body).toEqual({ ids: [8] });
    expect(calls.some((c) => c.path.includes("/accept"))).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "Accept" }));
    await waitFor(() => expect(reload).toHaveBeenCalled());
  });

  it("suggests tags with an AI label until the proposal is accepted", async () => {
    const reload = vi.fn();
    const tagProposal = proposal({ payload: { tag_ids: [["add", 3]] }, original: { tag_ids: [] } });
    mockFetch({
      "GET /api/v1/ai/status": status(["contacts.tag_suggest"]),
      "POST /api/v1/ai/features/contacts.tag_suggest/run": () => ({
        run_id: 21,
        feature: "contacts.tag_suggest",
        text: "{}",
        data: {
          suggested: [{ tag_id: 3, name: "Supplier", confidence: 0.9, reason: "We buy parts from them" }],
          parent: { id: 4, display_name: "Kranich Labs", reason: "same domain" },
        },
        ai_generated: true,
        citations: [],
        flags: [],
        proposals: [tagProposal],
        status: "ok",
      }),
      "POST /api/v1/ai/proposals/50/accept": () => ({ ...tagProposal, state: "accepted", ai_generated: false }),
    });
    wrap(<SuggestedTags resId={7} reload={reload} />);
    await userEvent.click(await screen.findByRole("button", { name: "Suggest tags" }));
    expect(await screen.findByText("Supplier")).toBeInTheDocument();
    expect(screen.getByText("We buy parts from them")).toBeInTheDocument();
    expect(screen.getByText("Kranich Labs")).toBeInTheDocument();
    expect(screen.getAllByRole("note").length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole("button", { name: "Accept" }));
    await waitFor(() => expect(reload).toHaveBeenCalled());
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("queues enrichment in the background and shows each value with its source", async () => {
    const enrich = proposal({
      id: 70,
      feature: "contacts.enrich",
      run_id: 33,
      payload: { website: "https://northwind-traders.test" },
      original: { website: null },
    });
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": status(["contacts.enrich"]),
      "GET /api/v1/ai/proposals": () => [enrich, proposal({ id: 71, feature: "contacts.tag_suggest" })],
      "GET /api/v1/ai/runs/33": () => ({
        output_data: {
          suggestions: [
            {
              field: "website",
              value: "https://northwind-traders.test",
              current: null,
              source: "contacts.enrichment_source:1",
              source_title: "Company registry",
              source_url: "https://registry.fixture.test/gb/04512877",
              entity: "Northwind Traders",
            },
          ],
        },
      }),
      "POST /api/v1/contacts/7/ai/enrich": () => ({ job_id: 1, state: "queued" }),
    });
    wrap(<EnrichmentSuggestions resId={7} reload={() => undefined} />);
    const table = await screen.findByRole("table", { name: "Suggested values and their sources" });
    expect(within(table).getByRole("link", { name: "Company registry" })).toHaveAttribute(
      "href",
      "https://registry.fixture.test/gb/04512877",
    );
    expect(screen.getAllByText("Update")).toHaveLength(1); // only the enrichment proposal is shown here
    await userEvent.click(screen.getByRole("button", { name: "Find missing details" }));
    expect(await screen.findByText(/You will get a notification/)).toBeInTheDocument();
    expect(calls.some((c) => c.method === "POST" && c.path === "/api/v1/contacts/7/ai/enrich")).toBe(true);
  });

  it("is hidden when switched off and says when AI is unavailable", async () => {
    mockFetch({ "GET /api/v1/ai/status": status([]) });
    const off = wrap(<SuggestedTags resId={7} reload={() => undefined} />);
    await waitFor(() => expect(off.container.querySelector(".ai-panel-hidden")).not.toBeNull());
    off.unmount();
    mockFetch({ "GET /api/v1/ai/status": status(["contacts.enrich"], false) });
    wrap(<EnrichmentSuggestions resId={7} reload={() => undefined} />);
    expect(await screen.findByText("AI unavailable")).toBeInTheDocument();
  });
});
