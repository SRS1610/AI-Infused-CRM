import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { mockFetch } from "../../test/mockApi";
import { clientAction } from "../../kernel/extensions";
import { SessionProvider } from "../../kernel/session";
import "./DiscussApp";

const session = () => ({
  user: { id: 2, login: "admin", name: "Admin", lang: "fr_FR", tz: "UTC", share: false },
  allowed_companies: [],
  selected_companies: [],
  groups: [],
  csrf_token: "c",
  lang: "fr_FR",
  modules: ["discuss"],
});

const channel = (id: number, name: string, unread = 0) => ({
  id,
  name,
  description: null,
  type: "channel",
  privacy: "public",
  member: true,
  member_count: 3,
  notify: "mentions",
  unread,
  partner_user_id: null,
});

const message = (id: number, body: string, author = { id: 3, name: "Dana Demo" }) => ({
  id,
  type: "comment",
  internal: false,
  subject: null,
  body,
  date: "2026-11-10T09:00:00Z",
  author,
  email_from: null,
  tracking: [],
  attachments: [],
  reactions: [],
  ai_assisted: false,
  editable: false,
});

const ALL = ["discuss.summary", "discuss.catch_up", "discuss.reply_draft", "discuss.translate"];
const status = (available = true, features = ALL) => ({
  available,
  features: Object.fromEntries(features.map((f) => [f, { title: f }])),
});

const run = (over: Record<string, unknown> = {}) => ({
  run_id: 77,
  feature: "discuss.summary",
  text: "- Launch on 3 March [source:chatter.message:11]\n- Screenshots by 18 February [source:chatter.message:12]",
  data: null,
  ai_generated: true,
  citations: [],
  flags: [],
  proposals: [],
  status: "ok",
  ...over,
});

const base = (ai = status()) => ({
  "GET /api/v1/auth/session": session,
  "GET /api/v1/ai/status": () => ai,
  "GET /api/v1/discuss/channels": () => [channel(1, "general"), channel(2, "sales", 3)],
  "GET /api/v1/discuss/presence": () => ({ online: [] }),
  "POST /api/v1/discuss/channels/*": () => ({ unread: 0 }),
  "GET /api/v1/chatter/discuss.channel/1/messages": () => ({
    items: [message(12, "<p>Screenshots by 18 Feb</p>"), message(11, "<p>Launch is on 3 March</p>")],
    has_more: false,
  }),
  "GET /api/v1/chatter/discuss.channel/2/messages": () => ({
    items: [message(21, "<p>Budget review on Thursday</p>")],
    has_more: false,
  }),
  "GET /api/v1/chatter/mentions": () => [],
});

function renderApp(path = "/") {
  const app = clientAction("discuss");
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <SessionProvider>
        <MemoryRouter initialEntries={[path]}>{app!()}</MemoryRouter>
      </SessionProvider>
    </QueryClientProvider>,
  );
}

const sse = (events: object[]) =>
  new Response(events.map((e) => `event: x\ndata: ${JSON.stringify(e)}\n\n`).join(""), {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });

