import { defineConfig, devices } from "@playwright/test";

// Smoke suite against a running stack: `make up` (or `tessera serve` + `npm run dev`), then `npm run e2e`.
// The suite signs in more than ten times a minute and waits for background jobs: start the API with
// TESSERA_LOGIN_RATE_LIMIT_PER_MINUTE=200 (as CI does) and with a jobs backend that runs (a worker, or the
// default `inline`, not `manual`).
const chromium = process.env.PW_CHROMIUM_PATH;

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  workers: 1, // projects share one database and edit the same demo records
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:5173",
    trace: "off",
    launchOptions: chromium ? { executablePath: chromium } : undefined,
  },
  projects: [
    {
      name: "desktop",
      use: { ...devices["Desktop Chrome"], launchOptions: chromium ? { executablePath: chromium } : undefined },
    },
    {
      name: "mobile",
      use: { ...devices["Pixel 7"], launchOptions: chromium ? { executablePath: chromium } : undefined },
    },
  ],
});
