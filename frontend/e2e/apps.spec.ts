import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

const ADMIN = { login: process.env.E2E_LOGIN ?? "admin", password: process.env.E2E_PASSWORD ?? "admin1234" };

async function login(page: Page) {
  await page.goto("/");
  await page.getByLabel("Login").fill(ADMIN.login);
  await page.getByLabel("Password").fill(ADMIN.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: /Welcome/ })).toBeVisible();
}

async function noSeriousA11y(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  const serious = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  expect(serious.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`)).toEqual([]);
}

async function csrf(page: Page): Promise<Record<string, string>> {
  const session = await (await page.request.get("/api/v1/auth/session")).json();
  return { "X-CSRF-Token": session.csrf_token };
}

test("CRM: pipeline board with stage totals, opportunity form with chatter and status", async ({ page }) => {
  await login(page);
  await page.goto("/a/crm.action_pipeline");
  await expect(page.getByRole("heading", { name: "Pipeline" })).toBeVisible();
  const board = page.getByRole("list", { name: /board/ });
  await expect(board.getByRole("listitem", { name: "Won" })).toBeVisible();
  await expect(
    board.getByRole("listitem", { name: "Negotiation" }).getByTitle("Total Expected revenue"),
  ).not.toHaveText("0");
  await noSeriousA11y(page);
  await board.getByText("Acme: 6 cobots for line 2").click();
  await expect(page.getByRole("radiogroup", { name: "Stage" })).toBeVisible();
  const chatter = page.getByRole("complementary", { name: "Discussion" });
  await chatter.getByRole("button", { name: "Log note" }).click();
  const note = `Call notes ${Date.now()}`;
  await chatter.getByLabel("Internal note").fill(note);
  await chatter.getByRole("button", { name: "Log", exact: true }).click();
  await expect(chatter.getByText(note)).toBeVisible();
  await noSeriousA11y(page);
});

test("CRM: sales analysis and forecast", async ({ page }) => {
  await login(page);
  await page.goto("/a/crm.action_reports");
  await expect(page.getByRole("heading", { name: "Sales analysis" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Funnel" })).toBeVisible();
  await expect(page.getByRole("rowheader", { name: "Negotiation" })).toBeVisible();
  await noSeriousA11y(page);
});

test("Quotation: customer opens the emailed link, sees the PDF link and accepts", async ({ page, browser }) => {
  await login(page);
  const headers = await csrf(page);
  const partner = await (
    await page.request.post("/api/v1/models/contacts.partner", {
      headers,
      data: { values: { name: "E2E Buyer", email: `buyer-${Date.now()}@example.test` } },
    })
  ).json();
  const quote = await (
    await page.request.post("/api/v1/models/sale.quotation", {
      headers,
      data: {
        values: {
          partner_id: partner.id,
          line_ids: [["create", { name: "Robot cell", price_unit: 1500, quantity: 2 }]],
        },
      },
    })
  ).json();
  const sent = await (
    await page.request.post(`/api/v1/models/sale.quotation/${quote.id}/call/action_send`, { headers, data: {} })
  ).json();
  const path = new URL(sent.result.portal_url).pathname + new URL(sent.result.portal_url).search;
  const context = await browser.newContext();
  const customer = await context.newPage();
  await customer.goto(path);
  await expect(customer.getByRole("heading", { name: /Quotation QT\// })).toBeVisible();
  await expect(customer.getByRole("link", { name: "Download PDF" })).toBeVisible();
  await noSeriousA11y(customer);
  await customer.getByRole("button", { name: "Accept" }).click();
  await customer.getByLabel(/Type your full name/).fill("E2E Buyer");
  await customer.getByRole("button", { name: "Accept and sign" }).click();
  await expect(customer.getByText("Accepted by E2E Buyer")).toBeVisible();
  const state = await (
    await page.request.get(`/api/v1/models/sale.quotation/${quote.id}`, { params: { fields: "state" } })
  ).json();
  expect(state.state).toBe("accepted");
  await context.close();
});

test("Discuss: post in #general and see it appear", async ({ page }) => {
  await login(page);
  await page.goto("/a/discuss.action_discuss");
  await expect(page.getByRole("navigation", { name: "Conversations" })).toBeVisible();
  await page.getByRole("button", { name: "Browse" }).click();
  await page.getByRole("button", { name: /# general/ }).click();
  const text = `hello team ${Date.now()}`;
  await page.getByLabel("Message to followers").fill(text);
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText(text)).toBeVisible();
  await noSeriousA11y(page);
});

test("Calendar: week view lists a recurring meeting created through the API", async ({ page }) => {
  await login(page);
  const headers = await csrf(page);
  const monday = new Date();
  monday.setDate(monday.getDate() - ((monday.getDay() + 6) % 7));
  monday.setHours(9, 0, 0, 0);
  const stop = new Date(monday.getTime() + 30 * 60 * 1000);
  const name = `E2E standup ${Date.now()}`;
  const created = await page.request.post("/api/v1/models/calendar.event", {
    headers,
    data: { values: { name, start: monday.toISOString(), stop: stop.toISOString(), rrule: "FREQ=DAILY;COUNT=5" } },
  });
  expect(created.ok()).toBeTruthy();
  await page.goto("/a/calendar.action_calendar");
  await page.getByRole("tab", { name: "Week" }).click();
  await expect(page.getByRole("button", { name: new RegExp(name) })).toHaveCount(5);
  await noSeriousA11y(page);
});

test("Contacts: merge two duplicates from the list and undo", async ({ page, isMobile }) => {
  test.skip(isMobile, "bulk selection is covered on desktop");
  await login(page);
  const headers = await csrf(page);
  const tag = `${Date.now()}`;
  for (const name of [`Dup Co ${tag}`, `Dup Co ${tag} Ltd`])
    await page.request.post("/api/v1/models/contacts.partner", {
      headers,
      data: { values: { name, is_company: true, email: `dup${tag}@example.test` } },
    });
  await page.goto("/a/contacts.action_partners?view=list");
  await page.getByRole("searchbox").fill(`Dup Co ${tag}`);
  await page.keyboard.press("Enter");
  await expect(page.getByRole("cell", { name: `Dup Co ${tag} Ltd` }).first()).toBeVisible();
  await page.getByRole("checkbox", { name: /Select all/ }).check();
  await page.getByRole("button", { name: "Merge" }).click();
  const dialog = page.getByRole("dialog", { name: "Merge contacts" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Merge" }).click();
  await expect(dialog.getByText("Contacts merged.")).toBeVisible();
  await dialog.getByRole("button", { name: "Undo" }).click();
});