describe("Discuss AI", () => {
  beforeEach(() => {
    Element.prototype.scrollIntoView = vi.fn();
  });

  it("summarises a channel with a label, feedback and links to the cited messages", async () => {
    const { calls } = mockFetch({
      ...base(),
      "POST /api/v1/discuss/ai/channels/1/summary": () => ({
        mode: "interactive",
        count: 2,
        result: run(),
        links: { "11": { channel_id: 1, author: "Dana Demo", date: "2026-11-10 09:00" }, "12": { channel_id: 1 } },
      }),
      "POST /api/v1/ai/runs/77/feedback": () => ({ id: 1 }),
    });
    renderApp();
    await userEvent.click(await screen.findByRole("button", { name: "✦ Summarize" }));
    const panel = await screen.findByRole("region", { name: "AI summary" });
    expect(within(panel).getByRole("note", { name: /AI-generated/ })).toBeInTheDocument();
    expect(within(panel).getByText("Launch on 3 March")).toBeInTheDocument();
    expect(within(panel).queryByText(/source:/)).not.toBeInTheDocument();
    await userEvent.click(
      within(panel).getByRole("button", { name: "Open message from Dana Demo at 2026-11-10 09:00" }),
    );
    await waitFor(() => expect(document.getElementById("discuss-msg-11")?.className).toContain("focused"));
    await userEvent.click(within(panel).getByTitle("Helpful"));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v1/ai/runs/77/feedback")).toBe(true));
  });

  it("runs large summaries in the background and opens them from the notification link", async () => {
    mockFetch({
      ...base(),
      "POST /api/v1/discuss/ai/channels/1/summary": () => ({ mode: "background", count: 120, job_id: 5 }),
      "GET /api/v1/ai/runs/55": () => ({
        id: 55,
        output_text: "- Budget review on Thursday [source:chatter.message:21]",
        safety_flags: [],
        citations: [{ id: "chatter.message:21", res_model: "chatter.message", res_id: 21, title: "Dana" }],
      }),
    });
    const first = renderApp();
    await userEvent.click(await screen.findByRole("button", { name: "✦ Summarize" }));
    expect(await screen.findByText(/120 messages: the summary is being prepared/)).toBeInTheDocument();
    first.unmount();
    renderApp("/a/discuss.action_discuss?channel=2&summary=55");
    const panel = await screen.findByRole("region", { name: "AI summary" });
    expect(await within(panel).findByText("Budget review on Thursday")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "#sales" })).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "Open message" })).toBeInTheDocument();
  });

  it("catches the user up with a ranked digest that links to channels", async () => {
    mockFetch({
      ...base(),
      "POST /api/v1/discuss/ai/catch-up": () => ({
        unread: 3,
        result: run({
          run_id: 78,
          feature: "discuss.catch_up",
          text: "1. #sales — budget review on Thursday [source:chatter.message:21]\n2. #general — launch on 3 March",
        }),
        links: { "21": { channel_id: 2, author: "Dana Demo", date: "09:00" } },
      }),
    });
    renderApp();
    await userEvent.click(await screen.findByRole("button", { name: "✦ Catch me up" }));
    const panel = await screen.findByRole("region", { name: "Catch me up" });
    expect(await within(panel).findByText("#sales — budget review on Thursday")).toBeInTheDocument();
    expect(within(panel).getByRole("note", { name: /AI-generated/ })).toBeInTheDocument();
    expect(within(panel).getByTitle("Helpful")).toBeInTheDocument();
    await userEvent.click(within(panel).getByRole("button", { name: /Open message from Dana Demo/ }));
    expect(await screen.findByRole("heading", { name: "#sales" })).toBeInTheDocument();
    await waitFor(() => expect(document.getElementById("discuss-msg-21")?.className).toContain("focused"));
  });

  it("translates a message into the user's language, labelled as AI", async () => {
    const { calls } = mockFetch({
      ...base(),
      "POST /api/v1/ai/features/discuss.translate/run": () =>
        run({ run_id: 79, feature: "discuss.translate", text: "Le lancement est le 3 mars" }),
    });
    renderApp();
    await userEvent.click((await screen.findAllByRole("button", { name: "Translate message to French" }))[0]);
    expect(await screen.findByText("Le lancement est le 3 mars")).toBeInTheDocument();
    expect(screen.getByRole("note", { name: /AI translation \(French\)/ })).toBeInTheDocument();
    const sent = calls.find((c) => c.path === "/api/v1/ai/features/discuss.translate/run")!;
    expect(sent.body).toEqual({ inputs: { message_id: 11, target_lang: "fr" } });
    await userEvent.selectOptions(screen.getByRole("combobox", { name: /Translate to/ }), "de");
    expect(screen.getAllByRole("button", { name: "Translate message to German" }).length).toBe(2);
  });

  it("drafts a reply in the composer and sends it only through the proposal", async () => {
    const proposal = {
      id: 41,
      kind: "message",
      title: "Reply in #general",
      res_model: "discuss.channel",
      res_id: 1,
      payload: { body: "Sure, 3 March works 👍" },
      original: null,
      state: "pending",
      reason: null,
      ai_generated: true,
      run_id: 80,
      feature: "discuss.reply_draft",
    };
    const { calls } = mockFetch({
      ...base(),
      "POST /api/v1/ai/features/discuss.reply_draft/stream": () =>
        sse([
          { event: "start", run_id: 80 },
          { event: "delta", text: "Sure, 3 March " },
          { event: "delta", text: "works 👍" },
          {
            event: "done",
            result: run({ run_id: 80, text: "Sure, 3 March works 👍", proposals: [proposal] }),
          },
        ]),
      "POST /api/v1/ai/proposals/41/accept": () => ({ ...proposal, state: "accepted", ai_generated: false }),
    });
    renderApp();
    await userEvent.click(await screen.findByRole("button", { name: /Draft with AI/ }));
    const box = screen.getByLabelText("Message to followers") as HTMLTextAreaElement;
    await waitFor(() => expect(box.value).toBe("Sure, 3 March works 👍"));
    expect(screen.getByRole("note", { name: /AI-drafted/ })).toBeInTheDocument();
    const stream = calls.find((c) => c.path === "/api/v1/ai/features/discuss.reply_draft/stream")!;
    expect(stream.body).toEqual({ res_model: "discuss.channel", res_id: 1 });
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v1/ai/proposals/41/accept")).toBe(true));
    expect(calls.some((c) => c.method === "POST" && c.path === "/api/v1/chatter/discuss.channel/1/messages")).toBe(
      false,
    );
  });

  it("hides the affordances when the features are off and says AI is unavailable when the gateway is down", async () => {
    mockFetch(base(status(true, [])));
    const off = renderApp();
    expect(await screen.findByRole("heading", { name: "#general" })).toBeInTheDocument();
    await screen.findByText("Launch is on 3 March");
    expect(screen.queryByRole("button", { name: /Summarize|Catch me up|Draft with AI/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Translate message/ })).not.toBeInTheDocument();
    off.unmount();
    mockFetch(base(status(false)));
    renderApp();
    await screen.findByText("Launch is on 3 March");
    expect((await screen.findAllByText("AI unavailable")).length).toBeGreaterThanOrEqual(3);
    expect(screen.queryByRole("button", { name: /✦/ })).not.toBeInTheDocument();
  });
});
