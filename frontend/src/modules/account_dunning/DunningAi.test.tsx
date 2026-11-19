import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import { mockFetch } from "../../test/mockApi";
import { formPanelsFor } from "../../kernel/extensions";
import { DUNNING_DRAFT } from "./DunningAi";

const wrap = (ui: React.ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );

const info = {
  title: "Personalised payment reminders",
  task_class: "draft",
  interactive: true,
  writes: true,
  outbound: true,
  disclosure: "ai_drafted",
  risk_class: "limited",
};
const status = (on = true, available = true) => ({ available, features: on ? { [DUNNING_DRAFT]: info } : {} });

function panel(record: Record<string, unknown>, reload = () => undefined) {
  const p = formPanelsFor("account.dunning_run_line").find((x) => x.title === "Personalised message (AI)");
  expect(p).toBeDefined();
  return wrap(p!.render({ model: "account.dunning_run_line", resId: 21, record, reload }));
}

const proposal = {
  id: 90,
  kind: "write",
  title: "Personalised reminder for Acme Corp",
  res_model: "account.dunning_run_line",
  res_id: 21,
  payload: { subject: "Reminder: 1,200.00 USD overdue", message: "<p>Dear Acme…</p>" },
  original: { subject: "Payment reminder", message: "<p>Standard</p>" },
  state: "pending",
  reason: null,
  ai_generated: true,
  run_id: 14,
  feature: DUNNING_DRAFT,
};

describe("Personalised dunning message", () => {
  it("drafts the reminder with its tone and lets the user accept the proposal", async () => {
    const reload = vi.fn();
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": () => status(),
      "POST /api/v1/ai/features/account_dunning.message_draft/run": () => ({
        run_id: 14,
        feature: DUNNING_DRAFT,
        text: "Dear Acme…",
        data: {
          subject: "Reminder: 1,200.00 USD overdue",
          body: "Dear Acme, invoice INV/2026/00012 of 1,200.00 USD is 20 days late.",
          removed_numbers: ["5%"],
          tone: "firm",
        },
        ai_generated: true,
        citations: [],
        flags: ["numbers_removed"],
        proposals: [proposal],
        status: "ok",
      }),
      "POST /api/v1/ai/proposals/90/accept": () => ({ ...proposal, state: "accepted", ai_generated: false }),
    });
    panel({ state: "draft" }, reload);
    await userEvent.click(await screen.findByRole("button", { name: "Draft personalised message" }));
    expect(await screen.findByText(/is 20 days late/)).toBeInTheDocument();
    expect(screen.getByText("Tone: firm")).toBeInTheDocument();
    expect(screen.getByText(/were removed: 5%/)).toBeInTheDocument();
    expect(calls.find((c) => c.path.endsWith("message_draft/run"))?.body).toEqual({
      res_model: "account.dunning_run_line",
      res_id: 21,
    });
    await userEvent.click(screen.getByRole("button", { name: "Accept" }));
    await waitFor(() => expect(reload).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: "Draft again" })).toBeInTheDocument();
  });

  it("is hidden once sent and when the feature is off; shows errors", async () => {
    mockFetch({ "GET /api/v1/ai/status": () => status() });
    const sent = panel({ state: "sent" });
    expect(sent.container.querySelector(".ai-panel-hidden")).not.toBeNull();
    sent.unmount();
    mockFetch({ "GET /api/v1/ai/status": () => status(false) });
    const off = panel({ state: "draft" });
    await waitFor(() => expect(off.container.querySelector(".ai-panel-hidden")).not.toBeNull());
    off.unmount();
    mockFetch({
      "GET /api/v1/ai/status": () => status(),
      "POST /api/v1/ai/features/account_dunning.message_draft/run": () =>
        new Response(JSON.stringify({ code: "feature_disabled", message: "off" }), { status: 404 }),
    });
    panel({ state: "no_email" });
    await userEvent.click(await screen.findByRole("button", { name: "Draft personalised message" }));
    expect(await screen.findByText("This AI feature is turned off.")).toBeInTheDocument();
  });
});
