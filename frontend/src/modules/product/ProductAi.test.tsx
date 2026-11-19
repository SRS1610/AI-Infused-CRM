import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { mockFetch } from "../../test/mockApi";
import { formPanelsFor } from "../../kernel/extensions";
import { DescriptionDraft } from "./ProductAi";

const wrap = (ui: React.ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );

const status =
  (features: string[], available = true) =>
  () => ({ available, features: Object.fromEntries(features.map((f) => [f, { title: f }])) });

const run = (proposals: unknown[]) => () => ({
  run_id: 60,
  feature: "product.description_draft",
  text: "A ceramic mug in white or navy.",
  ai_generated: true,
  citations: [],
  flags: [],
  status: "ok",
  data: {
    description: "A ceramic mug in white or navy.",
    removed: [{ sentence: "It holds 350 ml.", unsupported: ["350"] }],
  },
  proposals,
});

describe("product AI client", () => {
  it("adds the description panel to the product form", () => {
    expect(formPanelsFor("product.product").map((p) => p.title)).toContain("Sales description draft");
  });

  it("drafts a description as a proposal and lists what the spec check removed", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": status(["product.description_draft"]),
      "POST /api/v1/ai/features/product.description_draft/run": run([
        {
          id: 80,
          kind: "write",
          title: "Sales description for Ceramic mug",
          res_model: "product.product",
          res_id: 4,
          payload: { description_sale: "A ceramic mug in white or navy." },
          original: { description_sale: null },
          state: "pending",
          reason: null,
          ai_generated: true,
          run_id: 60,
          feature: "product.description_draft",
        },
      ]),
    });
    wrap(<DescriptionDraft resId={4} reload={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Draft a sales description" }));
    expect(await screen.findByText("Sales description for Ceramic mug")).toBeInTheDocument();
    const removed = screen.getByRole("list", { name: "Removed sentences" });
    expect(within(removed).getByText("“It holds 350 ml.” (350)")).toBeInTheDocument();
    expect(calls.find((c) => c.path.endsWith("/run"))?.body).toEqual({ res_model: "product.product", res_id: 4 });
  });

  it("shows the text with the AI label when the user cannot change the product", async () => {
    mockFetch({
      "GET /api/v1/ai/status": status(["product.description_draft"]),
      "POST /api/v1/ai/features/product.description_draft/run": run([]),
    });
    wrap(<DescriptionDraft resId={4} reload={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Draft a sales description" }));
    expect(await screen.findByText("A ceramic mug in white or navy.")).toBeInTheDocument();
    expect(screen.getByRole("note", { name: "AI-drafted: review before using" })).toBeInTheDocument();
  });

  it("is hidden when switched off and says so when the gateway is down", async () => {
    mockFetch({ "GET /api/v1/ai/status": status([]) });
    const { container, unmount } = wrap(<DescriptionDraft resId={4} reload={() => {}} />);
    await screen.findAllByText((_, el) => el?.classList.contains("ai-panel-hidden") ?? false);
    expect(container.querySelector("button")).toBeNull();
    unmount();
    mockFetch({ "GET /api/v1/ai/status": status(["product.description_draft"], false) });
    wrap(<DescriptionDraft resId={4} reload={() => {}} />);
    expect(await screen.findByText("AI unavailable")).toBeInTheDocument();
  });
});
