// The enterprise design system (D-103): icons, record header and command bar, kanban cards, related names,
// access-denied page.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { mockFetch } from "../test/mockApi";
import { ApiError } from "./api/client";
import type { ViewsResponse } from "./api/types";
import { RelatedNames } from "./fields/RelatedNames";
import { initials, menuIcon } from "./icons";
import { SessionProvider } from "./session";
import { AccessDenied, isAccessError } from "./shell/AccessDenied";
import { FormView } from "./views/FormView";
import { KanbanView } from "./views/KanbanView";

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SessionProvider>
        <MemoryRouter>{ui}</MemoryRouter>
      </SessionProvider>
    </QueryClientProvider>,
  );
}

const session = {
  "GET /api/v1/auth/session": () => ({
    user: { id: 2, login: "admin", name: "Admin", lang: "en_US", tz: "UTC" },
    allowed_companies: [],
    selected_companies: [],
    groups: [],
    csrf_token: "c",
    lang: "en_US",
    modules: [],
  }),
};

const f = (name: string, type: string, extra: Record<string, unknown> = {}) => ({
  name,
  type,
  string: name
    .replace(/_id$|_ids$/, "")
    .replace(/_/g, " ")
    .replace(/^./, (c) => c.toUpperCase()),
  required: false,
  readonly: false,
  store: true,
  ...extra,
});

const meta = {
  model: {
    name: "demo.deal",
    description: "Deal",
    rec_name: "name",
    order: "name",
    methods: ["action_confirm"],
    access: { read: true, write: true, create: true, unlink: true },
  },
  fields: {
    id: f("id", "integer", { readonly: true }),
    display_name: f("display_name", "char", { readonly: true, store: false }),
    name: f("name", "char"),
    amount_total: f("amount_total", "monetary", { string: "Total", currency_field: "currency_id" }),
    currency_id: f("currency_id", "many2one", { comodel: "core.currency" }),
    user_id: f("user_id", "many2one", { string: "Owner", comodel: "core.user" }),
    partner_id: f("partner_id", "many2one", { string: "Customer", comodel: "contacts.partner" }),
    state: f("state", "selection", {
      selection: [
        ["draft", "Draft"],
        ["sent", "Sent"],
        ["done", "Done"],
      ],
    }),
    priority: f("priority", "selection", {
      selection: [
        ["0", "Normal"],
        ["1", "Good"],
        ["2", "Very good"],
        ["3", "Excellent"],
      ],
    }),
    tag_ids: f("tag_ids", "many2many", { string: "Tags", comodel: "demo.tag" }),
  },
  views: {
    form: {
      type: "form",
      model: "demo.deal",
      root: {
        kind: "sheet",
        children: [
          { kind: "buttons", children: [{ kind: "button", method: "action_confirm", label: "Confirm" }] },
          { kind: "field", name: "state", widget: "statusbar" },
          { kind: "field", name: "name", widget: "title" },
          {
            kind: "group",
            columns: 2,
            children: [
              { kind: "field", name: "amount_total" },
              { kind: "field", name: "user_id" },
            ],
          },
        ],
      },
    },
    kanban: {
      type: "kanban",
      model: "demo.deal",
      root: {
        kind: "card",
        children: [
          { kind: "field", name: "name", widget: "title" },
          { kind: "field", name: "partner_id" },
          { kind: "field", name: "amount_total" },
          { kind: "field", name: "priority", widget: "priority" },
          { kind: "field", name: "user_id" },
          { kind: "field", name: "tag_ids", widget: "many2many_tags" },
        ],
      },
    },
  },
} as unknown as ViewsResponse;

const deal = {
  id: 1,
  display_name: "Big deal",
  name: "Big deal",
  amount_total: 1000,
  currency_id: null,
  user_id: { id: 7, display_name: "Ann Lee" },
  partner_id: { id: 9 }, // a customer the user cannot read: no "#9" on the card
  state: "sent",
  priority: "2",
  tag_ids: [4, 5],
};

describe("icons", () => {
  it("uses the declared icon, else one from the action, else a neutral one", () => {
    expect(menuIcon({ icon: "users", action: null })).toBe("users");
    expect(menuIcon({ icon: "no-such-icon", action: "account.action_invoices" })).toBe("receipt");
    expect(menuIcon({ action: "crm.action_pipeline" })).toBe("kanban");
    expect(menuIcon({ action: "ai_core.action_dashboard" })).toBe("sparkles");
    expect(menuIcon({ action: "web.action_modules" })).toBe("wrench");
    expect(menuIcon({ action: null })).toBe("circle");
  });

  it("makes two-letter initials", () => {
    expect(initials("Ann Lee")).toBe("AL");
    expect(initials("Administrator")).toBe("AD");
    expect(initials("Marie Claire Martin")).toBe("MM");
    expect(initials("")).toBe("?");
  });
});

