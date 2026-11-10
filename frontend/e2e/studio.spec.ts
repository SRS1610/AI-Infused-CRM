import { expect, test, type Page } from "@playwright/test";

// Studio (1.7-a, 1.7-b, 1.7-c): add a field from the browser and see it on the form without a restart,
// create an automated action (on stage change → schedule a to-do) and watch it fire, then roll both back.

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
