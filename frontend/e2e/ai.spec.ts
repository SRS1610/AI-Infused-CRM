import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

// AI in the browser (1.5-l, 1.5-j, 1.6-g): a streamed follow-up draft in the lead's chatter shows the
// AI-drafted label until the user edits it, is sent only when the user sends it, and disappears when an
// administrator switches the feature off for the company. Runs against the deterministic demo provider.

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

async function newLead(page: Page): Promise<number> {
  const headers = await csrf(page);
  const resp = await page.request.post("/api/v1/models/crm.lead", {
    headers,
    data: { values: { name: `AI e2e ${Date.now()}`, type: "opportunity", expected_revenue: 12000 } },
  });
  expect(resp.ok()).toBeTruthy();
  return (await resp.json()).id as number;
}

test("CRM follow-up draft streams with the AI-drafted label until edited, then is sent by the user", async ({
  page,
}) => {
  await login(page);
  // an interrupted earlier run of the kill-switch test below may have left the feature switched off
  const session = await (await page.request.get("/api/v1/auth/session")).json();
  await page.request.put("/api/v1/ai/admin/features/crm.followup_draft/flag", {
    headers: { "X-CSRF-Token": session.csrf_token },
    data: { enabled: true, company_id: session.company.id },
  });
  const id = await newLead(page);
  await page.goto(`/m/crm.lead/${id}`);
  const chatter = page.getByRole("complementary", { name: "Discussion" });
  await chatter.getByRole("button", { name: "Send message" }).click();
  const box = chatter.getByLabel("Message to followers");
  const started = Date.now();
  await chatter.getByRole("button", { name: /Draft with AI/ }).click();
  await expect(box).not.toHaveValue("", { timeout: 1500 }); // 1.5-l: first token in under 1.5 s
  const firstToken = Date.now() - started;
  expect(firstToken).toBeLessThan(1500);
  await expect(chatter.getByRole("note", { name: /AI-drafted/ })).toBeVisible();
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  expect(results.violations.filter((v) => v.impact === "serious" || v.impact === "critical")).toEqual([]);
  await box.press("End");
  await box.pressSequentially(" Best regards");
  await expect(chatter.getByRole("note", { name: /AI-drafted/ })).toHaveCount(0); // a human edited it
  await chatter.getByRole("button", { name: "Send", exact: true }).click();
  await expect(chatter.getByRole("listitem").filter({ hasText: "Best regards" }).first()).toBeVisible();
  // the run is audited on the record and records that a person edited the draft before sending it
  await expect
    .poll(
      async () =>
        (await (await page.request.get(`/api/v1/ai/runs?res_model=crm.lead&res_id=${id}`)).json())[0]?.decision,
    )
    .toBe("edited");
});

test("switching the draft feature off for the company hides it; switching it back restores it", async ({ page }) => {
  await login(page);
  const id = await newLead(page);
  const headers = await csrf(page);
  const flag = (enabled: boolean, company_id: number) =>
    page.request.put("/api/v1/ai/admin/features/crm.followup_draft/flag", { headers, data: { enabled, company_id } });
  const session = await (await page.request.get("/api/v1/auth/session")).json();
  const company: number = session.company.id;
  expect((await flag(false, company)).ok()).toBeTruthy();
  try {
    await page.goto(`/m/crm.lead/${id}`);
    const chatter = page.getByRole("complementary", { name: "Discussion" });
    await chatter.getByRole("button", { name: "Send message" }).click();
    await expect(chatter.getByLabel("Message to followers")).toBeVisible();
    await expect(chatter.getByRole("button", { name: /Draft with AI/ })).toHaveCount(0);
    const blocked = await page.request.post("/api/v1/ai/features/crm.followup_draft/stream", {
      headers,
      data: { res_model: "crm.lead", res_id: id },
    });
    expect(blocked.status()).toBe(404);
    expect((await blocked.json()).code).toBe("feature_disabled");
  } finally {
    await flag(true, company);
  }
  await page.reload();
  const chatter = page.getByRole("complementary", { name: "Discussion" });
  await chatter.getByRole("button", { name: "Send message" }).click();
  await expect(chatter.getByRole("button", { name: /Draft with AI/ })).toBeVisible();
});
