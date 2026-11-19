import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { mockFetch } from "../../test/mockApi";
import type { Proposal, RunResult } from "../../kernel/ai/api";
import { formPanelsFor, searchAddonsFor } from "../../kernel/extensions";
import { MeetingBrief, ScheduleWithAi, SuggestedTimes } from "./CalendarAi";

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
  id: 77,
  kind: "create",
  title: "New meeting: Lunch with Priya Nair, Tue 17 Nov 12:30",
  res_model: "calendar.event",
  res_id: null,
  payload: { name: "Lunch with Priya Nair", start: "2026-11-17T17:30:00+00:00", allday: false, location: "Café Lumen" },
  original: null,
  state: "pending",
  reason: null,
  ai_generated: true,
  run_id: 12,
  feature: "calendar.nl_schedule",
  ...over,
});

describe("calendar AI client", () => {
  it("registers the meeting panels and the scheduling input", () => {
    expect(formPanelsFor("calendar.event").map((p) => p.title)).toEqual(["Meeting brief", "Suggested times"]);
    expect(searchAddonsFor("calendar.event").map((a) => a.key)).toEqual(["calendar.nl_schedule"]);
  });

  it("prepares a labelled brief with sources and feedback", async () => {
    const run: RunResult = {
      run_id: 5,
      feature: "calendar.meeting_brief",
      text: "Purpose: review the quote [source:crm.lead:4].",
      data: null,
      ai_generated: true,
      citations: [{ id: "crm.lead:4", res_model: "crm.lead", res_id: 4, title: "Harbor Freight: telematics" }],
      flags: [],
      proposals: [],
      status: "ok",
    };
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": status(["calendar.meeting_brief"]),
      "GET /api/v1/calendar/events/9/brief": () => ({ brief: null }),
      "POST /api/v1/ai/features/calendar.meeting_brief/run": () => run,
      "POST /api/v1/ai/runs/5/feedback": () => ({ id: 1 }),
    });
    wrap(<MeetingBrief resId={9} />);
    await userEvent.click(await screen.findByRole("button", { name: "Prepare brief" }));
    expect(await screen.findByText("Purpose: review the quote.")).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("AI-generated brief");
    expect(screen.getByRole("link", { name: "Harbor Freight: telematics" })).toHaveAttribute("href", "/m/crm.lead/4");
    await userEvent.click(screen.getByTitle("Helpful"));
    const post = calls.find((c) => c.path.endsWith("/run"));
    expect(post?.body).toEqual({ res_model: "calendar.event", res_id: 9 });
    expect(calls.some((c) => c.path === "/api/v1/ai/runs/5/feedback")).toBe(true);
  });

  it("shows a brief prepared in the background, hides when off and says when AI is unavailable", async () => {
    mockFetch({
      "GET /api/v1/ai/status": status(["calendar.meeting_brief"]),
      "GET /api/v1/calendar/events/9/brief": () => ({ brief: { run_id: 3, text: "Prepared earlier.", citations: [] } }),
    });
    const { unmount } = wrap(<MeetingBrief resId={9} />);
    expect(await screen.findByText("Prepared earlier.")).toBeInTheDocument();
    unmount();
    mockFetch({ "GET /api/v1/ai/status": status([]) });
    const off = wrap(<MeetingBrief resId={9} />);
    await waitFor(() => expect(off.container.querySelector(".ai-panel-hidden")).not.toBeNull());
    expect(screen.queryByRole("button")).toBeNull();
    off.unmount();
    mockFetch({ "GET /api/v1/ai/status": status(["calendar.meeting_brief"], false) });
    wrap(<MeetingBrief resId={9} />);
    expect(await screen.findByText("AI unavailable")).toBeInTheDocument();
  });

  it("suggests times and moves the meeting only when the user clicks", async () => {
    const reload = vi.fn();
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": status(["calendar.suggest_times"]),
      "POST /api/v1/calendar/ai/suggest-times": () => ({
        slots: [
          {
            start: "2030-03-04T15:30:00+00:00",
            stop: "2030-03-04T16:00:00+00:00",
            score: 9.9,
            reasons: ["All 2 attendees are free"],
          },
        ],
      }),
      "PATCH /api/v1/models/calendar.event/9": () => ({ id: 9 }),
    });
    wrap(<SuggestedTimes resId={9} reload={reload} />);
    await userEvent.selectOptions(await screen.findByLabelText("Preferred time of day"), "morning");
    await userEvent.click(screen.getByRole("button", { name: "Find times" }));
    expect(await screen.findByText(/All 2 attendees are free/)).toBeInTheDocument();
    expect(calls.find((c) => c.path.endsWith("/suggest-times"))?.body).toEqual({ event_id: 9, prefer: "morning" });
    expect(calls.some((c) => c.method === "PATCH")).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: /Move the meeting to/ }));
    await waitFor(() => expect(reload).toHaveBeenCalled());
    expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({
      values: { start: "2030-03-04T15:30:00+00:00", stop: "2030-03-04T16:00:00+00:00" },
    });
  });

  it("turns a request into a proposal the user reviews, with unmatched names reported", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": status(["calendar.nl_schedule"]),
      "POST /api/v1/ai/features/calendar.nl_schedule/run": () => ({
        run_id: 12,
        feature: "calendar.nl_schedule",
        text: "{}",
        data: {
          resolved: [{ id: 3, name: "Priya Nair" }],
          unresolved: [
            {
              name: "Alex",
              candidates: [
                { id: 4, name: "Alex Chen" },
                { id: 5, name: "Alex Moreno" },
              ],
            },
          ],
          problems: [],
        },
        ai_generated: true,
        citations: [],
        flags: [],
        proposals: [proposal()],
        status: "ok",
      }),
      "POST /api/v1/ai/proposals/77/accept": () => proposal({ state: "accepted", ai_generated: false }),
    });
    wrap(<ScheduleWithAi />);
    await userEvent.click(await screen.findByRole("button", { name: /Schedule with AI/ }));
    await userEvent.type(screen.getByLabelText("Describe the meeting"), "Lunch with Priya and Alex next Tuesday 12:30");
    await userEvent.click(screen.getByRole("button", { name: "Draft" }));
    expect(await screen.findByText("Invites: Priya Nair")).toBeInTheDocument();
    expect(
      screen.getByText(/“Alex” was not added: several contacts match \(Alex Chen, Alex Moreno\)/),
    ).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("AI-drafted");
    expect(calls.find((c) => c.path.endsWith("/run"))?.body).toEqual({
      inputs: { text: "Lunch with Priya and Alex next Tuesday 12:30" },
    });
    expect(calls.some((c) => c.path.includes("/accept"))).toBe(false); // nothing is created yet
    await userEvent.click(screen.getByRole("button", { name: "Accept" }));
    expect(await screen.findByText("accepted")).toBeInTheDocument();
    expect(screen.queryByRole("note")).toBeNull(); // the label goes once a person accepted it
  });
});
