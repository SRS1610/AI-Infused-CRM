import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { mockFetch } from "../../test/mockApi";
import { clientAction } from "../../kernel/extensions";
import { SessionProvider } from "../../kernel/session";
import "./DiscussApp";

const session = () => ({
  user: { id: 2, login: "admin", name: "Admin", lang: "en_US", tz: "UTC", share: false },
  allowed_companies: [],
  selected_companies: [],
  groups: [],
  csrf_token: "c",
  lang: "en_US",
  modules: ["discuss"],
});

const message = (id: number, body: string) => ({
  id,
  type: "comment",
  internal: false,
  subject: null,
  body,
  date: "2026-11-10T09:00:00Z",
  author: { id: 3, name: "Dana Demo" },
  email_from: null,
  tracking: [],
  attachments: [],
  reactions: [],
  ai_assisted: false,
  editable: false,
});

describe("Discuss", () => {
  it("lists conversations with unread counts, shows messages oldest first, posts and marks read", async () => {
    let posted = 0;
    const seen: any[] = [];
    mockFetch({
      "GET /api/v1/auth/session": session,
      "GET /api/v1/discuss/channels": () => [
        {
          id: 1,
          name: "general",
          description: "All hands",
          type: "channel",
          privacy: "public",
          member: true,
          member_count: 5,
          notify: "mentions",
          unread: 2,
          partner_user_id: null,
        },
        {
          id: 9,
          name: "Dana Demo",
          description: null,
          type: "dm",
          privacy: "invite",
          member: true,
          member_count: 2,
          notify: "all",
          unread: 0,
          partner_user_id: 3,
        },
      ],
      "GET /api/v1/discuss/presence": () => ({ online: [3] }),
      "GET /api/v1/chatter/discuss.channel/1/messages": () => ({
        items: [message(12, "<p>second</p>"), message(11, "<p>first</p>")],
        has_more: false,
      }),
      "POST /api/v1/discuss/channels/1/seen": (_u, init) => {
        seen.push(JSON.parse(String(init.body)));
        return { unread: 0 };
      },
      "POST /api/v1/chatter/discuss.channel/1/messages": () => {
        posted += 1;
        return message(13, "<p>hello</p>");
      },
    });
    const app = clientAction("discuss");
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <SessionProvider>
          <MemoryRouter>{app!()}</MemoryRouter>
        </SessionProvider>
      </QueryClientProvider>,
    );
    expect(await screen.findByLabelText("2 unread")).toBeInTheDocument();
    expect(await screen.findByLabelText("online")).toBeInTheDocument();
    const bodies = (await screen.findAllByText(/first|second/)).map((el) => el.textContent);
    expect(bodies).toEqual(["first", "second"]);
    await waitFor(() => expect(seen).toEqual([{ message_id: 12 }]));
    await userEvent.type(screen.getByLabelText("Message to followers"), "hello");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(posted).toBe(1));
    await userEvent.click(screen.getByRole("button", { name: "New channel" }));
    expect(screen.getByRole("heading", { name: "New channel" })).toBeInTheDocument();
  });
});
