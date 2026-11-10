import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

// Studio (1.7-a, 1.7-b, 1.7-c): add a field from the browser and see it on the form without a restart,
// create an automated action (on stage change → schedule a to-do) and watch it fire, then roll both back.
// Then: reorder list columns and see the list follow; build a workflow on a new field and take its
// transitions on a record. Every test rolls its changes back (the suite shares one database).

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

async function noSeriousA11y(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  const serious = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  expect(serious.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`)).toEqual([]);
}

async function openStudio(page: Page) {
  await page.goto("/a/studio.action_studio");
  await expect(page.getByRole("heading", { name: "Studio" })).toBeVisible();
  await page.getByLabel("Model", { exact: true }).selectOption("crm.lead");
}

async function rollBack(page: Page, summary: string | RegExp) {
  const history = page.getByRole("region", { name: "Change history" });
  const rows = history.getByRole("listitem").filter({ hasText: summary });
  const done = rows.filter({ hasText: "rolled back" });
  const before = await done.count();
  // the newest applied change of that summary
  await rows
    .filter({ has: page.getByRole("button", { name: "Roll back" }) })
    .first()
    .getByRole("button")
    .click();
  await expect(done).toHaveCount(before + 1, { timeout: 30_000 });
}

test.describe.configure({ mode: "serial" });

test("Studio: add a field, use it on the form, automate a stage change, roll back", async ({ page, isMobile }) => {
  test.skip(isMobile, "Studio is a desktop tool; its API is covered by backend tests");
  await login(page);
  const tag = `${Date.now()}`.slice(-6);
  const label = `Competitor ${tag}`;
  await page.goto("/a/studio.action_studio");
  await expect(page.getByRole("heading", { name: "Studio" })).toBeVisible();
  await page.getByLabel("Model", { exact: true }).selectOption("crm.lead");

  // 1.7-a: a new field, placed on the form, usable at once
  const add = page.getByRole("form", { name: "Add a field" });
  await add.getByLabel("Label").fill(label);
  await add.getByLabel("Place on the form after").selectOption("partner_id");
  await add.getByRole("button", { name: "Add field" }).click();
  const history = page.getByRole("region", { name: "Change history" });
  // rebuilding the registry and refreshing field metadata takes a few seconds in-request
  await expect(history.getByText(new RegExp(`Added field “${label}”`))).toBeVisible({ timeout: 30_000 });

  // 1.7-c: on stage change, schedule a to-do
  await page.getByRole("tab", { name: "Automations" }).click();
  const auto = page.getByRole("form", { name: "Add an automated action" });
  await auto.getByLabel("Name", { exact: true }).fill(`Stage follow-up ${tag}`);
  await auto.getByLabel("When this field changes").selectOption("stage_id");
  await auto.getByLabel("Schedule a to-do").fill(`Check in ${tag}`);
  await auto.getByRole("button", { name: "Add automation" }).click();
  await expect(history.getByText(new RegExp(`Stage follow-up ${tag}`))).toBeVisible();

  const headers = await csrf(page);
  const created = await page.request.post("/api/v1/models/crm.lead", {
    headers,
    data: { values: { name: `Studio deal ${tag}`, type: "opportunity" } },
  });
  const id = (await created.json()).id as number;
  await page.goto(`/m/crm.lead/${id}`);
  const field = page.getByLabel(label);
  await expect(field).toBeVisible();
  await field.fill("Rival Robotics");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("button", { name: "Save" })).toBeDisabled({ timeout: 15_000 });
  await page.getByRole("radiogroup", { name: "Stage" }).getByRole("radio", { name: "Qualified" }).click();
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("button", { name: "Save" })).toBeDisabled({ timeout: 15_000 });
  const chatter = page.getByRole("complementary", { name: "Discussion" });
  await expect(chatter.getByText(`Check in ${tag}`)).toBeVisible();

  // 1.7-b: roll back the field and the automation from the history
  await page.goto("/a/studio.action_studio");
  await page.getByLabel("Model", { exact: true }).selectOption("crm.lead");
  for (const text of [`Stage follow-up ${tag}`, `Added field “${label}”`]) {
    const row = history.getByRole("listitem").filter({ hasText: text });
    await row.getByRole("button", { name: "Roll back" }).click();
    await expect(row.getByText("rolled back")).toBeVisible({ timeout: 30_000 });
  }
  await page.goto(`/m/crm.lead/${id}`);
  await expect(
    page.getByRole("heading", { name: `Studio deal ${tag}` }).or(page.getByText(`Studio deal ${tag}`).first()),
  ).toBeVisible();
  await expect(page.getByLabel(label)).toHaveCount(0);
});

test("Studio: reorder list columns, see the list follow, roll back", async ({ page, isMobile }) => {
  test.skip(isMobile, "Studio is a desktop tool; its API is covered by backend tests");
  await login(page);
  await openStudio(page);
  await noSeriousA11y(page); // the Fields tab, with the change history

  await page.getByRole("tab", { name: "Views" }).click();
  await page.getByRole("radio", { name: "List" }).click();
  const editor = page.getByRole("region", { name: "List columns" });
  await noSeriousA11y(page);
  // Stage moves two places up, before Customer; Expected Closing becomes an optional, hidden column
  await editor.getByRole("button", { name: "Move Stage up" }).click();
  await editor.getByRole("button", { name: "Move Stage up" }).click();
  await editor.getByLabel("Visibility of Expected Closing").selectOption("hide");
  await editor.getByRole("button", { name: "Save layout" }).click();
  const history = page.getByRole("region", { name: "Change history" });
  await expect(
    history.getByRole("listitem").filter({ hasText: "List columns changed in Studio" }).first(),
  ).toBeVisible();

  await page.goto("/a/crm.action_pipeline");
  await page.getByRole("tablist", { name: "View type" }).getByRole("tab", { name: "List" }).click();
  const headers = page.getByRole("columnheader");
  await expect(headers.filter({ hasText: "Stage" })).toBeVisible();
  const names = (await headers.allInnerTexts()).map((h) => h.trim());
  const at = (label: string) => names.findIndex((n) => n.startsWith(label));
  expect(at("Stage")).toBeGreaterThan(-1);
  expect(at("Stage")).toBeLessThan(at("Customer"));
  expect(at("Expected Closing")).toBe(-1); // optional and hidden by default

  await openStudio(page);
  await page.getByRole("tab", { name: "Search" }).click();
  await expect(page.getByRole("region", { name: "Search filters" }).getByText("My pipeline")).toBeVisible();
  await noSeriousA11y(page);
  await rollBack(page, "List columns changed in Studio");
  await page.goto("/a/crm.action_pipeline");
  await page.getByRole("tablist", { name: "View type" }).getByRole("tab", { name: "List" }).click();
  await expect(page.getByRole("columnheader").filter({ hasText: "Expected Closing" })).toBeVisible();
  const restored = (await page.getByRole("columnheader").allInnerTexts()).map((h) => h.trim());
  expect(restored.findIndex((n) => n.startsWith("Customer"))).toBeLessThan(
    restored.findIndex((n) => n.startsWith("Stage")),
  );
});

test("Studio: build a workflow on a new field, take its transitions on a record, roll back", async ({
  page,
  isMobile,
}) => {
  test.skip(isMobile, "Studio is a desktop tool; its API is covered by backend tests");
  await login(page);
  const tag = `${Date.now()}`.slice(-6);
  const name = `Approval ${tag}`;
  await openStudio(page);
  await page.getByRole("tab", { name: "Workflow" }).click();
  const form = page.getByRole("form", { name: "Build a workflow" });
  await form.getByLabel("Workflow name").fill(name);
  await form.getByLabel("Label of the new field").fill(name);
  for (const state of ["Draft", "Submitted", "Approved"]) {
    await form.getByRole("button", { name: "Add a state" }).click();
    await form.getByLabel("State name").last().fill(state);
  }
  await form
    .getByRole("list", { name: "States" })
    .getByRole("listitem")
    .last()
    .getByLabel("Done", { exact: true })
    .check();
  for (const [label, from, to] of [
    ["Submit", "Draft", "Submitted"],
    ["Approve", "Submitted", "Approved"],
  ]) {
    await form.getByRole("button", { name: "Add a transition" }).click();
    await form.getByRole("group", { name: "Transition …" }).getByLabel("Button label").fill(label);
    const tr = form.getByRole("group", { name: `Transition ${label}` });
    await tr.getByLabel("From", { exact: true }).selectOption({ label: from });
    await tr.getByLabel("To", { exact: true }).selectOption({ label: to });
  }
  await expect(form.getByRole("list", { name: "Workflow overview" })).toContainText("Submit to Submitted");
  await noSeriousA11y(page);
  await form.getByRole("button", { name: "Create workflow" }).click();
  const section = page.getByRole("region", { name: "Workflow" });
  await expect(section.getByText(new RegExp(`“${name}” follows the field`))).toBeVisible({ timeout: 30_000 });
  await expect(section.getByRole("list", { name: "Workflow overview" })).toContainText("Approve to Approved");
  await noSeriousA11y(page);

  // a new deal starts in the first state and moves through the transitions' buttons
  const headers = await csrf(page);
  const created = await page.request.post("/api/v1/models/crm.lead", {
    headers,
    data: { values: { name: `Workflow deal ${tag}`, type: "opportunity" } },
  });
  const id = (await created.json()).id as number;
  await page.goto(`/m/crm.lead/${id}`);
  const bar = page.getByLabel(`${name} workflow`);
  await expect(bar.locator('[aria-current="step"]')).toHaveText("Draft");
  await bar.getByRole("button", { name: "Submit" }).click();
  await expect(bar.locator('[aria-current="step"]')).toHaveText("Submitted");
  await expect(bar.getByRole("button", { name: "Submit" })).toHaveCount(0);
  await bar.getByRole("button", { name: "Approve" }).click();
  await expect(bar.locator('[aria-current="step"]')).toHaveText("Approved");

  // rolling back the field takes the workflow (and its list column) with it
  await openStudio(page);
  await rollBack(page, `Added field “${name}”`);
  const history = page.getByRole("region", { name: "Change history" });
  await expect(
    history
      .getByRole("listitem")
      .filter({ hasText: `Workflow “${name}”` })
      .getByText("rolled back"),
  ).toBeVisible();
  await page.goto(`/m/crm.lead/${id}`);
  await expect(page.getByText(`Workflow deal ${tag}`).first()).toBeVisible();
  await expect(page.getByLabel(`${name} workflow`)).toHaveCount(0);
});