describe("record page", () => {
  it("puts the record's buttons in the command bar, its stage in the process bar and key facts in the header", async () => {
    mockFetch({ ...session, "GET /api/v1/models/demo.deal/1": () => deal });
    wrap(
      <FormView
        model="demo.deal"
        recordId={1}
        meta={meta}
        view={meta.views.form!}
        onSaved={vi.fn()}
        onBack={vi.fn()}
      />,
    );
    const heading = await screen.findByRole("heading", { level: 1, name: "Big deal" });
    const header = heading.closest("header")!;
    expect(within(header).getByText("Deal")).toBeInTheDocument();
    expect(within(header).getByText("Total")).toBeInTheDocument();
    expect(within(header).getByText("Ann Lee")).toBeInTheDocument();
    expect(within(header).getByText("AL")).toBeInTheDocument();
    expect(within(header).getByText("Sent")).toHaveClass("badge", "badge-sent");
    // the sheet's buttons are not inside the form body any more
    const confirm = screen.getByRole("button", { name: "Confirm" });
    expect(confirm.closest("form")).toBeNull();
    expect(screen.getByRole("button", { name: "Save" })).toHaveAttribute("form");
    const stages = screen.getByRole("radiogroup", { name: "State" });
    expect(within(stages).getByRole("radio", { name: "Draft" })).toHaveClass("done");
    expect(within(stages).getByRole("radio", { name: "Sent" })).toHaveClass("current");
    expect(within(stages).getByRole("radio", { name: "Done" })).not.toHaveClass("done");
  });

  it("shows the access-denied page when the record cannot be read", async () => {
    mockFetch({
      ...session,
      "GET /api/v1/models/demo.deal/1": () =>
        new Response(JSON.stringify({ error: "AccessError", message: "no (demo.deal)" }), { status: 403 }),
    });
    wrap(
      <FormView
        model="demo.deal"
        recordId={1}
        meta={meta}
        view={meta.views.form!}
        onSaved={vi.fn()}
        onBack={vi.fn()}
      />,
    );
    expect(await screen.findByRole("heading", { name: "You don't have access to this page" })).toBeInTheDocument();
    expect(screen.queryByText(/demo\.deal/)).not.toBeInTheDocument();
  });
});

describe("kanban card", () => {
  it("shows the amount, stars, owner avatar and tag names, and hides unreadable records", async () => {
    mockFetch({
      ...session,
      "POST /api/v1/models/demo.deal/read_group": () => ({
        groups: [{ __count: 1, __domain: [["state", "=", "sent"]], state: "sent" }],
      }),
      "GET /api/v1/models/demo.deal": () => ({ items: [deal], total: 1, limit: 40, offset: 0 }),
      "GET /api/v1/models/demo.tag": () => ({
        items: [
          { id: 4, display_name: "Hardware" },
          { id: 5, display_name: "Upsell" },
        ],
        total: 2,
        limit: 2,
        offset: 0,
      }),
    });
    wrap(
      <KanbanView
        model="demo.deal"
        meta={meta}
        view={{ ...meta.views.kanban!, default_group_by: "state" } as never}
        domain={[]}
        groupBy={[]}
        onOpen={vi.fn()}
      />,
    );
    const title = await screen.findByText("Big deal");
    const card = title.closest("li")!;
    expect(title).toHaveClass("card-title");
    expect(within(card).getByLabelText("Very good")).toHaveTextContent("★★☆");
    expect(within(card).getByText("Ann Lee")).toBeInTheDocument();
    expect(await within(card).findByText("Hardware")).toHaveClass("tag");
    expect(within(card).queryByText("#9")).not.toBeInTheDocument();
    expect(within(card).queryByText(/records?$/)).not.toBeInTheDocument();
  });
});

describe("related names", () => {
  it("lists names, and falls back to the count when they cannot be read", async () => {
    mockFetch({
      "GET /api/v1/models/demo.tag": () => ({
        items: [{ id: 4, display_name: "VAT 20%" }],
        total: 1,
        limit: 1,
        offset: 0,
      }),
      "GET /api/v1/models/demo.secret": () =>
        new Response(JSON.stringify({ error: "AccessError", message: "no" }), { status: 403 }),
    });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <p data-testid="names">
          <RelatedNames comodel="demo.tag" ids={[4]} />
        </p>
        <p data-testid="hidden">
          <RelatedNames comodel="demo.secret" ids={[1, 2]} />
        </p>
        <p data-testid="none">
          <RelatedNames comodel="demo.tag" ids={[]} />
        </p>
      </QueryClientProvider>,
    );
    expect(await screen.findByText("VAT 20%")).toBeInTheDocument();
    expect(screen.getByTestId("hidden")).toHaveTextContent("2 records");
    expect(screen.getByTestId("none")).toBeEmptyDOMElement();
  });
});

describe("access denied", () => {
  it("explains and links home; only 403 counts as no access", () => {
    render(
      <MemoryRouter>
        <AccessDenied />
      </MemoryRouter>,
    );
    expect(screen.getByRole("link", { name: "Go to Home" })).toHaveAttribute("href", "/");
    expect(isAccessError(new ApiError(403, { error: "AccessError", message: "x" }))).toBe(true);
    expect(isAccessError(new ApiError(404, { error: "MissingError", message: "x" }))).toBe(false);
    expect(isAccessError(new Error("x"))).toBe(false);
  });
});
