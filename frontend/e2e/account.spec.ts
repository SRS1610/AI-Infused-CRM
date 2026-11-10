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

async function draftInvoice(page: Page, amount: number): Promise<number> {
  const headers = await csrf(page);
  const partner = await (
    await page.request.post("/api/v1/models/contacts.partner", {
      headers,
      data: { values: { name: `E2E Customer ${Date.now()}`, is_company: true, email: "ap@e2e.test" } },
    })
  ).json();
  const move = await (
    await page.request.post("/api/v1/models/account.move", {
      headers,
      data: {
        values: {
          move_type: "out_invoice",
          partner_id: partner.id,
          invoice_line_ids: [["create", { name: "Implementation services", quantity: 1, price_unit: amount }]],
        },
      },
    })
  ).json();
  return move.id as number;
}

test("Accounting: invoice list and form, confirm, partial payment, credit note", async ({ page }) => {
  await login(page);
  await page.goto("/a/account.action_invoices");
  await expect(page.getByRole("heading", { name: "Customer invoices" })).toBeVisible();
  await expect(page.getByRole("cell", { name: /INV\/\d{4}\/\d{5}/ }).first()).toBeVisible();
  await noSeriousA11y(page);

  const id = await draftInvoice(page, 1000);
  await page.goto(`/a/account.action_invoices?id=${id}`);
  await expect(page.getByRole("tab", { name: "Invoice lines" })).toBeVisible();
  await expect(page.getByText("Implementation services").first()).toBeVisible();
  await page.getByRole("button", { name: "Confirm", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: /INV\/\d{4}\/\d{5}/ }).or(page.getByText(/INV\/\d{4}\/\d{5}/).first()),
  ).toBeVisible();

  await page.getByRole("button", { name: "Register payment" }).click();
  const dialog = page.getByRole("dialog", { name: "Register a payment" });
  await expect(dialog).toBeVisible();
  await noSeriousA11y(page);
  await dialog.getByLabel(/Amount/).fill("400");
  await dialog.getByRole("button", { name: "Create payment" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByText("Partially paid").first()).toBeVisible();

  await page.getByRole("tab", { name: "Journal items" }).click();
  await expect(page.getByRole("cell", { name: /Accounts receivable/ }).first()).toBeVisible();

  await page.getByRole("button", { name: "Credit note", exact: true }).click();
  const reverse = page.getByRole("dialog", { name: "Create a credit note" });
  await reverse.getByLabel("Reason").fill("Duplicate order");
  await reverse.getByRole("button", { name: "Create" }).click();
  await expect(reverse).toBeHidden();
  await page.goto("/a/account.action_refunds");
  await expect(page.getByRole("heading", { name: "Customer credit notes" })).toBeVisible();
  await expect(page.getByRole("row").filter({ hasText: "Draft" }).first()).toBeVisible();
});

test("Accounting: configuration pages and journal items", async ({ page }) => {
  await login(page);
  for (const [action, heading] of [
    ["account.action_accounts", "Chart of accounts"],
    ["account.action_taxes", "Taxes"],
    ["account.action_journals", "Journals"],
    ["account.action_payment_terms", "Payment terms"],
    ["account.action_fiscal_positions", "Fiscal positions"],
    ["account.action_items", "Journal items"],
    ["account.action_bills", "Vendor bills"],
    ["account.action_payments_in", "Customer payments"],
  ] as const) {
    await page.goto(`/a/${action}`);
    await expect(page.getByRole("heading", { name: heading })).toBeVisible();
    await expect(page.getByRole("row").nth(1)).toBeVisible();
    await noSeriousA11y(page);
  }
});

test("Accounting: customer opens the invoice link without an account", async ({ page, browser }) => {
  await login(page);
  const headers = await csrf(page);
  const id = await draftInvoice(page, 250);
  await page.request.post(`/api/v1/models/account.move/${id}/call/action_post`, { headers, data: {} });
  const sent = await (
    await page.request.post(`/api/v1/models/account.move/${id}/call/action_send`, { headers, data: {} })
  ).json();
  const url = new URL(sent.result.portal_url);
  const token = url.searchParams.get("token");
  const context = await browser.newContext();
  const customer = await context.newPage();
  const res = await customer.request.get(`/api/v1/account/invoices/${id}?token=${token}`);
  expect(res.status()).toBe(200);
  const body = await res.json();
  expect(body.amount_total).toBe(250);
  expect((await customer.request.get(`/api/v1/account/invoices/${id}?token=wrong`)).status()).toBe(401);
  await context.close();
});
