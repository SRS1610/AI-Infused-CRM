import { expect, test, type Page } from "@playwright/test";

// The AI steps of the Phase 1 demo script (docs/demos/phase-1.md), executed in a browser against
// `tessera seed demo` with the deterministic Local test provider and its demo answers.

const ADMIN = { login: process.env.E2E_LOGIN ?? "admin", password: process.env.E2E_PASSWORD ?? "admin1234" };

async function login(page: Page) {
  await page.goto("/");
  await page.getByLabel("Login").fill(ADMIN.login);
  await page.getByLabel("Password").fill(ADMIN.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: /Welcome/ })).toBeVisible();
}

async function csrf(page: Page): Promise<Record<string, string>> {
  const session = await (await page.request.get("/api/v1/auth/session")).json();
  return { "X-CSRF-Token": session.csrf_token };
}

async function create(page: Page, model: string, values: Record<string, unknown>): Promise<number> {
  const resp = await page.request.post(`/api/v1/models/${model}`, { headers: await csrf(page), data: { values } });
  expect(resp.ok(), await resp.text()).toBeTruthy();
  return (await resp.json()).id as number;
}

const tag = () => `${Date.now()}`.slice(-6);

test.describe.configure({ mode: "serial" });
test.skip(({ isMobile }) => isMobile, "the demo is given on a desktop");

test("2. Contacts: duplicates are proposed for merge", async ({ page }) => {
  await login(page);
  const t = tag();
  const company = await create(page, "contacts.partner", { name: `Northwind ${t}`, is_company: true });
  const email = `lena.${t}@northwind.example`;
  const first = await create(page, "contacts.partner", { name: `Lena Ortiz ${t}`, email, parent_id: company });
  await create(page, "contacts.partner", { name: `Lena Ortíz ${t}`, email, parent_id: company });
  await page.goto(`/m/contacts.partner/${first}`);
  const panel = page.getByRole("region", { name: "Possible duplicates" });
  await expect(panel).toBeVisible();
  const check = panel.getByRole("button", { name: "Check now" });
  if (await check.isVisible()) await check.click();
  await expect(panel.getByText(`Lena Ortíz ${t}`)).toBeVisible();
  await panel.getByRole("checkbox", { name: new RegExp(`Lena Ortíz ${t}`) }).check();
  await panel.getByRole("button", { name: "Propose merge" }).first().click();
  await expect(panel.getByRole("note", { name: /AI-drafted/ }).first()).toBeVisible();
});

test("3. Discuss: catch me up", async ({ page }) => {
  await login(page);
  await page.goto("/a/discuss.action_discuss");
  await page.getByRole("button", { name: /Catch me up/ }).click();
  await expect(page.getByRole("region", { name: "Catch me up" })).toBeVisible();
});

test("4. Calendar: schedule in plain words, then accept the proposed event", async ({ page }) => {
  await login(page);
  await page.goto("/a/calendar.action_calendar");
  await page.getByRole("button", { name: /Schedule with AI/ }).click();
  await page.getByLabel("Describe the meeting").fill("lunch with Dana next Tuesday 12:30");
  await page
    .getByRole("button", { name: /^(Draft|Schedule|Propose)/ })
    .first()
    .click();
  await expect(page.getByText("Lunch with Dana").first()).toBeVisible();
  await expect(page.getByRole("note", { name: /AI-drafted/ }).first()).toBeVisible();
  await page.getByRole("button", { name: "Accept" }).first().click();
  await expect(page.getByText(/accepted/i).first()).toBeVisible();
});

