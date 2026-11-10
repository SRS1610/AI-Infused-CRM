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

test("login page is accessible and rejects bad credentials", async ({ page }) => {
  await page.goto("/");
  await expectNoSeriousA11yViolations(page);
  await page.getByLabel("Login").fill("admin");
  await page.getByLabel("Password").fill("wrong-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("alert")).toContainText("Invalid login or password");
});

test("list → form → save round trip on a generic model", async ({ page, isMobile }) => {
  await login(page);
  await expectNoSeriousA11yViolations(page);
  await page.goto("/a/web.action_companies");
  await expect(page.getByRole("heading", { name: "Companies" })).toBeVisible();
  const table = page.getByRole("table", { name: "Company" });
  await expect(table.getByText("My Company").first()).toBeVisible();
  await expectNoSeriousA11yViolations(page);
  await table.getByText("My Company").first().click();
  const phone = page.getByLabel("Phone");
  await expect(phone).toBeVisible();
  const value = `+1 555 ${Date.now() % 10000}`;
  await phone.fill(value);
  await page.keyboard.press("Control+s");
  await expect(page.getByRole("button", { name: "Save" })).toBeDisabled();
  await page.reload();
  await expect(page.getByLabel("Phone")).toHaveValue(value);
  await expectNoSeriousA11yViolations(page);
  if (!isMobile) {
    await page.keyboard.press("Control+k");
    await page.getByRole("combobox", { name: "Go to menu or record" }).fill("users");
    await page.keyboard.press("Enter");
    await expect(page.getByRole("heading", { name: "Users" })).toBeVisible();
  }
});

test("dark mode toggles and persists", async ({ page }) => {
  await login(page);
  await page.getByRole("button", { name: "Administrator" }).click();
  await page.getByRole("menuitem", { name: "Toggle dark mode" }).click();
  const theme = await page.evaluate(() => document.documentElement.dataset.theme);
  expect(["dark", "light"]).toContain(theme);
  await page.reload();
  expect(await page.evaluate(() => document.documentElement.dataset.theme)).toBe(theme);
});

test("realtime: a save in one browser refreshes the same form in another (1.2-l)", async ({ browser, isMobile }) => {
  test.skip(isMobile, "one viewport is enough for the realtime path");
  const [a, b] = await Promise.all([browser.newContext(), browser.newContext()]);
  const pageA = await a.newPage();
  const pageB = await b.newPage();
  await login(pageA);
  await login(pageB);
  for (const page of [pageA, pageB]) {
    await page.goto("/a/web.action_companies");
    await page.getByRole("table", { name: "Company" }).getByText("My Company").first().click();
    await expect(page.getByLabel("Phone")).toBeVisible();
  }
  await pageA.waitForTimeout(500); // let pageA's websocket subscribe to the record channel
  const value = `+44 20 ${Date.now() % 100000}`;
  await pageB.getByLabel("Phone").fill(value);
  await pageB.getByRole("button", { name: "Save" }).click();
  await expect(pageB.getByRole("button", { name: "Save" })).toBeDisabled();
  await expect(pageA.getByLabel("Phone")).toHaveValue(value, { timeout: 2000 });
  await a.close();
  await b.close();
});
