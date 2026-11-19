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

async function expectNoSeriousA11yViolations(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  const serious = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  expect(serious.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`)).toEqual([]);
}

test("notifications, activity dashboard and palette navigation", async ({ page, isMobile }) => {
  await login(page);
  const bell = page.getByRole("button", { name: /Notifications: \d+ unread/ });
  await expect(bell).toBeVisible();
  await bell.click();
  await expect(page.getByRole("dialog", { name: "Notifications" })).toBeVisible();
  await expectNoSeriousA11yViolations(page);
  await bell.click();
  await page.goto("/a/chatter.action_my_activities");
  await expect(page.getByRole("heading", { name: "My activities" })).toBeVisible();
  await expectNoSeriousA11yViolations(page);
  if (!isMobile) {
    await page.keyboard.press("Control+k");
    await page.getByRole("combobox", { name: "Go to menu or record" }).fill("background jobs");
    await page
      .getByRole("option", { name: /Background jobs/ })
      .first()
      .click();
    await expect(page.getByRole("heading", { name: "Background jobs" })).toBeVisible();
  }
});

test("import wizard: mapping suggestions and dry run write nothing", async ({ page }) => {
  await login(page);
  await page.goto("/a/web.action_companies");
  await page.getByRole("button", { name: "Import" }).click();
  await expect(page.getByRole("heading", { name: "Import Company" })).toBeVisible();
  const name = `E2E Import ${Date.now()}`;
  await page.setInputFiles('input[type="file"]', {
    name: "companies.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(`Company Name,Currency,Email\n${name},USD,e2e@example.test\n`),
  });
  await expect(page.getByLabel("Field for column Company Name")).toHaveValue("name");
  await expect(page.getByLabel("Field for column Currency")).toHaveValue("currency_id");
  await expectNoSeriousA11yViolations(page);
  await page.getByRole("button", { name: "Test (dry run)" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Nothing has been written yet" })).toBeVisible();
  await page.goto("/a/web.action_companies");
  await expect(page.getByRole("table", { name: "Company" }).getByText(name)).toHaveCount(0);
});

test("export dialog lists readable fields", async ({ page }) => {
  await login(page);
  await page.goto("/a/web.action_companies");
  await page.getByRole("button", { name: "Export" }).click();
  const dialog = page.getByRole("dialog", { name: "Export" });
  await expect(dialog.getByRole("button", { name: "+ Phone" })).toBeVisible();
  await expectNoSeriousA11yViolations(page);
  await dialog.getByRole("button", { name: "Close" }).click();
});