test("5. CRM intake: the extraction proposes the lead's details", async ({ page }) => {
  await login(page);
  const headers = await csrf(page);
  const sales = await (
    await page.request.get('/api/v1/models/crm.team?domain=[["name","=","Sales"]]&fields=id')
  ).json();
  const resp = await page.request.post("/api/v1/crm/leads/intake", {
    headers,
    data: {
      name: `Website enquiry ${tag()}`,
      description:
        "Hello, I'm Priya Shah, Head of Operations at Lumen Robotics (priya.shah@lumen-robotics.example, +44 20 7946 0958). " +
        "We need 8 collaborative robots for our Leeds plant by the end of March; our budget is about 90,000. Could someone call me this week?",
      team_id: sales.items[0]?.id,
      source: "email",
    },
  });
  expect(resp.status()).toBe(201);
  const lead = (await resp.json()).id ?? (await resp.json()).lead_id;
  // the proposal goes to the lead's salesperson (assigned round-robin): sign in as them
  const owner = (await (await page.request.get(`/api/v1/models/crm.lead/${lead}?fields=user_id`)).json()).user_id;
  if (owner?.display_name === "Dana Demo") {
    await page.request.post("/api/v1/auth/logout", { headers });
    await page.goto("/");
    await page.getByLabel("Login").fill("demo");
    await page.getByLabel("Password").fill("demo1234");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("heading", { name: /Welcome/ })).toBeVisible();
  }
  const panel = page.getByRole("region", { name: "AI suggestions" });
  // the extraction runs as a background job right after the intake commits
  await expect(async () => {
    await page.goto(`/m/crm.lead/${lead}`);
    await expect(panel.getByRole("region", { name: /lead fields/ })).toBeVisible({ timeout: 2_000 });
  }).toPass({ timeout: 20_000 });
  await expect(panel.getByLabel("Proposed contact_name")).toHaveValue("Priya Shah");
  await expect(panel.getByLabel("Proposed function")).toHaveValue("Head of Operations");
  await panel.getByRole("button", { name: "Accept" }).click();
  await expect
    .poll(
      async () =>
        (await (await page.request.get(`/api/v1/models/crm.lead/${lead}?fields=contact_name`)).json()).contact_name,
    )
    .toBe("Priya Shah");
});

test("9. Opportunity insights: score, why, next best action", async ({ page }) => {
  await login(page);
  await page.goto("/a/crm.action_pipeline");
  await page.getByText("Acme: 6 cobots for line 2").first().click();
  const insights = page.getByRole("region", { name: "AI insights" });
  await expect(insights).toBeVisible();
  await expect(insights.getByText(/AI score|Rule-based/).first()).toBeVisible();
  await page.getByRole("button", { name: "Suggest next actions" }).click();
  await expect(page.getByText("Send the revised quotation").first()).toBeVisible();
});

test("11. Call notes: summary, field proposals and audit trail", async ({ page }) => {
  await login(page);
  await page.goto("/a/crm.action_pipeline");
  await page.getByText("Acme: 6 cobots for line 2").first().click();
  const panel = page.getByRole("region", { name: "Summarise call notes" });
  await panel
    .getByLabel("Call or meeting notes")
    .fill(
      "Call with Jordan Lee (Acme Robotics). They confirmed 6 cobots for line 2, maybe 8 if the pilot goes well. " +
        "Budget approved at 75,600. Decision by 15 December. Jordan wants a revised quote with training included by Friday. " +
        "Their new procurement contact is Sam Patel, Purchasing Manager.",
    );
  await panel.getByRole("button", { name: /^Summari[sz]e$/ }).click();
  await expect(panel.getByText(/budget of\s+75,600/).first()).toBeVisible({ timeout: 20_000 });
  await panel.getByRole("button", { name: "Reject" }).first().click();
  await panel
    .getByRole("button", { name: /Accept|Send/ })
    .first()
    .click();
  await expect(
    panel.getByRole("region", { name: "AI run audit" }).or(panel.getByText("Audit trail")).first(),
  ).toBeVisible();
});

test("12. Search in plain words becomes a reviewable filter", async ({ page }) => {
  await login(page);
  await page.goto("/a/crm.action_pipeline?view=list");
  await page.getByRole("button", { name: "Ask AI" }).click();
  await page.getByLabel("Describe what you are looking for").fill("my open deals over 10k closing this quarter");
  await page.getByRole("button", { name: "Generate filter" }).click();
  const suggested = page.getByRole("region", { name: "Suggested filter" }).or(page.getByLabel("Suggested filter"));
  await expect(suggested.first()).toBeVisible();
  await page.getByRole("button", { name: "Apply filter" }).click();
  await expect(page.getByRole("button", { name: /Filters \(1\)/ })).toBeVisible();
});

test("14. Reports: forecast with a confidence range", async ({ page }) => {
  await login(page);
  await page.goto("/a/crm.action_reports");
  await expect(page.getByRole("heading", { name: "Forecast with confidence range" })).toBeVisible();
  await expect(page.getByText("Likely (P50)").first()).toBeVisible();
});

test("16. Administration: AI usage and kill switches", async ({ page }) => {
  await login(page);
  await page.goto("/a/ai_core.action_dashboard");
  await expect(page.getByRole("heading", { name: "AI usage and cost" })).toBeVisible();
  await expect(page.getByRole("table", { name: /AI usage per company and feature/ })).toBeVisible();
  await expect(page.getByRole("table", { name: /kill switches/ })).toBeVisible();
});
